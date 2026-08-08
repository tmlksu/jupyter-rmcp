# Changelog

Notable, user-visible changes. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this file starts at the first
public release — earlier history is the private development of the same code.

## [Unreleased]

### Fixed
- **A long execution no longer runs twice when the client gives up on it.** MCP
  clients abandon a tool call that doesn't answer in time, the agent reads that
  as a failure and resends the code, and a Jupyter kernel — which *queues* shell
  messages — dutifully ran it a second time: doubled variable updates, doubled
  file writes, a `pip install` racing itself. `execute_code` and `execute_cell`
  now hold a per-kernel execution slot; a second call while one is in flight
  returns `status: "busy"` and executes **nothing**, telling the agent how long
  the first has been running and, when the code matches, that this is its own
  retry. A caller that cancels does not release the slot (the work is still
  running), and if its identical retry arrives after the work finished, the
  recorded result is replayed once instead of re-executed. See
  [ADR 0017](docs/adr/0017-single-flight-execution.md).

- **A long execution always gets an answer, and is never killed to produce one.**
  The server used to spend up to `EXEC_TIMEOUT_SEC` (120 s) building a careful
  timeout reply for a client that had stopped listening 60 s earlier — so the
  agent saw a failed call and resent the code, and the 120 s cap meanwhile killed
  exactly the long jobs this server exists to host. The two clocks are now
  separate: `SOFT_REPLY_DEADLINE_SEC` (45 s) bounds the *reply*, after which the
  execution **detaches** and returns `{status: "still_running", exec_id,
  partial_output}` while it keeps running; `EXEC_TIMEOUT_SEC` bounds the *work* by
  interrupting the kernel, and now defaults to `0` (no cap). A `timeout` you pass
  yourself still interrupts. See
  [ADR 0018](docs/adr/0018-soft-reply-deadline.md).

### Added
- **`get_execution(kernel_id, exec_id=None, wait_seconds=0)` — what happened to
  the call that never came back.** Reports the execution running right now (with
  `partial_output`: what it has printed so far, live) or the last completed one
  (status, code head, output, timing, notebook write-back), flagging the ones
  whose caller had already stopped listening. `wait_seconds` long-polls, so one
  call replaces twenty. It reads in-process state with no kernel round-trip, so it
  answers *while* the kernel is busy — the moment it is actually needed. The
  local-kernel counterpart to `colab_log`. Tool surface: 31 → 32.
- `get_job(..., wait_seconds=20)` long-polls a background job the same way.
- `/health` reports `soft_reply_deadline_sec` and `exec_hard_timeout_sec`, so a
  deployment's actual execution policy is checkable from outside.
- Tool descriptions now spell out the protocol for a call that appears to fail —
  check `get_execution` first, resend only once you have confirmed it never ran —
  on `execute_code`, `execute_cell`, `get_job`, `start_kernel` (check
  `list_kernels` before starting a second kernel) and `interrupt_kernel`.
- `tests/test_singleflight.py` — pins the behavior that silently breaks: the
  second call executing nothing, the claim being released down every exit path
  (ok / error / timeout / session_lost / cancelled caller), a cancelled caller's
  retry still being refused, and the soft deadline detaching rather than killing.

### Changed
- **`EXEC_TIMEOUT_SEC` now defaults to `0` (no hard cap) and its meaning is
  narrower**: it is the deadline at which the kernel is *interrupted*, not the one
  at which you get a reply. **Upgrade note:** an existing `.env` pinning
  `EXEC_TIMEOUT_SEC=120` keeps interrupting long executions at two minutes — set
  it to `0` to get the detach-and-keep-running behavior.

## [1.0.0] — 2026-07-22

First public release.

### Added
- **`COLAB_ONLY=1` deployment mode.** Refuses to execute code on the local
  backend: every kernel runs on a Colab VM under the operator's own Google
  account, while the local Jupyter continues to store and serve the notebooks.
  `start_kernel` defaults to `backend="colab"` in this mode, `list_backends`
  reports `exec_enabled` per backend, and the server refuses to start if it has
  no usable Colab credentials. See [ADR 0014](docs/adr/0014-colab-only-mode.md).
- `docs/GUIDE.md` — end-to-end walkthrough for Kaggle competitions on a Colab
  GPU, including the VM lifecycle rules and troubleshooting.
- `docs/IAP-OAUTH.md` — runbook for putting an identity-aware proxy with OAuth in
  front of the server so the Claude mobile app can connect, worked through on
  Cloudflare Zero Trust (tunnel, Access policies, Managed OAuth, service tokens)
  with the verification steps and the two failure modes that waste the most
  time: a `PUT` that silently drops your policies, and a service token attached
  to an identity policy, where it is inert.
- **Slim notebook-store image** (`jupyter/Dockerfile.slim` +
  `compose.slim.yaml`): 261 MB instead of 1.29 GB, for `COLAB_ONLY`
  deployments on small hosts. In colab-only mode the local Jupyter never runs a
  kernel, so it needs neither a kernel nor a scientific stack — only the
  Contents API and JupyterLab. With it the whole stack idles at ~195 MB, which
  is what makes a 1 GB VM viable. Not usable without `COLAB_ONLY=1`.
- `docs/deploy/gce-cloudflare.md` — runbook for deploying on a Google Compute
  Engine free-tier `e2-micro` behind a Cloudflare tunnel, written to be executed
  by an agent: per-phase verification, explicit stop points for browser-only
  steps, guardrails, and the free-tier limits that actually bite (1 GB egress,
  possible external-IPv4 charges, build OOM without swap).
- `docs/ja/setup.md` — the account, billing and API prerequisites in Japanese,
  for colleagues setting up their own instance.
- `MCP_UID` / `MCP_GID` for hosts whose account is not uid/gid 1000.
- MIT `LICENSE`.

### Fixed
- An environment variable set to the **empty string** now falls back to its
  default instead of crashing the server at import. Compose interpolates any key
  missing from `.env` into an empty value, so a minimal `.env` — the normal case
  for a new deployment — made `EXEC_TIMEOUT_SEC` and friends raise
  `ValueError: could not convert string to float: ''`. An empty `JUPYTER_TOKEN`
  now fails with an explanation rather than starting in a broken state.

### Changed
- `scripts/smoke_test.py` detects the deployment mode from `/health` and swaps
  its local-execution checks for refusal checks, so it stays free to run against
  a colab-only server.
- `/health` reports `colab_only`.
- The Colab backend is now offered only when the credentials file actually
  exists, not merely when the environment variable is set.
- **`GCLOUD_ADC` must be set explicitly** to use Colab. It previously defaulted
  to a path in `compose.yaml`; the default is now `/dev/null`, so an instance
  that relied on the old one silently loses the Colab backend — or, with
  `COLAB_ONLY=1`, refuses to start. Put the absolute path to the file
  `gcloud auth application-default login` writes into `.env`.
- Documentation and configuration were generalized for public use: auth is
  documented as "set `MCP_BEARER`, or put an authenticating proxy in front"
  rather than assuming one specific provider, and `scripts/install.sh` generates
  a bearer token by default.
