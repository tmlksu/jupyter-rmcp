"""Workspace + colab file-transfer + secret-injection tools.

Secrets (setup_kaggle/setup_hf) and colab file transfer (upload_to_colab/
download_from_colab) cross the local↔colab seam; the contents tools (list_notebooks/
list_files/create_folder/upload_file/fetch_to_workspace) are LOCAL-only.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import socket
import tempfile
from typing import Any

import httpx

import backends
import config
from app import mcp
from backends import colab_cli, local_jupyter
from config import LOCAL
from outputs import _strip_ansi
from util import _check_workspace_path, _contents_url, _parent_dir


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
    workspace_path = _check_workspace_path(workspace_path, "workspace_path")
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
    dest = _check_workspace_path(
        (workspace_path or remote_path.rstrip("/").rsplit("/", 1)[-1]).lstrip("/"),
        "workspace_path")
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix="rmcp_dl_")
    tmp.close()
    try:
        def _over_cap() -> str | None:
            """Polled by _colab_cli while the download subprocess runs — the size
            check AFTER landing can't help once a too-big file has filled the disk."""
            try:
                size = os.path.getsize(tmp.name)
            except OSError:
                return None
            if size > config.MAX_FETCH_BYTES:
                return (f"'{remote_path}' exceeded the {config.MAX_FETCH_BYTES}-byte "
                        "transfer cap mid-download — killed and removed")
            return None
        rc, out = await colab_cli._colab_cli("download", "-s", kernel_id, remote_path,
                                             tmp.name, timeout=300, abort_check=_over_cap)
        if rc != 0:
            raise RuntimeError(f"colab download failed: {out.strip()}")
        # Belt & braces: a file that lands entirely inside one poll interval was
        # never seen by abort_check — the stat still has to stand guard.
        size = os.path.getsize(tmp.name)
        if size > config.MAX_FETCH_BYTES:
            raise RuntimeError(
                f"'{remote_path}' is {size} bytes, over the {config.MAX_FETCH_BYTES}-byte "
                "transfer cap — split it on the colab side and download the pieces")
        if _parent_dir(dest):
            await local_jupyter._ensure_dir(_parent_dir(dest))
        nbytes = await local_jupyter._write_file_chunked(dest, _file_chunks(tmp.name),
                                                         overwrite=True)
    finally:
        os.unlink(tmp.name)
    return {"kernel_id": kernel_id, "remote_path": remote_path, "workspace_path": dest, "bytes": nbytes}


async def _file_chunks(path: str):
    """Read a local file in ~_UPLOAD_CHUNK_BYTES pieces, so a large download never
    sits whole in this process's memory."""
    with open(path, "rb") as f:  # noqa: PTH123
        while piece := await asyncio.to_thread(f.read, local_jupyter._UPLOAD_CHUNK_BYTES):
            yield piece


@mcp.tool
async def list_notebooks(path: str = "") -> list[dict[str, Any]]:
    """List notebooks and subdirectories under `path` (relative to the work root).
    For ALL files (data, images, etc.) use list_files."""
    path = _check_workspace_path(path)
    r = await local_jupyter._http(LOCAL).get(_contents_url(path).rstrip("/"))
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
    path = _check_workspace_path(path)
    r = await local_jupyter._http(LOCAL).get(_contents_url(path).rstrip("/"))
    r.raise_for_status()
    content = r.json().get("content", [])
    return [{"name": c["name"], "path": c["path"], "type": c["type"],
             "size": c.get("size"), "last_modified": c.get("last_modified")} for c in content]


@mcp.tool
async def create_folder(path: str) -> dict[str, Any]:
    """Create a folder (and any missing parent folders) under the work root. Notebook/
    file writes also auto-create parents, so you rarely need this explicitly."""
    path = _check_workspace_path(path)
    await local_jupyter._ensure_dir(path)
    return {"path": path.strip("/"), "created": True}


