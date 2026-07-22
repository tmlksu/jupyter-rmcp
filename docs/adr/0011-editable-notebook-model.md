# 0011 — Editable-document notebook model + safe naming

**Status:** Accepted (2026-07-14)

## Context
Notebooks are now human-viewable/editable in JupyterLab ([0012](0012-expose-jupyterlab.md)),
so the agent and a human co-edit the same `.ipynb`. The old model — `execute_code`
**appends** a cell every run — caused two problems: (1) re-running the same work
**duplicates** cells (grows forever, no clean reproduce), and (2) `start_kernel`
create-or-append on a name meant a "new" notebook could silently **merge** into an
existing one. Mature notebook-agent tools (datalayer jupyter-mcp, Jupyter AI) use
cell-addressable editing, not append-only.

## Decision
Adopt the **editable-document model**, taking the editing design from the author's
own **[nbedit-mcp](https://github.com/tmlksu/nbedit-mcp)** (its ADRs on patch
uniqueness, cell-id addressing, and the write-revision guard), reimplemented on our
Jupyter **Contents API** (single file authority; no extra mount) — see
[ADR 0010's rationale for one access path]. Editing is **separate from execution**.

Editing tools (path-based, no execution): `notebook_rev`, `list_cells` (id + summary),
`read_cells` (batch by index/id), `insert_cell`/`insert_cells` (batch), **`patch_cell`
(unique-substring replace — the preferred cheap edit)**, `edit_cell` (full source),
`delete_cell`, `move_cell`, `create_notebook(cells=…)`.
- **Address cells by stable `id`** (assigned + persisted on first read), not the
  positional index that shifts on insert/delete/move.
- **`patch_cell(old, new)`** replaces a *unique* substring — no full-source resend
  (this was the main pain: many full-cell edits were expensive/awkward).
- **`rev` guard:** a content hash; pass a prior `rev` as `expected_rev` and the write
  is refused if the file changed on disk (a human editing in JupyterLab), so you
  re-read instead of clobbering. Mutating tools return the new `rev` to chain edits.
- **Batch** insert/read cut round-trips.

Execution tools (kernel-based): `execute_code(kernel_id, code)` appends+runs an ad-hoc
cell; `execute_cell(kernel_id, cell_id=…)` re-runs an existing cell **in place**
(updates its outputs, no duplicate). Execution is decoupled via `_run_on_kernel`.

**Safe naming:** `start_kernel(notebook_path=None)` → fresh `untitled-N.ipynb`;
`if_exists` = "open" (default, continue) / "new" (auto-suffix) / "error".

## Consequences
- Editing is cheap and safe: `patch_cell` by id + `rev` guard + batch = far fewer,
  smaller tool calls, and co-editing with a human in Lab doesn't clobber.
- Iterating no longer duplicates cells; edit and execute are cleanly separated.
- Not full RTC: same-cell concurrent edits are last-writer-wins, but the `rev` guard
  catches the common "file changed under me" case.
- colab and local behave identically (execution routed by backend; files on the server).
- Superseded from the earlier index-based `add_cell/edit_cell(index, execute=)` design.
