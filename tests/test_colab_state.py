"""Phase 3 — colab session state machine + single classifier.

The state machine (ColabSessionState + check_transition), the pure classifier
(classify_result), and the registry-backed transition helpers all live near
backends/colab_cli.py so "is this session lost?" is decided in ONE place. These
tests pin the transition table, the classifier's mapping of every colab-cli
result signature, and the invariant that a TRANSIENT failure never declares a
live session dead.
"""
import json

import pytest

import state
from backends import colab_cli
from registry import KernelRegistry
from util import _now

S = colab_cli.ColabSessionState


async def _coro(value):
    """Return `value` from an awaitable — for monkeypatching async colab_cli helpers."""
    return value

# The legal transition set, hardcoded here so the test independently pins the
# table (same-state moves are legal no-ops). Everything else must raise.
_LEGAL_PAIRS = {
    (S.STARTING, S.STARTING), (S.STARTING, S.LIVE), (S.STARTING, S.LOST), (S.STARTING, S.STOPPED),
    (S.LIVE, S.LIVE), (S.LIVE, S.LOST), (S.LIVE, S.STOPPED),
    (S.LOST, S.LOST),
    (S.STOPPED, S.STOPPED),
}


class TestTransitionTable:
    def test_every_pair_legal_or_raises(self):
        for cur in S:
            for new in S:
                if (cur, new) in _LEGAL_PAIRS:
                    assert colab_cli.check_transition(cur, new) is new
                else:
                    with pytest.raises(colab_cli.IllegalColabTransition):
                        colab_cli.check_transition(cur, new)

    def test_terminal_states_are_dead_ends(self):
        for terminal in (S.LOST, S.STOPPED):
            for new in (S.STARTING, S.LIVE):
                with pytest.raises(colab_cli.IllegalColabTransition):
                    colab_cli.check_transition(terminal, new)

    def test_live_cannot_go_back_to_starting(self):
        with pytest.raises(colab_cli.IllegalColabTransition):
            colab_cli.check_transition(S.LIVE, S.STARTING)


class TestClassifyResult:
    """classify_result maps (rc, output) -> live | lost | transient(+timed_out)."""

    def test_rc0_is_live(self):
        out = colab_cli.classify_result(0, "hello world\n")
        assert out.is_live and not out.is_lost and not out.is_transient
        assert out.timed_out is False

    def test_lost_appears_to_be_lost(self):
        out = colab_cli.classify_result(1, "Session rmcp-x appears to be lost (404/401)")
        assert out.is_lost and out.reason

    def test_lost_not_found(self):
        out = colab_cli.classify_result(1, "Error: session rmcp-x not found")
        assert out.is_lost

    def test_rc124_is_transient_timeout(self):
        out = colab_cli.classify_result(124, "[colab-cli timed out after 180s]")
        assert out.is_transient and out.timed_out

    def test_reply_timeout_is_transient_timeout(self):
        out = colab_cli.classify_result(1, "...\nTimeoutError: Timeout waiting for reply")
        assert out.is_transient and out.timed_out

    def test_plain_nonzero_is_transient_not_timeout(self):
        # rc!=0 with no loss/timeout signature: transient (a hiccup or a code error),
        # NOT lost — the session may well still be alive.
        out = colab_cli.classify_result(1, "Traceback: NameError: name 'x' is not defined")
        assert out.is_transient and not out.is_lost and not out.timed_out

    def test_lost_beats_timeout_ordering(self):
        # A definitive loss signature wins even if a timeout phrase co-occurs.
        out = colab_cli.classify_result(1, "appears to be lost\nTimeout waiting for reply")
        assert out.is_lost

    @pytest.mark.parametrize(
        "name", ["two_sessions", "no_sessions", "orphan", "mixed_with_log_noise", "real_capture"])
    def test_all_session_fixtures_classify_live_on_rc0(self, name, colab_sessions_fixture):
        # None of the `colab sessions` fixtures carry a loss/timeout signature, so a
        # successful (rc==0) result over any of them is `live`.
        assert colab_cli.classify_result(0, colab_sessions_fixture(name)).is_live

    @pytest.mark.parametrize(
        "name", ["two_sessions", "no_sessions", "orphan", "mixed_with_log_noise", "real_capture"])
    def test_all_session_fixtures_transient_on_rc1(self, name, colab_sessions_fixture):
        # The same text with rc!=0 (a transient CLI hiccup) must NOT be read as lost.
        out = colab_cli.classify_result(1, colab_sessions_fixture(name))
        assert out.is_transient and not out.is_lost