@mcp.tool
async def upload_file(path: str, content_base64: str, overwrite: bool = False) -> dict[str, Any]:
    """Write a file into the workspace from base64 bytes (binary-safe; persists on the server,
    visible to kernels and JupyterLab). Auto-creates parent folders. For a human uploading
    from a phone/laptop, JupyterLab's Upload button is usually easier; use this when a
    client can provide the bytes (e.g. Claude Code) — set overwrite=True to replace."""
    path = _check_workspace_path(path)
    if not overwrite:
        chk = await local_jupyter._http(LOCAL).get(_contents_url(path), params={"content": "0"})
        if chk.status_code == 200:
            raise RuntimeError(f"'{path}' already exists (pass overwrite=True to replace)")
    if _parent_dir(path):
        await local_jupyter._ensure_dir(_parent_dir(path))
    try:
        nbytes = len(base64.b64decode(content_base64))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"content_base64 is not valid base64: {e}") from e
    r = await local_jupyter._http(LOCAL).put(_contents_url(path),
                                             json={"type": "file", "format": "base64", "content": content_base64})
    r.raise_for_status()
    return {"path": path, "bytes": nbytes}


# ---- fetch_to_workspace: bounded download over a validated, pinned route ------
# A server-side fetch of an arbitrary URL is an SSRF primitive, so the target is
# constrained three ways (ADR 0021): http(s) only; the host's resolved addresses
# must be public — private/loopback/link-local/reserved/multicast are refused
# (FETCH_ALLOW_PRIVATE=1 opts out); and redirects are followed MANUALLY with every
# hop re-validated, while the connection itself is PINNED to the validated IP
# (original Host header + TLS SNI preserved) so a DNS rebind between check and
# connect can't smuggle a second answer through.
_MAX_FETCH_REDIRECTS = 8
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _fetch_client() -> httpx.AsyncClient:
    """The client fetch_to_workspace uses — a seam so tests inject a MockTransport.
    Redirects stay OFF: each hop is re-validated and re-pinned by hand."""
    return httpx.AsyncClient(follow_redirects=False, timeout=120.0, trust_env=False)


