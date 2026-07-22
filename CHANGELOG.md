# Changelog

Notable, user-visible changes. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this file starts at the first
public release — earlier history is the private development of the same code.

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
- `MCP_UID` / `MCP_GID` for hosts whose account is not uid/gid 1000.
- MIT `LICENSE`.

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
