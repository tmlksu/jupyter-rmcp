# 0021 — Transfer hardening: bounded fetches, SSRF guard, chunked upload

**Status:** Accepted (2026-10-31)

## Context

`fetch_to_workspace` asked the server to GET an arbitrary URL with
`follow_redirects=True`, buffered the entire body, and only then checked a 200 MB
cap. Two audit findings (#9, and the `download_from_colab` part of #16) made this
the worst-shaped code in the repo:

- **SSRF.** In a `COLAB_ONLY` deployment the fetch tool is the only primitive
  that touches the network off-host. Nothing stopped a caller from fetching
  `http://169.254.169.254/`, a docker-bridge gateway, or an internal service —
  a complete fetch-and-exfiltrate primitive. Scheme limits alone are weak, and a
  resolve-check-then-connect pattern is trivially bypassed by DNS rebinding.
- **Memory.** A size check evaluated *after* `r.content` is not a cap, it is a
  report. 200 MB of body + base64 + JSON in a container with `mem_limit: 256m`
  (compose.slim) is an OOM, not an error message. `download_from_colab` had the
  same shape: unbounded `read_bytes()`, base64 it, one 30 s PUT — a multi-GB
  checkpoint froze the event loop or died trying.

## Decision

**Streaming, with the cap enforced on the wire.** The body is iterated with a
running byte counter and aborted the moment the total passes `MAX_FETCH_BYTES`
(now an env var, default unchanged). A truthful `Content-Length` over the cap
refuses before the body is read at all. Nothing is ever buffered whole — a
response that is unbounded fails bounded.

**SSRF guard, per hop, with the dial pinned.** Targets must be `http`/`https`;
the host is resolved and every returned address must be public — private,
loopback, link-local, reserved, multicast and unspecified are refused, judged
through `ipv4_mapped` so `::ffff:127.0.0.1` is the v4 address it reaches.
Redirects are followed manually (`follow_redirects=False`, max 8 hops) and each
hop re-resolves and re-validates. The actual connection is then *pinned* to the
validated IP — the request URL is rewritten to the IP, the original host is kept
in the `Host` header, and TLS gets `sni_hostname` so SNI and certificate
verification still check the real name — so a second, hostile DNS answer between
validation and connect cannot redirect the dial. `FETCH_ALLOW_PRIVATE=1` is the
opt-out for legitimately fetching from the same LAN.

**Contents-API chunked upload for writes.** Instead of lowering the slim cap or
buffering-then-PUT, both `fetch_to_workspace` and `download_from_colab` write
through `PUT /api/contents/{path}` with `chunk: 1..N, -1` — the
`LargeFileContentsManager`/`AsyncLargeFileManager` protocol that is the
jupyter_server default. Each ~4 MB piece is base64'd, sent, and appended
server-side; the next piece is only read once the previous one is on the wire.
Memory stays ~one chunk at every `MAX_FETCH_BYTES` value, in both containers, so
the 200 MB cap survives as a *data policy* on a 256 MB container. `chunk=1`
replaces the file, so an aborted transfer leaves a partial file rather than a
corrupted old one — and never leaves the old file masquerading as the new one.
This is why `jupyter/requirements.txt` and `Dockerfile.slim` now floor
`jupyter_server>=2.18` (#13 asked for the floor on path-traversal grounds; the
chunked upload gives it a second reason).

**A shared path validator at every tool entry** (`util._check_workspace_path`):
rejects absolute paths, `..` segments, NUL bytes, backslashes, and the same
after URL-decoding (`%2e%2e`, `%00`). The MCP layer no longer leans on
jupyter-server's root-dir check alone. Legitimate inputs are untouched: `""` and
`.` mean the work root, `sub/dir/x.ipynb` and a leading `./` pass.

**Execution workers get a dedicated bounded pool** (`local_jupyter._exec_pool`).
The audit's item-2 question — do orphaned `to_thread` workers starve the default
executor? — is real: a kernel that never answers leaves its
`execute_interactive` worker parked until `_LIB_DEADLINE_SEC` (~30 days), and the
default `asyncio` executor has only `min(32, cpu+4)` threads shared by
`_connect_client`, `kc.stop`, and `list_variables`. The run now happens on a
dedicated `ThreadPoolExecutor(max_workers=max(4, MAX_KERNELS))`; a worker the
waiter abandons while it is still parked is counted (`_orphaned_exec_workers`)
and logged on both the orphaning and the eventual unwind, so the leak is a
metric, not a mystery. The bound keeps a pile-up of dead kernels from consuming
the process; the dedicated pool keeps the pile-up from blocking unrelated
`to_thread` work. A queued-but-abandoned worker is counted the same way.

## Consequences

- `fetch_to_workspace` can no longer be driven into private address space by
  URL or by redirect; the DNS-rebind race is closed by construction, not luck.
- Transfers are bounded end-to-end in both memory and bytes; the failure mode
  for a >cap transfer is a clean error, not an OOM kill.
- An interrupted chunked upload can leave a partial file. That is the correct
  failure: `chunk=1` already replaced the old content, so partial never reads
  as valid.
- `download_from_colab` refuses over-cap files after `stat` — split them on the
  colab side. `upload_to_colab`'s in-memory read and `upload_file` were left
  alone per the audit verdict: their bytes already arrived in memory.
- The base `compose.yaml` `mcp` service now carries `mem_limit: 1g` /
  `cpus: 1.0` (slim keeps its own 256m).
- Operators can tune `MAX_FETCH_BYTES` and `FETCH_ALLOW_PRIVATE` in `.env`.
