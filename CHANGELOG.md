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

### Added
- **`get_last_execution(kernel_id)` — what happened to the call that never came
  back.** Reports the execution running right now, or the last completed one
  (status, code head, output, timing), flagging the ones whose caller had already
  stopped listening. It reads in-process state with no kernel round-trip, so it
  answers *while* the kernel is busy — the moment it is actually needed. The
  local-kernel counterpart to `colab_log`. Tool surface: 31 → 32.
- `tests/test_singleflight.py` — pins the behavior that silently breaks: the
  second call executing nothing, the claim being released down every exit path
  (ok / error / timeout / session_lost / cancelled caller), and a cancelled
  caller's retry still being refused.

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
