"""colab-cli backend: ALL colab-cli subprocess invocation + stdout parsing.

The stdout parsing is quarantined here as SYNC PURE functions (`parse_sessions`,
`is_session_lost`, `is_reply_timeout`) — no I/O, no env, no event loop — so the
CLI-scraping that caused most of the Colab bug history is directly unit-testable.
Everything that shells out to `colab` lives in this file (and only this file).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import state
from config import COLAB, EXEC_TIMEOUT_SEC, log
from outputs import _strip_ansi

# =============================================================================
# Pure stdout parsing (no I/O, no env, no event loop — directly unit-testable)
# =============================================================================
# A real session row is `[NAME] <endpoint> | Hardware: X | Variant: Y`. Require the
# `Hardware:` field so we DON'T match colab-cli's log/status lines like
# `[colab] No active sessions found on server.` (that once got adopted as a ghost
# kernel named "colab" with age 0).
_COLAB_SESSION_RE = re.compile(r"^\[([^\]]+)\][^\n]*\bHardware:", re.MULTILINE)


def parse_sessions(text: str) -> set[str]:
    """Session names from `colab sessions` stdout. Strips ANSI first (a color-prefixed
    row must still match), requires the `Hardware:` field so log lines don't match, and
    returns the RAW matched names — including a `[?]` orphan; the `?`/`colab` filtering
    happens in `_live_colab_sessions`."""
    return set(_COLAB_SESSION_RE.findall(_strip_ansi(text)))


def is_session_lost(out: str) -> bool:
    """colab-cli's signature for a dead session (VM gone / 404/401): the CLI reports the
    session as lost, or as not found. The two literal markers appear ONLY here (see the
    Phase-3 single-classifier invariant); everything else classifies via classify_result."""
    o = out.lower()
    return "appears to be lost" in o or ("session" in o and "not found" in o)


def is_reply_timeout(out: str) -> bool:
    """colab exec hitting its OWN kernel-side --timeout surfaces as an rc=1 traceback whose
    tail is the reply-timeout marker below. rc=1 alone can't distinguish that from a genuine
    code error, so callers key off this phrase (which lives ONLY here)."""
    return "Timeout waiting for reply" in out


def _looks_colab(kernel_id: str) -> bool:
    """colab-cli session names are `rmcp-<hex>`; local kernels are UUIDs. Used to
    refuse routing a (dead) colab id to the LOCAL kernel — running in the wrong
    environment is worse than a clear error."""
    return kernel_id.startswith("rmcp-")


# =============================================================================
# Session state machine (Phase 3): ONE place decides "lost"
# =============================================================================
class ColabSessionState(StrEnum):
    """A colab session's lifecycle. `starting`/`live` are the tracked states; `lost`/
    `stopped` are terminal (the session is then forgotten from the registry, so it never
    routes anywhere). Persisted as the additive `state` field in kernels.json."""
    STARTING = "starting"
    LIVE = "live"
    LOST = "lost"
    STOPPED = "stopped"


# Legal forward transitions. Same-state is always a legal no-op (see check_transition).
# Terminal states go nowhere — a transition OUT of them is a logic bug we want loud.
_LEGAL_TRANSITIONS: dict[ColabSessionState, set[ColabSessionState]] = {
    ColabSessionState.STARTING: {ColabSessionState.LIVE, ColabSessionState.LOST, ColabSessionState.STOPPED},
    ColabSessionState.LIVE: {ColabSessionState.LOST, ColabSessionState.STOPPED},
    ColabSessionState.LOST: set(),
    ColabSessionState.STOPPED: set(),
}


class IllegalColabTransition(RuntimeError):
    """An attempted colab session-state transition the machine forbids (e.g.
    stopped -> live). Raised — a raise here means a real logic bug, not something to
    paper over."""


def check_transition(current: ColabSessionState, new: ColabSessionState) -> ColabSessionState:
    """Validate `current -> new` against the transition table. Returns `new` if legal
    (a no-op same-state move is legal), else raises IllegalColabTransition. This is the
    ONE place the transition table is enforced."""
    if new == current:
        return new
    if new not in _LEGAL_TRANSITIONS[current]:
        raise IllegalColabTransition(
            f"illegal colab session state transition: {current.value} -> {new.value}")
    return new


@dataclass(frozen=True)
class ColabOutcome:
    """Session-liveness classification of a colab-cli exec/restart/heartbeat result:
      - `live`      the session responded (rc==0, or it ran code that merely raised);
      - `lost`      a DEFINITIVE loss signature (VM gone / 404/401 / not found);
      - `transient` inconclusive — a timeout or a non-lost non-zero exit.
    A `transient` outcome must NEVER by itself move a session to `lost` (a hiccup is not a
    death); only a successful liveness lookup that omits the session may. `timed_out` marks
    the timeout flavor of transient (callers probe liveness before reporting a timeout)."""
    state: str
    reason: str | None = None
    timed_out: bool = False

    @property
    def is_live(self) -> bool:
        return self.state == "live"

    @property
    def is_lost(self) -> bool:
        return self.state == "lost"

    @property
    def is_transient(self) -> bool:
        return self.state == "transient"


def classify_result(rc: int, out: str) -> ColabOutcome:
    """The ONE brain that turns a colab-cli (returncode, output) into a session outcome,
    consolidating the loss/timeout/error signatures that used to be re-checked ad hoc at
    every call site (the source of the 2026-07 bug streak). Order matters: a definitive
    loss beats a timeout beats a generic non-zero exit; rc==0 is live."""
    if rc != 0 and is_session_lost(out):
        return ColabOutcome("lost", reason="colab reports the session lost (404/401 / not found)")
    if rc == 124:
        return ColabOutcome("transient", reason="colab-cli subprocess exceeded its deadline", timed_out=True)
    if is_reply_timeout(out):
        return ColabOutcome("transient", reason="colab kernel did not reply before the deadline", timed_out=True)
    if rc != 0:
        return ColabOutcome("transient", reason="colab-cli exited non-zero (no loss signature)")
    return ColabOutcome("live")


# =============================================================================
# colab-cli subprocess (the ONLY place that shells out to `colab`)
# =============================================================================
async def _colab_cli(*args: str, input_text: str | None = None, timeout: float = 300.0) -> tuple[int, str]:
    """Run the official `colab` CLI (google-colab-cli) with ADC auth. Returns
    (returncode, combined_output)."""
    proc = await asyncio.create_subprocess_exec(
        "colab", "--auth", "adc", *args,
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(input_text.encode() if input_text is not None else None),
            timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        # Best-effort: drain whatever the (now-killed) process already emitted so a
        # timeout doesn't discard partial stdout. communicate() was cancelled so some
        # buffered bytes may be lost, but any remaining in the pipe are recoverable.
        partial = b""
        try:
            if proc.stdout is not None:
                partial = await asyncio.wait_for(proc.stdout.read(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        await proc.wait()
        txt = partial.decode(errors="replace")
        marker = f"[colab-cli timed out after {timeout:.0f}s]"
        # rc 124 is our timeout sentinel (callers key off it to report status="timeout").
        return 124, (f"{txt}\n{marker}" if txt else marker)
    return proc.returncode, out.decode(errors="replace")


# =============================================================================
# colab session tracking / liveness (uses state + the pure parsers above)
# =============================================================================
async def _live_colab_sessions() -> set[str] | None:
    """Named sessions currently alive per colab-cli (`colab sessions`). Skips `[?]`
    orphans (no local record — not addressable) and the `[colab]` log prefix. Returns
    None if the lookup itself FAILED (transient) — distinct from an empty set (definitely
    no sessions) — so callers don't mistake a hiccup for 'the session died'."""
    if not (state.COLAB_AVAILABLE and COLAB in state._backends):
        return set()
    rc, out = await _colab_cli("sessions", timeout=30)
    if rc != 0:
        return None
    return {n for n in parse_sessions(out) if n not in ("?", "colab")}


