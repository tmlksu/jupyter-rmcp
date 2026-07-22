# Security model — what this thing actually is

**This server executes arbitrary code on the machine that runs it.** Every
security question about it reduces to that sentence. A kernel can read your
files, open network connections, and spend money on APIs whose credentials you
gave it. Treat a reachable `/mcp` endpoint exactly as you would treat an open
shell on that host, because functionally it is one.

That is not an argument against running it — it is the argument for being
deliberate about two things: **who can reach the endpoint**, and **what the
kernel can touch**.

## Default posture: nothing is exposed

Out of the box both host ports bind loopback only:

- `127.0.0.1:7130` — the MCP server. The only surface intended to go anywhere.
- `127.0.0.1:7131` — JupyterLab, so a human can watch and edit the same
  notebooks.

The Jupyter server has **no other host port**; it sits on a private Docker
network reachable only by the MCP container. Port-scanning the host from outside
finds nothing. If you never put a proxy in front, the attack surface is "someone
who is already on this machine", and `MCP_BEARER` is defence in depth rather
than the boundary.

Set `MCP_BEARER` anyway. It costs nothing and it is the difference between a
misconfigured proxy being an incident and being an error message.

## If you expose it

Anything reaching port 7130 must pass an identity check first — either
`MCP_BEARER` in this server, or an authenticating proxy in front of it.
[AUTH.md](AUTH.md) covers the options, and the one real trap: an OAuth-based
proxy sets its own `Authorization` header, which collides with `MCP_BEARER`. Use
one mechanism on that header, not both.

Two properties are worth insisting on when you choose a proxy:

- **It decides before forwarding.** An identity-aware proxy that evaluates policy
  at its edge means unauthorized requests never reach code execution at all.
- **The allowlist is you**, not "any authenticated user" and not a shared group
  you happen to be in. Scope it as narrowly as your provider allows, and re-check
  it after any configuration change — identity settings are easy to lose in a
  republish, and the failure mode is silent.

A tunnel that makes an outbound connection from your host (rather than opening
an inbound port) is a meaningful improvement: there is no listening port to find.

## What `COLAB_ONLY=1` does and does not guarantee

Colab-only mode is about the *blast radius on your machine*, and it is precise:

**It does guarantee** that no user code executes locally. `start_kernel` refuses
the local backend, execution routing refuses any local kernel — including one a
human started in JupyterLab, and including survivors of a mode switch — and the
server refuses to boot at all if it has no working Colab backend, rather than
degrading quietly. See [ADR 0014](adr/0014-colab-only-mode.md).

**It does not** make the deployment unauthenticated-safe. Specifically:

- The local Jupyter still runs, still stores your notebooks, and still serves
  JupyterLab on loopback. It is a file store, not an execution path — but it is
  still a service on your machine.
- Secrets in `.env` (`KAGGLE_API_TOKEN`, `HF_TOKEN`, `JUPYTER_TOKEN`,
  `MCP_BEARER`) are readable by the MCP server, and `setup_kaggle` / `setup_hf`
  deliberately place those tokens **on the Colab VM**. Code you run there can
  read them. That is the point of the tools, and it is a real trust decision:
  what runs on that VM has your Kaggle and HuggingFace credentials.
- Code still executes — on Google's infrastructure, under your Google account,
  spending your compute units. "Not local" is not "not anywhere".
- Anyone who can reach the MCP endpoint can still run whatever they want on that
  VM, and read anything the notebooks or workspace contain.

## What the kernel is contained by

The local Jupyter container is non-root, has no Docker socket, mounts nothing
from the host except the notebook and Kaggle data directories, and is capped
(`mem 2g`, `cpu 2.0`). That is meaningful isolation from the rest of your
machine and it is **not** a sandbox against a determined attacker who already
has valid access. Container escapes exist; the mitigation is the allowlist, not
the container.

Colab VMs are outside your control entirely — Google's isolation, Google's
lifetime, reclaimed on their schedule. Do not put anything on one that you would
mind losing or leaking.

## Practical rules

- **Do not point this at work data.** A personal-project execution endpoint is
  the wrong place for anything with a compliance story attached, and the
  credential-injection tools mean secrets genuinely travel.
- **Treat every token in `.env` as live.** Rotate them if the file leaks; they
  are not derived from anything and revocation is per-service.
- **Prefer `COLAB_ONLY=1` on machines you do not own outright** — a work laptop,
  a shared box. It turns "what can this run here?" into "nothing".
- **Review your proxy's access logs occasionally.** The interesting question is
  not whether the allowlist is right today but whether it still is.
