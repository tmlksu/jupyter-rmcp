# 0005 — Notebooks always on the server; remote backends are compute-only

**Status:** Accepted (2026-07-12)

## Context
With Colab/remote compute in the picture, notebooks could live on the server, on Colab,
or in Google Drive. Two sources of truth invite sync pain, and Colab storage is
ephemeral (runtime death loses files).

## Decision
**The server (`local` backend) is the single source of truth for notebooks.** Remote
backends (Colab tunnel or colab-cli) are **compute-only**. `execute_code` /
`colab_exec` write the executed cell + outputs back to the `.ipynb` **on the server**,
regardless of where the kernel ran.

## Consequences
- Notebooks survive kernel reaping, Colab death, and VPS swaps (they're in
  `data/notebooks`, bind-mounted). Matches the "notebooks must persist" goal.
- Kernel variables are still ephemeral (reaped) — re-run cells to restore, Colab-style.
- Data *artifacts* created on Colab's filesystem are NOT auto-saved; push them to
  Drive or pull back deliberately.