def _forget_colab(kernel_id: str) -> None:
    """Drop all state for a colab session that's gone, so we stop routing to it."""
    state._registry.forget(kernel_id)
    state._locks.pop(kernel_id, None)
    state._clients.pop(kernel_id, None)
    # NOT state.forget_exec(): this runs from inside a failing exec (_mark_lost), whose own
    # release is still pending, and the recorded session_lost result stays worth reading.


async def _confirm_lost(kernel_id: str) -> bool:
    """True iff a SUCCESSFUL colab-cli liveness lookup OMITS this session (it is definitely
    gone). A transient lookup failure (`_live_colab_sessions` -> None) returns False — the
    ONE rule that lets a session become `lost`: never on a hiccup, only on authoritative
    absence. Used to disambiguate an inconclusive exec/restart result (e.g. a timeout, or
    colab-cli's opaque restart-kernel traceback for a vanished session)."""
    live = await _live_colab_sessions()
    return live is not None and kernel_id not in live


def _current_state(kernel_id: str) -> ColabSessionState | None:
    """A tracked session's state as the enum, or None if the id is untracked."""
    raw = state._registry.get_state(kernel_id)
    return ColabSessionState(raw) if raw is not None else None


def set_session_state(kernel_id: str, new: ColabSessionState, reason: str | None = None) -> None:
    """Transition a tracked colab session's persisted state, VALIDATED by the machine.
    No-op for an untracked id (nothing to transition). Illegal transitions raise
    IllegalColabTransition (a loud logic-bug signal)."""
    current = _current_state(kernel_id)
    if current is None:
        return
    check_transition(current, new)   # raises on an illegal move
    state._registry.set_state(kernel_id, new.value, reason=reason)


