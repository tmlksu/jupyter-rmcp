"""jupyter-rmcp MCP server — entry point.

Streamable-HTTP MCP server that drives an internal, headless Jupyter Server so
that Claude (incl. mobile) can interactively run notebooks/code on the server.
This is the ONLY internet-facing surface; the Jupyter server itself has no host
port.

This module is deliberately thin: it wires the package together (the FastMCP
singleton lives in `app`; each tool module registers its tools by import side
effect), adds the /health route, and runs uvicorn. The architecture — config,
shared state, the Backend protocol + backend implementations, and the tool
modules — is split across the package (see docs/refactor/PHASE-2-package-split.md
and DESIGN.md).
"""
from __future__ import annotations

import os

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse

# The tool modules (jobs/kernels/notebook/workspace) register their tools via import
# side effects: each does `from app import mcp` and decorates its tools at import
# time. Imported here (by server.py, the entry point) purely so those registrations run.
import config
import jobs  # noqa: F401
import kernels  # noqa: F401
import notebook  # noqa: F401
import state
import workspace  # noqa: F401
from app import mcp
from backends import local_jupyter
from config import JUPYTER_URL, LOCAL, MCP_BEARER, MCP_PORT, log


# ---- health + ASGI app ------------------------------------------------------
@mcp.custom_route("/health", methods=["GET"])
async def _health(_request: Request) -> JSONResponse:
    try:
        await local_jupyter._http(LOCAL).get("/api")
        backend = "ok"
    except Exception as e:  # noqa: BLE001
        backend = f"unreachable: {e}"
    # The execution-policy fields are reported so a checker can assert the DEPLOYED values
    # (the smoke test times a slow execution against the real soft deadline, and a stale
    # EXEC_TIMEOUT_SEC silently reintroduces the interrupt this server no longer wants).
    return JSONResponse({"status": "ok", "jupyter": backend, "colab_only": config.COLAB_ONLY,
                         "soft_reply_deadline_sec": config.SOFT_REPLY_DEADLINE_SEC,
                         "exec_hard_timeout_sec": config.EXEC_TIMEOUT_SEC})


app = mcp.http_app(path="/mcp", stateless_http=True, allowed_hosts=["*"], allowed_origins=["*"])


def _check_colab_only() -> None:
    """Refuse to boot a colab-only server that has no working colab backend — otherwise
    every kernel call fails one at a time instead of the deployment failing once, loudly.
    `state.COLAB_AVAILABLE` only checks that the ADC env var is SET (compose always sets
    it), so verify the credentials file actually exists."""
    if not config.COLAB_ONLY:
        return
    adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not state.COLAB_AVAILABLE:
        raise SystemExit(
            "COLAB_ONLY=1 but colab-cli is unavailable (not on PATH, or "
            "GOOGLE_APPLICATION_CREDENTIALS unset). Nothing could execute code.")
    if not os.path.isfile(adc):
        raise SystemExit(
            f"COLAB_ONLY=1 but the Google credentials file is missing: {adc!r}. Run "
            "`gcloud auth application-default login` and point GCLOUD_ADC at the result.")


if __name__ == "__main__":
    _check_colab_only()
    log.info("jupyter-rmcp MCP on :%s/mcp -> %s (bearer=%s, colab_only=%s)",
             MCP_PORT, JUPYTER_URL, bool(MCP_BEARER), config.COLAB_ONLY)
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="info")
