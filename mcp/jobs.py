"""Background jobs: detached OS-process execution + polling (execute_code, get_job).

For long jobs (model download, training) that would otherwise blow past any exec
timeout: run the user's code as a SEPARATE OS PROCESS on the kernel host/VM, with its
stdout+stderr redirected to a file at the fd level (`python -u` = unbuffered). A tiny
shell wrapper records the exit status. The launching exec just spawns it and returns
immediately; get_job reads the files via a cheap kernel exec.

Why a process, not an in-kernel thread: fd-level redirection captures EVERYTHING the
job emits — plain prints, `subprocess`, tqdm/progress bars, and native C-library output
— which a sys.stdout-only tee silently misses (that was the "output is empty" bug). A
process also can't be captured thread-locally, so it never swallows a concurrent exec's
output. Trade-off: the job runs in its own interpreter, so it does NOT share the kernel
namespace — persist results to a file (then read/download them). It IS plain Python (no
IPython `!shell`/`%magic`; use the `subprocess` module for shell commands). The job
inherits the kernel process env (so a prior setup_hf/setup_kaggle token carries over).
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from typing import Any

import backends
import config
import reaper
import state
from app import mcp
from backends import local_jupyter
from config import log
from outputs import _format_outputs

_JOBS_DIR = "/tmp/rmcp_jobs"   # on the KERNEL's filesystem (local host or colab VM)


def _job_launch_code(job_id: str, user_code: str) -> str:
    # Built inside the kernel so it uses the kernel's OWN interpreter (sys.executable —
    # not a hardcoded "python", which may be missing) and shlex-quotes every path. The
    # shell wrapper runs the script unbuffered, captures all fd output, records status.
    code_b64 = base64.b64encode(user_code.encode()).decode()
    return (
        "import base64, subprocess, pathlib, sys, shlex\n"
        f"_d = pathlib.Path({_JOBS_DIR!r}); _d.mkdir(parents=True, exist_ok=True)\n"
        f"_py=_d/({job_id!r}+'.py'); _out=_d/({job_id!r}+'.out'); "
        f"_st=_d/({job_id!r}+'.status'); _shp=_d/({job_id!r}+'.sh')\n"
        f"_py.write_text(base64.b64decode({code_b64!r}).decode())\n"
        "_st.write_text('running')\n"
        "_q = lambda p: shlex.quote(str(p))\n"
        "_shp.write_text(\n"
        "    sys.executable + ' -u ' + _q(_py) + ' > ' + _q(_out) + ' 2>&1\\n'\n"
        "    'rc=$?\\n'\n"
        "    'if [ \"$rc\" -eq 0 ]; then echo done > ' + _q(_st) + '; '\n"
        "    'else echo \"error rc=$rc\" > ' + _q(_st) + '; fi\\n')\n"
        "subprocess.Popen(['bash', str(_shp)], start_new_session=True, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"print('rmcp background job started:', {job_id!r})\n"
    )


async def _write_back(kernel_id: str, code: str, reply: dict[str, Any],
                      result: dict[str, Any]) -> None:
    """Append the executed cell to the kernel's bound notebook, if it has one."""
    nb_path = await local_jupyter._notebook_for_kernel(kernel_id)
    if not nb_path:
        return
    try:
        await local_jupyter._append_cell(nb_path, code, reply)
        result["saved_to"] = nb_path
    except Exception as e:  # noqa: BLE001
        log.warning("notebook write-back failed for %s: %s", nb_path, e)


async def _execute_background(kernel_id: str, code: str) -> dict[str, Any]:
    job_id = "job-" + uuid.uuid4().hex[:8]
    reply = await backends._run_on_kernel(kernel_id, _job_launch_code(job_id, code), timeout=60)
    result = backends._reply_result(reply)
    # Only claim the job started if the LAUNCH exec actually succeeded. Otherwise (session
    # lost, error, timeout) surface that — never hand back a running_in_background/job_id for
    # a job that was never spawned (that produced the "get_job stuck at unknown forever" bug).
    if result.get("status") != "ok":
        result["note"] = (
            "background job did NOT start — the launch command failed (status/output above; "
            "the colab session may be lost/expired). NO job is running and no job_id is valid. "
            "Start a fresh session with start_kernel and retry.")
        return result
    result.update({"status": "running_in_background", "job_id": job_id,
                   "note": (f"Job {job_id} started detached (its own process) — the work is "
                            "NOT bounded by any timeout. Poll get_job(kernel_id, job_id) until "
                            "status != 'running_in_background' (it returns the latest output "
                            "window + next_offset; pass next_offset back to stream only what's "
                            "new). It does NOT share the kernel namespace — persist results to a "
                            "file and read/download them. Plain Python only (no !shell/%magic; "
                            "use subprocess).")})
    await _write_back(kernel_id, code, reply, result)
    return result


