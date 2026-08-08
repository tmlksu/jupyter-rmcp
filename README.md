# jupyter-rmcp

A self-hosted **MCP server** that gives Claude — including the mobile app — an
interactive Jupyter environment: real kernels, notebooks that persist as
`.ipynb` files you can open yourself, and optional **GPU offload to Google
Colab**. You run it; it runs your notebooks.

It exists because "Claude writes code" and "Claude runs code and iterates on the
output" are different things. Every execution lands in a notebook on your own
machine, which a human can watch and edit in JupyterLab at the same time.

- **Kaggle on a laptop:** `COLAB_ONLY=1` makes every kernel a Colab VM under
  your own Google account — nothing executes locally. See [docs/GUIDE.md](docs/GUIDE.md).
- **Kaggle from your phone:** the same mode fits a free-tier GCE `e2-micro`
  (~475 MB of 1 GB used), published to the Claude app through a Cloudflare
  tunnel — [docs/deploy/gce-cloudflare.md](docs/deploy/gce-cloudflare.md),
  with the account prerequisites in [日本語](docs/ja/setup.md).
- **Already have a host?** `python3 scripts/setup_cfzt.py` publishes it through
  Cloudflare Zero Trust in five commands, building the access policy before the
  hostname resolves — [docs/deploy/cloudflare-zero-trust.md](docs/deploy/cloudflare-zero-trust.md),
  [日本語](docs/ja/cfzt.md).
- **Reference:** [DESIGN.md](DESIGN.md) (architecture, session management),
  [docs/AUTH.md](docs/AUTH.md) (exposing it safely) and
  [docs/IAP-OAUTH.md](docs/IAP-OAUTH.md) (the runbook for reaching it from the
  Claude mobile app), [docs/SECURITY.md](docs/SECURITY.md) (threat model),
  [docs/COLAB.md](docs/COLAB.md) (GPU offload), [docs/adr/](docs/adr/) (why each
  choice was made).

## What runs

| Service   | Role                                     | Exposure                          |
|-----------|------------------------------------------|-----------------------------------|
| `jupyter` | Headless Jupyter Server (kernels + nbs)   | internal network + loopback Lab   |
| `mcp`     | FastMCP Streamable-HTTP server (`/mcp`)   | `127.0.0.1:7130`                  |

Both bind loopback only. Raw Jupyter — arbitrary code execution with a token —
is never the thing you expose; the MCP server is the only surface intended to go
anywhere, and only behind auth ([docs/AUTH.md](docs/AUTH.md)).

## Quickstart

```bash
git clone <this-repo> && cd jupyter-rmcp
bash scripts/install.sh      # generates .env + tokens, builds, starts, health-checks
python scripts/smoke_test.py # exercises the live /mcp endpoint end to end
```

`install.sh` is idempotent. Manual equivalent: `cp .env.example .env`, fill in
`JUPYTER_TOKEN` and `MCP_BEARER`, then `docker compose up -d --build`.

Notebooks live in `./data/notebooks` (bind-mounted, survive rebuilds) and are
served to a human at `http://127.0.0.1:7131` (JupyterLab, token from `.env`).

### Connect Claude

Claude Code, on the same machine:

```bash
claude mcp add --transport http jupyter-rmcp http://127.0.0.1:7130/mcp \
  --header "Authorization: Bearer $(grep '^MCP_BEARER=' .env | cut -d= -f2)"
```

The Claude app (including mobile) needs a public HTTPS URL, so it takes a
reverse proxy or tunnel in front of port 7130 — plus an authenticating layer,
because that URL executes code. [docs/AUTH.md](docs/AUTH.md) covers the options
and the one real gotcha: an OAuth-based proxy sets its own `Authorization`
header, which collides with `MCP_BEARER` and returns 401. Pick one, not both.

On Cloudflare Zero Trust that is scripted end to end:

```bash
python3 scripts/setup_cfzt.py init     # then fill in deploy.local/cfzt.env
python3 scripts/setup_cfzt.py check    # validates the token, changes nothing
python3 scripts/setup_cfzt.py apply    # Access policy + app + OAuth, THEN tunnel + DNS
python3 scripts/setup_cfzt.py connector && python3 scripts/setup_cfzt.py verify
```

[docs/deploy/cloudflare-zero-trust.md](docs/deploy/cloudflare-zero-trust.md)
([日本語](docs/ja/cfzt.md)) is the runbook around it;
[docs/IAP-OAUTH.md](docs/IAP-OAUTH.md) is the same setup by hand, and the
explanation of what each object is for.

## Colab-only mode

Set `COLAB_ONLY=1` in `.env` and the server refuses to execute code on the local
backend entirely: `start_kernel` defaults to `backend="colab"`, and a local
kernel request is rejected. The local Jupyter still stores and serves every
notebook — editing, listing and workspace files keep working — it just never
runs user code.

