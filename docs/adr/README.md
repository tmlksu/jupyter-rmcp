# Architecture Decision Records

Append-only record of *why* things are the way they are, so we don't re-litigate
settled choices. One decision per file. Format: Status / Context / Decision /
Consequences. When superseding one, add a new ADR and mark the old one Superseded.

New ADR: copy the shape of an existing one, next number, add a row below.

| # | Title | Status |
|---|-------|--------|
| [0001](0001-full-jupyter-server-and-custom-mcp.md) | Full Jupyter Server + custom FastMCP server | Accepted |
| [0002](0002-jupyter-internal-only.md) | Jupyter internal-only; MCP is the only public surface | Accepted |
| [0003](0003-trust-boundary-cloudflare-access.md) | Trust boundary = Cloudflare Access; app bearer disabled | Accepted |
| [0004](0004-mobile-auth-managed-oauth.md) | Claude app auth via Cloudflare Access Managed OAuth | Accepted |
| [0005](0005-notebooks-on-the-server.md) | Notebooks always on the server; remote backends are compute-only | Accepted |
| [0006](0006-kernel-reaping.md) | Kernel reaping: idle timeout + absolute max-age | Accepted |
| [0007](0007-colab-offload-official-cli.md) | Colab offload: official google-colab-cli (primary) + tunnel (alt) | Accepted |
| [0008](0008-docs-system.md) | Docs system: HANDOFF + ADR + CHANGELOG + CLAUDE rituals | Accepted |
| [0009](0009-kaggle-server-side-creds.md) | Kaggle: server-side credential injection into the kernel env | Accepted |
| [0010](0010-unified-notebook-interface.md) | Unified notebook interface (fold colab-cli into the kernel tools) | Accepted |
| [0011](0011-editable-notebook-model.md) | Editable-document notebook model + safe naming | Accepted |
| [0012](0012-expose-jupyterlab.md) | Expose JupyterLab to a human, behind Cloudflare Access | Accepted |
| [0013](0013-refactor-drop-tunnel-persistent-registry.md) | Refactor: drop tunnel backend; persistent registry + backend split | Accepted |
| [0014](0014-colab-only-mode.md) | COLAB_ONLY mode: a deployment that never executes code locally | Accepted |
| [0015](0015-public-release.md) | Public release: one generalized repo, deployment specifics git-ignored | Accepted |
| [0017](0017-single-flight-execution.md) | Single-flight execution: refuse the retry, keep the result | Accepted |
| [0018](0018-soft-reply-deadline.md) | Answer before the client gives up: soft deadline, detach, never interrupt | Accepted |
