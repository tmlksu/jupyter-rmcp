# Publish your host through Cloudflare Zero Trust

You already have the stack running on a Linux box — your own server, a VPS, a
spare machine at home. You want Claude on your phone to reach it. This is the
short path: one script does the Cloudflare side, and you do the two things a
browser is required for.

If you are starting from nothing and want a free GCE VM instead, use
[gce-cloudflare.md](gce-cloudflare.md) — it covers the machine as well, and
refers back here for the Cloudflare part. If you would rather understand every
API call and click through the dashboard yourself, [../IAP-OAUTH.md](../IAP-OAUTH.md)
is the manual version of exactly this. In Japanese: [../ja/cfzt.md](../ja/cfzt.md).

**Written to be executed by an agent** (Claude Code) with a human on hand.
Everything is idempotent; re-running a phase is always safe.

## What you end up with

```
Claude app (phone/web) ──OAuth login──┐
Claude Code (any host) ──service token┤
                                      ▼
              ┌── Cloudflare edge: your email, or nothing ──┐
   everyone else ─────► 401 / login page  (STOPS HERE)      │
              └──────────────────┬──────────────────────────┘
                                 │ only if allowed
                                 ▼
                  outbound tunnel from your host (no open port)
                                 ▼
                     127.0.0.1:7130 → kernel (runs code)
```

Two properties, both from the same design. **Your host opens no inbound port** —
`cloudflared` dials out, so a port scan finds nothing. **Policy is evaluated
before your host sees a byte** — an unauthorized request dies at Cloudflare's
edge. That is what makes exposing an arbitrary-code-execution endpoint
defensible at all; it is not defensible without it.

## Before you start

Three things the API cannot do for you. All one-time, all in a browser.

1. **A domain on Cloudflare.** Any domain, as long as its nameservers point at
   Cloudflare (Cloudflare Registrar sells them for a few dollars a year; an
   existing domain can be transferred in without moving the registrar). Zero
   Trust cannot protect a hostname Cloudflare does not serve.
2. **Zero Trust enabled** on the account: dash.cloudflare.com → Zero Trust →
   pick a *team name* (it becomes `<team>.cloudflareaccess.com`). The free plan
   covers 50 users; you need one.
3. **An API token**: My Profile → API Tokens → Create Token → **Create Custom
   Token**, with these permissions:

   | Type | Permission | Level |
   |---|---|---|
   | Account | Cloudflare Tunnel | Edit |
   | Account | Access: Apps and Policies | Edit |
   | Account | Access: Service Tokens | Edit *(only for the Claude Code path)* |
   | Zone | DNS | Edit |

   Scope it to your account and to that one zone. A token missing a row still
   works for everything else — the script tells you which checkbox is missing
   rather than failing with "authentication error".

And on the host itself: the stack up and healthy (`bash scripts/install.sh`,
then `curl -s http://127.0.0.1:7130/health`). Do not debug Cloudflare and your
own server at the same time.

## The one setting that will bite you

**`MCP_BEARER` must be empty in `.env`.** Once Access is doing OAuth it puts
*its own* token in the `Authorization` header; that no longer matches your
bearer, and the MCP server answers 401 to everything. The proxy is the trust
boundary now — you do not get to have both on the same header.

```bash
sed -i 's/^MCP_BEARER=.*/MCP_BEARER=/' .env && docker compose up -d mcp
```

The script refuses to run `apply` while a bearer is set, which is the cheapest
possible place to find this out.

## Run it

```bash
python3 scripts/setup_cfzt.py init      # writes deploy.local/cfzt.env, tells you what to fill in
$EDITOR deploy.local/cfzt.env           # HUMAN: token, hostname, your email
python3 scripts/setup_cfzt.py check     # validates everything, changes nothing, prints the plan
python3 scripts/setup_cfzt.py apply     # policy → app → OAuth → tunnel → DNS
python3 scripts/setup_cfzt.py connector # installs and starts cloudflared here (sudo)
python3 scripts/setup_cfzt.py verify    # the six checks that predict whether the app will connect
```

`check` is worth reading before `apply` — it resolves your account and zone,
probes each permission, and prints exactly what will be created. Nothing is
touched until `apply`.

### Order matters, and it is not the dashboard's order

The dashboard walkthrough has you create the tunnel first. That makes your
hostname answer on the public internet minutes before any policy exists — an
open code-execution endpoint for as long as you take to click through the next
screen, or as long as you are away making coffee. Several published guides stop
in the middle of that window.