async def _execute_foreground(kernel_id: str, code: str, timeout: float | None,
                              sink: list[dict[str, Any]]) -> dict[str, Any]:
    reply = await backends._run_on_kernel(kernel_id, code, timeout, sink)
    result = backends._reply_result(reply)
    # A lost session ran nothing — skip write-back (there is no cell to append, and probing
    # _notebook_for_kernel would re-create a bare registry entry for the just-forgotten id).
    if result.get("status") != "session_lost":
        await _write_back(kernel_id, code, reply, result)
    return result


@mcp.tool
async def execute_code(kernel_id: str, code: str, timeout: float | None = None,
                       background: bool = False) -> dict[str, Any]:
    """Run ad-hoc Python in the kernel (stateful; variables persist across calls) and
    **append** it as a new cell to the bound notebook (if any). For iterating on an
    EXISTING cell without duplicating it, use execute_cell / edit_cell instead. Works for
    local and colab backends. `timeout` (optional) is a HARD cap: exceeding it INTERRUPTS
    the kernel. Leave it unset for long work — see below; nothing is interrupted then.

    Foreground calls run through IPython, so `!shell` and `%magic` (e.g. `!pip install …`)
    DO work here. (Background jobs do NOT — see below.)

    IF THIS CALL APPEARS TO FAIL OR TIME OUT (a client-side error, or no response at all):
    the code may STILL be running, or may have ALREADY completed, on the kernel. NEVER
    resend the same code as your first response. Instead:
      1. call get_execution(kernel_id) to see whether it is running or already finished
         (colab_log also works for colab kernels)
      2. if it is still running, poll get_execution(kernel_id, wait_seconds=20) until done
      3. resend ONLY once you have CONFIRMED it never ran
    Resending without checking is how one command becomes two: doubled file writes, doubled
    pip installs, a counter incremented twice. The server refuses the obvious cases for you
    (see "busy" below), but a confirmed check is what actually makes it safe.

    LONG WORK ANSWERS EARLY, IT IS NOT KILLED. If the execution outlives the server's reply
    deadline you get {status: "still_running", exec_id, partial_output} — the code keeps
    running on the kernel, nothing was interrupted, and get_execution collects the result
    (and its notebook write-back) when it finishes. status="timeout" appears only when YOU
    passed `timeout`, which is a hard cap that interrupts the kernel.

    ONE EXECUTION PER KERNEL: a call arriving while another is in flight returns
    status="busy" and executes NOTHING, rather than queueing your code to run a second time
    behind the first. `is_retry` tells you it was your own resend.

    `background=True` — for LONG jobs (model download, training): the code runs as a detached
    OS process on the kernel host/VM and this returns IMMEDIATELY with {status:
    "running_in_background", job_id}. No timeout on the actual work. Poll get_job(kernel_id,
    job_id) for LIVE stdout/stderr (unbuffered — plain prints, subprocess, and tqdm/progress
    bars all show) + status (running/done/error). Caveats: the job runs in its OWN interpreter,
    so (a) it does NOT share the kernel's variables — **the code must be SELF-CONTAINED**: import
    modules and (re)build/load everything it needs; it can't see `torch`/`model`/etc. defined by
    earlier execute_code calls (that raises NameError). PERSIST results/checkpoints to files
    (`torch.save`, ...) and read/download them; and (b) it is plain Python — NO IPython
    `!shell`/`%magic`, use the `subprocess` module. It inherits the kernel env (setup_hf/
    setup_kaggle tokens carry over). Good for a self-contained long job (download+train+
    checkpoint). To CONTINUE interactive state instead, run foreground in bounded chunks (e.g.
    1000 iters/call) that checkpoint to disk, rather than one giant background job."""
    reaper._ensure_background()

    async def _run(sink: list[dict[str, Any]]) -> dict[str, Any]:
        if background:
            return await _execute_background(kernel_id, code)
        return await _execute_foreground(kernel_id, code, timeout, sink)

    # The background LAUNCH is an exec too (it runs on the kernel), so it takes the same
    # slot — but only for the second or two it takes to spawn the detached process; the
    # job itself holds nothing, which is why long work belongs there.
    return await state.run_single_flight(kernel_id, code, "execute_code", _run,
                                         deadline=config.SOFT_REPLY_DEADLINE_SEC)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_execution(kernel_id: str, exec_id: str | None = None, wait_seconds: float = 0,
                        max_chars: int = 4000) -> dict[str, Any]:
    """Collect a foreground execution — the answer to any call that did not come back.

    Call this INSTEAD of re-running code whenever execute_code/execute_cell returned
    status="still_running" or status="busy", timed out on your side, or errored in your
    client. It reads in-process bookkeeping (no kernel round-trip), so it answers even
    while the kernel is busy and is always safe to call. `exec_id` is optional — omit it
    for "whatever is or was running on this kernel".

    `wait_seconds` LONG-POLLS: wait up to that many seconds for the execution to finish
    before answering (clamped below the server's reply deadline). One call with
    wait_seconds=20 beats twenty polls.

    - status="running": still going; `running` has exec_id, code_head, elapsed_seconds and
      `partial_output` (what it has printed so far — local kernels only). Poll again. Do
      NOT resend the code. `interrupt_kernel` aborts it if you actually want it stopped.
    - status=<a finished status: ok / error / timeout / session_lost>: the completed
      execution — code_head, output `text` (last `max_chars`), execution_count, timing and
      `saved_to` (the notebook it was written back to). `client_abandoned` means your
      client had already stopped listening, i.e. this is precisely the result you missed.
    - status="none": nothing has run on this kernel through this server yet.

    One foreground execution is remembered per kernel, in memory only (an mcp restart
    clears it). Background jobs have get_job; a colab session's history has colab_log; the
    notebook cells are the durable record."""
    reaper._ensure_background()
    if wait_seconds and wait_seconds > 0:
        await state.wait_for_exec(kernel_id, exec_id, min(float(wait_seconds), _MAX_WAIT))
    running = state.running_exec(kernel_id)
    last = state._last_exec.get(kernel_id)
    if running is not None and (exec_id is None or running["exec_id"] == exec_id):
        running["partial_output"] = state.partial_output(kernel_id, max_chars)
        return {"kernel_id": kernel_id, "status": "running", "running": running,
                "note": (f"kernel '{kernel_id}' is STILL executing (started "
                         f"{running['elapsed_seconds']:.0f}s ago) — nothing to collect yet, and "
                         "NOT a failure. Poll again with wait_seconds; do not resend the code. "
                         "interrupt_kernel aborts it if you want it stopped.")}
    if last is not None and (exec_id is None or last["exec_id"] == exec_id):
        return {"kernel_id": kernel_id, **_last_exec_view(last, max_chars)}
    if exec_id is not None:
        return {"kernel_id": kernel_id, "exec_id": exec_id, "status": "unknown",
                "note": ("no execution with that exec_id is running or remembered on this kernel "
                         "(only the most recent one is kept, and an mcp restart clears it). Call "
                         "without exec_id to see the latest, or read the notebook cells.")}
    return {"kernel_id": kernel_id, "status": "none",
            "note": ("no foreground execution has been recorded for this kernel (it may be new, "
                     "or the mcp server restarted since). Nothing is running.")}


