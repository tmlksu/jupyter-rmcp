# PHASE 0 — remove the tunnel Colab backend

**Status:** DONE 2026-07-20
**Line references as-of:** commit `f9f4266` (they drift — trust function names).
**Depends on:** nothing. **Live-colab gate:** NOT required.

## Context

Two Colab paths exist: the official colab-cli (primary, `kind=="cli"`) and a
Cloudflare-quick-tunnel path (`register_colab` → a `_backends` entry with url+token,
`kind` defaulting to `"jupyter"`). The tunnel path is **unused** (user confirmed
2026-07-20; ADR 0013). It is thin but it widens the attack/bug surface and adds a
third backend flavor to every future refactor step. Remove it first — it's the
cheapest simplification and shrinks Phases 1–2.

Key fact: the tunnel path is NOT a separate code path — it rides the LOCAL
jupyter machinery (`_http`, `_kernels`, `_reap_backend`, `list_kernels` jupyter
branch, `_run_on_kernel` local branch). Those SHARED parts must stay.

## Scope (delete)

1. `@mcp.tool register_colab` (`mcp/server.py` ~1446–1469).
2. `@mcp.tool unregister_colab` (~1497–1509).
3. The reaper's dead-registered-backend auto-drop in `_reaper_loop` (~544–549):
   the `if backend != LOCAL and _backend_kind(backend) == "jupyter"` block inside
   the exception handler. With the tunnel gone it can never fire.
4. `colab/bootstrap.py` and the `colab/` directory (also remove its `extend-exclude`
   entry in `pyproject.toml` `[tool.ruff]`).
5. Docs scrub — remove/adjust every tunnel reference:
   - `docs/COLAB.md`: tunnel sections (keep the colab-cli content).
   - `README.md` (~lines 36, 68), `CLAUDE.md` (~42, 51), `docs/REINSTALL.md` (~111–112).
   - `docs/HANDOFF.md`: tunnel bullet in Current state, the register_colab gotcha,
     the "keep both Colab paths?" open question (answered: no — ADR 0013).
   - `docs/adr/0007-colab-offload-official-cli.md`: note under Status —
     "tunnel alternative dropped 2026-07 (ADR 0013)". Same note in ADR 0010 if it
     mentions the tunnel.
6. Update the tool-surface freezes **in the same commit**:
   - `tests/test_tool_surface.py`: remove the two names; `test_tool_count` → 31.
   - `scripts/smoke_test.py` `EXPECTED_TOOLS`: remove the same two names.

## Keep (do NOT touch)

- `list_backends` (the `kind=="jupyter"` branch serves `local`).
- `_backends` / `_http_clients` / `_backend_kind` / `_http` machinery.
- `_run_on_kernel`'s local/jupyter branch (update its "LOCAL / tunnel jupyter"
  comment to just LOCAL, that's all).
- Everything on the colab-cli path (`_colab_cli`, `_live_colab_sessions`,
  heartbeat, `_reap_cli_backend`, ...).
- `_looks_colab`'s docstring mentions tunnel kernels — fix the wording only.

## Out of scope

Registry, package split, any behavior change on local or colab-cli paths,
dependency changes.

## Acceptance criteria

1. `grep -rni "register_colab\|unregister_colab\|bootstrap.py\|quick tunnel" --include='*.py' --include='*.md' . | grep -v docs/adr/ | grep -v CHANGELOG` → only hits in `docs/refactor/` (this spec) — none in code or live docs. `colab/` directory gone.
2. `.venv/bin/pytest -q` green with the 31-name surface.
3. `docker compose up -d --build mcp` → `/health` ok → `.venv/bin/python scripts/smoke_test.py` exits 0 (with its 31-name `EXPECTED_TOOLS`).
4. `git diff` contains **no hunks** inside `_colab_cli`, `_live_colab_sessions`,
   `_reap_cli_backend`, `_heartbeat_colab`, or the `kind=="cli"` branch of
   `_run_on_kernel` (comment-only edits exempt). This is what makes the
   live-colab gate unnecessary.
5. `ruff check .` clean.

## Rollback

Single `git revert` of the phase commit(s); no state/format changes involved.
