"""Workspace + colab file-transfer + secret-injection tools.

Secrets (setup_kaggle/setup_hf) and colab file transfer (upload_to_colab/
download_from_colab) cross the local↔colab seam; the contents tools (list_notebooks/
list_files/create_folder/upload_file/fetch_to_workspace) are LOCAL-only.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import tempfile
from typing import Any

import httpx

import backends
from app import mcp
from backends import colab_cli, local_jupyter
from config import LOCAL, MAX_FETCH_BYTES
from outputs import _strip_ansi
from util import _parent_dir


@mcp.tool
async def setup_kaggle(kernel_id: str) -> dict[str, Any]:
    """Make Kaggle usable in this kernel (`!kaggle datasets download …`). For LOCAL
    kernels this is already configured (no-op). For a COLAB kernel it injects the
    server-side token into that Colab VM: the token is uploaded as a file and loaded
    into the VM env (`KAGGLE_API_TOKEN`) + `~/.kaggle/`, so it never appears in
    exec/session history. Run ONCE per colab kernel, and only on kernels you trust —
    the token is full-access and then lives on the Google VM for that session's life.
    Requires KAGGLE_API_TOKEN (or legacy KAGGLE_USERNAME+KAGGLE_KEY) in the MCP env."""
    if not backends.is_colab_kernel(kernel_id):
        return {"kernel_id": kernel_id, "ok": True,
                "detail": "local backend already has the Kaggle env; no setup needed"}
    session = kernel_id
    token = os.environ.get("KAGGLE_API_TOKEN", "").strip()
    user = os.environ.get("KAGGLE_USERNAME", "").strip()
    key = os.environ.get("KAGGLE_KEY", "").strip()
    if token:
        payload, remote, mode = token, "/content/.rmcp_kt", "token"
        setup = (
            "import os,pathlib\n"
            "p=pathlib.Path('/content/.rmcp_kt'); tok=p.read_text().strip()\n"
            "os.environ['KAGGLE_API_TOKEN']=tok\n"
            "d=pathlib.Path.home()/'.kaggle'; d.mkdir(exist_ok=True,parents=True)\n"
            "f=d/'access_token'; f.write_text(tok); f.chmod(0o600)\n"
            "p.unlink(missing_ok=True); print('kaggle configured (token)')\n"
        )
    elif user and key:
        payload = json.dumps({"username": user, "key": key})
        remote, mode = "/content/.rmcp_kj", "legacy"
        setup = (
            "import os,pathlib,shutil\n"
            "d=pathlib.Path.home()/'.kaggle'; d.mkdir(exist_ok=True,parents=True)\n"
            "shutil.move('/content/.rmcp_kj', str(d/'kaggle.json')); (d/'kaggle.json').chmod(0o600)\n"
            "print('kaggle configured (kaggle.json)')\n"
        )
    else:
        raise RuntimeError("no Kaggle creds in MCP env (set KAGGLE_API_TOKEN in .env)")

    tmp = tempfile.NamedTemporaryFile("w", delete=False, prefix="rmcp_kaggle_")
    try:
        tmp.write(payload)
        tmp.close()
        rc, out = await colab_cli._colab_cli("upload", "-s", session, tmp.name, remote, timeout=120)
        if rc != 0:
            raise RuntimeError(f"colab upload failed: {out.strip()}")
        rc2, out2 = await colab_cli._colab_cli("exec", "-s", session, input_text=setup, timeout=120)
    finally:
        os.unlink(tmp.name)
    return {"session": session, "mode": mode, "ok": rc2 == 0, "output": _strip_ansi(out2.strip())}


@mcp.tool
async def setup_hf(kernel_id: str, token: str | None = None) -> dict[str, Any]:
    """Make a HuggingFace token available in this kernel WITHOUT it appearing in exec/session
    history — needed for gated/private repos (`hf_hub_download`, `from_pretrained`). Same
    masked-injection design as setup_kaggle: for a COLAB kernel the token is uploaded as a file
    and loaded into the VM env (`HF_TOKEN` + `HUGGING_FACE_HUB_TOKEN`) and the huggingface_hub
    token cache (`~/.cache/huggingface/token`), then the file is deleted — so it never shows up
    in the kernel's exec history (unlike a plain `os.environ[...] = "hf_..."` cell).

    Token source: the server-side `HF_TOKEN` (or `HUGGING_FACE_HUB_TOKEN`) env by default. Pass
    `token=` ONLY if the server env isn't set — it's still injected via the masked file path (so
    it stays out of the kernel exec history), but the value then travels in THIS tool call, so it
    may appear in the MCP client's tool-call log; prefer setting `HF_TOKEN` in the MCP `.env`.
    Run ONCE per colab kernel, only on a kernel you trust — the token then lives on that Google
    VM for the session's life. LOCAL kernels: set `HF_TOKEN` in the jupyter container env (no-op
    here)."""
    if not backends.is_colab_kernel(kernel_id):
        return {"kernel_id": kernel_id, "ok": True,
                "detail": "local backend: set HF_TOKEN in the jupyter container env; no per-kernel injection"}
    token = (token or "").strip() or (os.environ.get("HF_TOKEN", "").strip()
                                      or os.environ.get("HUGGING_FACE_HUB_TOKEN", "").strip())
    if not token:
        raise RuntimeError("no HF token: set HF_TOKEN in the MCP .env, or pass token=... to this call")
    session, remote = kernel_id, "/content/.rmcp_hf"
    setup = (
        "import os,pathlib\n"
        "p=pathlib.Path('/content/.rmcp_hf'); tok=p.read_text().strip()\n"
        "os.environ['HF_TOKEN']=tok; os.environ['HUGGING_FACE_HUB_TOKEN']=tok\n"
        "d=pathlib.Path.home()/'.cache'/'huggingface'; d.mkdir(parents=True,exist_ok=True)\n"
        "f=d/'token'; f.write_text(tok); f.chmod(0o600)\n"
        "p.unlink(missing_ok=True); print('hf token configured')\n"
    )
    tmp = tempfile.NamedTemporaryFile("w", delete=False, prefix="rmcp_hf_")
    try:
        tmp.write(token)
        tmp.close()
        rc, out = await colab_cli._colab_cli("upload", "-s", session, tmp.name, remote, timeout=120)
        if rc != 0:
            raise RuntimeError(f"colab upload failed: {out.strip()}")
        rc2, out2 = await colab_cli._colab_cli("exec", "-s", session, "--timeout", "60", input_text=setup, timeout=120)
    finally:
        os.unlink(tmp.name)
    return {"session": session, "ok": rc2 == 0, "output": _strip_ansi(out2.strip())}


@mcp.tool
async def upload_to_colab(kernel_id: str, workspace_path: str,
                          remote_path: str | None = None) -> dict[str, Any]:
    """Copy a file from the server workspace (uploaded via JupyterLab / upload_file /
    fetch_to_workspace) ONTO the Colab VM for this kernel, so the notebook can open it
    there. `remote_path` defaults to /content/<basename>. Local kernels already see
    workspace files directly (no copy needed)."""
    if not backends.is_colab_kernel(kernel_id):
        return {"kernel_id": kernel_id, "note": f"local kernel already sees the workspace; "
                f"open '{workspace_path}' directly (relative to the work root)"}
    data = await local_jupyter._read_workspace_bytes(workspace_path)
    remote = remote_path or f"/content/{workspace_path.rstrip('/').rsplit('/', 1)[-1]}"
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix="rmcp_up_")
    try:
        tmp.write(data)
        tmp.close()
        rc, out = await colab_cli._colab_cli("upload", "-s", kernel_id, tmp.name, remote, timeout=300)
        if rc != 0:
            raise RuntimeError(f"colab upload failed: {out.strip()}")
    finally:
        os.unlink(tmp.name)
    return {"kernel_id": kernel_id, "workspace_path": workspace_path,
            "remote_path": remote, "bytes": len(data)}


@mcp.tool
async def download_from_colab(kernel_id: str, remote_path: str,
                              workspace_path: str | None = None) -> dict[str, Any]:
    """Copy a file FROM the Colab VM (e.g. a result/checkpoint the notebook produced) back
    into the server workspace so it persists and is visible in JupyterLab. `workspace_path`
    defaults to the remote basename at the work root. Colab kernels only."""
    if not backends.is_colab_kernel(kernel_id):
        raise RuntimeError("download_from_colab is for colab kernels; local files are already in the workspace")
    dest = (workspace_path or remote_path.rstrip("/").rsplit("/", 1)[-1]).lstrip("/")
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix="rmcp_dl_")
    tmp.close()
    try:
        rc, out = await colab_cli._colab_cli("download", "-s", kernel_id, remote_path, tmp.name, timeout=300)
        if rc != 0:
            raise RuntimeError(f"colab download failed: {out.strip()}")
        data = pathlib.Path(tmp.name).read_bytes()
    finally:
        os.unlink(tmp.name)
    if _parent_dir(dest):
        await local_jupyter._ensure_dir(_parent_dir(dest))
    put = await local_jupyter._http(LOCAL).put(f"/api/contents/{dest}",
                                               json={"type": "file", "format": "base64",
                                                     "content": base64.b64encode(data).decode()})
    put.raise_for_status()
    return {"kernel_id": kernel_id, "remote_path": remote_path, "workspace_path": dest, "bytes": len(data)}


@mcp.tool
async def list_notebooks(path: str = "") -> list[dict[str, Any]]:
    """List notebooks and subdirectories under `path` (relative to the work root).
    For ALL files (data, images, etc.) use list_files."""
    r = await local_jupyter._http(LOCAL).get(f"/api/contents/{path}".rstrip("/"))
    r.raise_for_status()
    content = r.json().get("content", [])
    return [
        {"name": c["name"], "path": c["path"], "type": c["type"], "last_modified": c.get("last_modified")}
        for c in content
        if c["type"] in ("notebook", "directory")
    ]


@mcp.tool
async def list_files(path: str = "") -> list[dict[str, Any]]:
    """List EVERYTHING (files + folders) under `path` in the workspace — notebooks,
    uploaded data, images, etc. Files live on the server (persistent) and are visible to
    kernels (cwd is the work root) and in JupyterLab."""
    r = await local_jupyter._http(LOCAL).get(f"/api/contents/{path}".rstrip("/"))
    r.raise_for_status()
    content = r.json().get("content", [])
    return [{"name": c["name"], "path": c["path"], "type": c["type"],
             "size": c.get("size"), "last_modified": c.get("last_modified")} for c in content]


@mcp.tool
async def create_folder(path: str) -> dict[str, Any]:
    """Create a folder (and any missing parent folders) under the work root. Notebook/
    file writes also auto-create parents, so you rarely need this explicitly."""
    await local_jupyter._ensure_dir(path)
    return {"path": path.strip("/"), "created": True}


@mcp.tool
async def upload_file(path: str, content_base64: str, overwrite: bool = False) -> dict[str, Any]:
    """Write a file into the workspace from base64 bytes (binary-safe; persists on the server,
    visible to kernels and JupyterLab). Auto-creates parent folders. For a human uploading
    from a phone/laptop, JupyterLab's Upload button is usually easier; use this when a
    client can provide the bytes (e.g. Claude Code) — set overwrite=True to replace."""
    if not overwrite:
        chk = await local_jupyter._http(LOCAL).get(f"/api/contents/{path}", params={"content": "0"})
        if chk.status_code == 200:
            raise RuntimeError(f"'{path}' already exists (pass overwrite=True to replace)")
    if _parent_dir(path):
        await local_jupyter._ensure_dir(_parent_dir(path))
    try:
        nbytes = len(base64.b64decode(content_base64))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"content_base64 is not valid base64: {e}") from e
    r = await local_jupyter._http(LOCAL).put(f"/api/contents/{path}",
                                             json={"type": "file", "format": "base64", "content": content_base64})
    r.raise_for_status()
    return {"path": path, "bytes": nbytes}


@mcp.tool
async def fetch_to_workspace(url: str, path: str, overwrite: bool = False) -> dict[str, Any]:
    """Download a URL to a file in the workspace, server-side (the server fetches
    it). Handy to bring in a test image/dataset by link when you can't upload from the
    device. Auto-creates parent folders; capped at 200 MB."""
    if not overwrite:
        chk = await local_jupyter._http(LOCAL).get(f"/api/contents/{path}", params={"content": "0"})
        if chk.status_code == 200:
            raise RuntimeError(f"'{path}' already exists (pass overwrite=True to replace)")
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.content
    if len(data) > MAX_FETCH_BYTES:
        raise RuntimeError(f"file is {len(data)} bytes, over the {MAX_FETCH_BYTES} cap")
    if _parent_dir(path):
        await local_jupyter._ensure_dir(_parent_dir(path))
    b64 = base64.b64encode(data).decode()
    put = await local_jupyter._http(LOCAL).put(f"/api/contents/{path}",
                                               json={"type": "file", "format": "base64", "content": b64})
    put.raise_for_status()
    return {"path": path, "bytes": len(data), "source_url": url}
