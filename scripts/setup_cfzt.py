#!/usr/bin/env python3
"""setup_cfzt.py — publish this server through Cloudflare Zero Trust, safely.

Turns the manual runbook (docs/IAP-OAUTH.md) into something you can run. It
drives the Cloudflare API to build, in this order:

    Access application + policy (allow: your email only)   <- the gate, FIRST
    Managed OAuth on that application                       <- what the Claude app needs
    Tunnel + ingress + DNS record                           <- the door, SECOND
    (you run the connector)                                 <- the door opens

**The order is the point.** The dashboard walkthrough has you create the tunnel
first, which makes the hostname answer on the public internet minutes before the
policy exists — an open arbitrary-code-execution endpoint for as long as you take
to click through the next screen. Here the gate exists before the hostname
resolves, so that window never opens.

Everything is idempotent: re-running reconciles rather than duplicating. Nothing
is deleted unless you ask for `destroy`.

Requires Python 3.9+ and nothing else (stdlib only — no jq, no curl).

    python3 scripts/setup_cfzt.py init            # write deploy.local/cfzt.env, tell you what to fill in
    python3 scripts/setup_cfzt.py check           # validate the token, resolve account + zone, dry-run the plan
    python3 scripts/setup_cfzt.py apply           # create/reconcile everything above
    python3 scripts/setup_cfzt.py connector       # install + start cloudflared on this host (needs sudo)
    python3 scripts/setup_cfzt.py verify          # the checks that decide whether the Claude app will work
    python3 scripts/setup_cfzt.py service-token   # optional: Claude Code from another machine
    python3 scripts/setup_cfzt.py destroy --yes   # remove what this script created

Config comes from deploy.local/cfzt.env (git-ignored); any value can be
overridden by an environment variable of the same name.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CFG_PATH = REPO / "deploy.local" / "cfzt.env"
TUNNEL_TOKEN_PATH = REPO / "deploy.local" / "tunnel.token"
SVC_TOKEN_PATH = REPO / "secrets" / "claude-code.env"
ENV_PATH = REPO / ".env"

API = "https://api.cloudflare.com/client/v4"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"

# Exact permission names as they appear in the Cloudflare token editor, so a 403
# tells you which checkbox you are missing instead of "authentication error".
PERMS = {
    "access": "Account → Access: Apps and Policies → Edit",
    "tunnel": "Account → Cloudflare Tunnel → Edit",
    "dns": "Zone → DNS → Edit (on the zone you are publishing under)",
    "svc": "Account → Access: Service Tokens → Edit",
    "org": "Account → Access: Organizations, Identity Providers, and Groups → Read",
    "zone": "Zone → Zone → Read",
}

CFG_TEMPLATE = """\
# cfzt.env — Cloudflare Zero Trust settings for this deployment.
# Git-ignored (deploy.local/). Chmod 600. Never commit, never paste in a chat.
# Every key can also be supplied as an environment variable of the same name.

# 1. API token. dash.cloudflare.com → My Profile → API Tokens → Create Token →
#    "Create Custom Token". Permissions (add all four rows):
#      Account → Cloudflare Tunnel              → Edit
#      Account → Access: Apps and Policies      → Edit
#      Account → Access: Service Tokens         → Edit   (only for the Claude Code path)
#      Zone    → DNS                            → Edit
#    Account Resources: your account. Zone Resources: the domain below.
CF_API_TOKEN=

# 2. Account id — dash.cloudflare.com, in the URL after /accounts/, or on the
#    right-hand side of any zone's Overview page. Leave empty to auto-detect
#    (works when the token can list accounts).
CF_ACCOUNT_ID=

# 3. The public hostname you want. The domain part must already be a zone on
#    this Cloudflare account. A dedicated subdomain, not your apex.
MCP_HOSTNAME=mcp.example.com

# 4. The email address you log into Cloudflare Access with. This single value is
#    your entire allowlist — whoever this is can execute code on this machine.
#    Comma-separate only if you really mean to let someone else in.
ACCESS_EMAIL=

# --- everything below has a working default -------------------------------

# Local port the MCP server listens on (read from ../.env if left empty).
MCP_LOCAL_PORT=

# Names of the objects created on Cloudflare. Change only to avoid a collision
# with something you already have.
APP_NAME=jupyter-mcp
TUNNEL_NAME=jupyter-rmcp
POLICY_NAME=jupyter-rmcp-human
SVC_POLICY_NAME=jupyter-rmcp-svc
SVC_TOKEN_NAME=claude-code-jupyter-rmcp

