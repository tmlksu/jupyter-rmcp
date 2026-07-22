# 0015 — Public release: one generalized repo, deployment specifics git-ignored

**Status:** Accepted (2026-07-22)

## Context

The project started as one person's deployment and its tracked tree recorded
that: a specific VPS hostname, a personal domain and email, absolute paths from
one machine, identity-provider policy names and UUIDs, and five scripts that
shell out to a private Cloudflare Tunnel CLI no one else has. None of it was
secret — no credential was ever committed — but all of it was noise or an
obstacle for anyone else trying to run the thing.

Other data scientists want to use it (mainly Kaggle competitions with Colab
GPUs, see ADR 0014), and the sustainable way to share it is one public
repository they can each deploy from, not a zip or a fork that drifts.

The obvious worry with publishing an existing repo is history. Here the history
contains personal *values*, not secrets — and those same values were in the
working tree, so rewriting history would have bought nothing that scrubbing the
tree did not already buy.

## Decision

1. **Publish a new public repository seeded from a squashed snapshot** of the
   generalized tree, rather than flipping the existing private repo public. The
   private repo is archived as the history record.
2. **The public repo becomes the single canonical repo.** The original
   deployment switches its `origin` to it. There is no ongoing public/private
   sync to maintain, because after generalization there is nothing private left
   in the tracked tree.
3. **Deployment specifics live in git-ignored files, not in branches.** `.env`
   and `secrets/` already worked this way; `deploy.local/` now holds one
   operator's infrastructure tooling (tunnel manifest, identity-provider policy
   scripts) and their private operational notes. An operator's overlay survives
   `git reset --hard`, so tracking the public repo costs them nothing.
4. **Documentation targets a stranger, in English.** Auth is documented as "set
   `MCP_BEARER`, or put an authenticating proxy in front" with one provider as
   an example rather than the design; `docs/GUIDE.md` is the colleague-facing
   walkthrough; `docs/HANDOFF.md` and `docs/REINSTALL.md` — a living session
   scratchpad and a runbook for one specific account — moved to `deploy.local/`,
   partially superseding the docs system in ADR 0008.
5. **MIT license**, chosen for the lowest adoption friction on a small tool. The
   bundled `google-colab-cli` remains Apache-2.0.
6. **"Nothing personal in the tracked tree" becomes an invariant** (recorded in
   CLAUDE.md) with a grep gate over hostnames, domains, emails, absolute paths
   and policy identifiers, run before publishing.

## Consequences

- Colleagues each deploy their own instance against their own Google account;
  nobody connects to anybody else's server, which keeps the original operator's
  "work and this project never touch" rule intact by construction.
- Contributions and issues land in one place, and the operator's own deployment
  gets them by pulling.
- The five Cloudflare/tunnel scripts are no longer version-controlled. That is
  the accepted cost of not shipping unrunnable, account-specific automation; the
  general guidance that replaced them lives in `docs/AUTH.md`.
- Losing `deploy.local/` means losing that tooling, since it is not backed up by
  git. Its contents are reproducible from the provider's dashboard, and the
  alternative — publishing account identifiers — is worse.
- Anyone auditing the project sees an execution server documented honestly as
  one (`docs/SECURITY.md`), which is the right default for software that runs
  arbitrary code.