def mark_session_live(kernel_id: str) -> None:
    """Record a freshly-created colab session: set the initial `starting` directly (there
    is nothing to transition from), then transition starting -> live."""
    state._registry.set_state(kernel_id, ColabSessionState.STARTING.value)
    set_session_state(kernel_id, ColabSessionState.LIVE)


def _mark_lost(kernel_id: str, reason: str | None) -> None:
    """The SINGLE lost-cleanup path: record the terminal `lost` state (validated) then
    forget the session so nothing routes to it. Every loss decision funnels through here."""
    try:
        set_session_state(kernel_id, ColabSessionState.LOST, reason=reason or "session lost")
    except IllegalColabTransition as e:
        log.warning("colab %s: %s (forgetting anyway)", kernel_id, e)
    _forget_colab(kernel_id)


async def _heartbeat_colab(kid: str, now) -> None:
    """Ping a colab session with a no-op exec to reset Colab's idle timer (see
    COLAB_HEARTBEAT_SEC). If the ping's result classifies as a definitive loss, drop the
    session; a mere transient failure LEAVES IT LIVE (a hiccup is not a death). Shares the
    per-session lock so it never races a user exec."""
    lock = state._locks.setdefault(kid, asyncio.Lock())
    async with lock:
        rc, out = await _colab_cli("exec", "-s", kid, "--timeout", "20", input_text="0\n", timeout=45)
    state._registry.touch_heartbeat(kid, now)
    outcome = classify_result(rc, out)
    if outcome.is_lost:
        log.info("heartbeat: colab session %s gone; dropping tracking", kid)
        _mark_lost(kid, outcome.reason)


# =============================================================================
# colab execution (the cli branch of the old _run_on_kernel; colab-only, no dispatch)
# =============================================================================
_COLAB_UNCAPPED_SEC = 86400.0   # colab-cli requires a --timeout; this stands in for "none"


