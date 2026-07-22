# PHASE 3 — colab session state machine (one place decides "lost")

**Status:** DONE 2026-07-20 (commit pending)
**References are function names** (post-Phase-2 layout). **Depends on:** Phase 2.
**Live-colab gate:** REQUIRED, including a destructive out-of-band kill.

## Context

Session-loss detection is smeared across four call sites, each with its own
slightly different logic: the exec path (lost-message check + the
timeout-then-liveness-probe), `get_job`'s polling, `restart_kernel`, and the
reaper/`list_kernels` reconcile. The 2026-07 bug streak (fake jobs, misleading
"may still be running" timeouts, sessions stuck "running") came from these
drifting apart. After Phase 2 they all live near `backends/colab_cli.py` — now
give them ONE brain.

## Design

- `ColabSessionState` enum: `starting → live → lost | stopped`, with `reason` and
  a UTC timestamp on the terminal states. Stored per-session in the registry as
  an additive `state` field in `kernels.json` (old entries without it default to
  `live`).
- Illegal transitions (e.g. `stopped → live`) **raise** — a raise here means a
  logic bug we want loud, not papered over.
- One classifier, pure function in `backends/colab_cli.py`:
  `classify_result(rc, out) -> Live | Lost(reason) | Transient(reason)`
  consolidating: `is_session_lost` messages, rc==124 / reply-timeout signatures,
  and rc!=0-but-not-lost (→ Transient or plain code error as appropriate).
- **Transient ≠ Lost** — this preserves the 2026-07-20 fix ("`_live_colab_sessions`
  returns `None` on a transient failure (≠ empty), so a hiccup never falsely
  declares a live session dead", CHANGELOG 2026-07-20 / HANDOFF "Colab liveness
  follow-ups"). A Transient result must never transition a session to `lost` by
  itself; only a SUCCESSFUL liveness lookup that omits the session may.
- The four call sites (exec, `get_job`, `restart_kernel`, reaper heartbeat +
  reconcile) all route their colab-cli results through `classify_result` and the
  state machine; the `session_lost` responses they return to clients keep their
  current shapes/messages (tool surface frozen).
- `_ensure_tracked`: retire it if the registry + reconcile make it dead code
  (expected); if you keep it, write the reason into this spec's DONE note.

## Out of scope

New tools (e.g. session reattach), preventing Colab preemption itself, changing
heartbeat cadence/policy, local-backend behavior.

## Acceptance criteria

1. Unit: full transition table test (legal transitions succeed; every illegal
   pair raises); `classify_result` against ALL `tests/fixtures/colab_sessions/`
   samples + lost-message fixtures + an rc!=0 transient case + rc==124 timeout.
2. `grep -rn "appears to be lost" mcp/ --include='*.py'` → exactly one hit, in
   `backends/colab_cli.py`. Same for the reply-timeout phrase.
3. Unit: a simulated transient CLI failure (monkeypatched to rc!=0) against a
   session in `live` leaves it `live` (never `lost`).
4. **Live-colab gate (REQUIRED) with destructive check:** start a session, run
   the standard gate, then kill it out-of-band —
   `docker exec jrmcp-mcp colab --auth adc stop -s <name>` — and confirm:
   `execute_code`, `get_job`, `restart_kernel` each report `session_lost` (not a
   hang, not a generic error), and `list_kernels` drops the session within one
   reaper cycle. (This mirrors the manual verification done 2026-07-18.)
5. `kernels.json` shows `state` transitions; an old-format file (no `state`)
   still loads.
6. `pytest -q`, `ruff check .`, rebuild + smoke exit 0.

## DONE notes (what landed; deviations from the plan)

- **`classify_result` + `ColabOutcome`** (pure, in `backends/colab_cli.py`) is the single
  classifier; `ColabSessionState` + `_LEGAL_TRANSITIONS` + `check_transition` are the state
  machine (illegal transitions raise `IllegalColabTransition`). Persisted via
  `registry.set_state`/`get_state` (additive; default `live`). The four call sites route loss
  through one `_mark_lost` (transition → forget).
- **`_ensure_tracked` retired** (as "expected"). Replacement: the persistent registry (Phase 1)
  covers the restart case, and a new reaper `_reconcile_colab` (startup) + the existing
  list_kernels reconcile adopt any live session the registry lost. All 9 call sites removed.
- **Deviation / addition — `_confirm_lost`:** the plan assumed `classify_result` alone could
  classify every loss. The live gate (criterion 4 uses `colab stop`) exposed that colab-cli's
  `restart-kernel` raises an OPAQUE `AttributeError` traceback (NOT the "not found" signature)
  when the local session record is gone. Keying off that traceback string would be fragile, so
  `restart_kernel` (and exec's timeout path, refactored to share it) now disambiguates an
  inconclusive result via `_confirm_lost` — the authoritative "successful liveness lookup omits
  the session" rule (which also preserves transient≠lost). This is an implementation detail
  discovered in verification, consistent with the spec's philosophy (only an authoritative
  absence declares lost); no design change, so no separate ADR.
- **Bonus fix (in scope of loss cleanup):** a pre-existing orphan-entry leak — a `session_lost`
  foreground `execute_code` re-created a bare registry entry via `_notebook_for_kernel`'s
  `set_notebook("")`. Write-back is now skipped for a lost session.
- Verified: 110 unit tests, ruff clean, rebuild + smoke exit 0, and the full live-colab gate
  incl. destructive out-of-band kill (`scripts/live_colab_gate_phase3.py`, 12 checks) — plus a
  one-off check that `kernels.json` shows `state=live` and stays `{}` (leak-free) after loss.

## Rollback

`git revert`; the `state` field is additive, old code ignores it.

## After this phase

The refactor plan is complete. Fold the "still deferred" Colab feedback items
(HANDOFF: reattach-to-live-VM, surface real GPU/HW at start) into normal feature
work — they become straightforward on top of the registry + backend split.
