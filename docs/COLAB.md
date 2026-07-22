# Colab offload

> **STATUS (2026-07-13): PRIMARY PATH = official `google-colab-cli`, working E2E.**
> The MCP bundles the official CLI (`google-colab-cli`, Apache-2.0) and drives it
> with **ADC** auth (headless, no browser tab). Tools: `colab_new(gpu="T4")` →
> `colab_exec(session, code, notebook_path=...)` (stateful; cell saved to a notebook
> **on the server**) → `colab_stop(session)`. Verified: CPU + real T4, stateful vars,
> write-back to the server. Benefits: no browser tab, no ToS-gray runtime tunnel,
> on-demand GPU provisioning, built-in keep-alive.
>
> **One-time setup (creds on the deployment host, user-approved):**
> ```
> gcloud auth application-default login \
>   --scopes=openid,https://www.googleapis.com/auth/cloud-platform,\
> https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
> ```
> The ADC file (`~/.config/gcloud/application_default_credentials.json`) is mounted
> read-only into the mcp container (`GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json`);
> the container runs as the host uid so it can read it; `HOME=/data-colab` (mounted rw)
> holds colab-cli state. Note: the bundled `--auth oauth2` client is rejected by Google
> ("OAuth client was not found"), so we use `--auth adc` via gcloud's client.

---

## How it works

Claude → the MCP server → route → either the local Jupyter (default) or a Colab VM
driven by colab-cli. **Notebooks + write-back always stay on the server**; the Colab VM
is disposable compute only. `start_kernel(backend="colab", gpu="T4")` provisions a
VM on demand; `list_kernels` shows each kernel's backend; a dead Colab session is
detected and reported honestly (never silently re-routed to the local kernel).

## Standing constraints

- **Data boundary:** personal only. Don't mount company Drive or process company
  data on Colab — Colab runs under your Google identity; keep work and this
  deployment separate.
- **Ephemerality:** Colab VMs die on idle, max lifetime, or preemption, and the VM
  filesystem is lost with them. Persist artifacts deliberately via
  `download_from_colab`; notebook cells+outputs survive on the server (write-back).

## History

An earlier design routed compute through a Cloudflare quick-tunnel exposing a
token-authed Jupyter inside the Colab runtime (a `register`-style tool plus a
bootstrap cell pasted into Colab). That path was **removed in refactor Phase 0**
(ADR 0013) — it was unused, ToS-gray, and widened the backend surface. The
pre-build design notes and validation for it live in git history if ever needed.
