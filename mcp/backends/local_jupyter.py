"""local backend: internal Jupyter Server via REST (httpx) + jupyter-kernel-client.

Owns everything that talks to the internal Jupyter: kernel/session REST, the
Contents API (notebooks + workspace files — LOCAL only, the single source of
truth), KernelClient WebSocket management, and local code execution. No colab
here, and no cross-backend dispatch (that lives in `backends/__init__.py`).
"""
from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import AsyncIterable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import httpx
from jupyter_kernel_client import KernelClient
from jupyter_kernel_client.client import output_hook as _output_hook

import state
from config import EXEC_TIMEOUT_SEC, LOCAL, MAX_KERNELS, log
from util import _contents_url, _parent_dir


def _http(backend: str = LOCAL) -> httpx.AsyncClient:
    c = state._http_clients.get(backend)
    if c is None:
        b = state._backends[backend]
        c = httpx.AsyncClient(
            base_url=b["url"],
            headers={"Authorization": f"token {b['token']}"},
            timeout=30.0,
        )
        state._http_clients[backend] = c
    return c


# ---- Jupyter REST helpers (backend-aware; contents/notebooks are LOCAL only) --
async def _kernels(backend: str = LOCAL) -> list[dict[str, Any]]:
    r = await _http(backend).get("/api/kernels")
    r.raise_for_status()
    return r.json()


async def _sessions(backend: str = LOCAL) -> list[dict[str, Any]]:
    r = await _http(backend).get("/api/sessions")
    r.raise_for_status()
    return r.json()


async def _rest_post(path: str, json: dict | None = None, backend: str = LOCAL) -> dict | None:
    r = await _http(backend).post(path, json=json)
    r.raise_for_status()
    return r.json() if r.content else None


async def _ensure_dir(dirpath: str) -> None:
    """Create a folder and any missing parents under the work root (idempotent)."""
    cur = ""
    for seg in (s for s in dirpath.strip("/").split("/") if s):
        cur = f"{cur}/{seg}" if cur else seg
        r = await _http(LOCAL).get(_contents_url(cur), params={"content": "0"})
        if r.status_code == 404:
            put = await _http(LOCAL).put(_contents_url(cur), json={"type": "directory"})
            put.raise_for_status()
        elif r.status_code != 200:
            r.raise_for_status()


