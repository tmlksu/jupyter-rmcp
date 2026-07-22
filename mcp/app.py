"""The FastMCP singleton + auth + server instructions (the import root).

Tool modules do `from app import mcp` and register their tools by decoration at
import time; `server.py` imports those modules for side effect. This module
imports NOTHING from the tool modules (or from backends), so there is no cycle.
"""
from __future__ import annotations

from fastmcp import FastMCP

import config
from config import MCP_BEARER, log

# ---- auth -------------------------------------------------------------------
auth = None
if MCP_BEARER:
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    auth = StaticTokenVerifier(tokens={MCP_BEARER: {"client_id": "owner", "scopes": ["use"]}})
    log.info("app-level bearer auth ENABLED")
else:
    log.warning("app-level bearer auth DISABLED (rely on Cloudflare Access)")

# ---- server instructions ----------------------------------------------------
# The backend paragraph depends on the deployment mode; everything else is shared.
_BACKEND_NORMAL = (
    "- Backend: DEFAULT 'local' (this server, fast, free). Use backend='colab' (gpu='T4' "
    "etc.) ONLY when a GPU/TPU/heavy compute is needed — it spins up a Colab VM "
    "(slower, uses compute units). Notebooks always live on this server.\n"
)
_BACKEND_COLAB_ONLY = (
    "- Backend: this server runs in COLAB-ONLY mode. ALL code executes on Google Colab "
    "VMs — start_kernel defaults to backend='colab' (pass gpu='T4' etc. when you need a "
    "GPU), and the 'local' backend REFUSES to run code. The local Jupyter still stores "
    "and serves every notebook, so editing, listing and workspace files work normally.\n"
)

mcp = FastMCP(
    name="jupyter-rmcp",
    auth=auth,
    instructions=(
        "Interactive Jupyter notebooks on a remote server, co-editable with a human in "
        "JupyterLab. Flow: start_kernel(...) -> work in its bound notebook -> stop_kernel.\n"
        + (_BACKEND_COLAB_ONLY if config.COLAB_ONLY else _BACKEND_NORMAL) +
        "- COLAB VM LIFECYCLE (important): one colab kernel_id == one VM. The VM lives as "
        "long as that kernel, so to STAY ON THE SAME VM keep reusing the SAME kernel_id "
        "(then /content files + pip installs persist). stop_kernel DESTROYS the VM (its "
        "files and installs are gone — a later start_kernel gives a brand-new, empty VM). "
        "restart_kernel KEEPS the VM and its files/installed packages but resets Python "
        "state — use it to clear variables, or to fix a stale import after a pip "
        "install/uninstall (reinstalling in-process often won't take without it). "
        "`if_exists` only picks the server-side .ipynb FILE; it does NOT reconnect to any "
        "previous VM.\n"
        "- Notebooks are the durable record and a HUMAN may view/edit them in JupyterLab. "
        "CONTINUE prior work: start_kernel(notebook_path='<exact name>') (if_exists='open'). "
        "NEW work: omit the name (fresh untitled-N.ipynb) or pass a new one; if_exists='new' "
        "auto-renames to avoid clobbering, 'error' fails if it exists.\n"
        "- EDITING is separate from EXECUTION and is by cell. First list_cells(path) to see "
        "each cell's stable `id` + summary; read_cells(path, ids=[...]) for full source. "
        "Prefer **patch_cell(path, old, new, cell_id=...)** for small edits (unique substring "
        "replace — no full resend); edit_cell for a full rewrite; insert_cells/insert_cell, "
        "delete_cell, move_cell. Address cells by `id` (stable), not index. For safe "
        "co-editing with the human, pass expected_rev (from notebook_rev / a prior write); "
        "the write is refused if the file changed — then re-read.\n"
        "- EXECUTION: execute_code(kernel_id, code) runs ad-hoc code and appends a cell; "
        "execute_cell(kernel_id, cell_id=...) re-runs an existing cell in place (updates its "
        "outputs, no duplicate).\n"
        "- Files/folders: notebooks & data live under one work root on the server (persistent, "
        "visible to kernels and JupyterLab). list_files to browse; create_folder or just "
        "write to a sub-path (parents auto-create); upload_file(path, base64) or "
        "fetch_to_workspace(url, path) to bring data in. A human can also drag files in via "
        "JupyterLab's Upload button. To use a workspace file on a COLAB kernel, first "
        "upload_to_colab(kernel_id, workspace_path) (copies it to /content on the VM); "
        "download_from_colab brings a result back to the workspace.\n"
        "- LONG/HEAVY jobs (model download, training): execute_code(..., background=True) "
        "returns a job_id IMMEDIATELY (no timeout on the work); poll get_job(kernel_id, "
        "job_id) for live stdout/stderr + status (running/done/error) — prefer this over "
        "growing `timeout`. A plain execute_code status='timeout' means the reply lagged, "
        "NOT that it failed (the cell may still be running); captured output + a note are "
        "returned. colab_log(kernel_id) shows a colab session's past COMPLETED executions + "
        "their outputs without re-running.\n"
        "- Secrets on a colab kernel (once each, masked — never in exec history): "
        "setup_kaggle(kernel_id) for Kaggle; setup_hf(kernel_id) for a HuggingFace token "
        "(gated/private repos)."
    ),
)
