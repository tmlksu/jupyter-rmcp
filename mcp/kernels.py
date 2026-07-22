"""Kernel lifecycle + introspection tools: start/stop/restart/interrupt/list/pin,
colab_log, list_variables, list_backends."""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
import uuid
from typing import Any

import backends
import config
import reaper
import state
from app import mcp
from backends import colab_cli, local_jupyter
from config import COLAB, LOCAL
from outputs import _format_outputs, _strip_ansi
from util import _now, _parse_ts


@mcp.tool
async def list_kernels() -> list[dict[str, Any]]:
    """List running kernels across all backends (local + registered Colab), with
    their backend, notebook binding, state, idle/age seconds, and pin status."""
    reaper._ensure_background()
    now = _now()
    out = []
    for backend in list(state._backends):
        if backends.is_cli(backend):
            # Reconcile with colab-cli's actual live set so state reflects REALITY (not a stale
            # cache): adopt live sessions we lost track of, and forget tracked ones that have
            # died/been reclaimed (they'd otherwise keep showing "running" — a real footgun).
            live = await colab_cli._live_colab_sessions()
            if live is not None:   # skip reconciliation on a transient lookup failure
                for name in live:
                    if state._registry.get_backend(name) != backend:
                        state._registry.track(name, backend)
                for kid in state._registry.ids_for_backend(backend):
                    if kid not in live:
                        # A SUCCESSFUL liveness lookup that omits the session is the one signal
                        # allowed to declare it lost (routed through the state machine).
                        colab_cli._mark_lost(kid, "absent from colab-cli live set")
            for kid in state._registry.ids_for_backend(backend):
                first = state._registry.first_seen(kid)
                out.append({
                    "kernel_id": kid, "backend": backend, "name": "colab-session",
                    "state": "running",   # confirmed against colab-cli's live set just above
                    "notebook_path": state._registry.get_notebook(kid) or None,
                    "idle_seconds": None,
                    "age_seconds": round((now - first).total_seconds(), 1) if first else None,
                    "pinned": state._registry.is_pinned(kid),
                })
            continue
        try:
            kernels = await local_jupyter._kernels(backend)
        except Exception as e:  # noqa: BLE001
            out.append({"backend": backend, "error": f"unreachable: {e}"})
            continue
        by_kernel = {}
        if backend == LOCAL:
            for s in await local_jupyter._sessions(LOCAL):
                if s.get("kernel"):
                    by_kernel[s["kernel"]["id"]] = s
        for k in kernels:
            kid = k["id"]
            if state._registry.get_backend(kid) is None:
                state._registry.track(kid, backend)   # adopt a human-started / survivor kernel
            s = by_kernel.get(kid)
            last = _parse_ts(k.get("last_activity"))
            first = state._registry.first_seen(kid)
            out.append({
                "kernel_id": kid,
                "backend": backend,
                "name": k.get("name"),
                "state": k.get("execution_state"),
                "notebook_path": (s.get("path") if s else None) or state._registry.get_notebook(kid) or None,
                "idle_seconds": round((now - last).total_seconds(), 1) if last else None,
                "age_seconds": round((now - first).total_seconds(), 1) if first else None,
                "pinned": state._registry.is_pinned(kid),
            })
    return out


