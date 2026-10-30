# 0020 — Restore XSRF + Origin checks on the Jupyter server

**Status:** Accepted (2026-10-11)

## Context

`jupyter/jupyter_server_config.py` set `disable_check_xsrf = True` and
`allow_origin = "*"` with a comment reasoning that the network was private — true
when written, but stale since ADR 0012: JupyterLab is published to a human
(loopback port by default, tunnel + Cloudflare Access when remote). A logged-in
operator opening a hostile page could have issued credentialed cross-site
requests to the Jupyter API (e.g. `POST /api/kernels`); modern cookie defaults
make the exploit hard, not impossible, and the loosening bought nothing.

The MCP side does not need either relaxation: it authenticates with the
`Authorization: token` header (exempt from XSRF, which only applies to
cookie-authed requests), and its REST + WebSocket calls are server-side — no
`Origin` header is ever sent.

## Decision

Delete both lines — restore Jupyter Server's default XSRF checking and default
Origin policy — and correct the stale "internal-only" comment to describe the
two real consumers (MCP over the compose network; human Lab via loopback or
Access-gated tunnel).

## Consequences

- The MCP path is unaffected (header token auth, no Origin).
- Human JupyterLab keeps working same-origin; the token URL stays the second
  factor behind Access (ADR 0012).
- If a future need genuinely requires cross-origin API access to Jupyter, it
  gets its own ADR rather than inheriting a blanket `"*"`.
