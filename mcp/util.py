"""Small pure helpers with no first-party dependencies (time + path).

Kept dependency-free so both the low-level backend modules and the tool modules
can share them without import cycles.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote, unquote


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parent_dir(path: str) -> str:
    p = path.strip("/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _assert_clean_path(path: str, what: str) -> None:
    if "\x00" in path:
        raise RuntimeError(f"invalid {what}: {path!r} contains a NUL byte")
    if "\\" in path:
        raise RuntimeError(f"invalid {what}: {path!r} contains a backslash — use '/' separators")
    if path.startswith("/"):
        raise RuntimeError(
            f"invalid {what}: {path!r} — paths are relative to the work root, no leading '/'")
    if any(seg == ".." for seg in path.split("/")):
        raise RuntimeError(f"invalid {what}: {path!r} — '..' segments are not allowed")


def _check_workspace_path(path: str, what: str = "path") -> str:
    """Normalize + validate a workspace-relative path; return the normalized form.

    The MCP layer does not lean on jupyter-server's own root-dir check alone
    (ADR 0021): every tool entry validates here first and uses the RETURNED
    path. Leading "/" is normalized away — callers mean "from the work root",
    and `list_files("/")` must keep working.

    Refused: `..` segments, NUL bytes, backslashes (Windows separators / UNC
    tricks), and the same after URL-decoding — a literal `%2e%2e` or `%00`
    would decode to a traversal server-side. Allowed: "" or "." or "/" (the
    work root), "sub/dir/x.ipynb", a leading "./"."""
    path = path.lstrip("/")
    _assert_clean_path(path, what)
    _assert_clean_path(unquote(path), what)
    return path


def _contents_url(path: str) -> str:
    """Build an /api/contents/{path} URL with the path percent-encoded — a raw
    `#` would truncate into a fragment, `?` into a query string, and `%` into
    a different name server-side. Slashes stay slashes (nested paths); every
    other unsafe byte is encoded exactly once (httpx sends the URL as given)."""
    return f"/api/contents/{quote(path, safe='/')}"
