"""Process-wide mutable singletons + trivial registry readers.

Every module that touches shared state imports THIS module and mutates the SAME
objects (never re-instantiates them) — that is the whole point of a single state
home. The heavier routing/dispatch logic lives in `backends/`; only the two
zero-dependency readers (`_backend_of`, `backend_kind`) live here so the backend
implementations can use them without importing the `backends` package (which
would be a cycle). The single-flight execution guard lives here for the same
reason: both tool modules that execute user code need it, neither may import the
other, and it is pure in-process state (see ADR 0017).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from config import JUPYTER_TOKEN, JUPYTER_URL, LOCAL, MCP_STATE_DIR, log
from outputs import _format_outputs
from registry import KernelRegistry
from util import _now

if TYPE_CHECKING:
    from jupyter_kernel_client import KernelClient

# ---- backends ---------------------------------------------------------------
# `local` is the internal Jupyter (kind="jupyter"); the colab-cli backend
# (kind="cli") is registered lazily by reaper._ensure_background when available.
_backends: dict[str, dict[str, str]] = {LOCAL: {"kind": "jupyter", "url": JUPYTER_URL, "token": JUPYTER_TOKEN}}
_http_clients: dict[str, httpx.AsyncClient] = {}

# ---- per-kernel runtime state ----------------------------------------------
_clients: dict[str, KernelClient] = {}          # kernel_id -> live KernelClient (in-memory)
_locks: dict[str, asyncio.Lock] = {}            # kernel_id -> serialize executes (per-process)
# Persistent tracking (backend, notebook, first_seen, last_heartbeat, pinned) — survives
# mcp restart/rebuild, killing the restart-amnesia bug class. See docs/refactor/PHASE-1.
_registry = KernelRegistry(os.path.join(MCP_STATE_DIR, "kernels.json"))

# colab-cli availability: requires BOTH the binary and a real ADC credentials FILE.
# Checking the file (not just the env var) matters because deployments without Colab
# still set the var — compose mounts /dev/null there — and an unusable colab backend
# should not be offered at all. Monkeypatched in tests.
COLAB_AVAILABLE = bool(shutil.which("colab")) and os.path.isfile(
    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))


def _backend_of(kernel_id: str) -> str | None:
    """The kernel's backend from the registry, or None if unknown. Unknown NO LONGER
    means 'local' — the old implicit fallback once ran a dead colab id on the LOCAL
    kernel. Execution routing goes through backends._resolve_backend (explicit, raises)."""
    return _registry.get_backend(kernel_id)


def backend_kind(name: str | None) -> str:
    return _backends.get(name, {}).get("kind", "jupyter")


# ---- single-flight execution guard (ADR 0017) -------------------------------
# ONE foreground execution per kernel at a time. A Jupyter kernel QUEUES messages on
# its shell channel, so a second execute_* arriving while the first is still running
# does not fail — it waits, then runs. That is exactly how a client-side timeout
# ("the tool call never returned, so it must have failed") turns into the same code
# running TWICE: doubled variable updates, doubled file writes, doubled pip installs.
# These two dicts make the second call REFUSABLE and the first call's result
# RECOVERABLE — an agent only stops retrying if it gets both.
#
# Deliberately in-process and NOT persisted to the registry: after an mcp restart
# nothing of ours is executing, so an empty in-flight map is the correct state.
_inflight: dict[str, dict[str, Any]] = {}    # kernel_id -> the execution running right now
_last_exec: dict[str, dict[str, Any]] = {}   # kernel_id -> the last one that COMPLETED

_CODE_HEAD_CHARS = 120     # how much code is echoed back to identify an execution
_LAST_TEXT_CHARS = 8000    # output text retained per kernel for get_execution
_LAST_EXEC_MAX = 256       # cap on remembered kernels (stop_kernel normally prunes)
_REPLAY_WINDOW_SEC = 300.0  # how long an abandoned execution's result stays replayable
_PARTIAL_TEXT_CHARS = 4000  # tail of the live output shown with a still_running reply


def _code_sha(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def running_exec(kernel_id: str) -> dict[str, Any] | None:
    """The foreground execution in flight on this kernel (with elapsed time), or None."""
    info = _inflight.get(kernel_id)
    if info is None:
        return None
    return {"exec_id": info["exec_id"], "tool": info["tool"], "code_head": info["code_head"],
            "started_at": info["started_at"],
            "detached": bool(info.get("detached") or info.get("abandoned")),
            "elapsed_seconds": round(time.monotonic() - info["started"], 1)}


def _next_action(kernel_id: str) -> str:
    return (f"WAIT, then call get_execution('{kernel_id}', wait_seconds=20) to collect the "
            "result once it finishes (it never touches the kernel, so it answers even while one "
            "is busy; for a colab kernel colab_log also works). To ABORT the running execution "
            "instead, call interrupt_kernel. For work that legitimately takes minutes, use "
            "execute_code(background=True) + get_job, which returns immediately.")


def _busy_result(kernel_id: str, code: str) -> dict[str, Any] | None:
    """The refusal payload if this kernel is already executing, else None (free to run)."""
    running = running_exec(kernel_id)
    if running is None:
        return None
    is_retry = _inflight[kernel_id]["code_sha"] == _code_sha(code)
    elapsed = running["elapsed_seconds"]
    if is_retry:
        note = (f"NOTHING WAS EXECUTED. This is the SAME code that is ALREADY running on kernel "
                f"'{kernel_id}' (started {elapsed:.0f}s ago) — i.e. a retry of a call that only "
                "appeared to fail. It did not fail: it is STILL RUNNING and your client gave up "
                "waiting for the reply. Do NOT resend it. A second copy would be queued behind "
                "the first and then run a second time.")
    else:
        note = (f"NOTHING WAS EXECUTED. Kernel '{kernel_id}' is already running a DIFFERENT "
                f"execution (started {elapsed:.0f}s ago). A kernel runs one thing at a time, and "
                "a second call would silently queue behind it, so it was refused instead.")
    return {"status": "busy", "kernel_id": kernel_id, "is_retry": is_retry,
            "running": running, "note": note, "next_action": _next_action(kernel_id)}


def _take_replay(kernel_id: str, code: str) -> dict[str, Any] | None:
    """Serve the result of an execution the CLIENT ABANDONED, once, if this call resends
    its exact code soon after. Covers the other half of the retry race: the client times
    out, the execution finishes anyway, and only THEN does the retry arrive to an idle
    kernel. Restricted to abandoned executions (we have positive evidence the caller never
    saw the result) and consumed on first use, so a deliberate re-run is never swallowed."""
    rec = _last_exec.get(kernel_id)
    if (rec is None or not rec.get("client_abandoned") or rec.get("replayed")
            or rec.get("code_sha") != _code_sha(code)
            or time.monotonic() - rec["finished"] > _REPLAY_WINDOW_SEC):
        return None
    rec["replayed"] = True
    out = {k: v for k, v in rec.items() if k not in ("code_sha", "finished", "client_abandoned")}
    out.update({"kernel_id": kernel_id, "replayed": True, "note": (
        "NOTHING WAS RE-EXECUTED — this is the RECOVERED result of the identical execution "
        f"your client abandoned {round(time.monotonic() - rec['finished'])}s ago (it completed "
        "on the kernel after your call timed out). The work is done; do not run it again. This "
        "replay is one-shot: sending the same code again really will execute it.")})
    return out


def _record_exec(kernel_id: str, info: dict[str, Any], result: dict[str, Any]) -> None:
    """Remember a COMPLETED foreground execution so its result survives the client that
    stopped listening for it. One entry per kernel — the point is recovery, not history.

    `client_abandoned` (the caller cancelled, so it got NOTHING) is tracked apart from
    `detached` (we answered `still_running`, so the caller holds an exec_id): only the
    former licenses a one-shot replay, because only there is a resend certainly blind."""
    abandoned = bool(info.get("abandoned"))
    text = str(result.get("text") or "")
    _last_exec[kernel_id] = {
        "exec_id": info["exec_id"], "tool": info["tool"], "code_head": info["code_head"],
        "code_sha": info["code_sha"], "status": result.get("status"),
        "execution_count": result.get("execution_count"),
        "text": text[-_LAST_TEXT_CHARS:], "text_truncated": len(text) > _LAST_TEXT_CHARS,
        "saved_to": result.get("saved_to"),
        "duration_seconds": round(time.monotonic() - info["started"], 1),
        "started_at": info["started_at"], "finished_at": _now().isoformat(),
        "finished": time.monotonic(), "client_abandoned": abandoned,
        "detached": bool(info.get("detached")),
    }
    while len(_last_exec) > _LAST_EXEC_MAX:
        _last_exec.pop(next(iter(_last_exec)))


def _release(kernel_id: str, exec_id: str) -> dict[str, Any] | None:
    """Drop the in-flight claim, but ONLY if it is still ours (never evict a newer one)."""
    info = _inflight.get(kernel_id)
    if info is not None and info["exec_id"] == exec_id:
        return _inflight.pop(kernel_id)
    return None


def forget_exec(kernel_id: str) -> None:
    """Drop all execution bookkeeping for a kernel that no longer exists."""
    _inflight.pop(kernel_id, None)
    _last_exec.pop(kernel_id, None)


async def _guarded(kernel_id: str, info: dict[str, Any],
                   runner: Callable[[list], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """The execution itself, wrapped so that release + recording happen INSIDE the task.

    Doing the bookkeeping here rather than in a done-callback is what lets any observer
    trust `task.done()`: by the time the task completes, the claim is gone and the result
    is in `_last_exec`. A callback would run a tick later, and `get_execution`'s long poll
    would race it."""
    try:
        result = await runner(info["sink"])
    except BaseException as e:
        _release(kernel_id, info["exec_id"])
        _record_exec(kernel_id, info, {"status": "error", "text": f"{type(e).__name__}: {e}"})
        raise
    _release(kernel_id, info["exec_id"])
    _record_exec(kernel_id, info, result)
    result.setdefault("exec_id", info["exec_id"])
    return result


def _still_running(kernel_id: str, info: dict[str, Any]) -> dict[str, Any]:
    """The soft-deadline reply: an honest 'not done yet', which beats no reply at all."""
    running = running_exec(kernel_id) or {}
    return {
        "status": "still_running", "kernel_id": kernel_id, "exec_id": info["exec_id"],
        "elapsed_seconds": running.get("elapsed_seconds"),
        "partial_output": _sink_text(info["sink"])[-_PARTIAL_TEXT_CHARS:],
        "note": (
            f"The code is STILL RUNNING on kernel '{kernel_id}' — this is NOT a failure and "
            "NOT a reason to resend it. Nothing was interrupted; the server answered early so "
            "your client would not time out waiting. Poll "
            f"get_execution('{kernel_id}', wait_seconds=20) until it returns a final status; "
            "the full output and the notebook write-back land there when it finishes. "
            "`partial_output` is what the kernel has printed so far (local kernels only). If "
            "you expected this to be long, prefer execute_code(background=True) next time — "
            "it returns immediately and streams via get_job."),
    }


def _sink_text(sink: list) -> str:
    """Whatever the running execution has printed so far (empty when live capture is
    unavailable, e.g. the colab backend, which only hands back output at the end)."""
    if not sink:
        return ""
    try:
        _, text = _format_outputs(list(sink))
    except Exception:  # noqa: BLE001 — a half-written output must never break the reply
        return ""
    return text


async def run_single_flight(kernel_id: str, code: str, tool: str,
                            runner: Callable[[list], Awaitable[dict[str, Any]]],
                            deadline: float | None = None) -> dict[str, Any]:
    """Run `runner` as the kernel's one foreground execution, and ALWAYS answer in time.

    Four exits, and every one of them leaves the guard consistent:
      - kernel busy      -> the refusal payload, having run NOTHING
      - runs in time     -> claim, run, release, remember the result
      - hits the soft deadline -> DETACH: the work keeps running, the claim stays held,
        and the caller gets `still_running` + an exec_id to poll. Answering late is the
        same as not answering — the client has already given up and told the agent it
        failed (ADR 0018).
      - caller cancelled -> same thing, minus the reply: the claim stays held until the
        work really ends, because releasing it would let the client's retry re-execute.

    The work is shielded in both detach cases, so nothing on the kernel is ever killed by
    this function; only a caller-supplied `timeout` interrupts a kernel.
    """
    busy = _busy_result(kernel_id, code)
    if busy is not None:
        return busy
    replay = _take_replay(kernel_id, code)
    if replay is not None:
        return replay
    info: dict[str, Any] = {
        "exec_id": "exec-" + uuid.uuid4().hex[:8], "tool": tool,
        "code_sha": _code_sha(code), "code_head": code[:_CODE_HEAD_CHARS],
        "started": time.monotonic(), "started_at": _now().isoformat(),
        "sink": [], "detached": False, "abandoned": False}
    _inflight[kernel_id] = info
    task = asyncio.ensure_future(_guarded(kernel_id, info, runner))
    info["task"] = task
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=deadline)
    except TimeoutError:
        info["detached"] = True
        log.info("%s on %s: soft deadline reached; detaching %s", tool, kernel_id, info["exec_id"])
        return _still_running(kernel_id, info)
    except asyncio.CancelledError:
        info["abandoned"] = True
        log.info("%s on %s: caller went away; holding the kernel claim until it finishes",
                 tool, kernel_id)
        raise


def partial_output(kernel_id: str, max_chars: int = _PARTIAL_TEXT_CHARS) -> str:
    """What the kernel's running execution has printed so far ("" if none / not capturable)."""
    info = _inflight.get(kernel_id)
    return "" if info is None else _sink_text(info["sink"])[-max(256, int(max_chars)):]


async def wait_for_exec(kernel_id: str, exec_id: str | None, seconds: float) -> None:
    """Block up to `seconds` for the kernel's running execution to finish. Never cancels
    it: a poller giving up must not kill the work it was waiting on."""
    info = _inflight.get(kernel_id)
    if info is None or (exec_id is not None and info["exec_id"] != exec_id):
        return
    task = info.get("task")
    if task is None or seconds <= 0:
        return
    await asyncio.wait({task}, timeout=seconds)
