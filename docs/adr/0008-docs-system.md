# 0008 — Docs system: HANDOFF + ADR + CHANGELOG + CLAUDE rituals

**Status:** Accepted (2026-07-13); partially superseded by
[0015](0015-public-release.md) — `HANDOFF.md` and `REINSTALL.md` are
operator-specific and no longer part of the published tree.

## Context
This is a solo project touched intermittently (and often by a fresh Claude
session). Without structure, each session re-derives context and re-litigates
settled choices, and "what's next" gets lost.

## Decision
Adopt a layered, low-overhead doc system:
- **`CLAUDE.md`** — entry point: start/end **session rituals**, invariants, repo map,
  key facts. Auto-read by Claude when working in the repo.
- **`docs/HANDOFF.md`** — "read first": current state, next steps, open questions,
  gotchas. **Rewritten every session.**
- **`CHANGELOG.md`** — human-readable history (Keep a Changelog).
- **`docs/adr/`** — one file per architectural decision (the "why").
- Existing `DESIGN/AUTH/SECURITY/COLAB/REINSTALL.md` stay as the reference layer.

Repo docs are the **portable** source of truth; Claude's machine-local memory is a
convenience cache, not the record.

## Consequences
- A new session orients via CLAUDE.md → HANDOFF and can resume in minutes.
- Small end-of-session tax (update HANDOFF, add CHANGELOG line, ADR if a decision
  was made). Worth it.
