"""Backend abstraction + cross-backend orchestration.

Defines the `Backend` protocol (the operations every backend supports) and hosts
the code that has to KNOW about more than one backend: execution routing
(`_resolve_backend`, `_run_on_kernel`), lifecycle dispatch (`_delete_kernel`),
and reaping (`reap_backend`). The `kind == "cli"` decisions all live here (behind
`is_cli`/`is_colab_kernel`) so the tool modules never branch on backend kind
themselves — that was one of the four root problems the refactor targets.

The concrete implementations are `backends.local_jupyter` and `backends.colab_cli`;
they hold only single-backend primitives and never import this package (so there
is no submodule↔__init__ cycle).
"""
from __future__ import annotations

from typing import Any, Protocol

import config
import state
from backends import colab_cli, local_jupyter
from backends.colab_cli import _looks_colab
from config import COLAB_HEARTBEAT_SEC, IDLE_TIMEOUT_SEC, LOCAL, MAX_AGE_SEC, log
from outputs import _format_outputs
from state import _backend_of, backend_kind
from util import _parse_ts


class Backend(Protocol):
    """The operations a compute backend supports. Realized functionally by the
    `local_jupyter` and `colab_cli` modules and dispatched here by backend kind."""

    async def execute(self, kernel_id: str, code: str, timeout: float | None) -> dict[str, Any]: ...
    async def reap(self, backend: str, now: Any) -> None: ...
    async def delete(self, kernel_id: str, backend: str) -> None: ...


# ---- backend-kind predicates (the ONLY place that compares against "cli") ----
def is_cli(name: str | None) -> bool:
    return backend_kind(name) == "cli"


def is_colab_kernel(kernel_id: str) -> bool:
    """True iff this kernel_id routes to the colab-cli backend (unknown ids -> False)."""
    return is_cli(_backend_of(kernel_id))


# ---- colab-only mode --------------------------------------------------------
LOCAL_EXEC_DISABLED = (
    "local code execution is disabled on this server (COLAB_ONLY mode). Use "
    "backend='colab' (or omit backend); notebooks are still stored and editable locally."
)


def _assert_exec_allowed(backend: str | None, kernel_id: str | None = None) -> None:
    """Refuse to run user code on a non-cli backend when the server is colab-only.

    Enforced here rather than only at start_kernel because `_resolve_backend` also
    ADOPTS local kernels nobody registered (a human's JupyterLab kernel, a survivor
    of a mode switch) — those must not become an execution path either."""
    if config.COLAB_ONLY and not is_cli(backend):
        subject = f"kernel '{kernel_id}' runs on the local backend: " if kernel_id else ""
        raise RuntimeError(subject + LOCAL_EXEC_DISABLED)


# ---- execution routing ------------------------------------------------------
async def _resolve_backend(kernel_id: str) -> str:
    """Explicit execution routing: return the kernel's backend or RAISE — never a
    silent LOCAL fallback. A registered id returns its backend. An unknown
    colab-style id is a lost/expired session (nothing runs). An unknown UUID that
    the LOCAL Jupyter actually has is adopted (covers human-started JupyterLab
    kernels and pre-registry survivors); otherwise it's a clean 'not found'. In
    COLAB_ONLY mode a resolved local kernel raises instead of returning — routing
    still works for lifecycle tools, but nothing may execute on it."""
    backend = state._registry.get_backend(kernel_id)
    if backend is None:
        if _looks_colab(kernel_id):
            raise RuntimeError(
                f"colab session '{kernel_id}' is no longer alive (lost/expired, or the VM was "
                "reclaimed). Nothing was run. Start a new one with start_kernel(backend='colab') "
                "and re-setup (setup_kaggle/setup_hf, re-download data).")
        if not any(k.get("id") == kernel_id for k in await local_jupyter._kernels(LOCAL)):
            raise RuntimeError(
                f"kernel '{kernel_id}' not found on any backend — it was stopped, reaped, or "
                "never existed. Start one with start_kernel().")
        state._registry.track(kernel_id, LOCAL)   # adopt a kernel a human started in JupyterLab
        backend = LOCAL
    _assert_exec_allowed(backend, kernel_id)
    return backend