@mcp.tool
async def start_kernel(name: str = "python3", notebook_path: str | None = None,
                       backend: str | None = None, gpu: str | None = None,
                       if_exists: str = "open") -> dict[str, Any]:
    """Start a notebook kernel and return its kernel_id (use it with execute_code /
    add_cell / execute_cell). Work is saved to a notebook ON THE SERVER (viewable/editable
    in JupyterLab).

    `notebook_path` — the notebook to work in. **None → a fresh `untitled-N.ipynb`.**
      `if_exists` controls a name that already exists: **"open"** (default, continue the
      existing notebook), **"new"** (auto-suffix `-1`/`-2`… so you never clobber),
      **"error"** (fail if it exists — use when you mean strictly-new). Tip: to continue
      prior work, pass its exact name with if_exists="open"; for brand-new work pass a
      new name (or None) — check `list_notebooks` first if unsure. NOTE: `if_exists`
      selects only the server-side .ipynb FILE — it does NOT reconnect to a previous
      kernel or (for colab) a previous VM.
    `backend` — **None → the server default**: "local" normally, "colab" when the server
      runs in colab-only mode (then local execution is refused entirely — the local
      Jupyter only stores the notebooks). "local" is fast and free; use **"colab"** when
      you need a GPU/TPU: it provisions a Colab VM (slower, uses compute units).
      **Colab VM lifecycle:** every start_kernel(backend="colab") builds a FRESH, EMPTY VM
      and returns a new kernel_id. To stay on the same VM (keep /content files + pip
      installs), keep reusing that kernel_id — do NOT stop_kernel (that destroys the VM)
      and do NOT call start_kernel again. Use restart_kernel to reset Python state while
      keeping the VM and its files/installed packages.
    `gpu` — colab only: "T4"/"L4"/"A100"/"H100" (None = CPU). Local kernels capped by MAX_KERNELS."""
    reaper._ensure_background()
    if backend is None:
        backend = COLAB if config.COLAB_ONLY else LOCAL
    if backend not in state._backends:
        raise RuntimeError(f"unknown backend '{backend}'. Available: {list(state._backends)}")
    backends._assert_exec_allowed(backend)
    notebook_path = await local_jupyter._resolve_notebook_path(notebook_path, if_exists)
    # Notebook file is always created/kept on LOCAL (single source of truth).
    if notebook_path:
        await local_jupyter._ensure_notebook(notebook_path)

    # colab-cli backend: provision a Colab session (its name is the kernel_id).
    if backends.is_cli(backend):
        session = f"rmcp-{uuid.uuid4().hex[:8]}"
        args = ["new", "-s", session] + (["--gpu", gpu] if gpu else [])
        rc, cout = await colab_cli._colab_cli(*args, timeout=420)
        if rc != 0:
            raise RuntimeError(f"colab new failed: {cout.strip()}")
        state._registry.track(session, backend, notebook=notebook_path or "")
        colab_cli.mark_session_live(session)   # starting -> live (state machine)
        return {"kernel_id": session, "backend": backend, "gpu": gpu or "CPU",
                "notebook_path": notebook_path}

    if backend == LOCAL and notebook_path:
        # Local: use a real Jupyter session so JupyterLab shows the binding.
        session = await local_jupyter._rest_post("/api/sessions", {
            "path": notebook_path,
            "name": notebook_path.rsplit("/", 1)[-1],
            "type": "notebook",
            "kernel": {"name": name},
        }, backend=LOCAL)
        kid = session["kernel"]["id"]
        state._registry.track(kid, LOCAL, notebook=session["path"])
        await local_jupyter._wait_ready(kid, LOCAL)
        return {"kernel_id": kid, "backend": LOCAL, "session_id": session["id"],
                "notebook_path": session["path"], "name": session["kernel"]["name"]}

    # Bare kernel on the chosen backend (Colab, or local scratch). For Colab we
    # track the notebook binding in-process; write-back targets the LOCAL file.
    if backend == LOCAL:
        await reaper._enforce_capacity()
    kernel = await local_jupyter._rest_post("/api/kernels", {"name": name}, backend=backend)
    kid = kernel["id"]
    state._registry.track(kid, backend, notebook=notebook_path or "")
    await local_jupyter._wait_ready(kid, backend)
    return {"kernel_id": kid, "backend": backend, "name": kernel["name"],
            "notebook_path": notebook_path}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def stop_kernel(kernel_id: str) -> dict[str, str]:
    """Terminate a kernel (and its session, if any) and free its resources. For a COLAB
    kernel this DESTROYS the VM — all /content files and pip installs on it are lost, and a
    later start_kernel gives a brand-new empty VM. To merely reset Python state while keeping
    the VM + files, use restart_kernel instead; to keep the VM alive, just stop calling and
    reuse the same kernel_id later (until the idle/max-age reaper reclaims it). NOTE: the
    calling client may require the human to APPROVE this call — a "No approval received"
    failure is that client-side prompt, not this server."""
    await backends._delete_kernel(kernel_id)
    return {"status": "stopped", "kernel_id": kernel_id}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def restart_kernel(kernel_id: str) -> dict[str, str]:
    """Restart a kernel, clearing all in-memory state (variables) but keeping the same id.
    For a COLAB kernel this restarts ONLY the Python process — the VM survives, so /content
    files, the HuggingFace/model cache and pip-installed packages persist; just the interpreter
    (and its module cache) is fresh. Use this to clear variables, to recover a hung colab cell
    (see interrupt_kernel), or to make a `pip install`/`uninstall` take effect when a module was
    already imported (an in-process reinstall alone often won't). Contrast stop_kernel, which
    destroys the VM entirely. NOTE: the calling client (e.g. the Claude app) may require the
    human to APPROVE this call — a "No approval received" failure comes from that client-side
    permission prompt, not from this server; the user must approve/allow it on their device."""
    if backends.is_colab_kernel(kernel_id):
        rc, out = await colab_cli._colab_cli("restart-kernel", "-s", kernel_id, timeout=120)
        outcome = colab_cli.classify_result(rc, out)
        # A non-live restart is ambiguous: colab-cli's restart-kernel raises an OPAQUE
        # traceback (not the "not found" signature) for a session whose local record is gone,
        # so a definitive loss message isn't enough. Consult the authoritative live set —
        # a successful lookup that omits the session confirms it's lost (like exec's probe).
        if not outcome.is_live and (outcome.is_lost or await colab_cli._confirm_lost(kernel_id)):
            colab_cli._mark_lost(kernel_id, outcome.reason or "session absent after restart")
            return {"status": "session_lost", "kernel_id": kernel_id,
                    "note": "colab session is gone (lost/reclaimed) — nothing to restart; start a new one."}
        return {"status": "restarted" if outcome.is_live else "error", "kernel_id": kernel_id,
                **({"detail": _strip_ansi(out.strip())} if not outcome.is_live else {})}
    if (state._backend_of(kernel_id) or LOCAL) == LOCAL and colab_cli._looks_colab(kernel_id):
        raise RuntimeError(f"colab session '{kernel_id}' is no longer alive; start a new one.")
    await local_jupyter._drop_client(kernel_id)
    await local_jupyter._rest_post(f"/api/kernels/{kernel_id}/restart", backend=state._backend_of(kernel_id) or LOCAL)
    return {"status": "restarted", "kernel_id": kernel_id}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def interrupt_kernel(kernel_id: str) -> dict[str, str]:
    """Interrupt a running kernel (raise KeyboardInterrupt) to stop a runaway execution.
    LOCAL kernels only — Colab has no interrupt."""
    if backends.is_colab_kernel(kernel_id):
        return {"status": "unsupported", "kernel_id": kernel_id,
                "detail": ("colab-cli has NO interrupt (no SIGINT-only stop). For a hung colab "
                           "cell the gentlest recovery is restart_kernel: it restarts just the "
                           "Python process while the VM disk SURVIVES — downloaded model "
                           "weights, the HuggingFace cache, /content files and pip installs all "
                           "persist, so re-running (e.g. load_model) hits the cache and won't "
                           "re-download. Use stop_kernel only to give up the VM entirely."),
                "recommended": "restart_kernel"}
    await local_jupyter._rest_post(f"/api/kernels/{kernel_id}/interrupt", backend=state._backend_of(kernel_id) or LOCAL)
    return {"status": "interrupted", "kernel_id": kernel_id}


