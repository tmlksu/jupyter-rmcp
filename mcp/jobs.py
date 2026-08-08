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

import base64
import json
import uuid
from typing import Any

import backends
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


async def _execute_foreground(kernel_id: str, code: str, timeout: float | None) -> dict[str, Any]:
    reply = await backends._run_on_kernel(kernel_id, code, timeout)
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
    EXISTING cell without duplicating it, use execute_cell / edit_cell instead. On
    timeout the kernel is interrupted. Works for local and colab backends.

    Foreground calls run through IPython, so `!shell` and `%magic` (e.g. `!pip install …`)
    DO work here. (Background jobs do NOT — see below.)

    TIMEOUT ≠ FAILURE: a result with status="timeout" (timed_out=True) means the reply
    didn't arrive in time, NOT that the code failed — the kernel may still be running (heavy
    compute or large output can make the reply lag the visible stdout). Any output captured
    before the cutoff is returned, and a `note` explains next steps.

    NEVER RESEND A CALL THAT SEEMED TO FAIL. One kernel runs ONE execution at a time. If a
    call is still in flight, this returns status="busy" and executes NOTHING (rather than
    queueing your code to run a second time behind the first); collect the first call's
    result with get_last_execution(kernel_id) instead of re-running it.

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

    async def _run() -> dict[str, Any]:
        if background:
            return await _execute_background(kernel_id, code)
        return await _execute_foreground(kernel_id, code, timeout)

    # The background LAUNCH is an exec too (it runs on the kernel), so it takes the same
    # slot — but only for the second or two it takes to spawn the detached process; the
    # job itself holds nothing, which is why long work belongs there.
    return await state.run_single_flight(kernel_id, code, "execute_code", _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_last_execution(kernel_id: str, max_chars: int = 4000) -> dict[str, Any]:
    """What is (or was) running on this kernel — the answer to a call that never came back.

    Use this INSTEAD of re-running code whenever an execute_code/execute_cell call appeared
    to fail, timed out client-side, or came back status="busy". It reads in-process
    bookkeeping only — no kernel round-trip — so it answers instantly even while the kernel
    is busy, and it is always safe to call.

    - status="running": that kernel is executing RIGHT NOW; `running` gives exec_id,
      code_head and elapsed_seconds. Wait and poll; do not resend the code.
    - status=<the finished execution's status>: the last COMPLETED foreground execution —
      its code_head, output `text` (last `max_chars`), execution_count and timing.
      `client_abandoned` means your client had already stopped listening when it finished,
      i.e. this is precisely the result you were missing.
    - status="none": nothing has run on this kernel through this server yet.

    Only foreground executions are tracked, one per kernel, and only in memory (an mcp
    restart clears it). Background jobs have get_job; a colab session's full history has
    colab_log; a local notebook's cells hold the durable record."""
    running = state.running_exec(kernel_id)
    last = state._last_exec.get(kernel_id)
    if running is not None:
        out = {"kernel_id": kernel_id, "status": "running", "running": running,
               "note": (f"kernel '{kernel_id}' is STILL executing (started "
                        f"{running['elapsed_seconds']:.0f}s ago). Nothing to collect yet — wait "
                        "and call this again. Do NOT resend the code; interrupt_kernel aborts it.")}
        if last is not None:
            out["previous"] = _last_exec_view(last, max_chars)
        return out
    if last is None:
        return {"kernel_id": kernel_id, "status": "none",
                "note": ("no foreground execution has been recorded for this kernel (it may be "
                         "new, or the mcp server restarted since). Nothing is running.")}
    return {"kernel_id": kernel_id, **_last_exec_view(last, max_chars)}


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
                  max_chars: int = 4000) -> dict[str, Any]:
    """Poll a background job started by execute_code(background=True): returns its `status`
    ("running_in_background" / "done" / "error" / "unknown") and a WINDOW of the stdout+stderr
    it has captured (unbuffered — live progress shows). Cheap, non-blocking; call repeatedly
    until status is no longer "running_in_background".

    Output is windowed so a chatty job doesn't flood you: by DEFAULT you get the LAST
    `max_chars` (the latest progress) plus `total_chars` (full size) and `next_offset` (the
    current end). To STREAM only what's new on the next poll, pass that `next_offset` back as
    `offset` — you'll get just the bytes appended since (tail -f, no re-reading). Pass
    `offset=0` to read from the start (e.g. to see the initial setup / first error); `more`=true
    means the returned window stopped at `max_chars` before the end, so page again with the new
    `next_offset`. The job ran in its own process — read any results it persisted to a file
    (execute_code / download_from_colab)."""
    reaper._ensure_background()
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
