# 0009 — Kaggle: server-side credential injection into the kernel env

**Status:** Accepted (2026-07-13) · Phase 1 (local) + Phase 2 (Colab) both done

## Context
We want notebooks to download Kaggle resources (`!kaggle datasets download …`),
but **not** put any Kaggle auth on the mobile client. The Kaggle API key is a
**full-access, unscopable** credential (download, competition submit, create/delete
kernels & datasets — acts as you).

## Decision
Store the Kaggle credential **server-side only** and inject it into the **kernel's
environment**, not the MCP tool layer and never the client. The clean integration
point is the execution environment: whichever backend runs the kernel gets the
credential in its env; notebook code uses the normal `kaggle` CLI/`kagglehub`.

- **Local backend (Phase 1, done):** `KAGGLE_API_TOKEN` (current single-token method;
  legacy `KAGGLE_USERNAME`/`KAGGLE_KEY` also accepted) set in `.env` → injected into
  the jupyter container. `kaggle` + `kagglehub` baked into the image. Downloads go to a
  dedicated `data/kaggle` mount (keeps big data out of the notebooks tree). Verified E2E
  via the MCP: `!kaggle datasets download` → file on the server → pandas load.
- **Colab backend (Phase 2, done):** `colab_setup_kaggle(session)` injects the token
  **on demand** — it **uploads** the token as a file and a setup exec reads it into the
  VM's `KAGGLE_API_TOKEN` env + `~/.kaggle/access_token`, so the token never appears in
  the exec/session history (no literal in code). Verified E2E: env var set + `!kaggle
  download` on a real Colab VM. The token transits to Google's VM for that session.

## Consequences
- Mobile never holds Kaggle auth; the phone just says "download X".
- The token is a strong secret: kept in `.env`/`secrets` (git-ignored), rotate on
  kaggle.com if leaked. **Colab injection means the token is briefly on a Google VM** —
  only inject on sessions where you trust the code (same class of decision as ADC).
- Competitions need website rules-acceptance before API download works.
- Storage: local downloads consume the server's disk (`data/kaggle`); prefer Colab for huge data.
