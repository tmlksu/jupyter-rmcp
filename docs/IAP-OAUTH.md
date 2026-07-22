# Setup: an identity-aware proxy with OAuth, for the Claude app

[AUTH.md](AUTH.md) explains *which* auth options exist. This is the runbook for
the involved one: putting an **identity-aware proxy (IAP)** in front of the MCP
server so the **Claude app, including mobile, can connect over OAuth**.

You want this only if you need the app. Claude Code on the same machine needs
none of it — `MCP_BEARER` over loopback is the whole story.

The worked example is **Cloudflare Zero Trust** (Tunnel + Access + Managed
OAuth), because it is the path this project was actually built and verified
against, and because it requires writing zero OAuth code. Alternatives are
discussed at the end. Cloudflare's UI and API change; treat exact field names as
things to verify, not gospel.

## What you are building

```
Claude app (phone/web) ─┐
                        ├─► IdP login / OAuth ─┐
Claude Code (any host) ─┘   service token ─────┤
                                               ▼
                          ┌──── proxy edge: policy evaluated HERE ────┐
   everyone else ────────►│ no match → 401 / login  (STOPS HERE)      │
                          └───────────────────┬───────────────────────┘
                                              │ only if allowed
                                              ▼
                             outbound tunnel from your host
                                              │
                                              ▼
                        MCP server 127.0.0.1:7130 → kernel (runs code)
```

Two properties matter and both come from the same design:

**Your host opens no inbound port.** The tunnel daemon makes an *outbound*
connection to the provider. Port-scanning your machine finds nothing to connect
to — there is no listening service to attack, only an established connection you
initiated.

**Policy is evaluated before your host sees a byte.** An unauthorized request is
rejected at the provider's edge. That is the difference between an IAP and a
plain reverse proxy, and it is why exposing an arbitrary-code-execution endpoint
this way is defensible at all.

## Prerequisites

- A Cloudflare account with Zero Trust enabled, and a domain on it.
- `cloudflared` installed on the host running this stack.
- The stack up and healthy on `127.0.0.1:7130` (`bash scripts/install.sh`).
- **`MCP_BEARER` empty in `.env`.** This is not optional: the OAuth flow puts the
  proxy's own token in `Authorization`, which will not match your bearer, and
  FastMCP returns 401. The proxy is the trust boundary now. Restart the mcp
  container after changing it.

For the API calls below, export a Cloudflare API token with **Access: Apps and
Policies Edit** (and **Access: Service Tokens Edit** if you want the Claude Code
path), plus your account id:

```bash
export CF_API_TOKEN=...   CF_ACCOUNT=...
cf() { curl -s -X "$1" "https://api.cloudflare.com/client/v4$2" \
        -H "Authorization: Bearer $CF_API_TOKEN" \
        -H 'Content-Type: application/json' ${3:+-d "$3"}; }
```

## 1. Tunnel: publish the port without opening one

In the Zero Trust dashboard: **Networks → Tunnels → Create a tunnel** (type
`cloudflared`), then run the connector command it gives you on your host. Add a
**public hostname** to the tunnel:

| Field | Value |
|---|---|
| Subdomain / domain | e.g. `mcp` . `example.com` |
| Service | `HTTP` → `localhost:7130` |

At this point `https://mcp.example.com/health` should answer — which means it is
currently **wide open to the internet**. Do not stop here. Go straight to step 2;
if you need to pause, stop the tunnel first.

## 2. Access application: the identity gate

**Access → Applications → Add an application → Self-hosted.**

| Field | Value |
|---|---|
| Application name | e.g. `jupyter-mcp` |
| Session duration | `24h` (shorter = more re-auth, more safety) |
| Application domain | the hostname from step 1 |

Then add a policy — this is what "just me" means:

| Field | Value |
|---|---|
| Policy name | e.g. `human` |
| Action | **Allow** |
| Include | **Emails** → your own address |

