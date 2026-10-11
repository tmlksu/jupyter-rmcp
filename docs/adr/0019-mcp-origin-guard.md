# 0019 — Validate Origin on /mcp; keep Host validation opt-in

**Status:** Accepted (2026-10-11)

## Context

`mcp.http_app` was called with `allowed_hosts=["*"], allowed_origins=["*"]` on the
assumption those values configured a guard. In FastMCP 3.4.4 the guard
(`HostOriginGuardMiddleware`) is not installed at all unless
`host_origin_protection` is set — the allowlists were inert and **every** request,
any Host, any Origin, reached the MCP endpoint.

That leaves one real attack: DNS rebinding / cross-origin browser requests against
a deployment that legitimately has no `MCP_BEARER` — the documented loopback-only
and OAuth-proxy (Cloudflare Access) configurations (ADR 0003). A page the operator
opens can drive `fetch("http://127.0.0.1:PORT/mcp")` or a rebound hostname, and
`stateless_http` means a single `tools/call` POST is arbitrary code execution.

The obvious fix — pin an allowlist of Hosts — breaks the normal deployment:
`cloudflared` (or any reverse proxy) forwards the *public* Host header, so a Host
allowlist containing only loopback names would 421 every proxied request.

## Decision

1. `host_origin_protection="auto"` on `mcp.http_app`.
2. `MCP_ALLOWED_ORIGINS` (env, comma-separated, default **empty**) is passed
   explicitly — an explicit-but-empty list turns Origin validation ON for every
   request carrying an `Origin` header. Such a request is accepted only when the
   origin is allowlisted, or when **Origin and Host are both loopback** (which is
   what a browser page opened from `localhost`/`127.0.0.1` produces). Requests
   with no `Origin` header — every non-browser MCP client, including Claude
   connectors — pass through unexamined.
3. `MCP_ALLOWED_HOSTS` (env, default empty → passed as `None`) stays an **opt-in**
   Host allowlist. Empty is deliberate: a proxied deployment must accept its
   public hostname in `Host`, and "auto" already pins loopback deployments to
   loopback Hosts via the guard's defaults.
4. Non-browser clients are unaffected; a browser page on a loopback deployment
   hitting `/mcp` cross-origin now gets 403, closing the rebinding path.

## Consequences

- A deployment whose browser flows legitimately POST to `/mcp` cross-origin must
  set `MCP_ALLOWED_ORIGINS` — none of the documented flows do that.
- Operators serving `/mcp` directly (no tunnel) can opt into Host pinning via
  `MCP_ALLOWED_HOSTS`.
- The guard also validates `Host` when the server itself binds loopback (host
  venv dev flow), with the usual loopback defaults — no behavior change there.
