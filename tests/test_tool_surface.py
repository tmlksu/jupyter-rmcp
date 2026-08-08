"""Tool-surface freeze: the EXACT set of MCP tool names.

This is the cross-phase interface gate for the refactor (docs/refactor/README.md):
the MCP tool surface is FROZEN during the refactor. Do NOT weaken this to a
subset/superset check — an exact-set mismatch failing IS the design. Update the
set ONLY when a phase spec explicitly says so (Phase 0 removed the two
tunnel-backend tools -> 31 names, in the same commit).

scripts/smoke_test.py carries the same set (EXPECTED_TOOLS) — keep them in sync.
"""
from fastmcp import Client

import server

EXPECTED_TOOLS = {
    # kernel lifecycle
    "list_kernels", "start_kernel", "stop_kernel", "restart_kernel",
    "interrupt_kernel", "pin_kernel", "colab_log",
    # execution
    "execute_code", "get_job", "get_execution", "execute_cell", "list_variables",
    # notebook editing (ADR 0011)
    "notebook_rev", "list_cells", "read_cells", "insert_cells", "insert_cell",
    "patch_cell", "edit_cell", "delete_cell", "move_cell",
    # backends / colab
    "list_backends", "setup_kaggle", "setup_hf",
    "upload_to_colab", "download_from_colab",
    # workspace / contents
    "list_notebooks", "list_files", "create_folder", "upload_file",
    "fetch_to_workspace", "create_notebook",
}


async def test_tool_names_exact():
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS, (
        f"tool surface changed!\n  added: {sorted(names - EXPECTED_TOOLS)}\n"
        f"  removed: {sorted(EXPECTED_TOOLS - names)}\n"
        "If this is intentional it must be called for by a docs/refactor/ phase spec; "
        "update scripts/smoke_test.py EXPECTED_TOOLS in the same commit."
    )


async def test_tool_count():
    assert len(EXPECTED_TOOLS) == 32  # +get_execution (ADR 0017); Phase 0 dropped the two tunnel tools
