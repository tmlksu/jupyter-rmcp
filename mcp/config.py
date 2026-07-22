"""Environment parsing + tunable constants for jupyter-rmcp.

The single home for all `os.environ` reads and the module-level tunables the rest
of the package imports. Importing this module has no side effects beyond reading
env and configuring the root logger; it depends on NOTHING first-party (so every
other module may import it without risking a cycle).
"""
from __future__ import annotations

import logging
import os
import pathlib

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("jupyter-rmcp")

# ---- backend identity -------------------------------------------------------
# `local` is the internal Jupyter and OWNS all notebooks. The colab-cli backend
# is compute-only: kernels run there, but notebooks + write-back stay on `local`.
LOCAL = "local"
COLAB = "colab"   # official colab-cli backend (kind="cli"); registered if available

# ---- deployment mode --------------------------------------------------------
# Colab-only deployments refuse to EXECUTE code on `local`: every kernel runs on a
# Colab VM under the operator's own Google account. The local Jupyter still stores
# and serves the notebooks (all notebook I/O goes through its Contents API) — it
# just never runs user code. Read it as `config.COLAB_ONLY`, never
# `from config import COLAB_ONLY`: the tests monkeypatch the module attribute.
COLAB_ONLY = os.environ.get("COLAB_ONLY", "").strip().lower() in ("1", "true", "yes", "on")

# ---- env config -------------------------------------------------------------
def _env(name: str, default: str) -> str:
    """Env value, treating empty or whitespace-only as unset.

    Compose interpolates a key that is absent from `.env` into an EMPTY string,
    so `os.environ.get(name, default)` returns "" rather than the default and
    every numeric conversion below would raise. A minimal `.env` is the normal
    case for a new deployment, so empty must mean "use the default"."""
    return os.environ.get(name, "").strip() or default


JUPYTER_URL = _env("JUPYTER_URL", "http://jupyter:8888").rstrip("/")
JUPYTER_TOKEN = _env("JUPYTER_TOKEN", "")
if not JUPYTER_TOKEN:
    raise SystemExit(
        "JUPYTER_TOKEN is required and must not be empty — the MCP server and the "
        "Jupyter server share it. Generate one with "
        "`python3 -c 'import secrets; print(secrets.token_hex(32))'` and set it in .env.")
MCP_BEARER = _env("MCP_BEARER", "")
MCP_PORT = int(_env("MCP_PORT", "8000"))
EXEC_TIMEOUT_SEC = float(_env("EXEC_TIMEOUT_SEC", "120"))
IDLE_TIMEOUT_SEC = float(_env("KERNEL_IDLE_TIMEOUT_SEC", "3600"))
# Absolute max kernel lifetime (Colab-style hard cap): reap regardless of
# activity once older than this. 0 disables. Applies even to pinned kernels.
MAX_AGE_SEC = float(_env("KERNEL_MAX_AGE_SEC", "28800"))  # 8h
MAX_KERNELS = int(_env("MAX_KERNELS", "8"))
REAPER_INTERVAL_SEC = float(_env("REAPER_INTERVAL_SEC", "60"))
# Colab keep-alive: colab-cli's own keep-alive daemon lives inside this container and
# dies if it restarts, after which Colab idle-reclaims the VM in ~10-15 min. To hold a
# session for the life of real work, the reaper pings each tracked colab session with a
# no-op exec every this-many seconds (resets Colab's idle timer). 0 disables. Bounded by
# KERNEL_MAX_AGE_SEC so nothing is kept alive forever.
COLAB_HEARTBEAT_SEC = float(_env("COLAB_HEARTBEAT_SEC", "300"))
# Persistent kernel-registry location (bind-mounted in compose). Defaults to a
# repo-local path so tests/dev work without the mount.
MCP_STATE_DIR = _env(
    "MCP_STATE_DIR", str(pathlib.Path(__file__).resolve().parent.parent / "data" / "mcp-state"))

# ---- output / payload tunables ----------------------------------------------
MAX_OUTPUT_CHARS = 20000          # cap text returned per execution
INCLUDE_IMAGE_BYTES = False       # keep mobile payloads small; images -> markers
MAX_FETCH_BYTES = 200 * 1024 * 1024   # cap fetch_to_workspace at 200 MB