Use a specific email, not "any authenticated user" and not a group you happen to
belong to. This single field is the difference between *your* code-execution
endpoint and *the group's*.

Verify the gate is live before going further:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://mcp.example.com/health   # 302 → login
```

A `302` to a login page is success here. Anything `200` means the policy is not
attached to that hostname.

## 3. Managed OAuth: what makes the Claude app work

An MCP client discovers how to authenticate by hitting the endpoint and reading
the `WWW-Authenticate` challenge (RFC 9728), then running OAuth with PKCE and
dynamic client registration. Cloudflare Access implements all of that for you —
it becomes the authorization server — once you enable `oauth_configuration` on
the app and allow Claude's callback URI.

The dashboard may expose this; the API call below is what was verified. Two
traps make this fiddlier than it looks:

- **The app update is a `PUT` with the full body.** A `PATCH` fails (error
  10405).
- **A `PUT` drops any policy you do not resend.** So fetch the app first and
  carry its policies forward — otherwise you enable OAuth and simultaneously
  remove your allowlist, which fails open.

```bash
HOST=mcp.example.com
CALLBACK=https://claude.ai/api/mcp/auth_callback

APPID=$(cf GET "/accounts/$CF_ACCOUNT/access/apps?per_page=50" \
        | jq -r --arg h "$HOST" '.result[]|select(.domain==$h)|.id' | head -1)

# preserve existing policies across the PUT
POLS=$(cf GET "/accounts/$CF_ACCOUNT/access/apps/$APPID" \
       | jq -c '[.result.policies[]|{id,precedence}]')

cf PUT "/accounts/$CF_ACCOUNT/access/apps/$APPID" "$(jq -nc \
  --arg host "$HOST" --arg cb "$CALLBACK" --argjson pols "$POLS" '{
    name:"jupyter-mcp", domain:$host, type:"self_hosted",
    session_duration:"24h", self_hosted_domains:[$host],
    policies:$pols,
    oauth_configuration:{
      enabled:true,
      dynamic_client_registration:{ allowed_uris:[$cb], allow_any_on_localhost:true }
    }
  }')" | jq '{oauth:.result.oauth_configuration, policies:[.result.policies[]|{name,decision}]}'
```

Some setups also need Anthropic's egress range (`160.79.104.0/21`) allowlisted.

### Verify before touching the app

Four checks, in order. The first is the make-or-break one.

```bash
# 1. an API-style request must return 401 WITH a challenge header
curl -s -o /dev/null -D - -H 'Accept: application/json' https://$HOST/mcp \
  | grep -iE '^(HTTP|www-authenticate)'

# 2. protected-resource metadata resolves
curl -s https://$HOST/.well-known/cloudflare-access-protected-resource/ | jq -c .

# 3. your team's authorization server advertises the endpoints + PKCE + DCR
curl -s https://<your-team>.cloudflareaccess.com/.well-known/oauth-authorization-server \
  | jq -c '{authorization_endpoint,token_endpoint,registration_endpoint,code_challenge_methods_supported}'
```

**Do not use `curl -I` for check 1.** A bare HEAD gets the browser path and
returns `302`, which tells you nothing. What the app needs is a `401` *with* a
`www-authenticate: Bearer …` line — some Access configurations have historically
returned the 401 without it, which Claude Code tolerates and the web/mobile app
does not. If the header is missing, the connector will fail no matter what you
do in the app UI.

Metadata takes a few seconds to propagate; retry before concluding it is broken.

### Add the connector

Claude → **Settings → Connectors → Add custom connector** →
`https://mcp.example.com/mcp` → you get bounced through the Access/IdP login →
done.

## 4. Optional: Claude Code from another machine

The app's OAuth path covers phones and the web. For Claude Code on a machine
that is not the host, use a **service token** — non-interactive, header-based.

