"""End-to-end smoke test against the running MCP endpoint (host loopback).

Assert-based regression gate: exits 0 with `SMOKE OK (N checks)` on success,
prints `SMOKE FAIL: ...` and exits 1 on the first failing check. This is the
tool-surface + local-backend E2E gate for the refactor phases
(docs/refactor/README.md) — run it after every `docker compose up -d --build mcp`.

Local backend only (colab costs compute units; the live-colab gate is a separate
manual checklist in the phase specs). Against a COLAB_ONLY server — detected from
/health — the local-execution checks are replaced by refusal checks, so the run
still costs nothing: notebook editing and workspace tools are exercised as usual.

Usage: python scripts/smoke_test.py [http://127.0.0.1:7130/mcp] [bearer]
Reads bearer from arg or ../.env (MCP_BEARER) if omitted (empty when an
authenticating proxy in front of the server is the trust boundary).
"""
import asyncio
import os
import sys
from pathlib import Path

import httpx
from fastmcp import Client

# The EXACT MCP tool surface. Frozen during the refactor: update ONLY when a
# docs/refactor/ phase spec says so (Phase 0 removed the two tunnel-backend
# tools -> 31 names), in the same commit, together with
# tests/test_tool_surface.py. Do NOT weaken this to a subset check.
EXPECTED_TOOLS = {
    "list_kernels", "start_kernel", "stop_kernel", "restart_kernel",
    "interrupt_kernel", "pin_kernel", "colab_log",
    "execute_code", "get_job", "get_execution", "execute_cell", "list_variables",
    "notebook_rev", "list_cells", "read_cells", "insert_cells", "insert_cell",
    "patch_cell", "edit_cell", "delete_cell", "move_cell",
    "list_backends", "setup_kaggle", "setup_hf",
    "upload_to_colab", "download_from_colab",
    "list_notebooks", "list_files", "create_folder", "upload_file",
    "fetch_to_workspace", "create_notebook",
}

NB = "smoke.ipynb"
_checks = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _checks
    if not cond:
        raise AssertionError(f"{name}: {detail}" if detail else name)
    _checks += 1
    print(f"OK  {name}")


def _load_bearer() -> str:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("MCP_BEARER="):
                return line.split("=", 1)[1].strip()
    return ""


async def _colab_only_checks(client: Client) -> None:
    """Everything a colab-only server must still do, and the one thing it must refuse.
    Deliberately starts no Colab VM: provisioning one costs compute units, so the live
    round-trip stays a manual step (docs/GUIDE.md)."""
    r = await client.call_tool("list_backends", {})
    entries = {e["backend"]: e for e in r.data}
    check("local backend still listed", "local" in entries, str(r.data))
    check("local exec disabled", entries["local"].get("exec_enabled") is False, str(entries))
    check("colab backend available", entries.get("colab", {}).get("exec_enabled") is True,
          str(entries))

    try:
        await client.call_tool("start_kernel", {"backend": "local", "notebook_path": NB})
        raise AssertionError("start_kernel(backend='local') was NOT refused")
    except Exception as e:  # noqa: BLE001
        check("local kernel refused", "disabled" in str(e), str(e)[:200])

    # Notebook + workspace tools are execution-free and must keep working.
    r = await client.call_tool("create_notebook", {"path": NB})
    check("create_notebook", r.data.get("created") is True, str(r.data))

    r = await client.call_tool("insert_cell",
                               {"path": NB, "index": 0, "source": "print('EDIT_ORIG', 40 + 2)"})
    cell_id = r.data.get("id")
    check("insert_cell", bool(cell_id), str(r.data))

    r = await client.call_tool("patch_cell",
                               {"path": NB, "old": "EDIT_ORIG", "new": "EDIT_PATCHED",
                                "cell_id": cell_id})
    check("patch_cell", r.data.get("id") == cell_id, str(r.data))

    r = await client.call_tool("read_cells", {"path": NB, "ids": [cell_id]})
    src = r.data["cells"][0].get("source", "")
    check("read_cells sees patch", "EDIT_PATCHED" in src and "EDIT_ORIG" not in src, src)

    r = await client.call_tool("list_notebooks", {"path": ""})
    check("list_notebooks shows nb", any(n.get("path") == NB for n in r.data), str(r.data))

    r = await client.call_tool("list_files", {"path": ""})
    check("list_files works", isinstance(r.data, list) and len(r.data) > 0, str(r.data))


async def _soft_deadline(mcp_url: str) -> float:
    """The server's ACTUAL reply deadline, so the timing check tests the deployment
    rather than a constant this script happens to agree with."""
    health = mcp_url.rsplit("/mcp", 1)[0] + "/health"
    async with httpx.AsyncClient(timeout=10) as h:
        return float((await h.get(health)).json().get("soft_reply_deadline_sec") or 45)


