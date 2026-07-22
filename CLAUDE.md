# CLAUDE.md — working on jupyter-rmcp

Orientation for a Claude session (or any new contributor) touching this repo.
Read [README.md](README.md) first for what the project *is*; this file is about
how to change it safely.

## Orient before touching anything

```bash
git log --oneline -8 && git status
docker compose ps && curl -s http://127.0.0.1:7130/health
```

Then skim [docs/adr/](docs/adr/) before revisiting anything that looks like a
settled decision — most surprising choices here have a recorded reason.

## Invariants (do not break without a new ADR)

- **Jupyter is never the exposed surface.** It has no host port beyond a
  loopback JupyterLab; the MCP server is the only thing meant to go anywhere,
  and only behind auth. Raw Jupyter is arbitrary code execution with a token.
- **Notebooks live on the server hosting this stack, always.** Colab and any
  future remote backend are compute-only; write-back targets the local `.ipynb`.
  This is why colab-only mode disables local *execution* rather than the local
  backend — all notebook I/O goes through its Contents API (ADR 0005, 0014).
- **The MCP tool surface is frozen** at exactly the set in
  `tests/test_tool_surface.py` and `scripts/smoke_test.py`. Adding or removing a
  tool means updating both in the same commit, deliberately. Deployment mode
  changes behavior, never the API.
- **Execution routing is explicit.** An unknown `kernel_id` raises; it never
  falls back to the local backend. `backends._resolve_backend` is the single
  choke point — new execution paths go through it (ADR 0013).
- **Secrets never in git.** `.env`, `secrets/`, `data/`, `deploy.local/` are
  git-ignored. Check `git diff --cached --name-only` before every commit.
- **Nothing personal in the tracked tree.** Hostnames, domains, emails, absolute
  paths and provider-specific policy identifiers belong in git-ignored files
  (`.env`, `deploy.local/`), never in code or docs. This repo is public.

## Where things live

| Need | File |
|------|------|
| What it is, how to run it | `README.md` |
| Colleague walkthrough (Kaggle + Colab GPU) | `docs/GUIDE.md` |
| Architecture, session management, reaper, write-back | `DESIGN.md` |
| Exposing it safely, Claude connector auth | `docs/AUTH.md` |
| IAP + OAuth setup runbook (Claude mobile app) | `docs/IAP-OAUTH.md` |
| Threat model | `docs/SECURITY.md` |
| Colab GPU offload internals | `docs/COLAB.md` |
| Why a decision was made | `docs/adr/` |
| History | `CHANGELOG.md` |

Package layout under `mcp/`: `server.py` (entry point, `/health`, uvicorn),
`app.py` (the FastMCP singleton + auth + instructions), `config.py` (every
`os.environ` read), `state.py` (process-wide singletons), `registry.py`
(persistent kernel tracking), `reaper.py` (idle/max-age reaping + lazy startup),
`backends/` (the `Backend` protocol, routing, and one module per backend), and
the four tool modules `kernels.py`, `notebook.py`, `jobs.py`, `workspace.py`
(each registers its tools by import side effect).

## Working here

- Tool changes go in the relevant `mcp/*.py` module; rebuild with
  `docker compose up -d --build mcp`.
- Read config as `config.NAME` when a test might need to vary it (deployment
  mode, for instance) — a `from config import NAME` binding cannot be
  monkeypatched.
- Match the surrounding conventions: loopback-only host binding, state
  bind-mounted under `./data`, no new host mounts without a reason.

## Verify before you commit

```bash
.venv/bin/ruff check . && .venv/bin/pytest -q     # unit, no network
python scripts/smoke_test.py                      # E2E against the running stack
```

Host venv: `python3 -m venv .venv && .venv/bin/pip install -r mcp/requirements.txt -r requirements-dev.txt`.
CI runs lint + unit on push. The smoke test is assert-based (exit 0/1) and
adapts to the deployment mode; run it after any change that touches tools,
routing, or the notebook write path.

## Leaving it maintainable

- **Write an ADR** (`docs/adr/NNNN-title.md`, add a row to `docs/adr/README.md`)
  for any non-obvious architectural or tooling decision, even a short one.
- **Add a `CHANGELOG.md` entry** for anything user-visible.
- **Commit on a branch** unless the change is trivial, and verify no secrets are
  staged.
