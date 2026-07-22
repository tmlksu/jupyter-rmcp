# 0001 — Full Jupyter Server + custom FastMCP server

**Status:** Accepted (2026-07-12)

## Context
We need Claude (incl. mobile) to interactively run and persist notebooks on the
deployment host.
Options considered: Jupyter Kernel Gateway (kernels only, no notebook storage);
datalayer `jupyter-mcp-server` (mature, but notebook/cell-centric with implicit
per-notebook kernels — no first-class start/stop/list + idle-timeout control);
building a custom MCP over a full Jupyter Server.

## Decision
Run a **full headless Jupyter Server** (kernels + sessions + contents API) and a
**custom FastMCP server** that drives it. Use `jupyter-kernel-client` for the
kernel WebSocket protocol; `httpx` for REST lifecycle.

## Consequences
- Full control over session semantics (explicit kernel lifecycle, idle/max-age
  reaping, notebook write-back) — which was an explicit requirement.
- We own more code than adopting datalayer's server, but reuse `jupyter-kernel-client`
  for the messy WS parts. `jupyter-mcp-server` remains a naming/UX reference.
- Notebook persistence comes "for free" via the Contents API.
