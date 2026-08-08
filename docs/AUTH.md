# Auth: exposing the MCP server safely

This server executes arbitrary code. A reachable, unauthenticated `/mcp`
endpoint is a remote shell — so the question is never *whether* to authenticate,
only *where* the check happens. This document covers the options and the
research behind them; [SECURITY.md](SECURITY.md) covers the threat model, and
[IAP-OAUTH.md](IAP-OAUTH.md) is the step-by-step runbook for the hardest option
(an identity-aware proxy with OAuth, which is what the Claude mobile app needs).

## Decide first: does anything need to reach it remotely?

**No** — you use Claude Code on the same machine. Then you are already done:
both ports bind `127.0.0.1`, and `MCP_BEARER` is defence in depth rather than
the boundary. This is the simplest safe deployment and the one most people want.

**Yes** — you want the Claude app, especially on a phone. Then you need a public
HTTPS URL, which means a proxy or tunnel in front of port 7130, which means an
authenticating layer. Read on.

## The two places auth can live

**1. `MCP_BEARER` (in this server).** FastMCP's `StaticTokenVerifier` checks
`Authorization: Bearer <token>` on every request. Set it in `.env`; clients send
the header. Simple, no infrastructure, and enough on its own for a client that
can send arbitrary headers (Claude Code can).

**2. An authenticating proxy in front.** A tunnel or reverse proxy that performs
its own identity check — Cloudflare Access, Tailscale, an OAuth-aware reverse
proxy, mTLS at nginx/Caddy — and only then forwards to `127.0.0.1:7130`. This is
what makes exposing the endpoint to the public internet reasonable, because the
identity decision happens before anything reaches code execution.

**The collision to know about:** if your proxy uses an OAuth flow, it puts *its
own* token in `Authorization`. That header then no longer matches `MCP_BEARER`
and FastMCP returns 401. In that setup leave `MCP_BEARER` **empty** and let the
proxy be the boundary. Use one mechanism or the other on the same header, never
both.

## What the Claude clients actually support

Researched 2026; verify against current docs before relying on it.

- **Transport:** Claude custom connectors use **Streamable HTTP** (a single
  `/mcp` endpoint). SSE is legacy. This server serves Streamable HTTP.
- **Claude app (web + mobile):**
  - **OAuth 2.0** (dynamic client registration, PKCE) — first-class, works
    everywhere. The connector runs OAuth discovery against your endpoint, so the
    proxy must return `401` with a `WWW-Authenticate: Bearer … resource_metadata=…`
    challenge (RFC 9728) to an API-style request.
  - **Static request headers** — a gated beta, and only an allowlisted set of
    header names (`authorization`, `x-api-key`, `x-auth-token`).
  - Machine-to-machine `client_credentials` with no user consent is **not**
    supported.
- **Claude Code:** can send arbitrary headers, so a static bearer or a
  proxy-specific header pair both work. Always the fastest thing to test first.

Consequence worth internalizing: proxy schemes that authenticate with custom
header names (for example Cloudflare Access **service tokens**, which use
`CF-Access-Client-Id` / `CF-Access-Client-Secret`) work fine for Claude Code but
**cannot** be used by the Claude app, because those header names are not on its
allowlist. For the app you need a real OAuth path.

## Connecting Claude Code

```bash
claude mcp add --transport http jupyter-rmcp http://127.0.0.1:7130/mcp \
  --header "Authorization: Bearer $(grep '^MCP_BEARER=' .env | cut -d= -f2)"
```

For a remote instance, swap the URL for your public one and add whatever headers
your proxy requires.

## Connecting the Claude app

On Cloudflare Zero Trust, this is scripted: `scripts/setup_cfzt.py`
(**[deploy/cloudflare-zero-trust.md](deploy/cloudflare-zero-trust.md)**,
[日本語](ja/cfzt.md)). Full runbook with the API calls done by hand, and the two
traps that cost the most time: **[IAP-OAUTH.md](IAP-OAUTH.md)**. In outline:

1. Put an authenticating tunnel or proxy in front of `127.0.0.1:7130` with an
   OAuth-capable identity layer, scoped to *you* — not "any authenticated user".
2. Leave `MCP_BEARER` empty (see the collision above).
3. Verify the challenge before touching the app. A bare `curl -I` (HEAD) often
   returns a `302` browser redirect and tells you nothing; what matters is:

   ```bash
   curl -s -o /dev/null -D - -H 'Accept: application/json' https://<your-host>/mcp
   ```

   You want `401` **with** a `WWW-Authenticate: Bearer …` header. Some Access-style
   setups have historically returned the 401 *without* it — Claude Code tolerates
   that, the web/mobile app does not.
4. Claude → Settings → Connectors → Add custom connector → `https://<your-host>/mcp`.
   The callback the identity provider must allow is
   `https://claude.ai/api/mcp/auth_callback`. Some providers also want
   Anthropic's egress range allowlisted (`160.79.104.0/21`).

If the OAuth path misbehaves on mobile, the fallbacks are requesting
`static_headers` beta access from Anthropic, or fronting the endpoint with a
small OAuth shim of your own.

## Rollout order that saves time

1. Local first: `python scripts/smoke_test.py`, then Claude Code with the bearer.
   Do not debug OAuth and your own server at the same time.
2. Publish behind the proxy; re-test with Claude Code against the public URL.
3. Only then add the connector in the Claude app and verify the 401 challenge.

Whatever you use, re-check auth after any change to the proxy configuration —
identity settings are easy to lose in a republish and the failure mode is an
open code-execution endpoint, not an error message.
