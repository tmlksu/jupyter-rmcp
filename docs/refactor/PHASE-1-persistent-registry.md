# PHASE 1 — persistent kernel registry (kill the restart-amnesia bug class)

**Status:** DONE 2026-07-20
**Function references:** current as of Phase 0 landing — locate by NAME, line
numbers in older docs are stale. **Depends on:** Phase 0. **Live-colab gate:** REQUIRED.

## Context

All kernel tracking lives in six in-process dicts (`_kernel_backend`,
`_first_seen`, `_kernel_notebook`, `_last_heartbeat`, `_pinned`, plus `_locks`),
declared near the top of `mcp/server.py`. Every mcp restart/rebuild wipes them,
which caused: colab sessions orphaned (`_ensure_tracked` self-heal hack),
heartbeat gaps → VM idle-reclaim, and — worst — the implicit
`_backend_of() -> "local"` fallback that once ran a dead colab id's code on the
LOCAL kernel. The dicts' write sites are concentrated in exactly five functions:
`_forget_colab`, `_delete_kernel`, `_ensure_tracked`, `start_kernel`,
`list_kernels` (readers: reaper, `list_kernels`). That concentration is the seam.

## Design decisions (fixed — amend the spec if you must deviate)

- **JSON file, not SQLite.** Single-writer process, tiny data, `cat`-debuggable,
  zero new deps. Atomic writes: write to `<file>.tmp` then `os.replace`.
- Storage: new bind mount `./data/mcp-state:/state` in `compose.yaml` (mcp
  service) + env `MCP_STATE_DIR=/state`; registry file `<MCP_STATE_DIR>/kernels.json`.
  Default `MCP_STATE_DIR` to a sane local path so tests/dev work without the mount.
  Add `data/mcp-state/` to `.gitignore`.
- **`mcp/Dockerfile` must change `COPY server.py .` to `COPY *.py .`** — today it
  copies only server.py; without this the container silently runs without
  `registry.py`.
- `asyncio.Lock`s stay in-memory (they are per-process by nature).

## Scope

1. New `mcp/registry.py`: `KernelRegistry` with
   `track(kernel_id, backend, notebook=None)`, `forget(kernel_id)`,
   `get_backend(kernel_id) -> str | None`, `set_notebook(...)`,
   `touch_heartbeat(...)`, `pin(...)/unpin(...)`, `first_seen(...)`,
   `all() -> dict`. Persists on every mutation; loads once at construction;
   corrupt/absent file → clean empty start (log a warning, don't crash).
2. Replace the six dicts' read/write sites with registry calls (the five writer
   functions above + readers `_reap_cli_backend`, `_reap_backend`,
   `_enforce_capacity`, `_heartbeat_colab`, `pin_kernel`, `_notebook_for_kernel`,
   `_run_on_kernel`'s heartbeat write).
3. **Routing becomes explicit** — `_backend_of` (or its replacement) returns
   `None` for an unknown kernel_id:
   - unknown + `_looks_colab(kernel_id)` → the existing `session_lost`-style
     error (nothing runs);
   - unknown UUID → verify against the LOCAL Jupyter API (`/api/kernels`); if it
     exists there, ADOPT it into the registry (covers kernels started by
     JupyterLab humans / pre-registry survivors); else clear "not found" error.
   - A startup reconcile (first `_ensure_background` or reaper tick) replaces the
     per-call `list_kernels` `setdefault` self-heal for local kernels.
4. Keep `_ensure_tracked` for now BUT registry-backed (it re-adopts live colab-cli
   sessions the registry doesn't know — still possible if kernels.json is deleted).
   Phase 3 decides its retirement.

## Out of scope

Package split (everything stays in server.py + the new registry.py), colab-cli
parsing changes, tool-surface changes, state-machine work.

## Acceptance criteria

1. Unit (new `tests/test_registry.py`, using `tmp_path` — no server import needed
   if registry.py is standalone):
   - track → new instance from same path → identical contents (round-trip).
   - `get_backend("nope") is None`.
   - corrupt file (garbage bytes) and absent file → empty registry, no raise.
   - after every mutation the on-disk file is valid JSON (atomicity smoke:
     no `.tmp` residue).
2. Replace `tests/test_misc.py::TestBackendRouting::test_backend_of_falls_back_to_local`
   with `test_unknown_kernel_is_not_local` asserting the new explicit behavior
   (the old test carries a `# CHANGES IN PHASE 1` marker pointing here).
3. **Restart test (local):** `start_kernel(notebook_path="restart-probe.ipynb")` →
   `docker compose restart mcp` → `list_kernels` shows the SAME kernel_id with
   backend/notebook/pin intact, `execute_code` on it works, and the mcp log has
   NO "self-healed" line. Clean up the kernel after.
4. `docker exec jrmcp-mcp cat /state/kernels.json` is valid JSON and visibly
   changes on start_kernel/pin_kernel/stop_kernel.
5. **Live-colab gate (REQUIRED):** `start_kernel(backend="colab")` (CPU variant ok)
   → full rebuild `docker compose up -d --build mcp` → within ~10 min:
   `list_kernels` still routes the session to colab **from the registry** (no
   self-heal log line), reaper log shows heartbeat resuming, `execute_code` works,
   then the standard live gate steps (background job, stop_kernel).
6. `pytest -q`, `ruff check .`, smoke exit 0.

## Rollback

`git revert` the series. Old code ignores `kernels.json` (and the extra mount is
harmless), so no data migration in either direction.
