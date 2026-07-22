"""Unit tests for the persistent kernel registry (Phase 1).

registry.py is standalone (no server import), so these use tmp_path directly and
never touch the network or a live server.
"""
import datetime as dt

from registry import KernelRegistry


def _path(tmp_path):
    return str(tmp_path / "kernels.json")


def test_roundtrip_across_instances(tmp_path):
    p = _path(tmp_path)
    r = KernelRegistry(p)
    r.track("k-uuid", "local", notebook="a.ipynb")
    r.track("rmcp-abcd", "colab")
    r.pin("k-uuid")
    r.touch_heartbeat("rmcp-abcd")

    r2 = KernelRegistry(p)   # fresh instance, same file
    assert r2.all() == r.all()
    assert r2.get_backend("k-uuid") == "local"
    assert r2.get_notebook("k-uuid") == "a.ipynb"
    assert r2.is_pinned("k-uuid") is True
    assert r2.get_backend("rmcp-abcd") == "colab"
    assert isinstance(r2.last_heartbeat("rmcp-abcd"), dt.datetime)
    assert isinstance(r2.first_seen("k-uuid"), dt.datetime)


def test_get_backend_unknown_is_none(tmp_path):
    r = KernelRegistry(_path(tmp_path))
    assert r.get_backend("nope") is None
    assert r.get_notebook("nope") is None
    assert r.first_seen("nope") is None
    assert r.last_heartbeat("nope") is None
    assert r.is_pinned("nope") is False


def test_absent_file_is_empty(tmp_path):
    r = KernelRegistry(str(tmp_path / "does-not-exist.json"))
    assert r.all() == {}


def test_corrupt_file_starts_empty(tmp_path):
    p = tmp_path / "kernels.json"
    p.write_bytes(b"\x00\x01 not json at all {{{")
    r = KernelRegistry(str(p))          # must NOT raise
    assert r.all() == {}
    r.track("k", "local")               # still usable
    assert KernelRegistry(str(p)).get_backend("k") == "local"


def test_non_object_root_starts_empty(tmp_path):
    p = tmp_path / "kernels.json"
    p.write_text("[1, 2, 3]")
    assert KernelRegistry(str(p)).all() == {}


def test_track_preserves_first_seen_updates_backend(tmp_path):
    r = KernelRegistry(_path(tmp_path))
    r.track("k", "local", notebook="a.ipynb")
    fs = r.first_seen("k")
    r.track("k", "colab")               # re-track: backend changes, first_seen kept
    assert r.get_backend("k") == "colab"
    assert r.first_seen("k") == fs
    assert r.get_notebook("k") == "a.ipynb"   # notebook untouched when not given


def test_forget_removes_entry(tmp_path):
    r = KernelRegistry(_path(tmp_path))
    r.track("k", "local")
    r.forget("k")
    assert r.get_backend("k") is None
    assert r.all() == {}


def test_unpin(tmp_path):
    r = KernelRegistry(_path(tmp_path))
    r.track("k", "local")
    r.pin("k")
    assert r.is_pinned("k") is True
    r.unpin("k")
    assert r.is_pinned("k") is False


def test_ids_for_backend(tmp_path):
    r = KernelRegistry(_path(tmp_path))
    r.track("a", "local")
    r.track("b", "local")
    r.track("c", "colab")
    assert sorted(r.ids_for_backend("local")) == ["a", "b"]
    assert r.ids_for_backend("colab") == ["c"]


def test_every_mutation_leaves_valid_json_no_tmp_residue(tmp_path):
    import json
    p = _path(tmp_path)
    r = KernelRegistry(p)
    for i in range(5):
        r.track(f"k{i}", "local", notebook=f"nb{i}.ipynb")
        r.pin(f"k{i}")
        r.touch_heartbeat(f"k{i}")
        with open(p, encoding="utf-8") as f:
            json.load(f)                # valid JSON after each mutation
    # atomicity smoke: os.replace leaves no .tmp files behind
    assert not list(tmp_path.glob(".kernels-*.tmp"))
    assert not list(tmp_path.glob("*.tmp"))
