# jupyter-rmcp — Architecture & Session Management

## Topology

```
Claude app (incl. mobile)
      │  HTTPS (Streamable HTTP MCP)
      ▼
Authenticating proxy / tunnel  ─────►  MCP host : 127.0.0.1:7130
                                                 │
                                          ┌──────┴───────┐  compose net "internal"
                                          │   mcp (FastMCP)   ← internet-facing surface
                                          │      │ REST + WS (JUPYTER_TOKEN)
                                          │   jupyter (Jupyter Server)  ← INTERNAL-ONLY, no host port
                                          └──────────────┘
```

Only the MCP server is reachable from outside. Raw Jupyter (arbitrary code exec)
is never exposed to the internet. Defense-in-depth: an authenticating proxy in
front (identity) and/or the app-level `MCP_BEARER` — set one of them, ideally
both (see [docs/AUTH.md](docs/AUTH.md)).

## Core objects

- **Kernel** — an interpreter process. Jupyter identifies it by `kernel_id` (uuid).
  Carries `name` (kernelspec, default `python3`), `last_activity`, `execution_state`
  (`idle`/`busy`/`starting`), `connections`. State (variables) persists across MCP
  calls — this is the whole point.
- **Notebook session** — Jupyter's `/api/sessions` binds a notebook `path`
  (e.g. `work/analysis.ipynb`) ↔ one kernel, with a human `name`. This is how we
  **distinguish multiple concurrent notebooks** and give kernels meaningful identity.
