# 0016 — Scripted Cloudflare Zero Trust setup: build the gate before the door

**Status:** Accepted (2026-07-25)

## Context

Everything about this project up to the trust boundary is one command
(`scripts/install.sh`); everything past it was a document. [IAP-OAUTH.md](../IAP-OAUTH.md)
is an accurate runbook, but it asks the reader to create a tunnel in a dashboard,
create an Access application in another screen, then hand-run `curl | jq`
pipelines against the Cloudflare API — after understanding why a `PUT` drops
policies and why a service token is inert inside an identity policy.

Two problems with that as the only path.

**It is unsafe in the middle.** The dashboard's own ordering has you create the
tunnel first. From the moment the hostname resolves until the Access policy is
attached, an arbitrary-code-execution endpoint is open to anyone who guesses the
URL. The document warns about this window in bold; a warning is not a control,
and the window is exactly where a novice slows down.

**It does not survive being unfamiliar.** The intended audience for the public
release (ADR 0015) is colleagues who want Kaggle-on-a-phone, not people who want
to learn Cloudflare Access. "Read 280 lines, understand identity vs non-identity
policies, then paste these API calls" is a real barrier, and the failure modes
are silent: a missing `WWW-Authenticate` header works fine from Claude Code and
fails only in the mobile app.

Everything in that runbook except three browser prerequisites (domain on
Cloudflare, Zero Trust enabled, API token created) is reachable from the
Cloudflare API. The operator-specific scripts under `deploy.local/` already did
the Access half; only the tunnel and DNS half was missing.

## Decision

1. **Ship `scripts/setup_cfzt.py`** — stdlib-only Python (no `jq`, no `curl`, no
   pip install on a 1 GB VM), idempotent, with subcommands `init`, `check`,
   `apply`, `connector`, `verify`, `service-token`, `destroy`.
2. **Order: gate before door.** `apply` creates the Access policy, the
   application and Managed OAuth *before* the tunnel, the ingress rule and the
   DNS record — and installing the connector is a separate command
   (`connector`). The hostname therefore never resolves to an unprotected
   endpoint. This inverts the dashboard's order deliberately, and is the main
   reason the script exists rather than a shell alias for the runbook.
3. **`verify` is a gate, not a summary.** Six checks, exit non-zero on failure,
   with the `401` + `WWW-Authenticate` challenge as the one that decides whether
   the Claude app can connect at all. It is what an agent is told to run and
   report before touching the app UI.
4. **Config in `deploy.local/cfzt.env`** (git-ignored, 600), generated with
   annotated placeholders by `init`, overridable per-key by environment
   variables. Cloudflare credentials stay out of `.env`, which is the container's
   environment.
5. **`apply` refuses to run while `MCP_BEARER` is set.** The bearer/OAuth
   `Authorization` collision is the single most common way this deployment
   half-works; the cheapest place to discover it is before anything is created.
6. **Keep the `self_hosted` application type.** Cloudflare now offers dedicated
   `mcp` and `mcp_portal` application types; all three support Managed OAuth, and
   `self_hosted` is the one this project has actually been verified against.
   Revisit when there is a reason, not because the name matches.
7. **Pin the request shapes in unit tests** (`tests/test_setup_cfzt.py`) with the
   API layer stubbed: the full-body app `PUT` re-sending both policies and
   `oauth_configuration`, the `non_identity` decision for service tokens, the
   trailing catch-all ingress rule, and the DNS lookup filter. These are the
   shapes whose absence fails open or fails silently.
8. **Two documents, not one.** `docs/deploy/cloudflare-zero-trust.md` is the
   agent-executable runbook for a host you already have;
   `docs/ja/cfzt.md` is a standalone Japanese version that also explains what
   Zero Trust *is*, for readers who have never used it. `IAP-OAUTH.md` stays as
   the explanation and the manual fallback.

## Consequences

- The Cloudflare side becomes five commands, and the unsafe window is gone by
  construction rather than by warning.
- A new prerequisite: an API token with four permissions. `check` probes each one
  and names the missing checkbox as it appears in Cloudflare's UI, because "your
  token cannot do this" is otherwise indistinguishable from "authentication
  error".
- **This script now tracks someone else's API.** Verification against current
  Cloudflare docs while writing it already found one silent breakage: DNS record
  lookup takes `?name.exact=`, and the legacy `?name=` matches nothing — which
  would read as "no record exists" and create duplicates on every run. Treat API
  drift as expected maintenance; the tests exist to make it loud.
- `deploy.local/` keeps its meaning (one operator's own infrastructure) while
  gaining a generated, generic member. The operator-specific scripts there are
  now redundant with the public path and can be retired when convenient.
- `destroy` makes the whole thing retryable, which matters more than it sounds
  for a first-time user who mistyped a hostname.
