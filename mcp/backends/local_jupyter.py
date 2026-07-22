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
from typing import Any

import httpx
from jupyter_kernel_client import KernelClient

import state
from config import EXEC_TIMEOUT_SEC, LOCAL, log
from util import _parent_dir


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
        r = await _http(LOCAL).get(f"/api/contents/{cur}", params={"content": "0"})
        if r.status_code == 404:
            put = await _http(LOCAL).put(f"/api/contents/{cur}", json={"type": "directory"})
            put.raise_for_status()
        elif r.status_code != 200:
            r.raise_for_status()


async def _ensure_notebook(path: str) -> None:
    """Create an empty .ipynb at `path` (relative to the work root) if it doesn't exist,
    auto-creating parent folders, so it shows up in JupyterLab and can be read back."""
    r = await _http(LOCAL).get(f"/api/contents/{path}", params={"type": "notebook"})
    if r.status_code == 200:
        return
    if _parent_dir(path):
        await _ensure_dir(_parent_dir(path))
    nb = {"type": "notebook", "format": "json", "content": {
        "cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }}
    put = await _http(LOCAL).put(f"/api/contents/{path}", json=nb)
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
    r = await _http(LOCAL).get(f"/api/contents/{path}", params={"type": "notebook"})
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
    r = await _http(LOCAL).put(f"/api/contents/{path}", json={"type": "notebook", "format": "json", "content": nb})
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
    r = await _http(LOCAL).get(f"/api/contents/{path}", params={"type": "notebook", "content": "0"})
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
    r = await _http(LOCAL).get(f"/api/contents/{path}", params={"type": "file", "format": "base64"})
    if r.status_code == 404:
        raise RuntimeError(f"workspace file not found: {path}")
    r.raise_for_status()
    j = r.json()
    return base64.b64decode(j["content"]) if j.get("format") == "base64" else j.get("content", "").encode()


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


# ---- local execution (the jupyter branch of the old _run_on_kernel) ---------
async def exec_local(kernel_id: str, backend: str, code: str,
                     timeout: float | None = None) -> dict[str, Any]:
    """Execute code on a LOCAL Jupyter kernel; return a normalized reply
    {status, execution_count, outputs(nbformat), timed_out}. No notebook write-back."""
    # Verify the kernel actually exists before attaching, so we never auto-create or
    # attach to a phantom (a stopped/reaped id must fail cleanly, not mis-route).
    if not any(k.get("id") == kernel_id for k in await _kernels(backend)):
        raise RuntimeError(f"kernel '{kernel_id}' not found on backend '{backend}' — it was stopped "
                           "or reaped. Start a new kernel with start_kernel().")
    to = float(timeout) if timeout else EXEC_TIMEOUT_SEC
    lock = state._locks.setdefault(kernel_id, asyncio.Lock())
    async with lock:
        kc = await _get_client(kernel_id)
        # jupyter_client's `timeout` does NOT bound total run time; enforce it here.
        # The worker thread can't be cancelled, so shield it, interrupt on timeout,
        # then wait a grace for it to unwind — holding the per-kernel lock throughout.
        task = asyncio.ensure_future(asyncio.to_thread(kc.execute, code, timeout=to + 3600))
        try:
            reply = await asyncio.wait_for(asyncio.shield(task), timeout=to)
        except TimeoutError:
            log.info("execute exceeded %.0fs on %s; interrupting", to, kernel_id)
            try:
                await _rest_post(f"/api/kernels/{kernel_id}/interrupt", backend=backend)
                reply = await asyncio.wait_for(asyncio.shield(task), timeout=15)
            except TimeoutError:
                await _drop_client(kernel_id)
                return {"status": "timeout", "execution_count": None, "outputs": [], "timed_out": True,
                        "note": ("interrupt did not unwind within the grace period — the kernel may "
                                 "still be busy. Captured output could not be recovered here; verify "
                                 "via a result the cell persists, or restart_kernel to abort.")}
            reply["timed_out"] = True
            return reply
        except Exception as e:  # noqa: BLE001
            await _drop_client(kernel_id)
            raise RuntimeError(f"execution failed: {e}") from e
    reply["timed_out"] = False
    return reply
