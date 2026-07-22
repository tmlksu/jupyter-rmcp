"""Characterization tests for small utilities + routing behavior pinned for Phase 1."""
import datetime as dt

import backends
import config
import util


class TestParseTs:
    def test_none(self):
        assert util._parse_ts(None) is None

    def test_empty(self):
        assert util._parse_ts("") is None

    def test_z_suffix(self):
        ts = util._parse_ts("2026-07-20T12:00:00Z")
        assert ts == dt.datetime(2026, 7, 20, 12, 0, 0, tzinfo=dt.UTC)

    def test_garbage_is_none(self):
        assert util._parse_ts("not a timestamp") is None


class TestParentDir:
    def test_nested(self):
        assert util._parent_dir("a/b/c.txt") == "a/b"

    def test_top_level(self):
        assert util._parent_dir("top.txt") == ""

    def test_leading_slash_stripped(self):
        assert util._parent_dir("/lead/slash.txt") == "lead"


class TestBackendRouting:
    def test_unknown_kernel_is_not_local(self):
        # Phase 1: an UNKNOWN kernel_id no longer silently routes to "local" — that
        # implicit fallback once ran a dead colab id's code on the LOCAL kernel.
        # `_backend_of` now returns None for anything the registry doesn't track;
        # execution routing (`_resolve_backend`) turns that into an explicit error.
        assert backends._backend_of("11111111-2222-3333-4444-555555555555") is None


class TestEnvDefaults:
    """compose interpolates a key missing from .env into an EMPTY string, so the
    module defaults must survive that — a minimal .env is the normal case."""

    def test_empty_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("EXEC_TIMEOUT_SEC", "")
        assert config._env("EXEC_TIMEOUT_SEC", "120") == "120"

    def test_whitespace_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("MAX_KERNELS", "   ")
        assert config._env("MAX_KERNELS", "8") == "8"

    def test_set_value_wins(self, monkeypatch):
        monkeypatch.setenv("MAX_KERNELS", "3")
        assert config._env("MAX_KERNELS", "8") == "3"

    def test_unset_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("REAPER_INTERVAL_SEC", raising=False)
        assert config._env("REAPER_INTERVAL_SEC", "60") == "60"