_MAX_WAIT = max(5.0, config.SOFT_REPLY_DEADLINE_SEC - 5)   # stay inside the client's patience


def _last_exec_view(rec: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Client-facing shape of a recorded execution (internal bookkeeping stripped)."""
    text = rec["text"][-max(256, int(max_chars)):]
    out = {k: v for k, v in rec.items() if k not in ("code_sha", "finished", "text", "replayed")}
    out["text"] = text
    out["text_truncated"] = rec["text_truncated"] or len(text) < len(rec["text"])
    if rec.get("client_abandoned"):
        out["note"] = ("this execution COMPLETED after the call that started it stopped being "
                       "listened to (client-side timeout/cancel) — it is the result you did not "
                       "receive. The work already happened; do not run it again.")
    return out


@mcp.tool(annotations={"readOnlyHint": True})
async def get_job(kernel_id: str, job_id: str, offset: int | None = None,
                  max_chars: int = 4000, wait_seconds: float = 0) -> dict[str, Any]:
    """Poll a background job started by execute_code(background=True): returns its `status`
    ("running_in_background" / "done" / "error" / "unknown") and a WINDOW of the stdout+stderr
    it has captured (unbuffered — live progress shows). Cheap, non-blocking; call repeatedly
    until status is no longer "running_in_background".

    `wait_seconds` > 0 LONG-POLLS: keep checking until the job leaves the running state or
    that many seconds pass (clamped below the server's reply deadline), then answer. One
    call with wait_seconds=20 beats twenty polls. While a job is running, do NOT resend the
    execute_code that started it — it is running, not failed.

    Output is windowed so a chatty job doesn't flood you: by DEFAULT you get the LAST
    `max_chars` (the latest progress) plus `total_chars` (full size) and `next_offset` (the
    current end). To STREAM only what's new on the next poll, pass that `next_offset` back as
    `offset` — you'll get just the bytes appended since (tail -f, no re-reading). Pass
    `offset=0` to read from the start (e.g. to see the initial setup / first error); `more`=true
    means the returned window stopped at `max_chars` before the end, so page again with the new
    `next_offset`. The job ran in its own process — read any results it persisted to a file
    (execute_code / download_from_colab)."""
    reaper._ensure_background()
    wait = min(float(wait_seconds or 0), _MAX_WAIT)
    deadline = time.monotonic() + wait
    while True:
        out = await _poll_job(kernel_id, job_id, offset, max_chars)
        if out.get("status") != "running_in_background" or time.monotonic() >= deadline:
            return out
        # Sleep in coarse steps: each poll is a real kernel exec, so a tight loop would
        # both waste the kernel and queue behind any foreground execution.
        await asyncio.sleep(min(_JOB_POLL_INTERVAL_SEC, max(0.0, deadline - time.monotonic())))


_JOB_POLL_INTERVAL_SEC = 5.0


async def _poll_job(kernel_id: str, job_id: str, offset: int | None,
                    max_chars: int) -> dict[str, Any]:
    off_lit = "None" if offset is None else str(int(offset))
    mx = max(256, int(max_chars))
    poll = (
        "import pathlib as _pl, json as _json\n"
        f"_d=_pl.Path({_JOBS_DIR!r})\n"
        f"_s=_d/({job_id!r}+'.status'); _o=_d/({job_id!r}+'.out')\n"
        "_st=_s.read_text().strip() if _s.exists() else 'unknown'\n"
        "_b=_o.read_bytes() if _o.exists() else b''\n"
        "_tot=len(_b)\n"
        f"_off={off_lit}; _mx={mx}\n"
        "if _off is None:\n"
        "    _chunk=_b[-_mx:]; _start=_tot-len(_chunk)\n"
        "else:\n"
        "    _off=max(0,min(int(_off),_tot)); _chunk=_b[_off:_off+_mx]; _start=_off\n"
        "_nx=_start+len(_chunk)\n"
        "print(_json.dumps({'status':_st,'total':_tot,'start':_start,'next':_nx,"
        "'more':_nx<_tot,'output':_chunk.decode('utf-8','replace')}))\n"
    )
    try:
        reply = await backends._run_on_kernel(kernel_id, poll, timeout=60)
    except RuntimeError as e:
        # routing guard fired: the (colab) session is already gone
        if "no longer alive" in str(e) or "not found" in str(e):
            return {"kernel_id": kernel_id, "job_id": job_id, "status": "session_lost",
                    "output": "", "note": str(e)}
        raise
    if reply.get("status") == "session_lost":
        return {"kernel_id": kernel_id, "job_id": job_id, "status": "session_lost",
                "output": "", "note": reply.get("note",
                "the colab session is gone (lost/reclaimed); its job files are wiped. Start a new one.")}
    _, text = _format_outputs(reply.get("outputs", []))
    data = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                pass
    if data is None:
        return {"kernel_id": kernel_id, "job_id": job_id, "status": "unknown", "raw": text[:2000]}
    st = data.get("status") or "unknown"
    norm = "running_in_background" if st == "running" else ("error" if st.startswith("error") else st)
    out = {"kernel_id": kernel_id, "job_id": job_id, "status": norm,
           "output": data.get("output", ""), "total_chars": data.get("total"),
           "offset": data.get("start"), "next_offset": data.get("next"),
           "more": data.get("more", False)}
    if st.startswith("error") and st != "error":
        out["detail"] = st   # e.g. "error rc=1"
    return out
