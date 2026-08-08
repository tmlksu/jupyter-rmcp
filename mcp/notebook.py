"""Notebook editing tools (path-based, cell-id addressed; nbedit design, ADR 0011).

Cells addressed by stable `id` (preferred) OR positional `index`. `patch_cell`
does a unique-substring replace (cheap edits). A `rev` (content hash) guards
against a concurrent external edit (human in JupyterLab): read `notebook_rev`,
pass it as `expected_rev`; a mutating tool refuses if the file changed since.
Editing is separate from execution — `execute_cell` runs an existing cell in place.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import backends
import reaper
import state
from app import mcp
from backends import local_jupyter
from outputs import _format_outputs


async def _require_notebook(kernel_id: str) -> str:
    p = await local_jupyter._notebook_for_kernel(kernel_id)
    if not p:
        raise RuntimeError("kernel is not bound to a notebook — start_kernel(notebook_path=...) first")
    return p


def _norm_source(cell: dict[str, Any]) -> str:
    s = cell.get("source", "")
    return "".join(s) if isinstance(s, list) else s


def _rev(nb: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(nb.get("cells", []), sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:12]


def _ensure_ids(nb: dict[str, Any]) -> bool:
    changed = False
    seen: set[str] = set()
    for c in nb.get("cells", []):
        cid = c.get("id")
        if not cid or cid in seen:
            c["id"] = uuid.uuid4().hex[:8]
            changed = True
        seen.add(c["id"])
    return changed


def _new_cell(cell_type: str, source: str, summary: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if summary:
        meta["summary"] = summary
    cell: dict[str, Any] = {"cell_type": cell_type, "metadata": meta,
                            "source": source, "id": uuid.uuid4().hex[:8]}
    if cell_type == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return cell


def _cell_summary(cell: dict[str, Any]) -> str:
    s = (cell.get("metadata") or {}).get("summary")
    if s:
        return s
    src = _norm_source(cell)
    if cell.get("cell_type") == "code":
        lead = []
        for line in src.splitlines():
            st = line.strip()
            if st.startswith("#"):
                lead.append(st.lstrip("#").strip())
            elif st == "" and not lead:
                continue
            else:
                break
        return " ".join(lead)[:120]
    for line in src.splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()[:120]
    return ""


def _resolve_target(nb: dict[str, Any], index: int | None, cell_id: str | None) -> int:
    cells = nb.get("cells", [])
    if (index is None) == (cell_id is None):
        raise RuntimeError("pass exactly one of `index` or `cell_id`")
    if cell_id is not None:
        matches = [i for i, c in enumerate(cells) if c.get("id") == cell_id]
        if len(matches) != 1:
            raise RuntimeError(f"cell_id {cell_id!r}: {len(matches)} matches (need exactly 1)")
        return matches[0]
    if not 0 <= index < len(cells):
        raise RuntimeError(f"index {index} out of range (0..{len(cells) - 1})")
    return index


async def _load_nb(path: str) -> dict[str, Any]:
    """Read a notebook, assign+persist stable cell ids, return it. Raises if absent."""
    nb = await local_jupyter._read_nb(path)
    if nb is None:
        raise RuntimeError(f"notebook not found: {path}")
    if _ensure_ids(nb):
        await local_jupyter._write_nb(path, nb)
    return nb


def _check_rev(nb: dict[str, Any], expected_rev: str | None) -> None:
    if expected_rev is not None and _rev(nb) != expected_rev:
        raise RuntimeError(f"notebook changed on disk (expected rev {expected_rev}, got {_rev(nb)}); "
                           "re-read (notebook_rev / list_cells) before editing")


@mcp.tool
async def notebook_rev(path: str) -> dict[str, Any]:
    """Current revision token of a notebook: {path, rev, cells}. `rev` is a content
    hash — pass it as `expected_rev` to an editing tool to guard against a concurrent
    external edit (e.g. a human in JupyterLab). Mutating tools also return the new rev."""
    nb = await _load_nb(path)
    return {"path": path, "rev": _rev(nb), "cells": len(nb.get("cells", []))}


@mcp.tool
async def list_cells(path: str) -> dict[str, Any]:
    """Overview of a notebook's cells (no full source): per cell {index, id, type,
    summary, num_lines, has_outputs, has_error}. `id` is stable across insert/delete/
    move — prefer addressing later edits by `id`. Also returns the notebook `rev`."""
    nb = await _load_nb(path)
    out = []
    for i, c in enumerate(nb.get("cells", [])):
        src = _norm_source(c)
        outs = c.get("outputs", []) or []
        out.append({
            "index": i, "id": c.get("id"), "type": c.get("cell_type"),
            "summary": _cell_summary(c), "num_lines": src.count("\n") + 1 if src else 0,
            "has_outputs": bool(outs),
            "has_error": any(o.get("output_type") == "error" for o in outs),
        })
    return {"path": path, "rev": _rev(nb), "cells": out}


@mcp.tool
async def read_cells(path: str, indices: list[int] | None = None,
                     ids: list[str] | None = None, max_source_chars: int = 8000) -> dict[str, Any]:
    """Read full source (+ a text summary of outputs) for specific cells, by `indices`
    OR `ids` (batch — one round-trip). Omit both to read all cells."""
    nb = await _load_nb(path)
    cells = nb.get("cells", [])
    if ids is not None:
        targets = [_resolve_target(nb, None, cid) for cid in ids]
    elif indices is not None:
        targets = [_resolve_target(nb, i, None) for i in indices]
    else:
        targets = list(range(len(cells)))
    out = []
    for i in targets:
        c = cells[i]
        src = _norm_source(c)
        item: dict[str, Any] = {"index": i, "id": c.get("id"), "type": c.get("cell_type"),
                                "source": src[:max_source_chars],
                                "truncated": len(src) > max_source_chars}
        if c.get("cell_type") == "code":
            _, text = _format_outputs(c.get("outputs", []) or [])
            item["outputs"] = text[:4000]
            item["execution_count"] = c.get("execution_count")
        out.append(item)
    return {"path": path, "rev": _rev(nb), "cells": out}


@mcp.tool
async def insert_cells(path: str, index: int, cells: list[dict[str, Any]],
                       expected_rev: str | None = None) -> dict[str, Any]:
    """Insert several cells at once, contiguously BEFORE `index` (index == cell count
    appends). `cells` = list of {cell_type, source, summary?}. Prefer this over many
    single inserts. Returns {indices, ids, rev}."""
    reaper._ensure_background()
    nb = await _load_nb(path)
    _check_rev(nb, expected_rev)
    idx = max(0, min(index, len(nb["cells"])))
    new = [_new_cell(c.get("cell_type", "code"), c.get("source", ""), c.get("summary")) for c in cells]
    nb["cells"][idx:idx] = new
    await local_jupyter._write_nb(path, nb)
    return {"path": path, "indices": list(range(idx, idx + len(new))),
            "ids": [c["id"] for c in new], "rev": _rev(nb)}


@mcp.tool
async def insert_cell(path: str, index: int, source: str, cell_type: str = "code",
                      summary: str | None = None, expected_rev: str | None = None) -> dict[str, Any]:
    """Insert one cell BEFORE `index` (index == cell count appends). For a block of
    cells prefer insert_cells. Returns {index, id, rev}."""
    r = await insert_cells(path, index, [{"cell_type": cell_type, "source": source, "summary": summary}],
                           expected_rev)
    return {"path": path, "index": r["indices"][0], "id": r["ids"][0], "rev": r["rev"]}


@mcp.tool
async def patch_cell(path: str, old: str, new: str, index: int | None = None,
                     cell_id: str | None = None, expected_rev: str | None = None) -> dict[str, Any]:
    """PREFERRED edit: replace a UNIQUE `old` substring with `new` in one cell (cheap —
    no full-source resend). Target by `cell_id` (stable, preferred) OR `index`. `old`
    must occur exactly once in the cell, else it errors (safer than a wrong guess).
    Does not touch outputs. Returns {index, id, rev}."""
    reaper._ensure_background()
    nb = await _load_nb(path)
    _check_rev(nb, expected_rev)
    i = _resolve_target(nb, index, cell_id)
    src = _norm_source(nb["cells"][i])
    n = src.count(old)
    if n != 1:
        raise RuntimeError(f"`old` occurs {n} times in cell {i} (need exactly 1); "
                           "make it unique or use edit_cell")
    nb["cells"][i]["source"] = src.replace(old, new, 1)
    await local_jupyter._write_nb(path, nb)
    return {"path": path, "index": i, "id": nb["cells"][i].get("id"), "rev": _rev(nb)}


@mcp.tool
async def edit_cell(path: str, source: str, index: int | None = None,
                    cell_id: str | None = None, summary: str | None = None,
                    expected_rev: str | None = None) -> dict[str, Any]:
    """Replace a cell's ENTIRE source (for small changes prefer patch_cell). Target by
    `cell_id` (preferred) OR `index`. `summary` sets metadata summary ("" clears).
    Does not execute or touch outputs. Returns {index, id, rev}."""
    reaper._ensure_background()
    nb = await _load_nb(path)
    _check_rev(nb, expected_rev)
    i = _resolve_target(nb, index, cell_id)
    nb["cells"][i]["source"] = source
    if summary is not None:
        nb["cells"][i].setdefault("metadata", {})["summary"] = summary
        if summary == "":
            nb["cells"][i]["metadata"].pop("summary", None)
    await local_jupyter._write_nb(path, nb)
    return {"path": path, "index": i, "id": nb["cells"][i].get("id"), "rev": _rev(nb)}


@mcp.tool
async def delete_cell(path: str, index: int | None = None, cell_id: str | None = None,
                      expected_rev: str | None = None) -> dict[str, Any]:
    """Delete a cell, addressed by `cell_id` (preferred) OR `index`. Returns {rev, remaining}."""
    reaper._ensure_background()
    nb = await _load_nb(path)
    _check_rev(nb, expected_rev)
    i = _resolve_target(nb, index, cell_id)
    nb["cells"].pop(i)
    await local_jupyter._write_nb(path, nb)
    return {"path": path, "deleted_index": i, "remaining": len(nb["cells"]), "rev": _rev(nb)}


@mcp.tool
async def move_cell(path: str, to_index: int, from_index: int | None = None,
                    from_id: str | None = None, expected_rev: str | None = None) -> dict[str, Any]:
    """Move a cell to `to_index` (final 0-based position). Address the moved cell by
    `from_id` (preferred) OR `from_index`. Returns {rev}."""
    reaper._ensure_background()
    nb = await _load_nb(path)
    _check_rev(nb, expected_rev)
    i = _resolve_target(nb, from_index, from_id)
    cell = nb["cells"].pop(i)
    to = max(0, min(to_index, len(nb["cells"])))
    nb["cells"].insert(to, cell)
    await local_jupyter._write_nb(path, nb)
    return {"path": path, "from": i, "to": to, "id": cell.get("id"), "rev": _rev(nb)}


@mcp.tool
async def execute_cell(kernel_id: str, index: int | None = None, cell_id: str | None = None,
                       timeout: float | None = None) -> dict[str, Any]:
    """Run an EXISTING code cell in the kernel's bound notebook and update THAT cell's
    outputs in place (no duplicate). Target by `cell_id` (stable, preferred) OR `index`.
    Editing (patch_cell/edit_cell) is separate — this only runs a cell as-is.

    Shares the kernel's single execution slot with execute_code: if something is already
    running there this returns status="busy" and runs NOTHING. Never resend a call that
    seemed to fail — collect it with get_last_execution(kernel_id)."""
    reaper._ensure_background()
    path = await _require_notebook(kernel_id)
    nb = await _load_nb(path)
    i = _resolve_target(nb, index, cell_id)
    cell = nb["cells"][i]
    if cell.get("cell_type") != "code":
        raise RuntimeError(f"cell {i} is {cell.get('cell_type')}, not code")
    cid = cell.get("id")
    source = _norm_source(cell)

    async def _run() -> dict[str, Any]:
        reply = await backends._run_on_kernel(kernel_id, source, timeout)
        nb = await _load_nb(path)  # re-read to avoid clobbering concurrent edits; find by id
        matches = [j for j, c in enumerate(nb["cells"]) if c.get("id") == cid]
        if matches:
            nb["cells"][matches[0]]["outputs"] = reply.get("outputs", [])
            nb["cells"][matches[0]]["execution_count"] = reply.get("execution_count")
            await local_jupyter._write_nb(path, nb)
        res = backends._reply_result(reply)
        res.update({"path": path, "id": cid, "rev": _rev(nb)})
        return res

    return await state.run_single_flight(kernel_id, source, "execute_cell", _run)


@mcp.tool
async def create_notebook(path: str, cells: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Create a new notebook (.ipynb) under the work root, optionally seeded with `cells`
    (list of {cell_type, source, summary?}). To inspect/edit existing notebooks use
    list_cells / read_cells / patch_cell etc."""
    if not path.endswith(".ipynb"):
        path += ".ipynb"
    seeded = [_new_cell(c.get("cell_type", "code"), c.get("source", ""), c.get("summary"))
              for c in (cells or [])]
    nb = {"cells": seeded, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    await local_jupyter._write_nb(path, nb)
    return {"path": path, "created": True, "cells": len(seeded), "rev": _rev(nb)}
