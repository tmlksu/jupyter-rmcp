"""Characterization tests for the notebook-editing helpers (nbedit design, ADR 0011)."""
import pytest

import notebook


def _nb(cells):
    return {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


def _code(source, cell_id="c1"):
    return {"cell_type": "code", "id": cell_id, "metadata": {}, "source": source,
            "outputs": [], "execution_count": None}


class TestNormSource:
    def test_string_passthrough(self):
        assert notebook._norm_source({"source": "a\nb"}) == "a\nb"

    def test_list_joined(self):
        assert notebook._norm_source({"source": ["a\n", "b"]}) == "a\nb"

    def test_missing_is_empty(self):
        assert notebook._norm_source({}) == ""


class TestRev:
    def test_deterministic_12_hex(self):
        nb = _nb([_code("x = 1")])
        r1, r2 = notebook._rev(nb), notebook._rev(nb)
        assert r1 == r2
        assert len(r1) == 12
        int(r1, 16)  # hex

    def test_changes_when_cell_changes(self):
        assert notebook._rev(_nb([_code("x = 1")])) != notebook._rev(_nb([_code("x = 2")]))

    def test_ignores_non_cell_metadata(self):
        a, b = _nb([_code("x")]), _nb([_code("x")])
        b["metadata"] = {"something": "else"}
        assert notebook._rev(a) == notebook._rev(b)


class TestEnsureIds:
    def test_fills_missing(self):
        nb = _nb([{"cell_type": "code", "source": "x", "metadata": {}}])
        assert notebook._ensure_ids(nb) is True
        assert nb["cells"][0]["id"]

    def test_dedupes_duplicates(self):
        nb = _nb([_code("a", "dup"), _code("b", "dup")])
        assert notebook._ensure_ids(nb) is True
        ids = [c["id"] for c in nb["cells"]]
        assert len(set(ids)) == 2
        assert ids[0] == "dup"  # first occurrence keeps its id

    def test_untouched_when_all_unique(self):
        nb = _nb([_code("a", "id1"), _code("b", "id2")])
        assert notebook._ensure_ids(nb) is False


class TestNewCell:
    def test_code_cell_has_outputs_and_count(self):
        c = notebook._new_cell("code", "x = 1")
        assert c["outputs"] == [] and c["execution_count"] is None
        assert len(c["id"]) == 8

    def test_markdown_cell_has_no_outputs(self):
        c = notebook._new_cell("markdown", "# hi")
        assert "outputs" not in c and "execution_count" not in c

    def test_summary_goes_to_metadata(self):
        assert notebook._new_cell("code", "x", summary="load data")["metadata"] == {"summary": "load data"}


class TestCellSummary:
    def test_metadata_summary_wins(self):
        c = _code("# comment\nx = 1")
        c["metadata"] = {"summary": "explicit"}
        assert notebook._cell_summary(c) == "explicit"

    def test_code_leading_comments_joined(self):
        assert notebook._cell_summary(_code("# load\n# the data\nx = 1")) == "load the data"

    def test_code_no_comment_is_empty(self):
        assert notebook._cell_summary(_code("x = 1")) == ""

    def test_markdown_first_nonempty_line(self):
        c = {"cell_type": "markdown", "metadata": {}, "source": "\n## Title\nbody"}
        assert notebook._cell_summary(c) == "Title"

    def test_capped_at_120(self):
        c = _code("# " + "a" * 300 + "\nx = 1")
        assert len(notebook._cell_summary(c)) == 120


class TestResolveTarget:
    def test_neither_raises(self):
        with pytest.raises(RuntimeError, match="exactly one"):
            notebook._resolve_target(_nb([_code("x")]), None, None)

    def test_both_raises(self):
        with pytest.raises(RuntimeError, match="exactly one"):
            notebook._resolve_target(_nb([_code("x")]), 0, "c1")

    def test_by_id(self):
        nb = _nb([_code("a", "aa"), _code("b", "bb")])
        assert notebook._resolve_target(nb, None, "bb") == 1

    def test_unknown_id_raises(self):
        with pytest.raises(RuntimeError, match="0 matches"):
            notebook._resolve_target(_nb([_code("a", "aa")]), None, "zz")

    def test_index_out_of_range_raises(self):
        with pytest.raises(RuntimeError, match="out of range"):
            notebook._resolve_target(_nb([_code("a")]), 5, None)

    def test_by_index(self):
        assert notebook._resolve_target(_nb([_code("a"), _code("b", "b2")]), 1, None) == 1


class TestCheckRev:
    def test_none_always_passes(self):
        notebook._check_rev(_nb([_code("x")]), None)

    def test_matching_passes(self):
        nb = _nb([_code("x")])
        notebook._check_rev(nb, notebook._rev(nb))

    def test_mismatch_raises(self):
        with pytest.raises(RuntimeError, match="changed on disk"):
            notebook._check_rev(_nb([_code("x")]), "deadbeef0000")