async def _detect_colab_only(mcp_url: str) -> bool:
    """Ask /health which deployment mode the server runs in (the route predates the
    field, so a missing key means a normal local+colab server)."""
    health = mcp_url.rsplit("/mcp", 1)[0] + "/health"
    async with httpx.AsyncClient(timeout=10) as h:
        return bool((await h.get(health)).json().get("colab_only"))


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7130/mcp"
    bearer = sys.argv[2] if len(sys.argv) > 2 else (os.environ.get("MCP_BEARER") or _load_bearer())
    colab_only = await _detect_colab_only(url)
    print(f"mode: {'colab-only' if colab_only else 'local+colab'}\n")
    client = Client(url, auth=bearer or None)
    async with client:
        # -- tool surface (exact set) --
        tools = await client.list_tools()
        names = {t.name for t in tools}
        check("tool surface exact", names == EXPECTED_TOOLS,
              f"added={sorted(names - EXPECTED_TOOLS)} removed={sorted(EXPECTED_TOOLS - names)}")

        if colab_only:
            await _colab_only_checks(client)
            print(f"\nSMOKE OK ({_checks} checks, colab-only mode)")
            return

        # -- kernel lifecycle + execution (local backend) --
        r = await client.call_tool("start_kernel", {"notebook_path": NB})
        kid = r.data.get("kernel_id")
        check("start_kernel returns kernel_id", bool(kid), str(r.data))
        check("start_kernel bound to notebook", r.data.get("notebook_path") == NB, str(r.data))

        try:
            r = await client.call_tool("execute_code",
                                       {"kernel_id": kid, "code": "x = 6*7\nprint('answer', x)"})
            check("execute ok", r.data.get("status") == "ok", str(r.data))
            check("execute stdout", "answer 42" in r.data.get("text", ""), str(r.data))

            r = await client.call_tool("execute_code", {"kernel_id": kid, "code": "x + 1"})
            check("variable persists", "43" in r.data.get("text", ""), str(r.data))

            # -- single-flight: a duplicate must be REFUSED, never queued (ADR 0017) --
            # The regression this guards: a client gives up on a slow call, resends the
            # same code, and the kernel runs it a second time. `_sf` counts real runs.
            slow = "import time\n_sf = globals().get('_sf', 0) + 1\ntime.sleep(6)\nprint('sf', _sf)"
            first = asyncio.ensure_future(
                client.call_tool("execute_code", {"kernel_id": kid, "code": slow, "timeout": 60}))
            await asyncio.sleep(2)
            async with Client(url, auth=bearer or None) as retry_client:   # the "failed" call resent
                r = await retry_client.call_tool("execute_code", {"kernel_id": kid, "code": slow})
                check("duplicate execute refused", r.data.get("status") == "busy", str(r.data))
                check("busy names it a retry", r.data.get("is_retry") is True, str(r.data))
                check("busy reports elapsed",
                      (r.data.get("running") or {}).get("elapsed_seconds", 0) > 0, str(r.data))

                r = await retry_client.call_tool("get_execution", {"kernel_id": kid})
                check("get_execution sees it running",
                      r.data.get("status") == "running", str(r.data))
            check("first execution completed", (await first).data.get("status") == "ok",
                  str((await first).data))

            r = await client.call_tool("get_execution", {"kernel_id": kid})
            check("get_execution returns the result", "sf 1" in r.data.get("text", ""),
                  str(r.data))

            r = await client.call_tool("execute_code", {"kernel_id": kid, "code": "print('runs', _sf)"})
            check("refused duplicate never ran", "runs 1" in r.data.get("text", ""), str(r.data))

            # -- soft deadline: answer before the client gives up, never kill the work --
            # Verified against the server's real SOFT_REPLY_DEADLINE_SEC, so this asserts
            # the deployed value is actually short enough to beat a client timeout.
            soft = await _soft_deadline(url)
            slow2 = (f"import time, sys\nfor _i in range({int(soft) + 12}):\n"
                     "    print('tick', _i, flush=True)\n    time.sleep(1)\nprint('SLOW_DONE')")
            t0 = asyncio.get_running_loop().time()
            r = await client.call_tool("execute_code", {"kernel_id": kid, "code": slow2})
            elapsed = asyncio.get_running_loop().time() - t0
            check("slow execution answers early", r.data.get("status") == "still_running", str(r.data))
            check("answered within the deadline", elapsed < soft + 10, f"{elapsed:.1f}s > {soft}s")
            exec_id = r.data.get("exec_id")
            check("still_running carries an exec_id", bool(exec_id), str(r.data))
            check("still_running shows live output", "tick" in r.data.get("partial_output", ""),
                  str(r.data)[:300])

            r = await client.call_tool("execute_code", {"kernel_id": kid, "code": "print('x')"})
            check("detached execution still holds the kernel", r.data.get("status") == "busy",
                  str(r.data))

            for _ in range(6):   # long-poll until the detached work completes
                r = await client.call_tool("get_execution",
                                           {"kernel_id": kid, "exec_id": exec_id, "wait_seconds": 20})
                if r.data.get("status") != "running":
                    break
            check("detached execution completes", r.data.get("status") == "ok", str(r.data)[:300])
            check("detached output is collected", "SLOW_DONE" in r.data.get("text", ""),
                  str(r.data)[:300])
            check("detached execution wrote back", r.data.get("saved_to") == NB, str(r.data)[:300])
            check("nothing was interrupted", "KeyboardInterrupt" not in r.data.get("text", ""),
                  str(r.data)[:300])

            r = await client.call_tool("execute_code", {"kernel_id": kid, "code": "1/0"})
            check("error status", r.data.get("status") == "error", str(r.data))
            check("error names exception", "ZeroDivisionError" in r.data.get("text", ""),
                  r.data.get("text", "")[:120])

            r = await client.call_tool("execute_code",
                                       {"kernel_id": kid, "code": "import time; time.sleep(30)",
                                        "timeout": 2})
            check("timeout status", r.data.get("status") == "timeout", str(r.data))
            check("timed_out flag", r.data.get("timed_out") is True, str(r.data))

            # -- a kernel restarted UNDER a detached execution must not wedge it --
            # The execution's reply can never arrive after this (the library waits for an
            # idle whose parent header the new kernel process will never send), so the
            # liveness watchdog is the only thing that releases the single-flight claim.
            # Without it every later call on this kernel returns "busy" forever.
            r = await client.call_tool("execute_code",
                                       {"kernel_id": kid, "code": "import time; time.sleep(600)"})
            check("long sleep detaches", r.data.get("status") == "still_running", str(r.data))
            r = await client.call_tool("restart_kernel", {"kernel_id": kid})
            check("restart under a detached exec", r.data.get("status") == "restarted", str(r.data))
            for _ in range(9):   # watchdog needs two consecutive non-busy observations
                r = await client.call_tool("get_execution", {"kernel_id": kid, "wait_seconds": 20})
                if r.data.get("status") != "running":
                    break
            check("wedged claim is released", r.data.get("status") != "running", str(r.data)[:300])
            r = await client.call_tool("execute_code", {"kernel_id": kid, "code": "print('alive')"})
            check("kernel is usable again", r.data.get("status") == "ok", str(r.data)[:300])
            check("kernel really re-executes", "alive" in r.data.get("text", ""), str(r.data)[:200])

            r = await client.call_tool("list_kernels", {})
            mine = [k for k in r.data if k.get("kernel_id") == kid]
            check("list_kernels shows kernel", len(mine) == 1, str(r.data))
            check("kernel backend is local", mine[0].get("backend") == "local", str(mine))

            r = await client.call_tool("list_notebooks", {"path": ""})
            check("list_notebooks shows nb", any(n.get("path") == NB for n in r.data), str(r.data))

            r = await client.call_tool("list_files", {"path": ""})
            check("list_files works", isinstance(r.data, list) and len(r.data) > 0, str(r.data))

            # -- notebook editing round-trip (ADR 0011 surface) --
            r = await client.call_tool("notebook_rev", {"path": NB})
            rev = r.data.get("rev")
            check("notebook_rev returns rev", isinstance(rev, str) and len(rev) == 12, str(r.data))

            r = await client.call_tool("insert_cell",
                                       {"path": NB, "index": 0,
                                        "source": "print('EDIT_ORIG', 40 + 2)",
                                        "expected_rev": rev})
            cell_id = r.data.get("id")
            check("insert_cell (with expected_rev)", bool(cell_id) and bool(r.data.get("rev")),
                  str(r.data))

            # KNOWN QUIRK (see docs/refactor/README.md): the rev RETURNED by a mutating
            # tool is computed from the in-memory notebook and can differ from the next
            # on-disk read (Jupyter Contents API normalizes on save), so chaining the
            # returned rev into the next expected_rev can spuriously fail. Re-read.
            r = await client.call_tool("notebook_rev", {"path": NB})
            rev = r.data.get("rev")

            r = await client.call_tool("patch_cell",
                                       {"path": NB, "old": "EDIT_ORIG", "new": "EDIT_PATCHED",
                                        "cell_id": cell_id, "expected_rev": rev})
            check("patch_cell", r.data.get("id") == cell_id, str(r.data))

            r = await client.call_tool("read_cells", {"path": NB, "ids": [cell_id]})
            src = r.data["cells"][0].get("source", "")
            check("read_cells sees patch", "EDIT_PATCHED" in src and "EDIT_ORIG" not in src, src)

            r = await client.call_tool("execute_cell", {"kernel_id": kid, "cell_id": cell_id})
            check("execute_cell ok", r.data.get("status") == "ok", str(r.data))
            check("execute_cell output", "EDIT_PATCHED 42" in r.data.get("text", ""), str(r.data))

            r = await client.call_tool("delete_cell", {"path": NB, "cell_id": cell_id})
            check("delete_cell", r.data.get("deleted_index") is not None, str(r.data))
        finally:
            r = await client.call_tool("stop_kernel", {"kernel_id": kid})
            check("stop_kernel", r.data.get("status") == "stopped", str(r.data))

    print(f"\nSMOKE OK ({_checks} checks)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(f"\nSMOKE FAIL: {e}")
        sys.exit(1)
