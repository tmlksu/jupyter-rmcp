# 0013 — Architecture refactor: drop tunnel backend; persistent registry + backend split

**Status:** Accepted (2026-07-20)

## Context

The 2026-07 bug streak (phantom "colab" kernel, silent local fallback for dead
colab ids, fake background jobs, heartbeat death on rebuild, sessions stuck
"running") all traces to four structural problems in the ~1800-line
`mcp/server.py`: (1) colab-cli stdout scraping scattered through the code,
(2) implicit kernel→backend routing that falls back to `local` for unknown ids,
(3) all kernel-tracking state in in-process dicts wiped by every mcp restart,
(4) no backend abstraction (`if kind == "cli"` branches inside shared functions).
Separately, the tunnel-Colab path (`register_colab`, `colab/bootstrap.py`) is
unused — the official colab-cli is the sole Colab path in practice (user
confirmed 2026-07-20).

## Decision

1. **Drop the tunnel Colab backend entirely** (amends ADR 0007's "tunnel as
   alternative"): remove `register_colab`/`unregister_colab` and
   `colab/bootstrap.py`. colab-cli is the only Colab path.
2. **Target architecture:** a persistent JSON kernel registry (bind-mounted;
   unknown kernel_id = explicit error, no local fallback), a `Backend` protocol
   with one implementation per backend (`local_jupyter`, `colab_cli` — the
   latter quarantining ALL CLI invocation + stdout parsing as pure, unit-tested
   functions), a package split of the monolith, and an explicit colab session
   state machine (`starting → live → lost|stopped`) with one result classifier.
3. **Execution model:** four independently-landable phases, each run by a
   fresh-context agent session driven by the specs in `docs/refactor/`
   (docs-only handoff, no issue tracker). Every phase leaves `main` green:
   pytest + ruff + assert-based smoke_test, plus a live-Colab gate where the
   spec requires it.
4. **The MCP tool surface is frozen** for the whole refactor, except the removal
   of the two tunnel tools in Phase 0. The exact tool set is asserted in
   `tests/test_tool_surface.py` and `scripts/smoke_test.py`.

## Consequences

- `register_colab`/`unregister_colab` disappear from the tool surface (Phase 0);
  clients keep working — the tools were unused.
- A new bind mount (`./data/mcp-state`) and `kernels.json` become part of the
  deployment (Phase 1); old code ignores them, so rollback is a plain revert.
- The self-heal hacks (`_ensure_tracked`, list_kernels re-adoption) become
  redundant and are retired as the registry/state machine land.
- Test/lint/CI infrastructure (pytest characterization suite, ruff, GitHub
  Actions) is now a standing gate; the legacy per-file lint ignores on
  `mcp/server.py` are deleted in Phase 2.
- `docs/refactor/` is temporary scaffolding: when all phases are DONE, its
  content collapses into DESIGN.md + this ADR trail.