# How long an Access session lasts before re-authentication. Shorter is safer.
SESSION_DURATION=24h
"""


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def step(msg):
    print(f"▶ {msg}")


def ok(msg):
    print(f"  ✓ {msg}")


def warn(msg):
    print(f"  ! {msg}")


def die(msg, hint=None):
    print(f"  ✗ {msg}", file=sys.stderr)
    if hint:
        print(f"    {hint}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def read_env_file(path):
    """Parse a KEY=VALUE file the way docker compose does. Missing file -> {}."""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_cfg(require=()):
    cfg = read_env_file(CFG_PATH)
    for key in list(cfg) + ["CF_API_TOKEN", "CF_ACCOUNT_ID", "MCP_HOSTNAME", "ACCESS_EMAIL"]:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    cfg.setdefault("APP_NAME", "jupyter-mcp")
    cfg.setdefault("TUNNEL_NAME", "jupyter-rmcp")
    cfg.setdefault("POLICY_NAME", "jupyter-rmcp-human")
    cfg.setdefault("SVC_POLICY_NAME", "jupyter-rmcp-svc")
    cfg.setdefault("SVC_TOKEN_NAME", "claude-code-jupyter-rmcp")
    cfg.setdefault("SESSION_DURATION", "24h")
    if not cfg.get("MCP_LOCAL_PORT"):
        cfg["MCP_LOCAL_PORT"] = read_env_file(ENV_PATH).get("MCP_HOST_PORT", "7130")

    missing = [k for k in require if not cfg.get(k) or cfg[k].endswith("example.com")]
    if missing:
        die(
            f"missing in {rel(CFG_PATH)}: {', '.join(missing)}",
            "run `python3 scripts/setup_cfzt.py init` first, then fill it in.",
        )
    return cfg


def rel(path):
    try:
        return str(Path(path).relative_to(REPO))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- #
# Cloudflare API
# --------------------------------------------------------------------------- #
class CFError(Exception):
    pass


def api(cfg, method, path, body=None, need=None, allow_404=False):
    """One Cloudflare API call. Raises CFError with the permission you're missing."""
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {cfg['CF_API_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raise CFError(f"{method} {path} -> HTTP {e.code}: {raw[:200]!r}") from None
        if e.code == 404 and allow_404:
            return None
        errs = "; ".join(
            f"{x.get('code', '?')}: {x.get('message', '')}" for x in payload.get("errors", [])
        ) or f"HTTP {e.code}"
        hint = ""
        if e.code in (401, 403) and need:
            hint = f"\n    Your API token is missing: {PERMS.get(need, need)}"
        elif e.code == 403 and not need:
            hint = "\n    Looks like a token permission problem — check the scopes in cfzt.env."
        raise CFError(f"{method} {path} -> {errs}{hint}") from None
    except urllib.error.URLError as e:
        raise CFError(f"{method} {path} -> network error: {e.reason}") from None

    if not payload.get("success", True):
        errs = "; ".join(
            f"{x.get('code', '?')}: {x.get('message', '')}" for x in payload.get("errors", [])
        )
        raise CFError(f"{method} {path} -> {errs}")
    return payload.get("result")


def resolve_account(cfg):
    if cfg.get("CF_ACCOUNT_ID"):
        return cfg["CF_ACCOUNT_ID"]
    accounts = api(cfg, "GET", "/accounts?per_page=50") or []
    if len(accounts) == 1:
        ok(f"account auto-detected: {accounts[0]['name']} ({accounts[0]['id']})")
        return accounts[0]["id"]
    if not accounts:
        die("no accounts visible to this token", "set CF_ACCOUNT_ID in cfzt.env explicitly.")
    names = ", ".join(f"{a['name']}={a['id']}" for a in accounts)
    die("this token can see several accounts", f"set CF_ACCOUNT_ID to one of: {names}")


def resolve_zone(cfg, hostname):
    """Find the zone that owns `hostname` by walking its parent domains."""
    labels = hostname.split(".")
    for i in range(len(labels) - 1):
        candidate = ".".join(labels[i:])
        zones = api(cfg, "GET", f"/zones?name={candidate}", need="zone") or []
        if zones:
            return zones[0]["id"], zones[0]["name"]
    die(
        f"no Cloudflare zone found for {hostname}",
        "the domain must be on this Cloudflare account (nameservers pointed at Cloudflare), "
        "and the token needs Zone → Zone → Read on it.",
    )


# --------------------------------------------------------------------------- #
# Access: policy, application, managed OAuth
# --------------------------------------------------------------------------- #
def ensure_email_policy(cfg, acct):
    """A reusable account-level Allow policy naming exactly the people you trust."""
    emails = [e.strip() for e in cfg["ACCESS_EMAIL"].split(",") if e.strip()]
    body = {
        "name": cfg["POLICY_NAME"],
        "decision": "allow",
        "include": [{"email": {"email": e}} for e in emails],
    }
    existing = api(cfg, "GET", f"/accounts/{acct}/access/policies?per_page=50", need="access") or []
    match = next((p for p in existing if p.get("name") == cfg["POLICY_NAME"]), None)
    if match:
        api(cfg, "PUT", f"/accounts/{acct}/access/policies/{match['id']}", body, need="access")
        ok(f"policy '{cfg['POLICY_NAME']}' updated -> allow {', '.join(emails)}")
        return match["id"]
    created = api(cfg, "POST", f"/accounts/{acct}/access/policies", body, need="access")
    ok(f"policy '{cfg['POLICY_NAME']}' created -> allow {', '.join(emails)}")
    return created["id"]


def find_app(cfg, acct, hostname):
    apps = api(cfg, "GET", f"/accounts/{acct}/access/apps?per_page=50", need="access") or []
    return next((a for a in apps if a.get("domain") == hostname), None)


def ensure_app(cfg, acct, hostname, policies):
    """Create-or-reconcile the Access application, with Managed OAuth enabled.

    Access app updates are a full-body PUT — a PATCH is rejected (10405) and a
    PUT drops anything you do not resend, including your policies and the OAuth
    config. So the body below is always complete; that is not redundancy.
    """
    app = find_app(cfg, acct, hostname)
    if app is None:
        app = api(
            cfg,
            "POST",
            f"/accounts/{acct}/access/apps",
            {
                "name": cfg["APP_NAME"],
                "domain": hostname,
                "type": "self_hosted",
                "session_duration": cfg["SESSION_DURATION"],
            },
            need="access",
        )
        ok(f"Access application created for {hostname} (id={app['id']})")
    else:
        ok(f"Access application found for {hostname} (id={app['id']})")

    body = {
        "name": cfg["APP_NAME"],
        "domain": hostname,
        "type": "self_hosted",
        "session_duration": cfg["SESSION_DURATION"],
        "self_hosted_domains": [hostname],
        "policies": policies,
        # Managed OAuth. `self_hosted` rather than the newer `mcp` application
        # type: both support this, and self_hosted is the one this project has
        # actually been verified against.
        "oauth_configuration": {
            "enabled": True,
            "dynamic_client_registration": {
                "enabled": True,
                "allowed_uris": [CLAUDE_CALLBACK],
                "allow_any_on_localhost": True,
                "allow_any_on_loopback": True,
            },
        },
    }
    res = api(cfg, "PUT", f"/accounts/{acct}/access/apps/{app['id']}", body, need="access")
    attached = [f"{p.get('name')} ({p.get('decision')})" for p in res.get("policies") or []]
    ok(f"policies attached: {', '.join(attached) or 'NONE — stop and investigate'}")
    oauth = (res.get("oauth_configuration") or {}).get("enabled")
    if oauth:
        ok("Managed OAuth enabled (the Claude app's login path)")
    else:
        warn(
            "the API did not report oauth_configuration.enabled — the Claude app connector "
            "will not work. `verify` will confirm; if it fails, enable OAuth in the dashboard "
            "(Access → the app → Managed OAuth) and re-run verify."
        )
    return res


# --------------------------------------------------------------------------- #
# Tunnel + DNS
# --------------------------------------------------------------------------- #
def ensure_tunnel(cfg, acct):
    name = cfg["TUNNEL_NAME"]
    tunnels = (
        api(cfg, "GET", f"/accounts/{acct}/cfd_tunnel?is_deleted=false&per_page=50", need="tunnel") or []
    )
    match = next((t for t in tunnels if t.get("name") == name), None)
    if match:
        ok(f"tunnel '{name}' found (id={match['id']})")
        return match["id"]
    # config_src=cloudflare keeps the ingress rules on Cloudflare's side, which is
    # what lets this script set them (and what keeps the VM's disk free of config).
    created = api(
        cfg,
        "POST",
        f"/accounts/{acct}/cfd_tunnel",
        {"name": name, "config_src": "cloudflare"},
        need="tunnel",
    )
    ok(f"tunnel '{name}' created (id={created['id']})")
    return created["id"]


def ensure_ingress(cfg, acct, tunnel_id, hostname):
    service = f"http://localhost:{cfg['MCP_LOCAL_PORT']}"
    current = (
        api(
            cfg,
            "GET",
            f"/accounts/{acct}/cfd_tunnel/{tunnel_id}/configurations",
            need="tunnel",
            allow_404=True,
        )
        or {}
    )
    ingress = ((current.get("config") or {}).get("ingress")) or []
    rules = [r for r in ingress if r.get("hostname") and r.get("hostname") != hostname]
    rules.append({"hostname": hostname, "service": service})
    rules.append({"service": "http_status:404"})  # required catch-all, must be last
    api(
        cfg,
        "PUT",
        f"/accounts/{acct}/cfd_tunnel/{tunnel_id}/configurations",
        {"config": {"ingress": rules}},
        need="tunnel",
    )
    ok(f"ingress: {hostname} -> {service}")


def ensure_dns(cfg, zone_id, hostname, tunnel_id):
    target = f"{tunnel_id}.cfargotunnel.com"
    # `name.exact` — the bare `?name=` filter is the legacy v4 form and no longer
    # matches, which would look like "no record exists" and create a duplicate.
    records = (
        api(cfg, "GET", f"/zones/{zone_id}/dns_records?name.exact={hostname}", need="dns") or []
    )
    if records:
        rec = records[0]
        if rec.get("type") == "CNAME" and rec.get("content") == target:
            ok(f"DNS {hostname} already points at the tunnel")
            return
        if rec.get("type") != "CNAME":
            die(
                f"{hostname} already has a {rec.get('type')} record",
                "delete it in the Cloudflare dashboard, or choose another hostname.",
            )
        api(
            cfg,
            "PUT",
            f"/zones/{zone_id}/dns_records/{rec['id']}",
            {"type": "CNAME", "name": hostname, "content": target, "proxied": True},
            need="dns",
        )
        ok(f"DNS {hostname} repointed at the tunnel")
        return
    api(
        cfg,
        "POST",
        f"/zones/{zone_id}/dns_records",
        {"type": "CNAME", "name": hostname, "content": target, "proxied": True},
        need="dns",
    )
    ok(f"DNS {hostname} -> {target} (proxied)")


def save_tunnel_token(cfg, acct, tunnel_id):
    token = api(cfg, "GET", f"/accounts/{acct}/cfd_tunnel/{tunnel_id}/token", need="tunnel")
    TUNNEL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TUNNEL_TOKEN_PATH.write_text(token if isinstance(token, str) else json.dumps(token))
    TUNNEL_TOKEN_PATH.chmod(0o600)
    ok(f"connector token -> {rel(TUNNEL_TOKEN_PATH)} (600; it is a credential)")


# --------------------------------------------------------------------------- #
# local checks
# --------------------------------------------------------------------------- #
def probe(url, headers=None, method="GET", timeout=20):
    """HTTP probe that does NOT follow redirects — a 302 here is information."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(4096)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(4096)
    except urllib.error.URLError as e:
        return None, {}, str(e.reason).encode()


def check_local_bearer():
    """MCP_BEARER must be empty once OAuth is the boundary, or every call 401s."""
    bearer = read_env_file(ENV_PATH).get("MCP_BEARER", "")
    if bearer:
        return False, (
            "MCP_BEARER is set in .env. With Managed OAuth the proxy puts its own token in "
            "the Authorization header, it will not match your bearer, and FastMCP returns 401 "
            "on every call. Blank it (`MCP_BEARER=`) and `docker compose up -d mcp`."
        )
    return True, "MCP_BEARER is empty — correct, Access is the trust boundary"


def team_domain(cfg, acct):
    try:
        org = api(cfg, "GET", f"/accounts/{acct}/access/organizations", need="org")
    except CFError:
        return None
    if isinstance(org, list):
        org = org[0] if org else {}
    return (org or {}).get("auth_domain")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_init(args):
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CFG_PATH.exists():
        ok(f"{rel(CFG_PATH)} already exists — keeping it")
    else:
        CFG_PATH.write_text(CFG_TEMPLATE)
        CFG_PATH.chmod(0o600)
        ok(f"wrote {rel(CFG_PATH)} (600)")
    print(
        f"""
Fill in four values in {rel(CFG_PATH)}, then run `check`:

  CF_API_TOKEN    dash.cloudflare.com → My Profile → API Tokens → Create Token
                  → Create Custom Token, with these permissions:
                      Account → Cloudflare Tunnel          → Edit
                      Account → Access: Apps and Policies  → Edit
                      Account → Access: Service Tokens     → Edit   (Claude Code only)
                      Zone    → DNS                        → Edit
  CF_ACCOUNT_ID   optional — auto-detected if the token can list accounts
  MCP_HOSTNAME    the public name you want, e.g. mcp.yourdomain.com
                  (the domain must already be a zone on this Cloudflare account)
  ACCESS_EMAIL    the address you log into Cloudflare with. This is the whole
                  allowlist: whoever it names can execute code on this machine.

Prerequisites the API cannot do for you (both are one-time, in the browser):
  1. the domain is on Cloudflare (nameservers pointed at it)
  2. Zero Trust is enabled on the account and you picked a team name
"""
    )


def cmd_check(args):
    cfg = load_cfg(require=["CF_API_TOKEN", "MCP_HOSTNAME", "ACCESS_EMAIL"])
    host = cfg["MCP_HOSTNAME"]

    step("API token")
    try:
        api(cfg, "GET", "/user/tokens/verify")
        ok("token is valid and active")
    except CFError as e:
        die(str(e), "recreate the token; the value in cfzt.env is wrong or expired.")

    step("account and zone")
    acct = resolve_account(cfg)
    zone_id, zone_name = resolve_zone(cfg, host)
    ok(f"account {acct}")
    ok(f"zone {zone_name} ({zone_id}) owns {host}")

    step("token permissions (read probes — write scope shows up when apply runs)")
    for label, path, need in [
        ("Access apps", f"/accounts/{acct}/access/apps?per_page=1", "access"),
        ("Tunnels", f"/accounts/{acct}/cfd_tunnel?per_page=1", "tunnel"),
        ("DNS", f"/zones/{zone_id}/dns_records?per_page=1", "dns"),
        ("Service tokens", f"/accounts/{acct}/access/service_tokens?per_page=1", "svc"),
    ]:
        try:
            api(cfg, "GET", path, need=need)
            ok(f"{label}: readable")
        except CFError:
            note = " (only needed for the Claude Code path)" if need == "svc" else ""
            warn(f"{label}: NOT readable — add {PERMS[need]}{note}")

    step("this host")
    good, msg = check_local_bearer()
    (ok if good else warn)(msg)
    status, _, body = probe(f"http://127.0.0.1:{cfg['MCP_LOCAL_PORT']}/health")
    if status == 200:
        ok(f"MCP server healthy on 127.0.0.1:{cfg['MCP_LOCAL_PORT']}: {body.decode()[:120]}")
    else:
        warn(
            f"nothing healthy on 127.0.0.1:{cfg['MCP_LOCAL_PORT']} "
            "— start the stack first (bash scripts/install.sh)"
        )
    if shutil.which("cloudflared"):
        ok("cloudflared installed")
    else:
        ok("cloudflared not installed yet — the `connector` step handles that")

    print(
        f"""
Plan for `apply` (nothing has changed yet):
  1. Access policy '{cfg['POLICY_NAME']}'  allow → {cfg['ACCESS_EMAIL']}
  2. Access app    '{cfg['APP_NAME']}'     {host}, session {cfg['SESSION_DURATION']}, Managed OAuth on
  3. Tunnel        '{cfg['TUNNEL_NAME']}'  ingress {host} → http://localhost:{cfg['MCP_LOCAL_PORT']}
  4. DNS           {host} → <tunnel>.cfargotunnel.com (proxied)
  5. connector token saved to {rel(TUNNEL_TOKEN_PATH)}

The gate (1, 2) is built before the door (3, 4), so the hostname never resolves
to an unprotected endpoint.
"""
    )


def cmd_apply(args):
    cfg = load_cfg(require=["CF_API_TOKEN", "MCP_HOSTNAME", "ACCESS_EMAIL"])
    host = cfg["MCP_HOSTNAME"]
    acct = resolve_account(cfg)

    good, msg = check_local_bearer()
    if not good and not args.allow_bearer:
        die(msg, "re-run with --allow-bearer if you know what you are doing.")

    step("1/5 Access policy (who is allowed)")
    policy_id = ensure_email_policy(cfg, acct)

    step("2/5 Access application + Managed OAuth (the gate)")
    policies = [{"id": policy_id, "precedence": 1}]
    svc_policy = find_policy(cfg, acct, cfg["SVC_POLICY_NAME"])
    if svc_policy:
        # A service-token policy exists from a previous `service-token` run; carry
        # it forward or this PUT would silently revoke Claude Code's access.
        policies = [{"id": svc_policy["id"], "precedence": 1}, {"id": policy_id, "precedence": 2}]
    ensure_app(cfg, acct, host, policies)

    step("3/5 tunnel")
    tunnel_id = ensure_tunnel(cfg, acct)

    step("4/5 ingress + DNS (the door)")
    ensure_ingress(cfg, acct, tunnel_id, host)
    zone_id, _ = resolve_zone(cfg, host)
    ensure_dns(cfg, zone_id, host, tunnel_id)

    step("5/5 connector token")
    save_tunnel_token(cfg, acct, tunnel_id)

    print(
        f"""
Cloudflare side is done. Nothing is reachable yet — no connector is running.

Next, on this host:
    python3 scripts/setup_cfzt.py connector     # installs + starts cloudflared (sudo)
    python3 scripts/setup_cfzt.py verify        # the checks that predict whether the app works

Then in Claude: Settings → Connectors → Add custom connector → https://{host}/mcp
"""
    )


def find_policy(cfg, acct, name):
    pols = api(cfg, "GET", f"/accounts/{acct}/access/policies?per_page=50", need="access") or []
    return next((p for p in pols if p.get("name") == name), None)


def cmd_connector(args):
    if not TUNNEL_TOKEN_PATH.exists():
        die(f"{rel(TUNNEL_TOKEN_PATH)} not found", "run `apply` first.")
    token = TUNNEL_TOKEN_PATH.read_text().strip()

    if not shutil.which("cloudflared"):
        step("installing cloudflared")
        if not args.yes:
            die(
                "cloudflared is not installed",
                "re-run with --yes to install it from Cloudflare's apt repo, or install it "
                "yourself: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
            )
        script = (
            "set -e; "
            "sudo mkdir -p --mode=0755 /usr/share/keyrings; "
            "curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg "
            "| sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null; "
            "echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] "
            "https://pkg.cloudflare.com/cloudflared any main' "
            "| sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null; "
            "sudo apt-get update -qq && sudo apt-get install -y cloudflared"
        )
        rc = subprocess.call(["bash", "-c", script])
        if rc != 0:
            die("cloudflared install failed", "install it manually, then re-run this command.")
        ok("cloudflared installed")

    step("installing the connector as a system service")
    print(f"  running: sudo cloudflared service install <token from {rel(TUNNEL_TOKEN_PATH)}>")
    rc = subprocess.call(["sudo", "cloudflared", "service", "install", token])
    if rc != 0:
        warn("service install returned non-zero — it may already be installed; continuing")
    subprocess.call(["sudo", "systemctl", "enable", "--now", "cloudflared"])
    subprocess.call(["sudo", "systemctl", "--no-pager", "--lines=5", "status", "cloudflared"])
    print("\nNow: python3 scripts/setup_cfzt.py verify")


def cmd_verify(args):
    cfg = load_cfg(require=["CF_API_TOKEN", "MCP_HOSTNAME"])
    host = cfg["MCP_HOSTNAME"]
    acct = resolve_account(cfg)
    failures = []

    step("1/6 the server itself")
    status, _, body = probe(f"http://127.0.0.1:{cfg['MCP_LOCAL_PORT']}/health")
    if status == 200:
        ok(f"local /health: {body.decode()[:120]}")
    else:
        failures.append("the MCP server is not healthy on loopback — nothing else matters yet")
        warn(f"local /health returned {status}")

    step("2/6 MCP_BEARER vs OAuth")
    good, msg = check_local_bearer()
    (ok if good else warn)(msg)
    if not good:
        failures.append("MCP_BEARER is set and will collide with the OAuth Authorization header")

    step("3/6 the gate is closed to strangers")
    status, _, _ = probe(f"https://{host}/health")
    if status in (301, 302, 401, 403):
        ok(f"https://{host}/health -> {status} (Access is asking who you are)")
    elif status == 200:
        failures.append(
            f"https://{host}/health returned 200 WITHOUT authentication — the endpoint is open "
            "to the internet right now. Stop cloudflared (sudo systemctl stop cloudflared) and "
            "re-run apply."
        )
        warn("200 — OPEN. See the summary below.")
    elif status is None:
        warn("no answer yet — DNS or the connector may still be starting; retry in a minute")
        failures.append("the hostname did not answer")
    else:
        warn(f"unexpected status {status}")

    step("4/6 the OAuth challenge (this is the make-or-break one)")
    status, headers, _ = probe(f"https://{host}/mcp", headers={"Accept": "application/json"})
    challenge = next((v for k, v in headers.items() if k.lower() == "www-authenticate"), None)
    if status == 401 and challenge:
        ok(f"401 + WWW-Authenticate: {challenge[:90]}")
    else:
        failures.append(
            f"/mcp returned {status} with WWW-Authenticate={challenge!r}. The Claude app "
            "discovers auth from that header; without it the connector fails no matter what "
            "you do in the app UI. Claude Code tolerates its absence, the app does not."
        )
        warn(f"{status}, challenge={challenge!r}")

    step("5/6 protected-resource metadata")
    status, _, body = probe(f"https://{host}/.well-known/cloudflare-access-protected-resource/")
    if status == 200 and body.strip().startswith(b"{"):
        ok(f"resolves: {body.decode()[:120]}")
    else:
        warn(f"status {status} — this can take a few seconds to propagate; retry before worrying")

    step("6/6 your team's authorization server")
    team = team_domain(cfg, acct)
    if not team:
        warn("could not read the team domain (needs " + PERMS["org"] + ") — skipping")
    else:
        status, _, body = probe(f"https://{team}/.well-known/oauth-authorization-server")
        if status == 200:
            try:
                meta = json.loads(body)
                ok(
                    "endpoints advertised: "
                    + ", ".join(
                        k
                        for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint")
                        if meta.get(k)
                    )
                )
            except json.JSONDecodeError:
                warn("metadata was not JSON")
        else:
            warn(f"{team} returned {status}")

    if SVC_TOKEN_PATH.exists():
        step("extra — the Claude Code service-token path")
        svc = read_env_file(SVC_TOKEN_PATH)
        status, _, _ = probe(
            f"https://{host}/health",
            headers={
                "CF-Access-Client-Id": svc.get("CF_ACCESS_CLIENT_ID", ""),
                "CF-Access-Client-Secret": svc.get("CF_ACCESS_CLIENT_SECRET", ""),
            },
        )
        if status == 200:
            ok("service token passes Access")
        elif status in (301, 302):
            warn(
                "service token is being redirected to login — its policy is probably an ordinary "
                "Allow rather than non_identity. Re-run `service-token`."
            )
        else:
            warn(f"service token got {status}")

    print()
    if failures:
        print("✗ VERIFY FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"✓ VERIFY OK — add the connector in Claude: https://{host}/mcp")
    print("  Settings → Connectors → Add custom connector")


def cmd_service_token(args):
    """Optional: non-interactive access for Claude Code on another machine."""
    cfg = load_cfg(require=["CF_API_TOKEN", "MCP_HOSTNAME"])
    host, acct = cfg["MCP_HOSTNAME"], resolve_account(cfg)
    name = cfg["SVC_TOKEN_NAME"]

    step("service token")
    tokens = api(cfg, "GET", f"/accounts/{acct}/access/service_tokens?per_page=50", need="svc") or []
    match = next((t for t in tokens if t.get("name") == name), None)
    if match:
        token_uuid = match["id"]
        warn(
            f"'{name}' already exists — its secret cannot be re-fetched. Keep "
            f"{rel(SVC_TOKEN_PATH)}; to rotate, delete the token in the dashboard and re-run."
        )
    else:
        created = api(
            cfg,
            "POST",
            f"/accounts/{acct}/access/service_tokens",
            {"name": name, "duration": "8760h"},
            need="svc",
        )
        token_uuid = created["id"]
        SVC_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SVC_TOKEN_PATH.write_text(
            f"CF_ACCESS_CLIENT_ID={created['client_id']}\n"
            f"CF_ACCESS_CLIENT_SECRET={created['client_secret']}\n"
        )
        SVC_TOKEN_PATH.chmod(0o600)
        ok(f"created; secret -> {rel(SVC_TOKEN_PATH)} (600)")
        warn("that file is your ONLY copy of the secret — back it up in a password manager")

    step("non_identity policy (a service token inside an ordinary Allow policy is inert)")
    body = {
        "name": cfg["SVC_POLICY_NAME"],
        "decision": "non_identity",
        "include": [{"service_token": {"token_id": token_uuid}}],
    }
    existing = find_policy(cfg, acct, cfg["SVC_POLICY_NAME"])
    if existing:
        api(cfg, "PUT", f"/accounts/{acct}/access/policies/{existing['id']}", body, need="access")
        svc_policy_id = existing["id"]
        ok("policy updated")
    else:
        svc_policy_id = api(cfg, "POST", f"/accounts/{acct}/access/policies", body, need="access")["id"]
        ok("policy created")

    step("re-attaching both policies to the application")
    human = find_policy(cfg, acct, cfg["POLICY_NAME"])
    if not human:
        die(f"policy '{cfg['POLICY_NAME']}' is missing", "run `apply` first.")
    ensure_app(
        cfg,
        acct,
        host,
        [{"id": svc_policy_id, "precedence": 1}, {"id": human["id"], "precedence": 2}],
    )

    svc = read_env_file(SVC_TOKEN_PATH)
    print(
        f"""
Register it on the other machine (the two header names are why this path is
Claude-Code-only — the Claude app allowlists only authorization / x-api-key /
x-auth-token, so it cannot send them):

    claude mcp add --scope user --transport http jupyter-rmcp-remote https://{host}/mcp \\
      --header "CF-Access-Client-Id: {svc.get('CF_ACCESS_CLIENT_ID', '<id>')}" \\
      --header "CF-Access-Client-Secret: <secret from {rel(SVC_TOKEN_PATH)}>"

Propagation takes a few seconds; `verify` re-tests this path.
"""
    )


def cmd_destroy(args):
    cfg = load_cfg(require=["CF_API_TOKEN", "MCP_HOSTNAME"])
    host, acct = cfg["MCP_HOSTNAME"], resolve_account(cfg)
    if not args.yes:
        die("refusing without --yes", f"this deletes the Access app, tunnel and DNS record for {host}.")

    step("Access application")
    app = find_app(cfg, acct, host)
    if app:
        api(cfg, "DELETE", f"/accounts/{acct}/access/apps/{app['id']}", need="access")
        ok(f"deleted app {app['id']}")
    else:
        ok("none found")

    step("DNS record")
    zone_id, _ = resolve_zone(cfg, host)
    records = api(cfg, "GET", f"/zones/{zone_id}/dns_records?name.exact={host}", need="dns") or []
    for rec in records:
        if "cfargotunnel.com" in (rec.get("content") or ""):
            api(cfg, "DELETE", f"/zones/{zone_id}/dns_records/{rec['id']}", need="dns")
            ok(f"deleted {rec['name']}")

    step("tunnel")
    tunnels = (
        api(cfg, "GET", f"/accounts/{acct}/cfd_tunnel?is_deleted=false&per_page=50", need="tunnel") or []
    )
    match = next((t for t in tunnels if t.get("name") == cfg["TUNNEL_NAME"]), None)
    if match:
        try:
            api(cfg, "DELETE", f"/accounts/{acct}/cfd_tunnel/{match['id']}/connections", need="tunnel")
        except CFError:
            pass
        try:
            api(cfg, "DELETE", f"/accounts/{acct}/cfd_tunnel/{match['id']}", need="tunnel")
            ok(f"deleted tunnel {match['name']}")
        except CFError as e:
            warn(f"{e} — stop cloudflared on the host first (sudo systemctl stop cloudflared)")
    else:
        ok("none found")

    print(
        "\nPolicies were left in place (they are reusable and may be attached elsewhere).\n"
        "On this host: sudo cloudflared service uninstall"
    )


def main():
    p = argparse.ArgumentParser(
        description="Publish jupyter-rmcp through Cloudflare Zero Trust, gate before door.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Order: init → check → apply → connector → verify (→ service-token).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="write deploy.local/cfzt.env and explain what to fill in")
    sub.add_parser("check", help="validate the token, resolve account/zone, print the plan")
    ap = sub.add_parser("apply", help="create/reconcile policy, app, OAuth, tunnel, ingress, DNS")
    ap.add_argument("--allow-bearer", action="store_true", help="proceed even if MCP_BEARER is set")
    cp = sub.add_parser("connector", help="install and start cloudflared on this host (sudo)")
    cp.add_argument("--yes", action="store_true", help="allow installing the cloudflared package")
    sub.add_parser("verify", help="the six checks that predict whether the Claude app will connect")
    sub.add_parser("service-token", help="optional: Claude Code from another machine")
    dp = sub.add_parser("destroy", help="delete the app, tunnel and DNS record this script created")
    dp.add_argument("--yes", action="store_true")

    args = p.parse_args()
    fn = {
        "init": cmd_init,
        "check": cmd_check,
        "apply": cmd_apply,
        "connector": cmd_connector,
        "verify": cmd_verify,
        "service-token": cmd_service_token,
        "destroy": cmd_destroy,
    }[args.cmd]
    try:
        fn(args)
    except CFError as e:
        die(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
