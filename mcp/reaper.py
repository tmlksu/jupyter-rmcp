"""Idle/max-age reaper, colab keep-alive heartbeat, capacity enforcement, and the
lazy background-task starter (`_ensure_background`).

FastMCP owns the ASGI lifespan, so the reaper is started on the first tool call
rather than at import. Per-backend reap logic lives in `backends`; this module is
the loop + local capacity cap.
"""
from __future__ import annotations

import asyncio

import backends
import config
import state
from backends import colab_cli, local_jupyter
from config import (
    COLAB,
    COLAB_HEARTBEAT_SEC,
    IDLE_TIMEOUT_SEC,
    LOCAL,
    MAX_AGE_SEC,
    MAX_KERNELS,
    REAPER_INTERVAL_SEC,
    log,
)
from util import _now, _parse_ts

_reaper_started = False


def _ensure_background() -> None:
    """Start the idle-reaper task once (lazily), and register the colab-cli backend
    if available. FastMCP owns the ASGI lifespan, so we do this on first tool call.

    In colab-only mode an unavailable colab backend leaves the server with nowhere to
    run code, so raise instead of silently accepting kernel calls that cannot work."""
    global _reaper_started
    if state.COLAB_AVAILABLE and COLAB not in state._backends:
        state._backends[COLAB] = {"kind": "cli"}
    if config.COLAB_ONLY and COLAB not in state._backends:
        raise RuntimeError(
            "COLAB_ONLY mode is on but the colab backend is unavailable: colab-cli is not "
            "on PATH, or GOOGLE_APPLICATION_CREDENTIALS is unset. No backend can execute "
            "code. Fix the credentials (see docs/GUIDE.md) or unset COLAB_ONLY.")
    if not _reaper_started:
        _reaper_started = True
        asyncio.create_task(_reaper_loop())
        log.info("reaper started (idle>%ss, max-age>%ss, every %ss, colab-heartbeat=%ss); colab-cli=%s",
                 IDLE_TIMEOUT_SEC, MAX_AGE_SEC or "off", REAPER_INTERVAL_SEC,
                 COLAB_HEARTBEAT_SEC or "off", state.COLAB_AVAILABLE)


async def _enforce_capacity() -> None:
    """Cap concurrent kernels on the LOCAL backend (protects the VPS). Colab
    kernels run on Google's resources and aren't counted."""
    kernels = await local_jupyter._kernels(LOCAL)
    if len(kernels) < MAX_KERNELS:
        return
    idle = [
        k for k in kernels
        if k.get("execution_state") == "idle" and not state._registry.is_pinned(k["id"])
    ]
    idle.sort(key=lambda k: _parse_ts(k.get("last_activity")) or _now())
    if not idle:
        raise RuntimeError(
            f"local kernel cap reached ({MAX_KERNELS}) and no idle, unpinned kernel to reap. "
            "Stop a kernel first (or run on a Colab backend)."
        )
    victim = idle[0]["id"]
    log.info("capacity: reaping oldest idle local kernel %s", victim)
    await backends._delete_kernel(victim, LOCAL)


async def _reconcile_local() -> None:
    """Adopt any LOCAL Jupyter kernels the registry doesn't know — survivors of a
    kernels.json wipe, or kernels a human started in JupyterLab — so routing and age
    work for them. Runs once at reaper startup; the reaper's per-tick pass keeps it
    fresh. Replaces the old per-call `setdefault` self-heal."""
    try:
        for k in await local_jupyter._kernels(LOCAL):
            if state._registry.get_backend(k["id"]) is None:
                state._registry.track(k["id"], LOCAL)
    except Exception as e:  # noqa: BLE001
        log.warning("startup local reconcile skipped: %s", e)


async def _reconcile_colab() -> None:
    """Adopt any live colab-cli sessions the registry doesn't know (survivors of a
    kernels.json wipe, or sessions started out-of-band), so routing works without the old
    per-call `_ensure_tracked` self-heal that Phase 3 retired. `_live_colab_sessions`
    returns set() when colab is unavailable and None on a transient lookup failure — in
    both cases there is nothing safe to adopt, so skip."""
    try:
        live = await colab_cli._live_colab_sessions()
    except Exception as e:  # noqa: BLE001
        log.warning("startup colab reconcile skipped: %s", e)
        return
    for name in live or ():
        if state._registry.get_backend(name) is None:
            state._registry.track(name, COLAB)
            log.info("colab reconcile: adopted live session %s", name)


async def _reaper_loop() -> None:
    await _reconcile_local()
    await _reconcile_colab()
    while True:
        try:
            await asyncio.sleep(REAPER_INTERVAL_SEC)
            now = _now()
            for backend in list(state._backends):
                try:
                    await backends.reap_backend(backend, now)
                except Exception as e:  # noqa: BLE001
                    log.warning("reaper error on %s: %s", backend, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("reaper error: %s", e)
