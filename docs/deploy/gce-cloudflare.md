# Deploy on a GCE free-tier VM, reachable from the Claude app

A runbook for putting jupyter-rmcp on a Google Compute Engine **e2-micro**
(always-free tier), published through a Cloudflare Tunnel behind Cloudflare
Access, so you can drive notebooks from Claude on your phone. All compute runs
on Colab under your own Google account; the VM only stores notebooks.

**Written to be executed by an agent** (Claude Code) with a human on hand for
the steps a browser is required for. Each phase ends in a verification you can
check before moving on. It is equally readable by a human — the structure just
makes failure obvious early.

If you have not done the account setup yet, start with the prerequisites:
**[日本語版セットアップガイド](../ja/setup.md)** covers every account, token and
API you need before phase 1.

---

## Read this first (agent: these are your guardrails)

**The dangerous window is between phase 4 and phase 6.** Once the tunnel is
connected, the hostname answers on the public internet, and until the Access
policy exists **anyone who guesses the URL gets arbitrary code execution**. Do
not stop work in the middle of that window. If you must stop, stop the
`cloudflared` service first (`sudo systemctl stop cloudflared`).

**Never:**
- commit `.env`, `secrets/`, or any token — they are git-ignored, keep it that way
- create GCP resources outside the free tier without asking (machine types other
  than `e2-micro`, regions other than the three listed, disks over 30 GB,
  reserved static IPs, Cloud NAT, load balancers)
- set an Access policy to "any authenticated user" or an email you were not
  given — the allowlist is one specific address
- leave `MCP_BEARER` set once OAuth is on (phase 6 explains the collision)

**Stop and ask the human when you hit** a browser login, a domain purchase, a
dashboard-only setting, or any prompt asking for a credential you were not
given. Those steps are marked **HUMAN**.

**Everything here is idempotent.** Re-running a phase is always safe; prefer
re-running to guessing.

---

## Why this fits in 1 GB of RAM

The free-tier e2-micro is **1 GB RAM, 2 shared vCPUs (0.25 sustained), 30 GB
disk, 1 GB/month North-American egress, in `us-west1` / `us-central1` /
`us-east1` only**. That is not enough to run notebooks — and it does not have
to be, because this deployment runs in **`COLAB_ONLY` mode**: the local Jupyter
never executes code, it only stores the `.ipynb` files and serves JupyterLab.
Every kernel is a Colab VM.

Measured footprint of exactly this configuration:

| Component | Memory |
|---|---|
| `jupyter` (slim image, no kernels) | ~110 MB |
| `mcp` | ~85 MB |
| `cloudflared` | ~30 MB |
| Debian + Docker daemon | ~250 MB |
| **Total** | **~475 MB of 1024 MB** |

Two consequences shape the rest of this document. Use the **slim image**
(`compose.slim.yaml`, 261 MB instead of 1.29 GB) — the default image bundles a
scientific stack for kernels this deployment will never start. And **add swap
before building**, because a Docker build is the one moment this VM is short on
memory.

### What can still cost money

Free tier is free until it isn't. Three things to know:

- **Egress is 1 GB/month.** Notebook edits are tiny, and Kaggle datasets land on
  the Colab VM rather than here, so normal use is far under. Browsing JupyterLab
  a lot is what would push you over — its assets are megabytes per cold load.
- **External IPv4 may be billable.** Google charges for in-use external IPv4
  addresses on VMs (since Feb 2024, ~$0.005/hour ≈ $3.60/month); the always-free
  documentation does not state whether e2-micro is exempt. **Do not assume.**
  Phase 1 sets a budget alert and you should check your first invoice.
- **Anything outside the three regions or above `e2-micro` is charged in full.**

Set the budget alert. It is the difference between a surprise and a number you
chose.

---

## Phase 1 — GCP project and VM

**HUMAN, once:** a Google Cloud account with billing enabled (free tier still
requires a card), `gcloud` installed and logged in (`gcloud auth login`).

```bash
export PROJECT=jupyter-rmcp-$RANDOM        # or an existing project id
export ZONE=us-west1-b                     # us-west1 | us-central1 | us-east1 only
export VM=jupyter-rmcp

gcloud projects create "$PROJECT" 2>/dev/null || true
gcloud config set project "$PROJECT"
gcloud services enable compute.googleapis.com
```

**HUMAN:** link a billing account to the project (Console → Billing) and set a
budget alert — Billing → Budgets & alerts → create a budget of a few dollars
with email alerts at 50%/100%. Do this now, not later.

