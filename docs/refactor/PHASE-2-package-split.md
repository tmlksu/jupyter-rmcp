# PHASE 2 — package split + Backend protocol (quarantine the CLI scraping)

**Status:** DONE 2026-07-20 (commit pending)
_Landed as specced. Two module homes the Phase-1 notes left open were added:
`util.py` (`_now`/`_parse_ts`/`_parent_dir`) and `state.py` (the shared singletons
+ `_backend_of`/`backend_kind`) — both anticipated by the "pick a home" / "ONE state
home" notes, so no ADR. The `kind=="cli"` branching is hidden behind
`backends.is_cli`/`is_colab_kernel` (the only `== "cli"` comparison lives in
`backends/__init__.py`); `_run_on_kernel` dispatches to `local_jupyter.exec_local` /
`colab_cli.exec_colab`. The optional returned-`rev` fix was NOT taken (kept the phase a
pure move-and-split). Live-colab gate: one background-launch hit colab-cli's 90s
exec-reply timeout on a cold VM (flaky, README known quirk) and passed cleanly on a
fresh-session retry; all other seams passed first try._
**References are FUNCTION NAMES only** — Phases 0/1 shifted all line numbers.
**Depends on:** Phase 1. **Live-colab gate:** REQUIRED (full).

## Context

`mcp/server.py` mixes config, auth, routing, REST helpers, output formatting,
reaper, jobs, notebook editing, workspace tools, and 31 tool definitions in one
file, with `if _backend_kind(...) == "cli"` branches inside shared functions
(`_run_on_kernel`, `list_kernels`, reaper). All colab-cli stdout parsing
(`_COLAB_SESSION_RE`, `_colab_session_lost`, the `"Timeout waiting for reply"`
check in `_run_on_kernel`) is scattered and only testable through monkeypatching.

## Target layout

```
mcp/
  server.py            # thin (≤ ~150 lines): imports tool modules + health route + uvicorn
  app.py               # auth + `mcp = FastMCP(...)` + instructions string (IMPORT ROOT)
  config.py            # all env parsing (JUPYTER_URL, timeouts, MAX_*, ...)
  registry.py          # from Phase 1 (unchanged interface)
  outputs.py           # _format_outputs, _strip_ansi (zero-dependency, pure)
  backends/
    __init__.py        # Backend Protocol: start/execute/interrupt/restart/stop/list_live
    local_jupyter.py   # REST helpers (_kernels/_sessions/_rest_post/...), KernelClient mgmt
    colab_cli.py       # _colab_cli subprocess + ALL stdout parsing as SYNC PURE functions:
                       #   parse_sessions(text) -> set[str], is_session_lost(text) -> bool,
                       #   is_reply_timeout(text) -> bool  (no I/O, no env, no event loop)
  kernels.py           # lifecycle tools: start/stop/restart/interrupt/list/pin/colab_log
  notebook.py          # ADR 0011 editing tools + helpers (_rev/_ensure_ids/...)
  jobs.py              # _job_launch_code, execute_code, get_job
  workspace.py         # contents tools: list_files/create_folder/upload/fetch/notebooks
  reaper.py            # _reaper_loop, _enforce_capacity, heartbeat
```

Registration pattern: tool modules do `from app import mcp` and decorate at import
time; `server.py` imports them all for side effect. Circular-import rule:
`app.py`/`config.py` import NOTHING from tool modules. `outputs.py` and the
parsing functions in `backends/colab_cli.py` import nothing async.

`mcp/Dockerfile`: COPY the whole package (e.g. `COPY . .` with a `.dockerignore`,
or explicit `COPY *.py backends/ ./`).

## Scope notes

- This is a MOVE-AND-SPLIT phase: behavior-preserving except where the Backend
  protocol forces the `kind=="cli"` branches out of shared functions. Resist
  drive-by fixes; log them in HANDOFF instead.
- Optional (recommended, small): fix the returned-`rev` vs on-disk-`rev` quirk
  (see README "Known quirks") in `notebook.py` by re-reading after write before
  computing the returned rev. If done, extend smoke to chain revs again and note
  it in CHANGELOG.
- Existing unit tests must keep passing **with only import-path edits**
  (e.g. `server._format_outputs` → `outputs.format_outputs`); assertions stay
  byte-identical. Update `pyproject.toml` `known-first-party` accordingly.

## Acceptance criteria

1. Tool surface: `tests/test_tool_surface.py` still asserts the exact 31-name set,
   green. Also diff descriptions: dump `[(t.name, t.description) for t in tools]`
   via the in-memory client on `main` and on your branch — identical.
2. All pre-existing unit tests green with only import-path edits (no assertion
   changes; `git diff tests/` shows imports/monkeypatch-targets only).
3. `grep -rn 'kind == "cli"\|kind=="cli"' mcp/ --include='*.py'` → hits only under
   `mcp/backends/`.
4. `grep -rn "subprocess\|create_subprocess" mcp/ --include='*.py'` → colab
   invocation only in `backends/colab_cli.py`.