async def exec_colab(kernel_id: str, code: str, timeout: float | None = None) -> dict[str, Any]:
    """Execute code on a colab session via colab-cli; return a normalized reply
    {status, execution_count, outputs(nbformat), timed_out}. Serializes on the per-session
    lock (colab-cli ops share one sessions.json; concurrent invocations can corrupt it)."""
    lock = state._locks.setdefault(kernel_id, asyncio.Lock())
    async with lock:
        # Same policy as the local backend (ADR 0018): only an EXPLICIT cap interrupts.
        # This used to floor at 180s, which — now that EXEC_TIMEOUT_SEC defaults to 0 —
        # would have left colab, the GPU backend, as the one place a long job still died
        # at a hidden default. colab-cli needs *some* number, so an uncapped run gets a day.
        cto = float(timeout) if timeout else (EXEC_TIMEOUT_SEC or _COLAB_UNCAPPED_SEC)
        # Give colab exec its OWN kernel-side --timeout (default is only 30s) so it
        # returns partial output cleanly at the same deadline we intend; our subprocess
        # kill in _colab_cli is just a backstop a grace period later.
        rc, out = await _colab_cli("exec", "-s", kernel_id, "--timeout", str(cto),
                                   input_text=code, timeout=cto + 30.0)
        # ONE classifier decides what this result means (see classify_result).
        outcome = classify_result(rc, out)
        # Session died server-side (VM gone). Report it honestly and stop routing
        # to it, rather than pretending the exec ran.
        if outcome.is_lost:
            log.info("colab session %s lost during exec; dropping tracking", kernel_id)
            _mark_lost(kernel_id, outcome.reason)
            return {"status": "session_lost", "execution_count": None, "outputs": [],
                    "timed_out": False,
                    "note": (f"colab session '{kernel_id}' was LOST (404/401) — its VM/tunnel is "
                             "gone and /content is wiped. Nothing ran. Start a new session with "
                             "start_kernel(backend='colab') and re-setup (setup_kaggle/setup_hf, "
                             "re-download data).")}
        state._registry.touch_heartbeat(kernel_id)   # any exec resets Colab's idle timer too
        timed_out = outcome.timed_out
        if timed_out:
            # A DYING session's websocket hangs, which looks just like a slow cell. Check
            # liveness once (via _confirm_lost) so we don't emit misleading "still running"
            # timeouts for a VM that's actually gone — and never lie on a transient hiccup.
            if await _confirm_lost(kernel_id):
                log.info("colab session %s not live after exec timeout; treating as lost", kernel_id)
                _mark_lost(kernel_id, "session gone (confirmed absent after exec timeout)")
                return {"status": "session_lost", "execution_count": None, "outputs": [],
                        "timed_out": False,
                        "note": (f"colab session '{kernel_id}' is GONE (VM reclaimed/lost) — the "
                                 "exec 'timed out' because the session died, not because your code "
                                 "is slow. Nothing survives. Start a new session with "
                                 "start_kernel(backend='colab') and re-setup.")}
            # Drop colab-cli's noisy reply-timeout traceback; keep the stdout/stderr the
            # cell actually produced before the cutoff (that's what the caller wants).
            for marker in ("╭─", "Traceback (most recent call last)"):
                idx = out.find(marker)
                if idx != -1:
                    out = out[:idx].rstrip()
                    break
        res = {"status": "timeout" if timed_out else ("ok" if outcome.is_live else "error"),
               "execution_count": None,
               "outputs": [{"output_type": "stream", "name": "stdout", "text": out}],
               "timed_out": timed_out}
        if timed_out:
            res["note"] = (
                "colab exec timed out WAITING FOR THE CELL'S REPLY — the kernel may STILL be "
                "running it. Heavy compute or a large output can make the reply lag well behind "
                "the visible stdout, so a timeout here is NOT necessarily a failure. Any stdout "
                "captured before the cutoff is included above. To confirm the real outcome, have "
                "the cell persist a result (e.g. write JSON to a file) and read it back in a "
                "later call; raise `timeout=` for known-long cells. Use restart_kernel only if "
                "you actually want to abort (it keeps the VM + files, just resets Python)."
            )
        return res