@mcp.tool(annotations={"readOnlyHint": True})
async def colab_log(kernel_id: str, limit: int = 10) -> dict[str, Any]:
    """Review a COLAB session's recent COMPLETED executions — each one's code AND the output
    it captured — WITHOUT re-running anything. Handy to retrieve a past cell's result or
    confirm what already ran on the VM. `limit` = how many recent executions to return.
    IMPORTANT: an execution is logged only when it COMPLETES, so a cell that timed out or is
    still running does NOT appear here — for a long-running job use execute_code(background=
    True) + get_job, or have the cell persist a result file. Colab kernels only (for local,
    read the notebook's cells/outputs)."""
    reaper._ensure_background()
    if not backends.is_colab_kernel(kernel_id):
        raise RuntimeError("colab_log is for colab kernels; for local, read the notebook cells/outputs")
    # The terminal `colab log` truncates and hides outputs; the JSONL export carries the full
    # structured code + outputs per execution (verified on live colab). colab-cli runs in this
    # same container, so the file it writes is readable here directly.
    tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl", prefix="rmcp_log_")
    tmp.close()
    try:
        rc, out = await colab_cli._colab_cli("log", "-s", kernel_id, "-t", "execution", "-o", tmp.name, timeout=60)
        if rc != 0:
            raise RuntimeError(f"colab log failed: {out.strip()}")
        events = []
        for line in pathlib.Path(tmp.name).read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event_type") == "execution":
                events.append(ev)
    finally:
        os.unlink(tmp.name)
    events = events[-max(1, int(limit)):]
    result = []
    for ev in events:
        _, text = _format_outputs(ev.get("outputs", []) or [])
        result.append({"timestamp": ev.get("timestamp"),
                       "code": (ev.get("code") or "")[:2000],
                       "output": text[:4000]})
    return {"kernel_id": kernel_id, "count": len(result), "executions": result}


