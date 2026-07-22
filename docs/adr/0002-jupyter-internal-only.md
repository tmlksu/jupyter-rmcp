# 0002 — Jupyter internal-only; MCP is the only public surface

**Status:** Accepted (2026-07-12)

## Context
The Jupyter server executes arbitrary code. Exposing it to the internet — even
token-authed — is a large attack surface.

## Decision
The Jupyter container has **no host port**; it lives on a private Docker network
reachable only by the MCP container. The **MCP server is the only internet-facing
surface** (bound to `127.0.0.1:7130`, published behind a reverse proxy / tunnel
that authenticates callers — see [docs/AUTH.md](../AUTH.md)). MCP↔Jupyter auth
uses a shared `JUPYTER_TOKEN`.

## Consequences
- The internet-facing surface is the curated MCP tool set, not raw Jupyter.
- A compromise of the MCP layer is still serious (it can exec), but the front
  gate controls who reaches the MCP at all (see [0003](0003-trust-boundary-cloudflare-access.md)).
- `JUPYTER_TOKEN` is freely regenerable (internal only).
