# 0010 — Unified notebook interface (fold colab-cli into the kernel tools)

**Status:** Accepted (2026-07-13) · supersedes the separate `colab_*` tool set from 0007.
Tunnel-Colab backend dropped 2026-07 (ADR 0013, refactor Phase 0).

## Context
We had two parallel interfaces: `start_kernel`/`execute_code`/`stop_kernel`
(local + tunnel-Colab) and a separate `colab_new`/`colab_exec`/`colab_stop` set
(official colab-cli). Real-world use showed the LLM **reaches for the prominent
`colab_*` GPU tools by default**, provisioning a Colab VM (slow, spends compute
units) even for trivial work, and the two interfaces have different handles
(kernel_id vs session) and return shapes — no single mental model.

## Decision
**One interface.** colab-cli becomes a backend `kind: "cli"` inside the existing
multi-backend model. All work goes through:
`start_kernel(backend="local"|"colab", gpu=…)` → `execute_code(kernel_id, …)` →
`stop_kernel` / `list_kernels`, plus `setup_kaggle(kernel_id)` (no-op on local).
The old `colab_new/colab_exec/colab_list/colab_stop/colab_setup_kaggle` tools are
**removed**. **Default is `local`**; `backend="colab"` is opt-in for GPU/TPU/heavy
compute, steered by the MCP `instructions` and the `start_kernel` docstring.

## Consequences
- One mental model; the LLM defaults to local and only escalates to Colab when a
  GPU is actually needed → fewer wasted compute units.
- `execute_code`/`stop_kernel`/`restart_kernel`/`list_kernels` route by backend
  kind internally (colab-cli via subprocess; jupyter via WS/REST). Interface is
  uniform; `interrupt_kernel` is unsupported on colab (returns a clear note).
- colab-cli sessions are reaped by the **absolute max-age cap only** (cost safety
  net); Colab's own idle-recycle handles the rest. Never auto-drop the `local` or
  `colab` backend registration.
- Verified E2E: identical tools drive local and colab; cross-backend write-back to
  the same server-side notebook; `list_backends` shows both with kind + reachability.