- **Addressing** — MCP tools accept either a `kernel_id` (precise) or a notebook
  `path` (resolved to its session's kernel, created lazily). Clients are encouraged
  to address by notebook path; raw kernels are for scratch use.

## Kernel lifecycle (MCP tools → Jupyter API)

| Action     | Jupyter call                                  |
|------------|-----------------------------------------------|
| list       | `GET /api/kernels`, `GET /api/sessions`        |
| start bare | `POST /api/kernels {name}`                      |
| start nb   | `POST /api/sessions {path,name,type:notebook,kernel:{name}}` |
| execute    | WS `/api/kernels/{id}/channels` (see below)     |
| interrupt  | `POST /api/kernels/{id}/interrupt`              |
| restart    | `POST /api/kernels/{id}/restart`                |
| stop       | `DELETE /api/kernels/{id}` (also drops session) |

Auth on every call: `Authorization: token <JUPYTER_TOKEN>` (header form; also
works as `?token=` on the WS URL).

## Execution + output capture

Per `execute_code`:
1. Open WS to `/api/kernels/{id}/channels`.
2. Send `execute_request` with a fresh `msg_id`; remember it.
3. Collect iopub messages whose `parent_header.msg_id` == our `msg_id`:
   - `stream` → stdout/stderr text (concatenated, order-preserving)
   - `execute_result` / `display_data` → mimebundle. Return `text/plain`; for
     `image/png` etc. return a marker `[image/png N bytes]` (base64 optionally
     included, size-capped) so mobile text stays readable.
   - `error` → `{ename, evalue, traceback}`
4. Completion = received the shell-channel `execute_reply` (status ok/error,
   `execution_count`) **and** the iopub `status: idle` for our msg_id.
5. Serialize concurrent executes to the **same** kernel with a per-kernel async
   lock (Jupyter queues them anyway; the lock keeps our output correlation clean).

### Single-flight admission (ADR 0017)
The lock above serializes; it does **not** decide whether a second execution
*should* happen. That question matters because an MCP client abandons a call it
finds slow, the agent resends the code, and a queued duplicate is how "the long
job ran several times and broke" happens.

So `execute_code` and `execute_cell` claim a per-kernel slot (`state._inflight`)
for the whole call. A second one arriving meanwhile returns
`{status: "busy", running: {exec_id, code_head, elapsed_seconds}, is_retry}` and
runs **nothing**. A cancelled caller keeps holding the slot — the work is shielded
and still running — and releases it from a done-callback when it truly ends;
releasing early would just move the double-execution one step later.

Each completed foreground execution is remembered (`state._last_exec`, one per
kernel, in memory only) so `get_last_execution` can hand back the result of a
call whose client stopped listening — and, if that exact code is resent within
5 minutes, replay it once instead of running it again. Background jobs release
the slot as soon as the detached process is spawned.

### Per-execution timeout
Each execute has a timeout (tool arg, default `EXEC_TIMEOUT_SEC`). On expiry:
1. `POST /interrupt` the kernel,
2. wait a short grace for the `error`/`reply`,
3. return `{status: "timeout", outputs: <partial>, timed_out: true}`.
Never block indefinitely.

**Implementation note (verified empirically):** `jupyter-kernel-client` /
`jupyter_client`'s own `timeout` argument does **not** bound total execution time
(a `sleep(20)` under `timeout=3` still ran the full 20 s). So the deadline is
enforced at the asyncio layer: the blocking `KernelClient.execute` runs in a
worker thread wrapped in `asyncio.wait_for`; the thread is `shield`-ed (threads
can't be cancelled), and on timeout we `POST /interrupt`, then wait a 15 s grace
for the `KeyboardInterrupt` to unwind — holding the per-kernel lock throughout so
no concurrent execute touches the same client. Interrupted runs return the
captured `KeyboardInterrupt` traceback with `status: "timeout"`.

## Kernel reaper (Colab-style: idle timeout + absolute max age)
Background task in the MCP server (single source of truth; Jupyter's own culler
is disabled). Runs every `REAPER_INTERVAL_SEC` (default 60 s) over `GET /api/kernels`:

1. **Absolute max-age hard cap** — `KERNEL_MAX_AGE_SEC` (default 8 h; 0 disables).
   Reap a kernel once `now - first_seen > MAX_AGE`, **regardless of state or pin**.
   This is the Colab-like "it turns off after a while" guarantee — even a kernel
   kept warm by periodic activity dies here. `first_seen` is tracked in-process
   (set at `start_kernel`, or on first sighting for pre-existing kernels).
2. **Idle timeout** — `KERNEL_IDLE_TIMEOUT_SEC` (default 1 h). Reap kernels that
   are `execution_state == idle` and `now - last_activity > IDLE_TIMEOUT`.
   `pin_kernel` exempts a kernel from *this* rule only (not the max-age cap).

Note: idle is based on actual kernel **execution** activity, not MCP transport
traffic or open connections (`cull_connected` is off), so a lingering client
connection does not keep a kernel alive. `list_kernels` reports both
`idle_seconds` and `age_seconds` so you can see where each kernel stands.
All three knobs (`KERNEL_MAX_AGE_SEC`, `KERNEL_IDLE_TIMEOUT_SEC`,
`REAPER_INTERVAL_SEC`) are env-configurable.

## Capacity
`MAX_KERNELS` caps concurrent kernels. On `start` at cap: reap the oldest idle
kernel if any, else return a clear error. Prevents a phone tap from exhausting
the VPS.

## Notebook persistence (implemented)
Two independent lifetimes — don't conflate them:
- **Kernel** = process + in-memory variables. Reaped by idle/max-age. Ephemeral.
- **Notebook `.ipynb`** = file in `./data/notebooks` (bind-mounted). **Never
  auto-deleted** by the reaper; survives kernel death, container rebuilds, VPS
  swaps (if you back up the dir).

When `execute_code` runs against a **notebook-bound** kernel (started with
`notebook_path`), the executed cell (source + raw nbformat outputs, incl. images)
is appended to that `.ipynb` via the Contents API. So the notebook becomes a
durable record of the session: after the kernel is reaped you can still
`read_notebook` it or open it in JupyterLab, and re-bind a fresh kernel to keep
going (variables are gone — re-run cells, Colab-style). The result includes
`saved_to`. **Bare kernels** (no `notebook_path`) are scratch — not written back.

## Security posture (v1)
- Jupyter internal-only, non-root (`jovyan` uid 1000), `mem_limit 2g`, `cpus 2.0`.
- No Docker socket, no host mounts except `./data/notebooks`.
- MCP behind an authenticating proxy and/or `MCP_BEARER` checked in-app.
- Arbitrary code execution is the *feature*, so the trust boundary is whatever
  gates the MCP endpoint — keep the connector's access policy tight (just you).
  A `COLAB_ONLY=1` deployment removes local execution entirely (ADR 0014).
