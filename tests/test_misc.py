"""Characterization tests for small utilities + routing behavior pinned for Phase 1."""
import datetime as dt

import backends
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
