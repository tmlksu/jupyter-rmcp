# 0006 — Kernel reaping: idle timeout + absolute max-age

**Status:** Accepted (2026-07-12)

## Context
Kernels hold RAM/CPU on the VPS. An idle-only timeout lets a kernel that's kept
warm by periodic activity live forever. We wanted Colab-like "it turns off after
a while."

## Decision
The MCP reaper (single source of truth; Jupyter's own culler disabled) applies
**two** rules over all backends every `REAPER_INTERVAL_SEC`:
1. **Absolute max-age hard cap** (`KERNEL_MAX_AGE_SEC`, default 8 h; 0 disables) —
   reap regardless of state or pin.
2. **Idle timeout** (`KERNEL_IDLE_TIMEOUT_SEC`, default 1 h) — reap idle, unpinned
   kernels. `pin_kernel` exempts idle only, never the max-age cap.

Idle is based on actual kernel execution, not transport/connections.

## Consequences
- Nothing lives past the hard cap; a kept-warm kernel still dies.
- All knobs are env-configurable. `list_kernels` reports `idle_seconds` and `age_seconds`.
- `MAX_KERNELS` caps concurrent **local** kernels (VPS protection); Colab kernels
  aren't counted (Google's resources).
