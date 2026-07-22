"""Tests for the colab-cli stdout parsing, now in backends/colab_cli.py.

Phase 2 moved the parsing into `backends.colab_cli` as SYNC PURE functions
(`parse_sessions`, `is_session_lost`, `is_reply_timeout`), so the pure-parsing
tests below call them directly (no monkeypatching, no event loop). The
`_live_colab_sessions` tests remain monkeypatch-based — only the targets moved
(server -> colab_cli / state).
"""
import state
from backends import colab_cli


class TestParseSessions:
    def test_two_sessions(self, colab_sessions_fixture):
        assert colab_cli.parse_sessions(colab_sessions_fixture("two_sessions")) == {
            "rmcp-a1b2c3d4", "rmcp-e5f6a7b8"}

    def test_no_sessions_log_line_is_not_a_session(self, colab_sessions_fixture):
        # Regression: `[colab] No active sessions found on server.` once got adopted
        # as a phantom kernel named "colab" (fixed 2026-07-20, commit f9f4266).
        assert colab_cli.parse_sessions(colab_sessions_fixture("no_sessions")) == set()

    def test_orphan_row_matches(self, colab_sessions_fixture):
        # parse_sessions DOES match a `[?]` orphan row — the `?`/`colab` filtering
        # happens downstream in _live_colab_sessions.
        assert colab_cli.parse_sessions(colab_sessions_fixture("orphan")) == {"?", "rmcp-a1b2c3d4"}

    def test_log_noise_skipped(self, colab_sessions_fixture):
        # `[colab] ...` log lines lack the `Hardware:` field → no match.
        assert colab_cli.parse_sessions(colab_sessions_fixture("mixed_with_log_noise")) == {
            "rmcp-11223344", "rmcp-55667788"}

    def test_real_capture(self, colab_sessions_fixture):
        # Captured live from `docker exec jrmcp-mcp colab --auth adc sessions`
        # (2026-07-20, no sessions running) — pins the CLI's real no-sessions text.
        assert colab_cli.parse_sessions(colab_sessions_fixture("real_capture")) == set()

    def test_leading_ansi_is_stripped_then_matched(self):
        # Phase 2: parse_sessions strips ANSI first, so a color-prefixed session row
        # now MATCHES (the old regex anchored on `^[` and silently dropped it).
        row = "\x1b[32m[rmcp-deadbeef] https://x | Hardware: T4 | Variant: GPU"
        assert colab_cli.parse_sessions(row) == {"rmcp-deadbeef"}


class TestLiveColabSessions:
    """_live_colab_sessions: None on lookup failure (≠ empty set), filters ?/colab."""

    def _enable_colab(self, monkeypatch):
        monkeypatch.setattr(state, "COLAB_AVAILABLE", True)
        monkeypatch.setitem(state._backends, colab_cli.COLAB, {"kind": "cli"})

    async def test_unavailable_returns_empty_set(self, monkeypatch):
        monkeypatch.setattr(state, "COLAB_AVAILABLE", False)
        assert await colab_cli._live_colab_sessions() == set()

    async def test_cli_failure_returns_none_not_empty(self, monkeypatch):
        # Distinction matters: None = transient lookup failure, set() = definitely
        # no sessions. Confusing the two once declared live sessions dead.
        self._enable_colab(monkeypatch)

        async def fake_cli(*args, **kwargs):
            return 1, "some transient error"
        monkeypatch.setattr(colab_cli, "_colab_cli", fake_cli)
        assert await colab_cli._live_colab_sessions() is None

    async def test_parses_sessions_and_skips_orphans(self, monkeypatch, colab_sessions_fixture):
        self._enable_colab(monkeypatch)
        out = colab_sessions_fixture("orphan")

        async def fake_cli(*args, **kwargs):
            return 0, out
        monkeypatch.setattr(colab_cli, "_colab_cli", fake_cli)
        assert await colab_cli._live_colab_sessions() == {"rmcp-a1b2c3d4"}

    async def test_no_sessions_is_empty_set(self, monkeypatch, colab_sessions_fixture):
        self._enable_colab(monkeypatch)
        out = colab_sessions_fixture("no_sessions")

        async def fake_cli(*args, **kwargs):
            return 0, out
        monkeypatch.setattr(colab_cli, "_colab_cli", fake_cli)
        assert await colab_cli._live_colab_sessions() == set()


class TestSessionLost:
    def test_appears_to_be_lost(self):
        assert colab_cli.is_session_lost(
            "Session rmcp-a1b2c3d4 appears to be lost (404/401)") is True

    def test_session_not_found(self):
        assert colab_cli.is_session_lost("Error: session rmcp-x not found") is True

    def test_case_insensitive(self):
        assert colab_cli.is_session_lost("SESSION RMCP-X NOT FOUND") is True

    def test_plain_error_is_not_lost(self):
        assert colab_cli.is_session_lost("Error: connection refused") is False

    def test_timeout_is_not_lost(self):
        assert colab_cli.is_session_lost("Timeout waiting for reply") is False


class TestReplyTimeout:
    def test_reply_timeout_phrase(self):
        assert colab_cli.is_reply_timeout(
            "...\nTimeoutError: Timeout waiting for reply") is True

    def test_plain_output_is_not_timeout(self):
        assert colab_cli.is_reply_timeout("hello\nworld\n") is False

    def test_lost_message_is_not_reply_timeout(self):
        # session-lost and reply-timeout are DIFFERENT signatures; don't conflate.
        assert colab_cli.is_reply_timeout("Session rmcp-x appears to be lost (404)") is False


class TestLooksColab:
    def test_rmcp_prefix(self):
        assert colab_cli._looks_colab("rmcp-a1b2c3d4") is True

    def test_uuid_is_not_colab(self):
        assert colab_cli._looks_colab("1c1c9de3-71b2-4a5e-9d0e-aaaabbbbcccc") is False
