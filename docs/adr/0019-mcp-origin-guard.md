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
   public hostname in `Host`.
4. Non-browser clients are unaffected; a browser page on a loopback deployment
   hitting `/mcp` cross-origin now gets 403, closing the rebinding path.

### What the guard actually checks (verified by live probing)

The middleware keys its *automatic* Host validation off `scope["server"]` — the
connection's **local address**, not the configured bind. In the documented Docker
deployment uvicorn binds `0.0.0.0` inside the container, so `scope["server"]` is
the container IP (e.g. `172.x`): the loopback Host fallback does **not** engage,
and `Host: evil.example` with no Origin returns **200**. In other words, in the
container deployment only the Origin check protects `/mcp`. That is sufficient:
the attack this guard exists for is a browser-driven request, and a browser
cross-origin POST always carries `Origin`; every client that legitimately omits
it is a non-browser MCP client.

Caveat: the same `scope["server"]` rule works the other way on a **non-container**
run. A bare `uvicorn` bound to `127.0.0.1` with cloudflared on the same host sees
a loopback local address, so Host validation *does* engage with loopback defaults
— and a proxied request carrying the public Host gets **421**. Such a deployment
must set `MCP_ALLOWED_HOSTS` to its public hostname.

### Rejected: `host_origin_protection=True` (strict mode)

Strict validates `Host` on every request against loopback defaults + the explicit
list + `scope["server"]`'s host. In the container that last entry is the container
IP, not the public hostname — so strict 421s the tunnel deployment exactly like a
loopback-only Host allowlist would, while buying nothing the `"auto"` + explicit
origins combination doesn't already provide. Rejected for the same reason the
Host allowlist is opt-in.

## Consequences

- A deployment whose browser flows legitimately POST to `/mcp` cross-origin must
  set `MCP_ALLOWED_ORIGINS` — none of the documented flows do that.
- A **non-container** deployment behind a same-host tunnel/proxy must set
  `MCP_ALLOWED_HOSTS` to its public hostname (see the caveat above). The
  documented Docker deployment needs neither.
- Host forgery is *not* defended in the container deployment — a non-browser
  client can send any Host. No documented flow depends on Host, so this is
  accepted; operators who need it can opt in via `MCP_ALLOWED_HOSTS`.