```bash
cf POST "/accounts/$CF_ACCOUNT/access/service_tokens" '{"name":"claude-code","duration":"8760h"}' \
  | jq -r '.result|"CF_ACCESS_CLIENT_ID=\(.client_id)\nCF_ACCESS_CLIENT_SECRET=\(.client_secret)"' \
  > secrets/claude-code.env && chmod 600 secrets/claude-code.env
```

**The secret is shown exactly once.** That file is your only copy — back it up in
a password manager, and treat it as a password: whoever holds those two values
and the URL reaches your server.

Now the trap that costs people an afternoon:

> **A service token only works through a policy whose decision is
> `non_identity`.** Putting `any_valid_service_token` inside an ordinary `Allow`
> (identity) policy is **inert** — it silently does nothing, and every request
> keeps getting the `302` login redirect.

So create a second policy, scoped to that one token, and attach both:

```bash
TOKEN_UUID=$(cf GET "/accounts/$CF_ACCOUNT/access/service_tokens" \
             | jq -r '.result[]|select(.name=="claude-code")|.id' | head -1)

SVC_ID=$(cf POST "/accounts/$CF_ACCOUNT/access/policies" "$(jq -nc --arg t "$TOKEN_UUID" \
  '{name:"svc",decision:"non_identity",include:[{service_token:{token_id:$t}}]}')" \
  | jq -r .result.id)
```

Then re-attach policies with the same full-body `PUT` from step 3, listing both
(`{id:$SVC_ID,precedence:1}` and your human policy at precedence 2) and keeping
`oauth_configuration` in the body. Prefer a policy scoped to *your* token over
the account-wide "any valid service token" one — the latter admits every token in
your account, including ones created for unrelated services.

Verify (expect anything but `302`; propagation takes a few seconds):

```bash
set -a; . secrets/claude-code.env; set +a
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  https://$HOST/health
```

Register it:

```bash
set -a; . secrets/claude-code.env; set +a
claude mcp add --scope user --transport http jupyter-rmcp-remote https://$HOST/mcp \
  --header "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  --header "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"
```

Note these header *names* are why this path is Claude-Code-only: the Claude app
allowlists only `authorization`, `x-api-key` and `x-auth-token`, so it cannot
send them. That is not a limitation you can configure around — it is why the app
needs the OAuth path.

## Operating it

- **Re-check after any change to the app.** Tooling that recreates or republishes
  the Access application often does not carry `oauth_configuration` (or your
  policies) with it. Re-apply and re-run the verification. The failure mode is an
  endpoint that answers without asking who you are.
- **Rotate the service token** by deleting and recreating it if the secret file
  ever leaks. That cuts the Claude Code path instantly and leaves OAuth alone.
- **Read the access logs occasionally.** The useful question is not whether the
  allowlist was right when you wrote it.
- **Your provider terminates TLS** and can see the traffic. Inherent to this
  design; worth stating out loud.
- **None of this sandboxes the kernel.** Anyone who gets through has full code
  execution ([SECURITY.md](SECURITY.md)). The allowlist is the control.

Exposing JupyterLab (port 7131) the same way works and is genuinely convenient
on a phone, but it is a second arbitrary-code-execution surface with its own
token. If you do it, give it its own application and an email-only policy —
never a service-token policy.

## Other proxies

**Tailscale** (or any WireGuard mesh): far less work, no OAuth, no public
hostname — the endpoint is simply only reachable from your own devices. Excellent
for Claude Code anywhere. It does **not** give you the Claude app, which needs a
publicly resolvable HTTPS URL.

**oauth2-proxy / Pomerium / Ory behind nginx or Caddy:** self-hosted and provider
independent. Getting the Claude *app* working means your proxy must implement the
MCP OAuth discovery surface — the `WWW-Authenticate` challenge, protected-resource
metadata, dynamic client registration, PKCE. That is real work. For Claude Code
alone, any of these authenticating in front of the port is fine and you can skip
this entire document.

**Nothing, plus `MCP_BEARER`:** legitimate when the port never leaves loopback,
or on a private network you already trust. It is the default this project ships
with, and for most people it is the right answer.