async def _run_on_kernel(kernel_id: str, code: str, timeout: float | None = None) -> dict[str, Any]:
    """Execute code on a kernel (any backend); return a normalized reply
    {status, execution_count, outputs(nbformat), timed_out}. No notebook write-back.
    Routing is explicit (unknown id raises — never a silent LOCAL fallback)."""
    backend = await _resolve_backend(kernel_id)
    if is_cli(backend):
        return await colab_cli.exec_colab(kernel_id, code, timeout)
    return await local_jupyter.exec_local(kernel_id, backend, code, timeout)


def _reply_result(reply: dict[str, Any]) -> dict[str, Any]:
    structured, text = _format_outputs(reply.get("outputs", []))
    out = {"status": "timeout" if reply.get("timed_out") else reply.get("status"),
           "execution_count": reply.get("execution_count"), "outputs": structured, "text": text}
    if reply.get("timed_out"):
        out["timed_out"] = True
    if reply.get("note"):
        out["note"] = reply["note"]
    return out


# ---- lifecycle dispatch -----------------------------------------------------
async def _delete_kernel(kernel_id: str, backend: str | None = None) -> None:
    if backend is None:
        # Deletion is idempotent (a missing local kernel just 404s), so an unknown id
        # may safely fall back to LOCAL here — unlike execution routing.
        backend = state._backend_of(kernel_id) or LOCAL
    if is_cli(backend):
        await colab_cli._colab_cli("stop", "-s", kernel_id, timeout=90)
        # Record the terminal `stopped` state (validated) before forgetting the session.
        colab_cli.set_session_state(kernel_id, colab_cli.ColabSessionState.STOPPED, reason="stop_kernel")
    else:
        await local_jupyter._drop_client(kernel_id)
        r = await local_jupyter._http(backend).delete(f"/api/kernels/{kernel_id}")
        if r.status_code not in (204, 404):
            r.raise_for_status()
    state._registry.forget(kernel_id)


# ---- reaping dispatch -------------------------------------------------------
async def _reap_cli_backend(backend: str, now: Any) -> None:
    """colab-cli sessions: enforce the absolute max-age cap, and (COLAB_HEARTBEAT_SEC)
    keep live sessions from idle-reclaiming by periodically pinging them — colab-cli's own
    keep-alive daemon dies if this container restarts, so we back it up here."""
    for kid in state._registry.ids_for_backend(backend):
        first = state._registry.first_seen(kid)
        # Absolute max-age cap (cost safety net against a forgotten GPU session).
        if MAX_AGE_SEC and first and not state._registry.is_pinned(kid) and (now - first).total_seconds() > MAX_AGE_SEC:
            log.info("reaper: stopping colab session %s@%s (max-age)", kid, backend)
            await _delete_kernel(kid, backend)
            continue
        # Keep-alive heartbeat.
        if COLAB_HEARTBEAT_SEC:
            last = state._registry.last_heartbeat(kid)
            if last is None or (now - last).total_seconds() >= COLAB_HEARTBEAT_SEC:
                try:
                    await colab_cli._heartbeat_colab(kid, now)
                except Exception as e:  # noqa: BLE001
                    log.warning("heartbeat error for %s: %s", kid, e)


async def _reap_jupyter_backend(backend: str, now: Any) -> None:
    for k in await local_jupyter._kernels(backend):
        kid = k["id"]
        # Periodic reconcile: adopt kernels the registry doesn't know (survivors of a
        # kernels.json wipe, or kernels a human started in JupyterLab).
        if state._registry.get_backend(kid) is None:
            state._registry.track(kid, backend)
        first = state._registry.first_seen(kid) or now
        age = (now - first).total_seconds()
        # Absolute hard cap (Colab-style): reap regardless of state or pin.
        if MAX_AGE_SEC and age > MAX_AGE_SEC:
            log.info("reaper: deleting kernel %s@%s (max-age %.0fs > %.0fs)", kid, backend, age, MAX_AGE_SEC)
            await _delete_kernel(kid, backend)
            continue
        # Idle timeout: only idle, unpinned kernels.
        if state._registry.is_pinned(kid) or k.get("execution_state") != "idle":
            continue
        last = _parse_ts(k.get("last_activity"))
        if last and (now - last).total_seconds() > IDLE_TIMEOUT_SEC:
            log.info("reaper: deleting idle kernel %s@%s (idle %.0fs)", kid, backend, (now - last).total_seconds())
            await _delete_kernel(kid, backend)


async def reap_backend(backend: str, now: Any) -> None:
    if is_cli(backend):
        await _reap_cli_backend(backend, now)
    else:
        await _reap_jupyter_backend(backend, now)