Create the VM. Note what is *absent*: no firewall rule opening any port. The
tunnel dials out, so nothing needs to listen.

```bash
gcloud compute instances create "$VM" \
  --zone="$ZONE" \
  --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-standard \
  --scopes=default
```

**Verify:** `gcloud compute instances describe "$VM" --zone="$ZONE" --format='value(status,machineType)'`
prints `RUNNING` and a machine type ending in `e2-micro`. Connect with
`gcloud compute ssh "$VM" --zone="$ZONE"`; everything below runs on the VM.

## Phase 2 — VM bootstrap

Swap first. Without it the image build will be killed by the OOM reaper, and the
failure looks like an unrelated compiler error.

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Docker, plus your user in the `docker` group:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
```

**Verify:** `free -h` shows 2.0Gi of swap, and `docker run --rm hello-world`
succeeds.

## Phase 3 — The stack, in colab-only mode

```bash
git clone https://github.com/tmlksu/jupyter-rmcp.git && cd jupyter-rmcp

python3 - > .env <<'PY'
import secrets
print(f"JUPYTER_TOKEN={secrets.token_hex(32)}")
print("MCP_BEARER=")              # empty on purpose: Access is the boundary
print("COLAB_ONLY=1")
print("MCP_HOST_PORT=7130")
print("JUPYTER_LAB_HOST_PORT=7131")
PY
{ echo "MCP_UID=$(id -u)"; echo "MCP_GID=$(id -g)"; } >> .env
chmod 600 .env
```

Everything else (timeouts, kernel caps) falls back to the defaults in
`mcp/config.py`, so a minimal `.env` is correct rather than lazy. `MCP_BEARER`
must stay empty here: once OAuth is on, the proxy puts its own token in
`Authorization` and a bearer would collide (phase 6).

**HUMAN, interactive:** Colab credentials. The VM has no browser, so this prints
a URL to open on your own machine and a code to paste back. It must be **your
Google account with Colab access** — the VM's service account cannot use Colab.

GCE's Debian images ship `gcloud` already; check with `gcloud --version` and
install the Google Cloud CLI only if it is missing.

```bash
gcloud auth application-default login --no-launch-browser \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
echo "GCLOUD_ADC=$HOME/.config/gcloud/application_default_credentials.json" >> .env
```

Add your Kaggle token to `.env` as `KAGGLE_API_TOKEN=...` if you want
`setup_kaggle` to work.

Build and start with the slim overlay:

```bash
docker compose -f compose.yaml -f compose.slim.yaml up -d --build
```

**Verify** — all three must hold before continuing:

```bash
curl -s http://127.0.0.1:7130/health     # {"status":"ok","jupyter":"ok","colab_only":true}
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}'
docker compose logs mcp --tail 20        # no restart loop
```

`colab_only:true` is the whole point; if the container is restarting instead,
the credentials are wrong and the logs say which — a colab-only server refuses
to start rather than failing one call at a time. Fix it here, not later.

## Phases 4–6, the scripted way (preferred)

`scripts/setup_cfzt.py` does all three phases through the Cloudflare API, in an
order that has no open window at all — the Access policy exists before the DNS
record does, so the paragraph above about "the dangerous window" stops applying:

```bash
python3 scripts/setup_cfzt.py init      # then fill in deploy.local/cfzt.env
python3 scripts/setup_cfzt.py check
python3 scripts/setup_cfzt.py apply     # policy + app + OAuth, then tunnel + DNS
python3 scripts/setup_cfzt.py connector # installs cloudflared on the VM
python3 scripts/setup_cfzt.py verify
```

Details, prerequisites and troubleshooting: [cloudflare-zero-trust.md](cloudflare-zero-trust.md)
([日本語](../ja/cfzt.md)). Skip to phase 7 when `verify` passes. The three phases
below are the same work by hand — keep them for when the API token cannot be
given the permissions it needs, or when something has to be fixed one object at
a time.

## Phase 4 — Tunnel

**HUMAN:** a domain on your Cloudflare account, and Zero Trust enabled. Create
the tunnel in the dashboard (Networks → Tunnels → Create → `cloudflared`); it
gives you an install command containing a token. Run it on the VM:

```bash
# the dashboard's own command, e.g.:
sudo cloudflared service install <TOKEN>
sudo systemctl enable --now cloudflared
```

Add a **public hostname** to the tunnel: subdomain `mcp`, your domain, service
`HTTP` → `localhost:7130`.

**Verify:** `curl -s -o /dev/null -w '%{http_code}\n' https://mcp.example.com/health`
returns `200`.