@mcp.tool
async def pin_kernel(kernel_id: str, pinned: bool = True) -> dict[str, Any]:
    """Pin/unpin a kernel to exempt it from the IDLE reaper (e.g. a long-lived notebook).
    Note: pinning does NOT exempt it from the absolute max-age hard cap
    (KERNEL_MAX_AGE_SEC) — nothing lives past that. Pin state persists across MCP
    restarts (kept in the kernel registry)."""
    if pinned:
        state._registry.pin(kernel_id)
    else:
        state._registry.unpin(kernel_id)
    return {"kernel_id": kernel_id, "pinned": pinned}


@mcp.tool
async def list_variables(kernel_id: str) -> Any:
    """List the user-defined variables currently held in the kernel (name/type/size)."""
    # Talks to the local kernel directly, so it bypasses backends._resolve_backend —
    # guard it here too (introspection still evaluates code in the kernel).
    backends._assert_exec_allowed(state._backend_of(kernel_id), kernel_id)
    kc = await local_jupyter._get_client(kernel_id)
    return await asyncio.to_thread(kc.list_variables)


@mcp.tool
async def list_backends() -> list[dict[str, Any]]:
    """List available compute backends for start_kernel: `local` (the server's own
    Jupyter) and `colab` (official colab-cli, GPU on demand) if configured. Shows kind,
    reachability, and `exec_enabled` — false for `local` when the server runs colab-only,
    meaning it still stores/serves notebooks but refuses to run code."""
    reaper._ensure_background()
    out = []
    for name, b in state._backends.items():
        kind = b.get("kind", "jupyter")
        entry: dict[str, Any] = {"backend": name, "kind": kind}
        entry["exec_enabled"] = backends.is_cli(name) or not config.COLAB_ONLY
        if backends.is_cli(name):
            entry["reachable"] = state.COLAB_AVAILABLE
            entry["note"] = "GPU on demand via start_kernel(backend='colab', gpu='T4')"
        else:
            entry["url"] = b.get("url")
            if not entry["exec_enabled"]:
                entry["note"] = ("code execution disabled (COLAB_ONLY mode) — notebook "
                                 "storage/editing only")
            try:
                r = await local_jupyter._http(name).get("/api")
                entry["reachable"] = r.status_code == 200
            except Exception as e:  # noqa: BLE001
                entry["reachable"] = False
                entry["error"] = str(e)
        out.append(entry)
    return out
