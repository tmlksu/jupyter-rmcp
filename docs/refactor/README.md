# Architecture refactor — index + execution protocol

_This directory is the **handoff artifact** for a phased refactor of `mcp/server.py`,
executed by fresh-context agent sessions (one phase per session). Decision record:
[ADR 0013](../adr/0013-refactor-drop-tunnel-persistent-registry.md)._

## Phase table

| Phase | Spec | Status | Depends on |
|-------|------|--------|------------|
| 0 — remove tunnel backend | [PHASE-0-remove-tunnel.md](PHASE-0-remove-tunnel.md) | DONE 2026-07-20 | — |
| 1 — persistent kernel registry | [PHASE-1-persistent-registry.md](PHASE-1-persistent-registry.md) | DONE 2026-07-20 | 0 |
| 2 — package split + Backend protocol | [PHASE-2-package-split.md](PHASE-2-package-split.md) | DONE 2026-07-20 | 1 |
| 3 — colab session state machine | [PHASE-3-colab-state-machine.md](PHASE-3-colab-state-machine.md) | DONE 2026-07-20 | 2 |

## Why (the 4 root problems)

Recent bug history (2026-07-16..20) is dominated by Colab issues. They
all trace to four architectural problems in the 1800-line `mcp/server.py`:

1. **colab-cli stdout scraping.** Session discovery, loss detection, and timeout
   detection all regex/string-match the CLI's human-oriented output
   (`_COLAB_SESSION_RE`, `_colab_session_lost`, `"Timeout waiting for reply"`).
   The phantom-"colab"-kernel bug was a log line matching the session regex. Every
   CLI output change is a new bug. → quarantine parsing as pure, unit-tested
   functions (Phase 2), classify results in ONE place (Phase 3).
2. **Implicit routing with fallback-to-local.** `_backend_of()` returns `"local"`
   for any unknown kernel_id. A dead colab id once silently ran code on the LOCAL
   kernel (wrong env, root, no /content). The `_looks_colab()` name-prefix
   heuristic is a patch on that patch. → unknown id = explicit error (Phase 1).
3. **In-process state lost on restart.** `_kernel_backend`/`_first_seen`/
   `_kernel_notebook`/`_last_heartbeat`/`_pinned` are dicts; every mcp rebuild
   orphans colab sessions, kills the heartbeat, and forced "self-heal" hacks
   (`_ensure_tracked`, list_kernels re-adoption). → persistent registry (Phase 1).
4. **No backend abstraction.** Jupyter-WS execution and CLI-subprocess execution
   share single functions with `if kind == "cli"` branches (`_run_on_kernel` etc.).
   → `Backend` protocol, one implementation per backend (Phase 2).

## Target architecture (after Phase 2/3)

```
mcp/
  server.py            # thin: imports tool modules (registration) + uvicorn entry
  app.py               # auth + the FastMCP singleton + instructions (import root)
  config.py            # env parsing
  registry.py          # persistent kernel registry (JSON on a bind mount)
  outputs.py           # nbformat output -> (structured, text)
  backends/
    __init__.py        # Backend Protocol: start/execute/interrupt/restart/stop/list_live
    local_jupyter.py   # internal Jupyter (REST + jupyter-kernel-client)
    colab_cli.py       # ALL colab-cli invocation + stdout parsing (pure fns) + session states
  kernels.py           # kernel lifecycle tools (start/stop/restart/list/pin/...)
  notebook.py          # cell-id editing tools (ADR 0011)
  jobs.py              # background jobs (execute_code background=True, get_job)
  workspace.py         # contents/files tools
  reaper.py            # idle/max-age reaper + colab heartbeat
```

## Invariants (breaking one needs a spec amendment + user sign-off)

- **MCP tool surface is FROZEN** — names, arguments, result shapes — except the
  removal of `register_colab`/`unregister_colab` in Phase 0. The exact set is
  asserted in `tests/test_tool_surface.py` AND `scripts/smoke_test.py`
  (`EXPECTED_TOOLS`); both change only when a phase spec says so, in the same commit.
- Notebooks live on the server (`local` backend); remote backends are compute-only (ADR 0005).
- Jupyter stays internal-only; the MCP server is the only public surface (ADR 0002).
- **Each phase lands independently and leaves `main` green** (all gates below).
- No new runtime dependencies unless the phase spec lists them.

## Execution protocol (for the agent running a phase)

1. **Read, in order:** `CLAUDE.md` → `docs/GUIDE.md` → this file → your
   `PHASE-N.md` → the code sites it names. Do not start phase N+1 in the same
   session as phase N.
2. **Baseline (record results BEFORE editing anything):**
   ```bash
   git status                      # must be clean
   docker compose ps               # both services up, jupyter healthy
   .venv/bin/pytest -q             # green   (venv: see "Test harness" below)
   .venv/bin/ruff check .          # clean
   .venv/bin/python scripts/smoke_test.py   # SMOKE OK, exit 0
   ```
   If the baseline is red, STOP and fix/report that first — never start a phase
   on a broken base.
