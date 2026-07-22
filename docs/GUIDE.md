# Guide: Kaggle competitions with Claude and a Colab GPU

This is the walkthrough for the setup most people want: **run it on your own
laptop in colab-only mode**, so no code executes locally, all compute happens on
a Colab VM under your own Google account, and your notebooks stay as real
`.ipynb` files you can open in JupyterLab and commit somewhere.

If you only want the general picture, read the [README](../README.md) first.

## What you need

- **Docker** with Compose v2 (`docker compose version`).
- **A Google account with Colab access.** GPU time comes out of your own compute
  units. A paid Colab tier makes T4/L4/A100 far more available; the free tier
  works but you will be queued and reclaimed more.
- **The `gcloud` CLI**, for one-time credential setup.
- **A Kaggle API token** — kaggle.com → Settings → API → *Generate New Token*.

## One-time setup

**1. Colab credentials.** Colab is driven by the official `google-colab-cli`,
which authenticates with Application Default Credentials:

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/colaboratory,https://www.googleapis.com/auth/cloud-platform
```

Note the path it writes (usually
`~/.config/gcloud/application_default_credentials.json`) — that goes in `.env`
as `GCLOUD_ADC`. The file is mounted read-only into the container; nothing else
about your Google account is shared.

**2. Bring the stack up.**

```bash
git clone <this-repo> && cd jupyter-rmcp
bash scripts/install.sh
```

That generates `.env` with fresh tokens, builds, starts, and health-checks.

**3. Switch to colab-only mode.** Edit `.env`:

```ini
COLAB_ONLY=1
GCLOUD_ADC=/home/you/.config/gcloud/application_default_credentials.json
KAGGLE_API_TOKEN=<the token from kaggle.com>
```

Then `docker compose up -d mcp` and confirm:

```bash
curl -s http://127.0.0.1:7130/health
# {"status":"ok","jupyter":"ok","colab_only":true}
python scripts/smoke_test.py     # prints "mode: colab-only"
```

If the container exits instead, read its logs — a colab-only server refuses to
start when it has no usable Colab credentials, on purpose. See Troubleshooting.

**4. Connect Claude Code.**

```bash
claude mcp add --transport http jupyter-rmcp http://127.0.0.1:7130/mcp \
  --header "Authorization: Bearer $(grep '^MCP_BEARER=' .env | cut -d= -f2)"
```

For the Claude mobile app you need a public HTTPS URL and an authenticating
proxy — that is a bigger decision, covered in [AUTH.md](AUTH.md).

Open `http://127.0.0.1:7131` in a browser (token = `JUPYTER_TOKEN` from `.env`)
to watch the notebooks change as Claude works. Editing them yourself at the same
time is supported and safe: writes carry a revision check.

## A competition, end to end

Ask Claude in plain language; these are the tool calls it will make.

**Start a GPU kernel bound to a notebook.** One `kernel_id` is one VM, and the
VM lives exactly as long as that kernel:

```
start_kernel(gpu="T4", notebook_path="titanic.ipynb")
```

In colab-only mode `backend` defaults to `colab`, so it can be omitted.
Provisioning takes a minute or two — slower than a local kernel, which is the
trade you made.

**Put your Kaggle credentials on the VM, once per kernel:**

```
setup_kaggle(kernel_id)
```

This uploads the token and loads it into the VM's environment without it ever
appearing in execution history. Then the CLI just works:

```
execute_code(kernel_id, "!kaggle competitions download -c titanic -p /content/data --unzip")
```

**Work normally.** `execute_code` appends and runs a cell; `execute_cell`
re-runs an existing one in place. Variables persist between calls, so the
session behaves like a notebook you are sitting in front of.

**For anything slow — training, big downloads, model pulls — use a background
job** rather than a longer timeout:

```
execute_code(kernel_id, "<training code>", background=True)   # returns job_id
get_job(kernel_id, job_id)                                    # poll: live stdout/stderr
```

A plain `execute_code` that reports `status: "timeout"` means the *reply* lagged,
not that the work failed — the cell may well still be running.

**Bring results back to your machine:**

```
download_from_colab(kernel_id, "/content/submission.csv", "titanic/submission.csv")
```

The file lands in the workspace (`./data/notebooks/...`), visible in JupyterLab.
Going the other way is `upload_to_colab`.

**Submit**, from the VM:

```
execute_code(kernel_id, "!kaggle competitions submit -c titanic -f /content/submission.csv -m 'first try'")
```

## Colab VM lifecycle — the part that surprises people

**One `kernel_id` == one VM.** To stay on the same machine, keep reusing that
`kernel_id`. Everything about the VM — `/content` files, `pip install`s, the
downloaded dataset — lives and dies with it.

- **`restart_kernel`** keeps the VM and its files and installed packages, and
  resets Python state. Use it to clear variables, or to fix a stale import after
  a `pip install`/`uninstall` (re-importing in-process often will not take).
- **`stop_kernel` destroys the VM.** Files and installs are gone. A later
  `start_kernel` gives you a brand-new, empty one — you will re-download the
  dataset and re-run `setup_kaggle`.
- **`notebook_path` / `if_exists` only select the `.ipynb` file** on your
  machine. They never reconnect you to a previous VM.
- Colab reclaims idle VMs on its own schedule. The server sends a periodic
  keep-alive (`COLAB_HEARTBEAT_SEC`) while a session is tracked, but a
  long-abandoned VM will still be taken back.

Because the notebook lives on your machine and not on the VM, losing a VM costs
you the *environment*, never the work.

## Troubleshooting

**The mcp container exits at startup.** In colab-only mode that is the intended
response to unusable credentials. `docker compose logs mcp` will say which:
either colab-cli is missing / `GOOGLE_APPLICATION_CREDENTIALS` is unset, or the
file it points at does not exist. Check that `GCLOUD_ADC` in `.env` is an
absolute path to the file `gcloud` actually wrote.

**Credentials exist but `start_kernel` fails on `colab new`.** The ADC file is
present but not valid for Colab — usually the login was done without the
`colaboratory` scope. Re-run the `gcloud auth application-default login` command
above with both scopes.

**`Permission denied` reading the credentials.** The ADC file is mode 600 and
owned by you; the container runs as uid/gid 1000 by default. If your account is
not 1000, set `MCP_UID` / `MCP_GID` in `.env` (`id -u`, `id -g`).

**"local code execution is disabled on this server".** Working as intended —
something asked for a local kernel. Omit `backend`, or pass `backend="colab"`.

**A colab session is "no longer alive".** The VM was reclaimed or expired.
Nothing ran. Start a new kernel and redo the per-VM setup (`setup_kaggle`,
re-download data).

**Everything feels slow.** It is: every execution pays Colab round-trip latency,
including trivial ones. That is the cost of the guarantee. If you are on a
machine where local execution is fine, drop `COLAB_ONLY` and use
`backend="colab"` only when you actually need the GPU.

## Before you point this at anything sensitive

Read [SECURITY.md](SECURITY.md). The short version: `setup_kaggle` and
`setup_hf` place real credentials on the Colab VM, code running there can read
them, and colab-only mode limits what runs *on your laptop* — not what runs at
all.