class TestSessionStateHelpers:
    """Registry-backed transition helpers on an isolated registry."""

    @pytest.fixture()
    def reg(self, tmp_path, monkeypatch):
        r = KernelRegistry(str(tmp_path / "kernels.json"))
        monkeypatch.setattr(state, "_registry", r)
        return r

    def test_mark_session_live_starting_then_live(self, reg):
        reg.track("rmcp-a", colab_cli.COLAB)
        colab_cli.mark_session_live("rmcp-a")
        assert reg.get_state("rmcp-a") == "live"

    def test_set_session_state_untracked_is_noop(self, reg):
        # No entry -> nothing to transition; must not raise or create one.
        colab_cli.set_session_state("rmcp-ghost", S.LOST)
        assert reg.get_backend("rmcp-ghost") is None

    def test_set_session_state_illegal_raises(self, reg):
        reg.track("rmcp-b", colab_cli.COLAB)
        reg.set_state("rmcp-b", S.STOPPED.value)   # force terminal directly
        with pytest.raises(colab_cli.IllegalColabTransition):
            colab_cli.set_session_state("rmcp-b", S.LIVE)

    def test_mark_lost_records_then_forgets(self, reg):
        reg.track("rmcp-c", colab_cli.COLAB)
        colab_cli.mark_session_live("rmcp-c")
        colab_cli._mark_lost("rmcp-c", "gone")
        assert reg.get_backend("rmcp-c") is None   # forgotten (nothing routes to it)
        assert reg.get_state("rmcp-c") is None

    async def test_transient_heartbeat_leaves_session_live(self, reg, monkeypatch):
        # Acceptance criterion 3: a transient CLI failure (rc!=0, no loss signature)
        # against a `live` session must LEAVE IT LIVE — never `lost`.
        reg.track("rmcp-d", colab_cli.COLAB)
        colab_cli.mark_session_live("rmcp-d")

        async def fake_cli(*a, **k):
            return 1, "transient boom (no loss signature)"
        monkeypatch.setattr(colab_cli, "_colab_cli", fake_cli)
        await colab_cli._heartbeat_colab("rmcp-d", _now())
        assert reg.get_state("rmcp-d") == "live"
        assert reg.get_backend("rmcp-d") == colab_cli.COLAB

    async def test_confirm_lost_true_when_absent_from_live_set(self, reg, monkeypatch):
        # A successful liveness lookup that OMITS the session -> definitely lost.
        monkeypatch.setattr(colab_cli, "_live_colab_sessions", lambda: _coro(set()))
        assert await colab_cli._confirm_lost("rmcp-x") is True

    async def test_confirm_lost_false_on_transient_lookup(self, reg, monkeypatch):
        # A transient lookup failure (None) must NOT be read as lost.
        monkeypatch.setattr(colab_cli, "_live_colab_sessions", lambda: _coro(None))
        assert await colab_cli._confirm_lost("rmcp-x") is False

    async def test_confirm_lost_false_when_present(self, reg, monkeypatch):
        monkeypatch.setattr(colab_cli, "_live_colab_sessions", lambda: _coro({"rmcp-x"}))
        assert await colab_cli._confirm_lost("rmcp-x") is False

    async def test_lost_heartbeat_marks_lost_and_forgets(self, reg, monkeypatch):
        reg.track("rmcp-e", colab_cli.COLAB)
        colab_cli.mark_session_live("rmcp-e")

        async def fake_cli(*a, **k):
            return 1, "Session rmcp-e appears to be lost (404/401)"
        monkeypatch.setattr(colab_cli, "_colab_cli", fake_cli)
        await colab_cli._heartbeat_colab("rmcp-e", _now())
        assert reg.get_backend("rmcp-e") is None


class TestRegistryStateField:
    """The additive `state` field: transitions persist; old files still load."""

    def _path(self, tmp_path):
        return str(tmp_path / "kernels.json")

    def test_default_live_and_unknown_none(self, tmp_path):
        r = KernelRegistry(self._path(tmp_path))
        r.track("rmcp-a", "colab")
        assert r.get_state("rmcp-a") == "live"     # tracked, no state field -> live
        assert r.get_state("nope") is None         # untracked -> None

    def test_state_transitions_persist_with_reason_and_timestamp(self, tmp_path):
        p = self._path(tmp_path)
        r = KernelRegistry(p)
        r.track("rmcp-a", "colab")
        r.set_state("rmcp-a", "starting")
        r.set_state("rmcp-a", "live")
        r.set_state("rmcp-a", "lost", reason="gone")
        r2 = KernelRegistry(p)                      # reload from disk
        assert r2.get_state("rmcp-a") == "lost"
        e = r2.all()["rmcp-a"]
        assert e["state_reason"] == "gone"
        assert "state_since" in e                   # UTC timestamp stamped on terminal

    def test_old_format_without_state_loads_and_defaults_live(self, tmp_path):
        p = tmp_path / "kernels.json"
        p.write_text(json.dumps({
            "rmcp-old": {"backend": "colab", "first_seen": "2026-07-20T00:00:00+00:00",
                         "last_heartbeat": None, "pinned": False}}))
        r = KernelRegistry(str(p))                  # must not raise
        assert r.get_state("rmcp-old") == "live"
        assert r.get_backend("rmcp-old") == "colab"
