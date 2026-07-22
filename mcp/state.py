"""Process-wide mutable singletons + trivial registry readers.

Every module that touches shared state imports THIS module and mutates the SAME
objects (never re-instantiates them) — that is the whole point of a single state
home. The heavier routing/dispatch logic lives in `backends/`; only the two
zero-dependency readers (`_backend_of`, `backend_kind`) live here so the backend
implementations can use them without importing the `backends` package (which
would be a cycle).
"""
from __future__ import annotations

import asyncio
import os
import shutil
from typing import TYPE_CHECKING

import httpx

from config import JUPYTER_TOKEN, JUPYTER_URL, LOCAL, MCP_STATE_DIR
from registry import KernelRegistry

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
