# Changelog

Notable, user-visible changes. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this file starts at the first
public release — earlier history is the private development of the same code.

## [Unreleased]

### Added
- **`scripts/setup_cfzt.py` — scripted Cloudflare Zero Trust publishing.**
  Everything `docs/IAP-OAUTH.md` describes by hand (Access policy, application,
  Managed OAuth, tunnel, ingress, DNS record, connector, optional service token),
  done through the API by an idempotent stdlib-only script:
  `init` → `check` → `apply` → `connector` → `verify`. It builds the **gate
  before the door** — the Access policy and application exist before the DNS
  record does, so the hostname never resolves to an unauthenticated
  code-execution endpoint, which the dashboard's own ordering cannot avoid.
  `check` names the exact Cloudflare permission any missing token scope
  corresponds to, `apply` refuses to run while `MCP_BEARER` is set (the
  `Authorization` collision that makes this deployment half-work), and `verify`
  exits non-zero unless `/mcp` answers `401` **with** the `WWW-Authenticate`
  challenge the Claude app needs. See
  [ADR 0016](docs/adr/0016-scripted-cfzt-setup.md).
- `docs/deploy/cloudflare-zero-trust.md` — the runbook around that script for a
  host you already have, written to be executed by an agent with a human on hand
  for the browser-only steps.
- `docs/ja/cfzt.md` — a standalone Japanese guide that starts from what
  Cloudflare Zero Trust *is* and why a tunnel plus an identity gate is the shape,
  for colleagues who have never used it.
- `tests/test_setup_cfzt.py` — pins the request shapes whose absence fails open
  or fails silently: the full-body application `PUT` re-sending both policies and
  `oauth_configuration`, the `non_identity` decision a service token needs, the
  trailing catch-all ingress rule, and the DNS lookup filter.

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
