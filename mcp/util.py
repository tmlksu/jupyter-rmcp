"""Small pure helpers with no first-party dependencies (time + path).

Kept dependency-free so both the low-level backend modules and the tool modules
can share them without import cycles.
"""
from __future__ import annotations

import datetime as dt


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
