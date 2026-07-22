# 0007 — Colab offload: official google-colab-cli (primary) + tunnel (alternative)

**Status:** Accepted (2026-07-13). Tunnel alternative dropped 2026-07 (ADR 0013,
refactor Phase 0) — colab-cli is now the only Colab path.

## Context
Colab has no public API on free/Pro. First we built a **tunnel** path: a Colab cell
runs a token-authed Jupyter + cloudflared quick tunnel; the server drives it as a remote
Jupyter backend. It works, but has downsides: the Colab tab must stay open, a
bootstrap cell must be pasted, and running a tunnel inside the runtime is ToS-gray.
Then Google shipped the **official `google-colab-cli`** (Apache-2.0), explicitly for
"headless automation and AI agent integrations."

## Decision
Adopt **`google-colab-cli` as the primary Colab path**, driven by the MCP with
**ADC** auth. Tools: `colab_new` (provision CPU/GPU, no browser tab), `colab_exec`
(stateful; write-back to a server-side notebook per [0005](0005-notebooks-on-the-server.md)),
`colab_list`, `colab_stop`. Keep the **tunnel path** as an alternative (and for
non-Colab remote Jupyters).

## Consequences
- No Colab tab / bootstrap cell; on-demand GPU provisioning; official (ToS-clean);
  built-in keep-alive. Verified E2E on a real T4.
- Requires Google **ADC** creds on the deployment host (user-approved), mounted read-only into
  the mcp container; the container runs as the host uid to read them, `HOME=/data-colab`.
- Colab-CLI's bundled `--auth oauth2` client is rejected by Google ("OAuth client
  not found") → use `--auth adc` via `gcloud auth application-default login` with the
  `colaboratory` scope.
- The tunnel path needed a fix: force the `token` Authorization scheme (Colab's
  Jupyter 2.18.x rejects `Bearer`).
- Trade-off: `colab_exec` output is text (not full nbformat rich outputs); acceptable.
