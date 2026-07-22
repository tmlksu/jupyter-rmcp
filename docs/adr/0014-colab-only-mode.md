# 0014 — COLAB_ONLY mode: a deployment that never executes code locally

**Status:** Accepted (2026-07-22)

## Context

The project is being generalized so other data scientists can each deploy their
own instance (see ADR 0015). Their main use case is Kaggle competitions with
Colab GPUs, and many of them will run the server on a work laptop. In that
setting "this MCP server can execute arbitrary code on the machine it runs on"
is the objection that blocks adoption, even though the compute they actually
want lives on a Colab VM under their own Google account.

The obvious shape — "a build without the local backend" — does not work. All
notebook and workspace I/O goes through the local Jupyter's Contents API
(`backends/local_jupyter.py`: `_read_nb`/`_write_nb`, `_ensure_notebook`,
`_resolve_notebook_path`), and notebooks are the durable record of every
session (ADR 0005, 0011). Removing the local backend would remove notebook
editing along with it, leaving only fire-and-forget execution.

## Decision

1. **`COLAB_ONLY=1` disables local *execution*, not the local backend.** The
   jupyter container stays: it stores the notebooks, serves JupyterLab, and
   answers every Contents API call. It simply never runs user code.
2. **Enforcement lives at two choke points**, not at each tool:
   `backends._resolve_backend` (the single execution-routing path — covers
   `execute_code`, `execute_cell`, `get_job`, *and* the branch that adopts
   unregistered local kernels a human started in JupyterLab) and `start_kernel`.
   `list_variables` gets the same guard because it talks to the kernel client
   directly. Lifecycle tools (`stop_kernel`, `restart_kernel`,
   `interrupt_kernel`) stay allowed on local kernels so survivors of a mode
   switch can be cleaned up.
3. **`start_kernel(backend=None)` resolves to the server default** — `colab` in
   colab-only mode, `local` otherwise — so clients need no per-call knowledge of
   the deployment mode. `list_backends` reports `exec_enabled` per backend, and
   the server `instructions` string swaps its backend paragraph.
4. **Fail loud, at boot.** A colab-only server whose colab backend is
   unavailable can execute nothing at all, so `server.py` exits at startup
   (checking that the ADC file actually exists, not merely that the env var is
   set) and `reaper._ensure_background` raises on first tool call.
5. **The tool surface does not change** (still 31 tools, frozen by
   `tests/test_tool_surface.py`). Mode changes behavior, never the API.

## Consequences

- Colleagues can state a real guarantee: no user code runs on their laptop; the
  only local process touching notebooks is a Jupyter server with no host port
  other than a loopback JupyterLab.
- Pre-existing local kernels are handled by refusal plus the normal idle reaper,
  not by a startup purge — switching modes never silently destroys state.
- `scripts/smoke_test.py` detects the mode from `/health` and swaps its
  local-execution checks for refusal checks, so the gate stays free to run
  (provisioning a Colab VM costs compute units; that round-trip stays manual).
- The flag is read as `config.COLAB_ONLY` (module attribute) rather than a
  from-import, so tests can monkeypatch a mode per case.
- A colab-only deployment is strictly slower for small work: every trivial
  execution pays Colab VM latency. That is the accepted price of the guarantee.