5. Parsing functions (`parse_sessions`, `is_session_lost`, `is_reply_timeout`)
   are importable and testable with no env vars, no event loop — add direct unit
   tests against the `tests/fixtures/colab_sessions/` files (replacing the
   monkeypatch-based indirection where sensible). Also: `parse_sessions` should
   now strip ANSI first — flip `test_leading_ansi_breaks_the_match` accordingly.
6. New `mcp/server.py` ≤ ~150 lines.
7. `pyproject.toml`: the `mcp/server.py` per-file-ignores block is DELETED and
   `ruff check .` passes on the whole tree.
8. Rebuild + smoke exit 0.
9. **Live-colab gate (REQUIRED, full):** standard gate PLUS
   `upload_to_colab`/`download_from_colab` round-trip and `setup_kaggle` on the
   live session (these cross the backend seam being refactored).

## Notes from the Phase-1 session (landed 2026-07-20, commit dcdd13b)

Concrete, non-obvious items the split will hit — read before starting:

- **Shared mutable state needs ONE home (the hardest part).** These live as
  `server.py` module globals today and are read/written by would-be-separate
  modules (kernels, jobs, reaper, backends, routing): `_registry` (the Phase-1
  `KernelRegistry` singleton), `_clients`, `_locks`, `_backends`,
  `_http_clients`, `COLAB_AVAILABLE`. The target layout names no state module —
  decide one (e.g. `state.py`, imported by everyone) so all modules mutate the
  SAME objects. Do NOT re-instantiate per module. `_registry` is created at
  import from `MCP_STATE_DIR`; keep that a single instance.
- **Test import-path map (criterion 2 — assertions stay byte-identical):**
  - `test_misc`: `_parse_ts`, `_parent_dir` (no util module exists — pick a home),
    `_backend_of`, `LOCAL`.
  - `test_outputs`: `_strip_ansi`, `_format_outputs` → `outputs.py`; but
    `MAX_OUTPUT_CHARS` is config → test imports from TWO modules.
  - `test_notebook_helpers`: `_norm_source/_rev/_ensure_ids/_new_cell/_cell_summary/
    _resolve_target/_check_rev` → `notebook.py`.
  - `test_jobs`: `_job_launch_code` → `jobs.py`.
  - `test_registry`: unchanged (imports `registry`).
  - `test_tool_surface`: does `Client(server.mcp)` — keep `server.mcp` valid, e.g.
    `server.py` does `from app import mcp` (re-export) so the surface freeze works.
- **criterion 2 vs 5 tension in `test_colab_parsing`:** its `_live_colab_sessions`
  async tests stay monkeypatch-based (import-path edit only); the pure-parsing
  tests (`_COLAB_SESSION_RE`, `_colab_session_lost`) become direct calls to
  `backends/colab_cli.parse_sessions/is_session_lost/is_reply_timeout`. That file
  is a partial rewrite, not pure import edits — that's expected, per criterion 5.
- **pyproject:** add every new module to `[tool.ruff.lint.isort] known-first-party`
  (`app, config, outputs, registry, kernels, notebook, jobs, workspace, reaper,
  backends`) or isort will reorder imports wrongly. Also set
  `pytest pythonpath`/import roots so `from app import mcp` resolves (pythonpath is
  `["mcp"]` today — a subpackage `backends/` under it works as `import backends...`).
- **The 4 legacy per-file-ignores are DELETED (criterion 7) — two are real code
  fixes, not free:** `E402`/`I001` vanish once each module has a clean import top;
  `UP017` = replace `dt.timezone.utc` → `dt.UTC`; `UP041` = `asyncio.TimeoutError`
  → `TimeoutError`. Grep server.py for both. (registry.py already uses `dt.UTC`.)
- **Dockerfile:** build context is `./mcp` (compose `build: ./mcp`), so all new
  modules must sit under `mcp/`. `COPY *.py .` (Phase-1) will NOT copy `backends/`
  — change to `COPY . .` (add a `.dockerignore` for `__pycache__`) or add an
  explicit `COPY backends ./backends`. Keep `COPY requirements.txt` first for layer
  caching. **If a module doesn't get copied, the container silently runs stale code.**
- **Phase-1 routing you'll be moving:** `_resolve_backend` (explicit, async, raises
  — the execution router) and the `_backend_of(...) or LOCAL` safe-default sites in
  `restart_kernel`/`interrupt_kernel`/`_get_client`/`_run_on_kernel` timeout path.
  `_backend_of` now returns `None` for unknown ids (no silent LOCAL fallback).
- **Live-colab gate is FULL (criterion 9):** standard gate PLUS upload/download
  round-trip + `setup_kaggle` on the live session. Budget ONE Colab CPU session;
  rebuild FIRST (a rebuild kills colab-cli's keep-alive), then run promptly.
- Baseline before starting: `git status` clean, `.venv/bin/pytest -q` (75),
  `ruff check .`, `scripts/smoke_test.py` exit 0 — all green at dcdd13b.

## Rollback

`git revert` the series; no data-format change (kernels.json untouched).