This is the mode to use on a work laptop, or anywhere "what exactly can this
thing run on my machine?" needs a short answer. It requires working Colab
credentials; the server refuses to start without them rather than failing one
call at a time. Details in [docs/GUIDE.md](docs/GUIDE.md) and
[ADR 0014](docs/adr/0014-colab-only-mode.md).

## How you use it

**One unified interface for both backends:** `start_kernel(backend=…, gpu=…,
notebook_path=…, if_exists=…)` → work in the bound notebook → `stop_kernel`.
`backend` defaults to `local` (fast, free) or to `colab` in colab-only mode;
`backend="colab"` provisions a GPU/CPU Colab VM on demand (official
`google-colab-cli`, ADC auth, no browser tab). **Notebooks always live on the
server** regardless of where the compute is.

- **Notebooks are editable documents.** `start_kernel()` with no name → a fresh
  `untitled-N.ipynb`; pass an existing name to continue (`if_exists`:
  `open`/`new`/`error`). **Edit by cell id, not index:** `list_cells(path)` for
  ids + summaries, then `patch_cell(path, old, new, cell_id=…)` for cheap
  unique-substring edits, or `edit_cell`/`insert_cells`/`move_cell`/`delete_cell`.
  Pass `expected_rev` (from `notebook_rev`) so a human's concurrent Lab edit is
  never clobbered. **Run:** `execute_code` appends and runs ad-hoc code;
  `execute_cell(kernel_id, cell_id=…)` re-runs an existing cell in place.
- **Workspace files** share one work root with the notebooks (persistent, visible
  to kernels and JupyterLab). Sub-folders auto-create when you write to a
  sub-path. Bring data in with `upload_file(path, base64)`,
  `fetch_to_workspace(url, path)`, or JupyterLab's Upload button. To use a
  workspace file on a Colab kernel, `upload_to_colab(kernel_id, path)` copies it
  to the VM's `/content`; `download_from_colab` brings a result back.
- **Kernel state persists** across executions, and across MCP restarts: the
  kernel registry is on disk, so a rebuild does not lose track of live kernels.
- **Long jobs:** `execute_code(..., background=True)` returns a `job_id`
  immediately; poll `get_job` for live stdout/stderr. Prefer that over growing
  `timeout` for training runs and big downloads.
- **Kaggle:** set `KAGGLE_API_TOKEN` in `.env` (kaggle.com/settings/api →
  "Generate New Token"). It stays server-side. Local kernels have it in their
  env; on a Colab kernel call `setup_kaggle(kernel_id)` once — it injects the
  token into the VM without it ever appearing in execution history. Same idea
  for HuggingFace via `setup_hf`.
- **Long executions:** a foreground `execute_code` that outruns
  `SOFT_REPLY_DEADLINE_SEC` (45 s) answers `status: "still_running"` with an
  `exec_id` and the output so far — **the work is not killed**; collect it with
  `get_execution(kernel_id, wait_seconds=20)`. One execution per kernel: a second
  call meanwhile is refused (`status: "busy"`) rather than queued, so a client
  timeout can't turn into the same code running twice. Nothing interrupts a
  kernel unless you pass `timeout` (or set `EXEC_TIMEOUT_SEC` > 0).
- **Housekeeping:** idle kernels are reaped after
  `KERNEL_IDLE_TIMEOUT_SEC` (`pin_kernel` to exempt), at most `MAX_KERNELS` at
  once, and nothing outlives `KERNEL_MAX_AGE_SEC`.

## MCP tools

Kernels and execution: `start_kernel`, `execute_code`, `get_execution`, `get_job`, `execute_cell`,
`stop_kernel`, `restart_kernel`, `interrupt_kernel`, `pin_kernel`, `list_kernels`,
`list_variables`, `list_backends`, `colab_log`, `setup_kaggle`, `setup_hf`.

Notebook editing (id-addressed, no execution — design from
[nbedit-mcp](https://github.com/tmlksu/nbedit-mcp)): `notebook_rev`, `list_cells`,
`read_cells`, `insert_cell`/`insert_cells`, `patch_cell`, `edit_cell`,
`delete_cell`, `move_cell`, `create_notebook`, `list_notebooks`.

Workspace files: `list_files`, `create_folder`, `upload_file`,
`fetch_to_workspace`, `upload_to_colab`, `download_from_colab`.

## Operations

```bash
docker compose ps
docker compose logs -f mcp
docker compose restart mcp
docker compose down            # stop everything
```

Extend the kernel's Python libs by editing `jupyter/requirements.txt`, then
`docker compose up -d --build jupyter`.

Tests: `pytest -q` and `ruff check .` (unit, no network) plus
`python scripts/smoke_test.py` (end-to-end against a running stack). CI runs the
first two on every push.

## License

MIT — see [LICENSE](LICENSE). Bundles the official `google-colab-cli`
(Apache-2.0) for the Colab backend.