3. **Implement** the phase's Scope. Anything under Out-of-scope is forbidden even
   if tempting — later phases depend on the intermediate state.
4. **Gates** (all must pass; run in this order):
   - `.venv/bin/pytest -q` and `.venv/bin/ruff check .`
   - `docker compose up -d --build mcp` then
     `curl -s http://127.0.0.1:7130/health` → `{"status":"ok","jupyter":"ok"}`
   - `.venv/bin/python scripts/smoke_test.py` → exit 0
   - The phase's own acceptance criteria (grep checks, restart tests, ...)
   - **Live-colab gate** if the spec marks it REQUIRED (definition below)
5. **Docs ritual** (same session): record what changed in the project's history
   docs; flip the phase's Status header
   in its spec to `DONE <date> <commit>` and update the table above; write a new
   ADR ONLY if you deviated from the spec (0013 already covers the plan itself).
6. **Commit** as `refactor(phase-N): <what>` (a small series is fine). Push only
   after user confirmation (CLAUDE.md rule).

## Verification gates — live-colab gate definition

"Verified live on Colab" means, at minimum (phases may add steps):

1. `start_kernel(backend="colab")` returns a `rmcp-*` kernel on a real VM
   (CPU variant is fine unless the spec says otherwise — saves compute units).
2. Foreground `execute_code` returns `status:"ok"` with correct stdout.
3. `execute_code(background=True)` returns a job_id; `get_job` polls to `done`
   with the job's output visible.
4. `stop_kernel` destroys the session (`colab sessions` no longer lists it).

Required for Phases **1, 2, 3**. NOT required for Phase 0 (its acceptance
criteria prove the CLI path is untouched). Each live run costs compute units —
do one deliberate pass, not trial-and-error loops.

## Test harness

- **Unit tests:** `tests/` (pytest). Host venv is the ONLY supported way to run
  them — the mcp container image does not ship pytest or the tests:
  ```bash
  python3 -m venv .venv
  .venv/bin/pip install -r mcp/requirements.txt -r requirements-dev.txt
  .venv/bin/pytest -q && .venv/bin/ruff check .
  ```
- Unit tests NEVER touch the network or a live server; `tests/conftest.py` sets
  dummy env before importing `server`. If a test needs a server, it belongs in
  smoke, not here.
- **E2E:** `scripts/smoke_test.py` (assert-based, exit-coded) against the running
  stack on the deployment host. Local backend only.
- Tests marked `# CHANGES IN PHASE N` pin CURRENT behavior that phase N
  intentionally changes — that phase updates them as its spec directs. All other
  test changes during a phase are a red flag: justify or revert.
- CI (`.github/workflows/ci.yml`) runs ruff + pytest on every push/PR. It cannot
  run smoke (no server) — smoke is your job on the deployment host.

## Gotchas (verbatim warnings — each of these has already cost a session)

- **Rebuilding mcp kills colab-cli's in-container keep-alive daemon** → any live
  Colab VM idle-dies in ~10–15 min. Never verify against a session started before
  your last rebuild: rebuild FIRST, then run the live gate promptly.
- **Line numbers drift.** Phase specs cite line numbers as-of the commit named in
  their header; after earlier phases land they WILL be stale. Trust function
  names, not line numbers.
- `server.py` requires `JUPYTER_TOKEN` at import (conftest sets a dummy).
- `mcp/Dockerfile` copies **only `server.py`** — adding any new `.py` module
  requires widening that COPY (Phase 1 spec covers it) or the container silently
  runs without your new code.
- `jupyter-kernel-client` must use the `token` auth scheme, not `Bearer`
  (`_connect_client`) — do not "clean this up".
- `_live_colab_sessions` returning `None` (lookup failed) is DIFFERENT from
  `set()` (definitely no sessions). Collapsing them re-introduces the
  falsely-declared-dead bug fixed 2026-07-20.
- A `[?]` orphan colab session cannot be stopped by us (token lost) — only
  Colab's idle-reclaim clears it. Don't try.
- The tool-set asserts in pytest + smoke fail LOUDLY on any surface change —
  that is the design; never weaken them to subset checks.

## Known quirks (pre-existing; fix only where a phase says so)

- **Returned `rev` vs on-disk `rev`:** mutating notebook tools return a `rev`
  computed from the in-memory notebook, but the Jupyter Contents API normalizes
  on save, so the next read can hash differently — chaining a returned rev into
  the next `expected_rev` can spuriously fail (observed live 2026-07-20;
  smoke_test re-reads `notebook_rev` instead). Candidate fix in Phase 2
  (`notebook.py`): compute `rev` from a re-read after write, or normalize before
  hashing. Until then: clients should re-read rather than chain.
- A fresh kernel's first execute occasionally hit the full exec timeout
  (observed once, 2026-07-20, not reproduced). If smoke fails ONLY on
  "execute ok" right after a cold start, retry once before digging.