async def _ensure_notebook(path: str) -> None:
    """Create an empty .ipynb at `path` (relative to the work root) if it doesn't exist,
    auto-creating parent folders, so it shows up in JupyterLab and can be read back."""
    r = await _http(LOCAL).get(_contents_url(path), params={"type": "notebook"})
    if r.status_code == 200:
        return
    if _parent_dir(path):
        await _ensure_dir(_parent_dir(path))
    nb = {"type": "notebook", "format": "json", "content": {
        "cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }}
    put = await _http(LOCAL).put(_contents_url(path), json=nb)
    put.raise_for_status()


async def _wait_ready(kernel_id: str, backend: str = LOCAL, timeout: float = 20.0) -> bool:
    """Poll until a freshly-created kernel leaves the 'starting' state (idle/busy),
    so the first execute_code doesn't race kernel startup and hang."""
    for _ in range(int(timeout / 0.5)):
        for k in await _kernels(backend):
            if k["id"] == kernel_id:
                if k.get("execution_state") in ("idle", "busy"):
                    return True
                break
        await asyncio.sleep(0.5)
    return False


async def _notebook_for_kernel(kernel_id: str) -> str | None:
    """Return the notebook path a kernel is bound to (for write-back), or None for
    a bare/scratch kernel. Cached; falls back to a sessions lookup."""
    p = state._registry.get_notebook(kernel_id)
    if p is not None:
        return p or None
    for s in await _sessions():
        if (s.get("kernel") or {}).get("id") == kernel_id and s.get("type") == "notebook":
            nb = s.get("path") or ""
            state._registry.set_notebook(kernel_id, nb)
            return nb or None
    state._registry.set_notebook(kernel_id, "")   # cache "no notebook"
    return None


async def _read_nb(path: str) -> dict[str, Any] | None:
    """Read a notebook (nbformat dict) from the LOCAL contents API, or None if absent."""
    r = await _http(LOCAL).get(_contents_url(path), params={"type": "notebook"})
    if r.status_code != 200:
        return None
    nb = r.json().get("content") or {}
    nb.setdefault("cells", [])
    nb.setdefault("nbformat", 4)
    nb.setdefault("nbformat_minor", 5)
    nb.setdefault("metadata", {})
    return nb


async def _write_nb(path: str, nb: dict[str, Any]) -> None:
    if _parent_dir(path):
        await _ensure_dir(_parent_dir(path))
    r = await _http(LOCAL).put(_contents_url(path), json={"type": "notebook", "format": "json", "content": nb})
    r.raise_for_status()


def _code_cell(code: str, reply: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"cell_type": "code", "metadata": {}, "source": code,
            "id": uuid.uuid4().hex[:8],
            "execution_count": (reply or {}).get("execution_count"),
            "outputs": (reply or {}).get("outputs", [])}


async def _append_cell(path: str, code: str, reply: dict[str, Any]) -> None:
    """Append an executed code cell (source + raw nbformat outputs) to the notebook."""
    nb = await _read_nb(path)
    if nb is None:
        return
    nb["cells"].append(_code_cell(code, reply))
    await _write_nb(path, nb)


async def _nb_exists(path: str) -> bool:
    r = await _http(LOCAL).get(_contents_url(path), params={"type": "notebook", "content": "0"})
    return r.status_code == 200


async def _next_untitled() -> str:
    """Next free `untitled-N.ipynb` at the notebooks root (Colab-style auto name)."""
    r = await _http(LOCAL).get("/api/contents")
    existing = {c["name"] for c in (r.json().get("content", []) if r.status_code == 200 else [])}
    n = 1
    while f"untitled-{n}.ipynb" in existing:
        n += 1
    return f"untitled-{n}.ipynb"


async def _resolve_notebook_path(notebook_path: str | None, if_exists: str) -> str | None:
    """Resolve the target notebook per intent. None -> a fresh untitled-N.ipynb.
    if_exists on an existing name: 'open' (reuse), 'new' (auto-suffix -1/-2…), 'error'."""
    if not notebook_path:
        return await _next_untitled()
    if not notebook_path.endswith(".ipynb"):
        notebook_path += ".ipynb"
    if not await _nb_exists(notebook_path):
        return notebook_path
    if if_exists == "open":
        return notebook_path
    if if_exists == "error":
        raise RuntimeError(f"notebook '{notebook_path}' already exists (if_exists='error')")
    if if_exists == "new":
        stem = notebook_path[:-6]
        i = 1
        while await _nb_exists(f"{stem}-{i}.ipynb"):
            i += 1
        return f"{stem}-{i}.ipynb"
    raise RuntimeError("if_exists must be 'open', 'new', or 'error'")


async def _read_workspace_bytes(path: str) -> bytes:
    """Read a workspace file (on the server) as raw bytes via the Contents API."""
    r = await _http(LOCAL).get(_contents_url(path), params={"type": "file", "format": "base64"})
    if r.status_code == 404:
        raise RuntimeError(f"workspace file not found: {path}")
    r.raise_for_status()
    j = r.json()
    return base64.b64decode(j["content"]) if j.get("format") == "base64" else j.get("content", "").encode()


# Contents-API chunked upload (LargeFileContentsManager — the jupyter_server
# >= 2.18 default, and the floor jupyter/requirements.txt now pins): the file
# crosses as ~4 MB pieces appended server-side, so neither this process nor the
# Jupyter server buffers the whole body (ADR 0021).
_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024


async def _put_file_chunk(path: str, data: bytes, chunk: int) -> None:
    """One Contents-API upload chunk: 1 = first (creates/replaces), 2..N = append,
    -1 = last. A lone chunk=1 is already a complete write; the trailing -1 just
    closes the sequence (runs the post-save hooks)."""
    r = await _http(LOCAL).put(
        _contents_url(path),
        json={"type": "file", "format": "base64",
              "content": base64.b64encode(data).decode(), "chunk": chunk})
    r.raise_for_status()


def _partial_path(path: str) -> str:
    """Sibling temp name for an in-flight upload. NOT dot-prefixed: the default
    contents manager (allow_hidden=False) refuses to read or rename hidden files."""
    return f"{path}.partial-{uuid.uuid4().hex[:8]}"


async def _write_file_chunked(path: str, chunks: AsyncIterable[bytes],
                              overwrite: bool = False) -> int:
    """Write `chunks` to a workspace file ATOMICALLY-ISH: chunked upload into a
    sibling temp name, renamed into place (PATCH) only once the whole body landed.

    An abort/cap/error mid-stream leaves the temp file (deleted best-effort) and
    never touches the target — no partial file masquerading as content, no good
    file clobbered by a failed overwrite. With overwrite=True an existing target
    is DELETEd first (rename refuses an existing destination)."""
    tmp = _partial_path(path)
    try:
        it = aiter(chunks)
        pending = await anext(it, None)
        total = 0
        if pending is None:
            await _put_file_chunk(tmp, b"", 1)   # an empty stream still creates the file
        else:
            n = 0
            while True:
                nxt = await anext(it, None)
                total += len(pending)
                await _put_file_chunk(tmp, pending,
                                      1 if n == 0 else (-1 if nxt is None else n + 1))
                n += 1
                if nxt is None:
                    break
                pending = nxt
            if n == 1:   # a lone piece was a full write; -1 closes the sequence
                await _put_file_chunk(tmp, b"", -1)
        if overwrite:
            # PATCH rename 409s on an existing destination — remove it first.
            chk = await _http(LOCAL).get(_contents_url(path), params={"content": "0"})
            if chk.status_code == 200:
                d = await _http(LOCAL).delete(_contents_url(path))
                if d.status_code not in (204, 404):
                    d.raise_for_status()
        r = await _http(LOCAL).patch(_contents_url(tmp), json={"path": path})
        r.raise_for_status()
        return total
    except BaseException:
        try:
            await _http(LOCAL).delete(_contents_url(tmp))
        except Exception as e:  # noqa: BLE001
            log.warning("cleanup of partial upload %s failed: %s", tmp, e)
        raise


# ---- KernelClient management (backend-aware) --------------------------------
def _connect_client(kernel_id: str, backend: str) -> KernelClient:
    b = state._backends[backend]
    # Use the `token <tok>` Authorization scheme (via headers) rather than
    # jupyter-kernel-client's default `Bearer`: older Jupyter Servers (e.g. Colab's
    # 2.18.x) reject Bearer with 403. token=None avoids the lib overwriting our
    # header. The `token` scheme is accepted by all Jupyter Server versions.
    kc = KernelClient(
        server_url=b["url"], token=None, kernel_id=kernel_id,
        headers={"Authorization": f"token {b['token']}"},
    )
    kc.start()  # attaches to the existing kernel over WS
    return kc


async def _get_client(kernel_id: str) -> KernelClient:
    kc = state._clients.get(kernel_id)
    if kc is None:
        kc = await asyncio.to_thread(_connect_client, kernel_id, state._backend_of(kernel_id) or LOCAL)
        state._clients[kernel_id] = kc
    return kc


async def _drop_client(kernel_id: str) -> None:
    kc = state._clients.pop(kernel_id, None)
    if kc is not None:
        try:
            await asyncio.to_thread(kc.stop)
        except Exception as e:  # noqa: BLE001
            log.warning("error stopping client %s: %s", kernel_id, e)


def _execute_streaming(kc: KernelClient, code: str, sink: list[dict[str, Any]],
                       timeout: float) -> dict[str, Any]:
    """`KernelClient.execute`, but appending each output to `sink` AS IT ARRIVES.

    The library builds its output list in a local and only hands it back at the end, so a
    long execution looks silent until it finishes — exactly when we most need to show that
    it is alive (the `still_running` reply, ADR 0018). `execute_interactive`'s output_hook
    is the supported seam for this; reaching it means going through the manager's client, so
    an upstream reshuffle falls back to the plain call rather than breaking execution.

    The fallback is chosen BEFORE anything is sent to the kernel. Wrapping the execution
    itself in the `except` would mean an AttributeError raised mid-run — from the output
    hook, say — retried the whole cell on a kernel that had already run it."""
    client = getattr(getattr(kc, "_manager", None), "client", None)
    if client is None or not hasattr(client, "execute_interactive"):  # pragma: no cover
        log.warning("live output capture unavailable; falling back to buffered execute")
        return kc.execute(code, timeout=timeout)
    reply = client.execute_interactive(
        code, allow_stdin=False, stop_on_error=True, timeout=timeout,
        output_hook=partial(_output_hook, sink))
    content = reply["content"]
    for output in sink:
        output.pop("transient", None)
    return {"execution_count": content.get("execution_count"), "outputs": list(sink),
            "status": content["status"]}


# An execution with no hard cap must still hand the library a FINITE deadline: past its
# own timeout `execute_interactive` degrades into a zero-second wait loop that spins a
# core, so a small value would burn CPU for the rest of a long run. A month is finite
# enough to let an orphaned worker thread eventually unwind and long enough that no real
# execution ever reaches it — the liveness watchdog below is what actually bounds us.
_LIB_DEADLINE_SEC = 30 * 24 * 3600
_LIVENESS_INTERVAL_SEC = 30.0

# Execution workers run the blocking jupyter-client call on a DEDICATED bounded
# pool. A kernel that never answers (an interrupt-immune C-extension loop, a dead
# VM, a restart that kept the websocket open) leaves its worker parked until
# _LIB_DEADLINE_SEC; on the shared default executor those orphans would
# eventually starve every other asyncio.to_thread call (connect, stop,
# list_variables). A worker the waiter gave up on is counted and logged so the
# leak is visible instead of silent (audit #17, item 2).
_exec_pool = ThreadPoolExecutor(
    max_workers=max(4, MAX_KERNELS), thread_name_prefix="rmcp-exec")
_orphaned_exec_workers = 0


def _orphan_worker(task: asyncio.Future, kernel_id: str) -> None:
    """Mark an execution worker we are abandoning while it may still run.

    The flag on the future makes repeated calls idempotent (several cleanup paths
    can observe the same still-parked worker); the done callback restores the
    count when the library finally unwinds it — or confirms it never does."""
    global _orphaned_exec_workers
    if task.done() or getattr(task, "_rmcp_orphaned", False):
        return
    task._rmcp_orphaned = True  # type: ignore[attr-defined]
    _orphaned_exec_workers += 1
    log.warning("execution worker on %s orphaned (kernel stopped replying); %d parked",
                kernel_id, _orphaned_exec_workers)

    def _unwound(_t: asyncio.Future) -> None:
        global _orphaned_exec_workers
        _orphaned_exec_workers -= 1
        log.info("orphaned execution worker on %s unwound; %d still parked",
                 kernel_id, _orphaned_exec_workers)

    task.add_done_callback(_unwound)


async def _kernel_is_running(kernel_id: str, backend: str) -> bool:
    """Is this kernel still executing something? Errors count as 'yes' — a transient
    Jupyter hiccup must never be read as 'your execution died'."""
    try:
        return any(k.get("id") == kernel_id and k.get("execution_state") == "busy"
                   for k in await _kernels(backend))
    except Exception as e:  # noqa: BLE001
        log.warning("liveness check failed for %s: %s", kernel_id, e)
        return True


# A hung Jupyter must not make this check outlast the soft reply deadline (the shared
# client waits 30s before the caller's own lookup would even start).
_OOB_CHECK_TIMEOUT_SEC = 5.0


async def _kernel_busy_oob(kernel_id: str, backend: str = LOCAL) -> bool:
    """Is this kernel executing something NOT launched through this server (a human's
    JupyterLab cell — though transient kernel requests like completions/kernel_info
    report busy too)? running_exec can't see out-of-band work, but /api/kernels can.
    A lookup failure answers False — the caller's own request surfaces the real error
    rather than a misleading 'busy'."""
    try:
        r = await _http(backend).get("/api/kernels", timeout=_OOB_CHECK_TIMEOUT_SEC)
        r.raise_for_status()
        return any(k.get("id") == kernel_id and k.get("execution_state") == "busy"
                   for k in r.json())
    except Exception:  # noqa: BLE001
        return False


async def _await_uncapped(task: asyncio.Future, kernel_id: str, backend: str) -> dict[str, Any]:
    """Wait for an execution with no hard cap — but notice if the kernel stops running it.

    `execute_interactive` returns only on an iopub `idle` whose parent matches ITS request.
    A kernel restarted in place (the JupyterLab button a co-editing human has, or Jupyter's
    own restart after an OOM kill) keeps the websocket open and never emits that message,
    so the call would wait forever: the single-flight claim would never be released and the
    kernel would answer `busy` for the life of the process. Two consecutive non-busy
    observations mean our execution is gone, whatever the library still believes.

    Never cancels the task — a watchdog that killed the work would defeat the whole point."""
    misses = 0
    while True:
        done, _ = await asyncio.wait({task}, timeout=_LIVENESS_INTERVAL_SEC)
        if done:
            return task.result()
        if await _kernel_is_running(kernel_id, backend):
            misses = 0
            continue
        misses += 1
        if misses >= 2:
            await _drop_client(kernel_id)   # let the orphaned worker unwind
            _orphan_worker(task, kernel_id)
            raise RuntimeError(
                f"kernel '{kernel_id}' stopped running this execution before it finished — it "
                "was restarted or died underneath it (a manual restart in JupyterLab, or the "
                "kernel process being killed). NOTHING further will arrive for this call and "
                "its result is unrecoverable. The kernel itself is usable again; re-run the "
                "code if you still need it.")


# ---- local execution (the jupyter branch of the old _run_on_kernel) ---------
async def exec_local(kernel_id: str, backend: str, code: str, timeout: float | None = None,
                     output_sink: list[dict[str, Any]] | None = None,
                     acquire_timeout: float | None = None) -> dict[str, Any]:
    """Execute code on a LOCAL Jupyter kernel; return a normalized reply
    {status, execution_count, outputs(nbformat), timed_out}. No notebook write-back.

    `timeout` (or EXEC_TIMEOUT_SEC when it is None) is a HARD cap: it INTERRUPTS the kernel.
    An explicit 0 means NO cap ever — the introspection callers use that because they must
    never interrupt work (ADR 0018). With no cap nothing is interrupted — a long execution
    is meant to be detached and polled, not killed. `acquire_timeout` bounds the wait on
    the per-kernel lock: on expiry the reply is status="kernel_busy" — used by
    introspection execs that must answer promptly rather than queue behind a running one.
    `output_sink` receives outputs as they arrive so a detached execution can show live
    progress."""
    # Verify the kernel actually exists before attaching, so we never auto-create or
    # attach to a phantom (a stopped/reaped id must fail cleanly, not mis-route).
    if not any(k.get("id") == kernel_id for k in await _kernels(backend)):
        raise RuntimeError(f"kernel '{kernel_id}' not found on backend '{backend}' — it was stopped "
                           "or reaped. Start a new kernel with start_kernel().")
    to = float(timeout) if timeout is not None else EXEC_TIMEOUT_SEC
    sink = output_sink if output_sink is not None else state.BoundedSink()
    lock = state._locks.setdefault(kernel_id, asyncio.Lock())
    try:
        await asyncio.wait_for(lock.acquire(), acquire_timeout)
    except TimeoutError:
        # Another exec holds this kernel's lock and outlasted the caller's patience.
        # Introspection reports busy instead of queueing — a kernel exec behind a
        # day-long run is the no-reply pattern ADR 0018 exists to remove.
        return {"status": "kernel_busy", "execution_count": None, "outputs": [],
                "timed_out": False}
    try:
        kc = await _get_client(kernel_id)
        # jupyter_client's `timeout` does NOT bound total run time; enforce it here.
        # The worker thread can't be cancelled, so shield it, interrupt on timeout,
        # then wait a grace for it to unwind — holding the per-kernel lock throughout.
        # It runs on the dedicated _exec_pool so a never-answering kernel parks a
        # worker here instead of starving the shared default executor.
        task = asyncio.get_running_loop().run_in_executor(
            _exec_pool, partial(_execute_streaming, kc, code, sink, (to or 0) + _LIB_DEADLINE_SEC))
        try:
            # No hard cap (`to` == 0): wait for the work itself, watching the kernel rather
            # than a clock. Detaching (state.run_single_flight) is what keeps the CALLER
            # responsive; nothing here kills the execution, so a long job is never truncated.
            reply = (await asyncio.wait_for(asyncio.shield(task), timeout=to) if to
                     else await _await_uncapped(task, kernel_id, backend))
        except TimeoutError:
            log.info("execute exceeded %.0fs on %s; interrupting", to, kernel_id)
            try:
                await _rest_post(f"/api/kernels/{kernel_id}/interrupt", backend=backend)
                reply = await asyncio.wait_for(asyncio.shield(task), timeout=15)
            except TimeoutError:
                # The interrupt did not land. Do NOT return yet: the kernel is still running
                # our code, and returning would release the single-flight claim (ADR 0017)
                # while it does — handing a retry an idle-looking kernel. Wait for the kernel
                # itself to say it is done, with the same watchdog that bounds the uncapped
                # path. Whatever the sink captured before the cutoff is still ours to report.
                log.info("interrupt did not unwind on %s; waiting for the kernel to go idle",
                         kernel_id)
                try:
                    reply = await _await_uncapped(task, kernel_id, backend)
                except RuntimeError:
                    await _drop_client(kernel_id)
                    return {"status": "timeout", "execution_count": None,
                            "outputs": list(sink), "timed_out": True,
                            "note": ("the interrupt did not unwind and the kernel then stopped "
                                     "running this code (restarted or killed). Any output "
                                     "captured before the cutoff is included; re-run if you "
                                     "still need the result.")}
            reply["timed_out"] = True
            return reply
        except Exception as e:  # noqa: BLE001
            _orphan_worker(task, kernel_id)
            await _drop_client(kernel_id)
            raise RuntimeError(f"execution failed: {e}") from e
    finally:
        lock.release()
    reply["timed_out"] = False
    return reply
