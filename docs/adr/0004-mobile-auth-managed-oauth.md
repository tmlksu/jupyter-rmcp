# 0004 — Claude app auth via Cloudflare Access Managed OAuth

**Status:** Accepted (2026-07-12)

## Context
The Claude app (mobile/web) connector supports OAuth 2.0 (DCR/PKCE) or a gated
`static_headers` beta limited to allowlisted header names. Cloudflare Access
**Service Tokens** use `CF-Access-Client-Id/Secret` headers, which are **not** on
Claude's allowlist — so service tokens work for Claude Code but **not the app**.

## Decision
Front the app with **Cloudflare Access Managed OAuth** (enabled per-app via
`oauth_configuration`, enabled out-of-band by the operator). Cloudflare acts as the
OAuth server; we write zero OAuth code. Claude Code keeps using the scoped
**service token** (a service-token policy, `token`-scheme).

## Consequences
- Mobile/web connect via browser OAuth against Cloudflare; verified an
  API-style request to `/mcp` returns `401 + WWW-Authenticate: Bearer …
  resource_metadata=…` (RFC 9728), satisfying claude.ai (avoids issue #410).
- `oauth_configuration` is **not** captured by the tunnel/Access publish manifest;
  re-run the enable script after any re-publish of the app.
- Pairs with [0003](0003-trust-boundary-cloudflare-access.md) (no app bearer).
