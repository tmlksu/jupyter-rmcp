# 0012 — Expose JupyterLab to a human, behind Cloudflare Access

**Status:** Accepted (2026-07-13) · relaxes [0002](0002-jupyter-internal-only.md)

## Context
Humans need to view and edit the notebooks the agent produces. The best notebook
UI is JupyterLab — and the internal jupyter container already runs a full Lab over
`data/notebooks`. ADR 0002 kept Jupyter internal-only (MCP as the sole public
surface) for security. But a read-only viewer doesn't meet "human edits", and a
bespoke UI is wasteful.

## Decision
Expose the existing internal **JupyterLab** at a hostname of your own (e.g.
`lab.example.com`) behind a tunnel + authenticating proxy (via the operator's own
tunnel tooling; Cloudflare Tunnel + Access here), gated by an **email-allowlist**
policy for a single address — the same identity as the MCP human path. Loopback host port
`127.0.0.1:${JUPYTER_LAB_HOST_PORT:-7131}`; the MCP still talks to jupyter over the
internal network. Restricting who may reach it is the proxy's job (docs/AUTH.md).

## Consequences
- Natural bidirectional flow: the agent writes notebooks via MCP; the human views/
  edits the *same* files in Lab; changes are mutually visible (see [0011](0011-editable-notebook-model.md)).
- **Relaxes 0002:** the public surface now also includes the full Lab (terminal,
  file ops, arbitrary exec). Accepted because the gate is the same single-identity
  Access — that user already has arbitrary exec via the MCP, so the *trust boundary*
  is unchanged; only the UI surface for that one identity grows.
- Lab kernels run on the server (CPU); the Colab GPU path is NOT available from the Lab
  kernel picker (see [docs/COLAB.md](../COLAB.md)). GPU stays via the MCP or
  `colab url`.
- Lab uses the Jupyter token in the URL as a second factor behind Access.