async def _resolve_ips(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a fetch target's host. Module-level so tests stub DNS, never touch it."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out, seen = [], set()
    for *_, sockaddr in infos:
        try:
            addr = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])   # strip any v6 scope
        except ValueError:
            continue
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def _public_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Only globally-routable unicast addresses are fetchable targets. An
    IPv4-mapped v6 address is judged as the v4 address it actually reaches."""
    a = getattr(addr, "ipv4_mapped", None) or addr
    return (a.is_global and not (a.is_private or a.is_loopback or a.is_link_local
                                 or a.is_multicast or a.is_reserved or a.is_unspecified))


async def _pin_ip(url: httpx.URL) -> str:
    """Pick the IP the connection will be pinned to, after validation. Refuses a
    host whose answers are all non-public (FETCH_ALLOW_PRIVATE lifts the filter)."""
    host = url.host
    port = url.port or (443 if url.scheme == "https" else 80)
    try:
        ips = await _resolve_ips(host, port)
    except OSError as e:
        raise RuntimeError(f"cannot resolve fetch host {host!r}: {e}") from e
    if not config.FETCH_ALLOW_PRIVATE:
        bad = [str(a) for a in ips if not _public_ip(a)]
        if bad or not ips:
            raise RuntimeError(
                f"refusing to fetch {host!r}: it resolves to non-public addresses "
                f"{bad or 'none'} (private/loopback/link-local/reserved/multicast). "
                "Set FETCH_ALLOW_PRIVATE=1 to permit same-LAN targets.")
    return str(ips[0])


def _pinned_request(url: httpx.URL, ip: str) -> tuple[httpx.URL, dict[str, str], dict[str, Any]]:
    """The connect-to-IP form of `url`: dial the validated IP while the original
    host stays in the Host header and (for TLS) the SNI + cert verification name.
    Host comes from `netloc` — IDN punycode, IPv6 brackets, and a non-default
    port all preserved; SNI comes from `raw_host` (punycode) and is skipped for
    IP literals (SNI is a hostname, not an address)."""
    extensions: dict[str, Any] = {}
    if url.scheme == "https":
        try:
            ipaddress.ip_address(url.host)
        except ValueError:
            extensions["sni_hostname"] = url.raw_host.decode("ascii")
    return url.copy_with(host=ip), {"Host": url.netloc.decode("ascii")}, extensions


async def _download_chunks(resp: httpx.Response):
    """Yield the body as ≤_UPLOAD_CHUNK_BYTES pieces, aborting the stream the moment
    the running total passes the cap — the file is never buffered whole. (httpx's
    aiter_bytes re-chunks transport pieces up to _DOWNLOAD_CHUNK_BYTES, so a single
    piece can exceed the upload granularity; slice the buffer, don't dump it.)"""
    total = 0
    buf = bytearray()
    async for piece in resp.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
        total += len(piece)
        if total > config.MAX_FETCH_BYTES:
            raise RuntimeError(
                f"download exceeded the {config.MAX_FETCH_BYTES}-byte cap; aborted")
        buf += piece
        while len(buf) >= local_jupyter._UPLOAD_CHUNK_BYTES:
            yield bytes(buf[:local_jupyter._UPLOAD_CHUNK_BYTES])
            del buf[:local_jupyter._UPLOAD_CHUNK_BYTES]
    if buf:
        yield bytes(buf)


@mcp.tool
async def fetch_to_workspace(url: str, path: str, overwrite: bool = False) -> dict[str, Any]:
    """Download a URL to a file in the workspace, server-side (the server fetches
    it). Handy to bring in a test image/dataset by link when you can't upload from the
    device. Auto-creates parent folders; the transfer is capped (MAX_FETCH_BYTES,
    default 200 MB) and only http(s) targets resolving to public IPs are fetched —
    private/loopback/link-local hosts are refused (FETCH_ALLOW_PRIVATE=1 lifts that)."""
    _check_workspace_path(path)
    if not overwrite:
        chk = await local_jupyter._http(LOCAL).get(_contents_url(path), params={"content": "0"})
        if chk.status_code == 200:
            raise RuntimeError(f"'{path}' already exists (pass overwrite=True to replace)")
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL as e:
        raise RuntimeError(f"invalid URL: {e}") from e
    if _parent_dir(path):
        await local_jupyter._ensure_dir(_parent_dir(path))
    async with _fetch_client() as client, asyncio.timeout(config.FETCH_TOTAL_TIMEOUT_SEC):
        for _hop in range(_MAX_FETCH_REDIRECTS + 1):
            if target.scheme not in ("http", "https"):
                raise RuntimeError(
                    f"unsupported URL scheme {target.scheme!r} — only http/https can be fetched")
            ip = await _pin_ip(target)
            pinned, headers, extensions = _pinned_request(target, ip)
            async with client.stream("GET", pinned, headers=headers,
                                     extensions=extensions) as resp:
                if resp.status_code in _REDIRECT_STATUSES and resp.headers.get("location"):
                    target = target.join(resp.headers["location"])
                    continue   # the next hop is validated and pinned again
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    raise RuntimeError(
                        f"fetch of {target} failed: HTTP {resp.status_code}") from e
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > config.MAX_FETCH_BYTES:
                    raise RuntimeError(
                        f"file is {declared} bytes, over the {config.MAX_FETCH_BYTES}-byte cap")
                nbytes = await local_jupyter._write_file_chunked(
                    path, _download_chunks(resp), overwrite=overwrite)
                return {"path": path, "bytes": nbytes, "source_url": url}
        raise RuntimeError(f"fetch redirected more than {_MAX_FETCH_REDIRECTS} times")