`apply` builds the **gate before the door**: the Access application and its
allowlist exist before the DNS record does, and the connector is a separate
command you run afterwards. There is no window.

That is also why `apply` and `connector` are separate. Do not "helpfully" merge
them.

### The allowlist is one email

`ACCESS_EMAIL` is the whole security model. Whoever it names can execute
arbitrary code on that machine — there is no sandbox behind it
([../SECURITY.md](../SECURITY.md)). Use your own address. Not a group, not "any
authenticated user", not a domain wildcard.

## Add the connector in Claude

Once `verify` prints `VERIFY OK`:

Claude → **Settings → Connectors → Add custom connector** →
`https://<your-hostname>/mcp` → you get bounced through the Cloudflare login →
done. Same URL on the phone.

Optional, for Claude Code on a *different* machine than the host:

```bash
python3 scripts/setup_cfzt.py service-token
```

It creates a token, writes the secret to `secrets/claude-code.env` (600, and the
secret is shown exactly once — back it up), attaches a `non_identity` policy, and
prints the `claude mcp add` command. The two header names it uses
(`CF-Access-Client-Id` / `CF-Access-Client-Secret`) are not on the Claude app's
allowlist, which is why the app needs the OAuth path and cannot use this one.

## When it fails

**`verify` step 4 fails: no `WWW-Authenticate` header.** The one that matters.
The Claude app discovers how to authenticate from that header (RFC 9728); without
it the connector fails no matter what you do in the app UI, while Claude Code
keeps working and makes you think the server is fine. Give Managed OAuth a minute
to propagate, re-run `verify`, and if it persists check the app in the dashboard
(Access → your app → Managed OAuth) and re-run `apply`.

**`verify` step 3 says 200 — OPEN.** The hostname answers without asking who you
are. Stop the connector *now* (`sudo systemctl stop cloudflared`), then re-run
`apply` and check that the policy attached.

**Everything returns 302 after it used to work.** Someone edited or re-created
the Access application and the update dropped a policy or the OAuth config — the
API's `PUT` replaces the whole object. Re-run `apply`; it is idempotent and
re-sends the full body.

**`Access: Apps and Policies` says NOT readable in `check`.** The token is
missing that permission. Cloudflare tokens cannot be edited to add a permission
class in some UI states — creating a fresh token is faster than fighting it.

**The service token gets redirected to login.** It is attached to an ordinary
`Allow` policy, where service tokens are silently inert. It needs a
`non_identity` policy; `service-token` creates one, so re-run it.

**`cloudflared` runs but the hostname 502s.** The tunnel is up and the ingress
points at a port with nothing on it. Check `MCP_LOCAL_PORT` in `cfzt.env` matches
`MCP_HOST_PORT` in `.env`, and that `/health` answers on loopback.

## Operating it

- **Re-run `verify` after any change to the Cloudflare app.** The failure mode of
  this system is not an error message, it is an endpoint that stops asking who
  you are.
- **`destroy --yes`** removes the application, tunnel and DNS record if you want
  to start over. Policies are left alone (they are reusable objects), and
  `sudo cloudflared service uninstall` cleans up the host.
- **JupyterLab (port 7131) is deliberately not published.** Reach it over SSH
  (`ssh -L 7131:127.0.0.1:7131 you@host`). Publishing it through Access works and
  is genuinely nice on a phone, but it is a second arbitrary-code-execution
  surface: give it its own application and an email-only policy, never a service
  token.
- **Cloudflare terminates TLS** and can see the traffic. Inherent to the design;
  worth saying out loud.

## Agent guardrails

If you are an agent executing this document:

- **Stop and ask the human** at the browser steps: domain, Zero Trust team name,
  API token creation, adding the connector in Claude. They are marked HUMAN
  above and there is no API path to them.
- **Never** widen `ACCESS_EMAIL` beyond the address you were given, never set a
  policy to "any authenticated user", and never attach a service-token policy to
  the JupyterLab application.
- **Never** commit `.env`, `deploy.local/`, `secrets/`, or paste any token into a
  message. Check `git diff --cached --name-only` before any commit.
- **Do not** run `connector` before `apply` has succeeded, and do not stop
  between them for long — after `connector` the hostname is live.
- Run `verify` and report its output verbatim. If it exits non-zero, do not
  proceed to the Claude app step.
