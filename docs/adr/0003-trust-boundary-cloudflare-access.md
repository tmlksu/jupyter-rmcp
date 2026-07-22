# 0003 — Trust boundary = Cloudflare Access; app-level bearer disabled

**Status:** Accepted (2026-07-12)

## Context
The MCP uses Streamable HTTP (`/mcp`). We initially added an app-level bearer
(`MCP_BEARER`, FastMCP `StaticTokenVerifier`) as defense-in-depth. But once the
Claude app authenticates via Cloudflare Access **Managed OAuth**, it sends
Access's own token in the `Authorization` header — which is not our bearer — so
FastMCP rejected every request with 401.

## Decision
**Disable the app-level bearer** (`MCP_BEARER` empty → FastMCP `auth=None`). The
**sole trust boundary is Cloudflare Access.** Only identities in an
email-allowlist group (human/OAuth) and a scoped service token (Claude Code)
can reach the origin.

## Consequences
- OAuth (mobile/web) works; Claude Code still passes via its headers.
- One less defense layer at the app; acceptable because Access is a strong gate
  and the connector policy is scoped tightly. See [docs/SECURITY.md](../SECURITY.md).
- **Do not re-enable an `Authorization` bearer while Managed OAuth is on.**
- **Since the public release**, Cloudflare Access is only *one* way to deploy.
  The general guidance is: **set `MCP_BEARER`, or put an authenticating proxy in
  front of the MCP** (the two are mutually exclusive only for the Managed-OAuth
  case above). Whatever you pick, something must gate `/mcp`. See
  [docs/AUTH.md](../AUTH.md).