> **That 200 means the endpoint is open to the world right now.** Go straight to
> phase 5. If you need to stop, `sudo systemctl stop cloudflared` first.

## Phase 5 — Access policy: close it

**HUMAN**, dashboard: Access → Applications → Add → Self-hosted.

| Field | Value |
|---|---|
| Name | `jupyter-mcp` |
| Session duration | `24h` |
| Application domain | `mcp.example.com` |
| Policy | Action **Allow**, Include **Emails** → your address |

One specific email. Not a group, not "any authenticated user".

**Verify:** the same curl now returns `302` (a redirect to login). A `200` means
the policy is not attached to that hostname — fix it before doing anything else.

## Phase 6 — OAuth, so the Claude app can connect

Follow **[IAP-OAUTH.md](../IAP-OAUTH.md) phase 3** — enabling
`oauth_configuration` on the app, the two traps (a `PUT` silently dropping your
policies, and service tokens being inert inside an identity policy), and the
four verification calls. It is written provider-first and applies unchanged.

The short version: the Claude app authenticates by OAuth discovery, so
`https://mcp.example.com/mcp` must answer an `Accept: application/json` request
with **`401` plus a `WWW-Authenticate: Bearer …` header**. Check that with
`curl -D -`, never `curl -I` — a HEAD request returns the browser `302` and
tells you nothing.

Then: Claude → Settings → Connectors → Add custom connector →
`https://mcp.example.com/mcp`.

## Phase 7 — Verify the whole thing

```bash
python3 -m venv .venv && .venv/bin/pip install -q -r mcp/requirements.txt -r requirements-dev.txt
.venv/bin/python scripts/smoke_test.py     # prints "mode: colab-only", 11 checks
```

Then, from Claude, one real round trip — this is the only step that spends Colab
compute units:

```
start_kernel(gpu="T4", notebook_path="hello.ipynb")
execute_code(kernel_id, "import torch; print(torch.cuda.get_device_name(0))")
stop_kernel(kernel_id)
```

Open `https://mcp.example.com` in a browser too: you should get the Access login,
not the server.

## Operating it

**JupyterLab** is on the VM's `127.0.0.1:7131` and deliberately not published.
Reach it with an SSH tunnel when you want it:
`gcloud compute ssh "$VM" --zone="$ZONE" -- -L 7131:127.0.0.1:7131`. Publishing
it through Access as a second application works and is pleasant on a phone, but
it is a second code-execution surface — if you do it, give it its own
application and an email-only policy, never a service token.

**Updating:** `git pull && docker compose -f compose.yaml -f compose.slim.yaml up -d --build`.
Read `CHANGELOG.md` first; the build is the memory-hungry moment, so keep swap on.

**Restarts:** Docker restarts the containers (`restart: unless-stopped`) and
systemd restarts `cloudflared`, so a VM reboot recovers on its own. Confirm
after the first reboot rather than assuming.

**Stopping the bill:** `gcloud compute instances stop "$VM" --zone="$ZONE"`
keeps the disk (still inside the 30 GB free allowance) and stops everything
else. `gcloud compute instances delete "$VM" --zone="$ZONE"` removes it —
notebooks live on that disk, so copy anything you want first.

## When it fails

**mcp restarts in a loop.** Colab credentials. `docker compose logs mcp` says
whether the ADC file is missing or unreadable. Check `GCLOUD_ADC` in `.env` is
an absolute path to the file `gcloud` actually wrote, and that `MCP_UID`/
`MCP_GID` match `id -u` / `id -g` — the file is mode 600 and the container must
be able to read it.

**Build killed / mysterious compiler errors.** Out of memory. Confirm swap is on
(`free -h`), and confirm you used `-f compose.slim.yaml` — the default image
is five times larger for no benefit here.

**`start_kernel` fails on `colab new`.** The ADC file exists but is not valid for
Colab, usually because the login omitted the `colaboratory` scope. Redo the
`gcloud auth application-default login` in phase 3 with both scopes.

**"local code execution is disabled on this server".** Working as intended —
something asked for a local kernel. Omit `backend`, or pass `backend="colab"`.

**The connector fails in the Claude app but Claude Code works.** Almost always
the missing `WWW-Authenticate` header (phase 6). Re-run the check with
`curl -D -` and `Accept: application/json`.

**It worked yesterday and now everything 302s.** Someone re-published or edited
the Access application and the `PUT` dropped a policy or the OAuth config.
Re-apply phase 6 and re-verify.
