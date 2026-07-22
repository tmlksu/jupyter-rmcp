"""Persistent kernel registry — a single-writer JSON file with atomic writes.

Replaces six in-process dicts that server.py used to track kernels
(`_kernel_backend`, `_first_seen`, `_kernel_notebook`, `_last_heartbeat`,
`_pinned`). Those were wiped on every mcp restart/rebuild, which orphaned Colab
sessions, dropped the keep-alive heartbeat, and let a dead colab id silently fall
back to the LOCAL kernel. Persisting the mapping kills that whole bug class.
See docs/refactor/PHASE-1-persistent-registry.md.

The mcp process is the sole writer, so no locking is needed on disk; every
mutation rewrites the file atomically (`<file>.tmp` -> os.replace). Datetimes are
stored as ISO strings on disk and exposed as aware `datetime` objects in memory.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger("jupyter-rmcp.registry")

_DT_FIELDS = ("first_seen", "last_heartbeat")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class KernelRegistry:
    """kernel_id -> {backend, notebook?, first_seen, last_heartbeat, pinned}.

    `backend` absent  → id known only for its pin state (routing treats as unknown).
    `notebook` absent → binding not yet resolved (callers may look it up); ""
                        means resolved-to-no-notebook; a path means bound.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ---- persistence --------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("registry root is not a JSON object")
        except FileNotFoundError:
            self._data = {}
            return
        except Exception as e:  # noqa: BLE001 — corrupt file must not crash startup
            log.warning("kernel registry %s unreadable (%s); starting empty", self._path, e)
            self._data = {}
            return
        data: dict[str, dict[str, Any]] = {}
        for kid, e in raw.items():
            if not isinstance(e, dict):
                continue
            for field in _DT_FIELDS:
                if isinstance(e.get(field), str):
                    try:
                        e[field] = dt.datetime.fromisoformat(e[field])
                    except ValueError:
                        e[field] = None
            data[kid] = e
        self._data = data

    def _save(self) -> None:
        d = os.path.dirname(self._path) or "."
        os.makedirs(d, exist_ok=True)
        serializable = {
            kid: {
                k: (v.isoformat() if isinstance(v, dt.datetime) else v)
                for k, v in e.items()
            }
            for kid, e in self._data.items()
        }
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".kernels-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)   # atomic on POSIX
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- mutation -----------------------------------------------------------
    def track(self, kernel_id: str, backend: str, notebook: str | None = None) -> None:
        """Record (or update) a kernel's backend. Preserves an existing
        `first_seen`; sets `notebook` only when a value is given."""
        e = self._data.get(kernel_id)
        if e is None:
            e = {"first_seen": _now(), "last_heartbeat": None, "pinned": False}
            self._data[kernel_id] = e
        e["backend"] = backend
        if notebook is not None:
            e["notebook"] = notebook
        self._save()

    def forget(self, kernel_id: str) -> None:
        if self._data.pop(kernel_id, None) is not None:
            self._save()

    def set_notebook(self, kernel_id: str, notebook: str) -> None:
        e = self._data.setdefault(
            kernel_id, {"first_seen": _now(), "last_heartbeat": None, "pinned": False})
        e["notebook"] = notebook
        self._save()

    def touch_heartbeat(self, kernel_id: str, when: dt.datetime | None = None) -> None:
        e = self._data.get(kernel_id)
        if e is None:
            return
        e["last_heartbeat"] = when or _now()
        self._save()

    def set_state(self, kernel_id: str, state: str, reason: str | None = None,
                  when: dt.datetime | None = None) -> None:
        """Record a session lifecycle `state` (additive; see backends.colab_cli's
        ColabSessionState). A `reason` (given for terminal states) also stamps
        `state_reason` + a UTC `state_since` ISO timestamp. No validation here — the
        legality of a transition is enforced by the state machine in colab_cli."""
        e = self._data.setdefault(
            kernel_id, {"first_seen": _now(), "last_heartbeat": None, "pinned": False})
        e["state"] = state
        if reason is not None:
            e["state_reason"] = reason
            e["state_since"] = (when or _now()).isoformat()
        self._save()

    def get_state(self, kernel_id: str) -> str | None:
        """The session's lifecycle state, or None if the id is untracked. A tracked entry
        WITHOUT a `state` field (pre-Phase-3 format) defaults to 'live'."""
        e = self._data.get(kernel_id)
        if e is None:
            return None
        return e.get("state", "live")

    def pin(self, kernel_id: str) -> None:
        e = self._data.setdefault(
            kernel_id, {"first_seen": _now(), "last_heartbeat": None})
        e["pinned"] = True
        self._save()

    def unpin(self, kernel_id: str) -> None:
        e = self._data.get(kernel_id)
        if e is not None and e.get("pinned"):
            e["pinned"] = False
            self._save()

    # ---- read ---------------------------------------------------------------
    def get_backend(self, kernel_id: str) -> str | None:
        e = self._data.get(kernel_id)
        return e.get("backend") if e else None

    def get_notebook(self, kernel_id: str) -> str | None:
        e = self._data.get(kernel_id)
        return e.get("notebook") if e else None

    def first_seen(self, kernel_id: str) -> dt.datetime | None:
        e = self._data.get(kernel_id)
        return e.get("first_seen") if e else None

    def last_heartbeat(self, kernel_id: str) -> dt.datetime | None:
        e = self._data.get(kernel_id)
        return e.get("last_heartbeat") if e else None

    def is_pinned(self, kernel_id: str) -> bool:
        e = self._data.get(kernel_id)
        return bool(e.get("pinned")) if e else False

    def ids_for_backend(self, backend: str) -> list[str]:
        return [kid for kid, e in self._data.items() if e.get("backend") == backend]

    def all(self) -> dict[str, dict[str, Any]]:
        """A snapshot copy of the full registry (entry dicts are shallow-copied)."""
        return {kid: dict(e) for kid, e in self._data.items()}
