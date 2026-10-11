"""Regression tests for the audit fix batch.

- #7  colab heartbeat must not block on a held session lock (reaper starvation)
- #8  _enforce_capacity must run on the /api/sessions path (it was dead code)
- #10 BoundedSink: merged same-stream outputs + head+tail budget
- #11 introspection (get_job poll, list_variables) answers kernel_busy instead of
      queueing — and NEVER interrupts; list_variables refuses non-local backends
- #14 the in-kernel job poll reads a seek window, not the whole .out file
- #17 _locks is dropped on kernel delete; /health doesn't leak internals
- #6  the /mcp Origin guard (TestClient against server.app)
"""
import asyncio
import json
import time

import pytest

import backends
import jobs
import kernels
import reaper
import state
from backends import colab_cli, local_jupyter
from registry import KernelRegistry
from util import _now


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Fresh per-kernel maps + registry per test; never spawn the reaper."""
    monkeypatch.setattr(state, "_locks", {})
    monkeypatch.setattr(state, "_inflight", {})
    monkeypatch.setattr(state, "_last_exec", {})
    monkeypatch.setattr(state, "_registry", KernelRegistry(str(tmp_path / "kernels.json")))
    monkeypatch.setattr(reaper, "_reaper_started", True)


def _stream(name: str, text: str) -> dict:
    return {"output_type": "stream", "name": name, "text": text}


# ---- #7: heartbeat skipped while the session lock is held -------------------
class TestHeartbeatSkip:
    async def test_locked_session_lock_skips_the_ping(self, monkeypatch):
        calls = []

        async def fake_cli(*a, **k):
            calls.append(a)
            return 0, ""
        monkeypatch.setattr(colab_cli, "_colab_cli", fake_cli)
        lock = state._locks.setdefault("rmcp-x", asyncio.Lock())
        await lock.acquire()
        try:
            await colab_cli._heartbeat_colab("rmcp-x", _now())
        finally:
            lock.release()
        assert calls == []   # a running exec already keeps the VM awake

    async def test_free_lock_still_pings(self, monkeypatch):
        calls = []

        async def fake_cli(*a, **k):
            calls.append(a)
            return 0, ""
        monkeypatch.setattr(colab_cli, "_colab_cli", fake_cli)
        await colab_cli._heartbeat_colab("rmcp-y", _now())
        assert len(calls) == 1 and calls[0][0] == "exec"


# ---- #8: _enforce_capacity on the sessions path ------------------------------
class TestEnforceCapacity:
    async def test_called_before_sessions_post(self, monkeypatch):
        order = []

        async def _cap():
            order.append("capacity")
        monkeypatch.setattr(reaper, "_enforce_capacity", _cap)

        async def _rest_post(path, json=None, backend="local"):
            order.append(path)
            assert path == "/api/sessions"
            return {"id": "sess-1", "path": "nb.ipynb",
                    "kernel": {"id": "kid-1", "name": "python3"}}
        monkeypatch.setattr(local_jupyter, "_rest_post", _rest_post)

        async def _resolve(_path, _if_exists):
            return "nb.ipynb"
        monkeypatch.setattr(local_jupyter, "_resolve_notebook_path", _resolve)

        async def _noop(*_a, **_k):
            return None
        monkeypatch.setattr(local_jupyter, "_ensure_notebook", _noop)
        monkeypatch.setattr(local_jupyter, "_wait_ready", _noop)

        r = await kernels.start_kernel(backend="local", notebook_path="nb.ipynb")

        assert r["kernel_id"] == "kid-1"
        assert order == ["capacity", "/api/sessions"]


# ---- #10: BoundedSink --------------------------------------------------------
class TestBoundedSink:
    def test_consecutive_same_stream_outputs_merge(self):
        s = state.BoundedSink()
        for i in range(5):
            s.append(_stream("stdout", f"line{i}\n"))
        assert len(s) == 1
        assert s[0]["text"] == "".join(f"line{i}\n" for i in range(5))

    def test_different_stream_names_do_not_merge(self):
        s = state.BoundedSink()
        s.append(_stream("stdout", "a"))
        s.append(_stream("stderr", "b"))
        s.append(_stream("stdout", "c"))
        assert len(s) == 3
        assert [o["name"] for o in s] == ["stdout", "stderr", "stdout"]

    def test_head_and_tail_kept_with_elision_marker(self):
        s = state.BoundedSink()
        budget = state._SINK_STREAM_CHARS
        s.append(_stream("stdout", "H" * budget))
        s.append(_stream("stdout", "T" * budget))
        text = s[0]["text"]
        assert len(s) == 1
        assert state._STREAM_ELIDED in text
        assert text.startswith("H" * (budget // 2))
        assert text.endswith("T" * (budget // 2))
        assert len(text) <= budget + len(state._STREAM_ELIDED)

    def test_tail_slides_forward_on_later_appends(self):
        s = state.BoundedSink()
        s.append(_stream("stdout", "A" * (state._SINK_STREAM_CHARS + 100)))
        s.append(_stream("stdout", "ZZZ"))
        assert s[0]["text"].endswith("ZZZ")
        assert s[0]["text"].count(state._STREAM_ELIDED) == 1

    def test_entry_count_is_bounded(self, monkeypatch):
        monkeypatch.setattr(state, "_SINK_MAX_OUTPUTS", 10)
        s = state.BoundedSink()
        for i in range(50):
            s.append({"output_type": "execute_result", "data": {"text/plain": str(i)}})
        assert len(s) == 10
        assert s[-1]["data"]["text/plain"] == "49"   # the newest survives

    def test_sink_text_stays_readable(self):
        s = state.BoundedSink()
        s.append(_stream("stdout", "hello\n"))
        assert "hello" in state._sink_text(s)


# ---- #11: introspection never blocks and never interrupts --------------------
class TestPollJobBusy:
    async def test_foreground_exec_returns_kernel_busy(self, monkeypatch):
        state._inflight["kid-1"] = {
            "exec_id": "exec-abc", "tool": "execute_code", "code_head": "x=1",
            "code_sha": "sha", "started_at": "2026-10-11T00:00:00+00:00",
            "started": time.monotonic(), "sink": state.BoundedSink(),
            "detached": False, "abandoned": False}

        async def _no_run(*_a, **_k):
            raise AssertionError("poll must not reach the kernel while busy")
        monkeypatch.setattr(jobs.backends, "_run_on_kernel", _no_run)

        r = await jobs._poll_job("kid-1", "job-1", None, 4000)

        assert r["status"] == "kernel_busy"
        assert r["running"]["exec_id"] == "exec-abc"

    async def test_out_of_band_busy_kernel_returns_kernel_busy(self, monkeypatch):
        """A JupyterLab cell (invisible to running_exec) -> kernel_busy, nothing sent."""
        state._registry.track("kid-1", "local")

        async def _kernels_list(backend="local"):
            return [{"id": "kid-1", "execution_state": "busy"}]
        monkeypatch.setattr(local_jupyter, "_kernels", _kernels_list)

        sent = []

        async def _run(*a, **_k):
            sent.append(a)
            return {"status": "ok", "outputs": [], "timed_out": False}
        monkeypatch.setattr(jobs.backends, "_run_on_kernel", _run)

        r = await jobs._poll_job("kid-1", "job-1", None, 4000)

        assert r["status"] == "kernel_busy"
        assert r["running"] is None
        assert sent == []

    async def test_held_lock_returns_busy_and_nothing_is_interrupted(self, monkeypatch):
        """The exec lock is held (out-of-band or a just-started exec): the poll must
        answer kernel_busy — and must NOT hit /interrupt (ADR 0018)."""
        state._registry.track("kid-1", "local")

        async def _kernels_list(backend="local"):
            return [{"id": "kid-1", "execution_state": "idle"}]
        monkeypatch.setattr(local_jupyter, "_kernels", _kernels_list)
        posts = []

        async def _rest_post(path, json=None, backend="local"):
            posts.append(path)
            return None
        monkeypatch.setattr(local_jupyter, "_rest_post", _rest_post)
        monkeypatch.setattr(state, "_INTROSPECT_LOCK_WAIT_SEC", 0.05)

        lock = state._locks.setdefault("kid-1", asyncio.Lock())
        await lock.acquire()
        try:
            r = await jobs._poll_job("kid-1", "job-1", None, 4000)
        finally:
            lock.release()

        assert r["status"] == "kernel_busy"
        assert posts == []          # no /interrupt, no exec: nothing was killed


class TestListVariablesGuards:
    async def test_colab_kernel_is_rejected(self, monkeypatch):
        monkeypatch.setitem(state._backends, "colab", {"kind": "cli"})
        state._registry.track("rmcp-x", "colab")
        with pytest.raises(RuntimeError, match="not supported"):
            await kernels.list_variables("rmcp-x")

    async def test_out_of_band_busy_returns_kernel_busy(self, monkeypatch):
        async def _kernels_list(backend="local"):
            return [{"id": "kid-1", "execution_state": "busy"}]
        monkeypatch.setattr(local_jupyter, "_kernels", _kernels_list)

        r = await kernels.list_variables("kid-1")

        assert r["status"] == "kernel_busy"
        assert r["running"] is None

    async def test_held_lock_returns_kernel_busy(self, monkeypatch):
        async def _kernels_list(backend="local"):
            return [{"id": "kid-1", "execution_state": "idle"}]
        monkeypatch.setattr(local_jupyter, "_kernels", _kernels_list)
        monkeypatch.setattr(state, "_INTROSPECT_LOCK_WAIT_SEC", 0.05)

        lock = state._locks.setdefault("kid-1", asyncio.Lock())
        await lock.acquire()
        try:
            r = await kernels.list_variables("kid-1")
        finally:
            lock.release()

        assert r["status"] == "kernel_busy"


# ---- #14: the job poll snippet reads a window, not the whole file ------------
class TestJobPollCode:
    def test_tail_window_without_offset(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(jobs, "_JOBS_DIR", str(tmp_path))
        (tmp_path / "job-1.status").write_text("running")
        (tmp_path / "job-1.out").write_bytes(b"x" * 9000 + b"y" * 1000)

        exec(compile(jobs._job_poll_code("job-1", None, 500), "<poll>", "exec"), {})

        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "running"
        assert data["total"] == 10000
        assert data["start"] == 9500
        assert data["output"] == "y" * 500
        assert data["more"] is False

    def test_forward_window_with_offset(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(jobs, "_JOBS_DIR", str(tmp_path))
        (tmp_path / "job-1.status").write_text("done")
        (tmp_path / "job-1.out").write_bytes(b"abcdefghij" * 1024)   # 10240 bytes

        exec(compile(jobs._job_poll_code("job-1", 100, 300), "<poll>", "exec"), {})

        data = json.loads(capsys.readouterr().out)
        assert data["start"] == 100 and data["next"] == 400
        assert data["output"] == (b"abcdefghij" * 1024)[100:400].decode()
        assert data["more"] is True

    def test_missing_files_report_unknown(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(jobs, "_JOBS_DIR", str(tmp_path))
        exec(compile(jobs._job_poll_code("job-ghost", None, 500), "<poll>", "exec"), {})
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "unknown" and data["total"] == 0 and data["output"] == ""


# ---- #17: _locks cleanup + sanitized /health ---------------------------------
class TestDeleteKernelCleanup:
    async def test_local_delete_drops_the_lock(self, monkeypatch):
        state._locks["kid-1"] = asyncio.Lock()
        state._registry.track("kid-1", "local")

        async def _drop(_kid):
            return None
        monkeypatch.setattr(local_jupyter, "_drop_client", _drop)

        class _Resp:
            status_code = 204
            def raise_for_status(self):
                return None

        class _Http:
            async def delete(self, _path):
                return _Resp()
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Http())

        await backends._delete_kernel("kid-1", "local")

        assert "kid-1" not in state._locks


class TestHealthSanitized:
    async def test_unreachable_backend_does_not_leak_the_internal_url(self, monkeypatch):
        import server

        class _Client:
            async def get(self, _path):
                raise RuntimeError("connect failed: http://jupyter:8888 internal")
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Client())

        resp = await server._health(None)

        assert b"unreachable" in resp.body
        assert b"jupyter:8888" not in resp.body
        assert b"internal" not in resp.body


# ---- #6: the /mcp Host/Origin guard ------------------------------------------
class TestOriginGuard:
    """Starlette TestClient against the REAL server.app — the guard is FastMCP's
    HostOriginGuardMiddleware, only active because http_app now sets
    host_origin_protection="auto" + an explicit origins list (ADR 0019)."""

    @pytest.fixture()
    def client(self):
        from starlette.testclient import TestClient

        import server
        return TestClient(server.app)

    def test_request_without_origin_passes_the_guard(self, client):
        # Non-browser MCP clients send no Origin — they must always reach the app
        # (any non-403/421 status proves the guard let it through; /health answers
        # 200 even with the Jupyter unreachable in tests).
        r = client.get("/health")
        assert r.status_code not in (403, 421)

    def test_foreign_origin_is_rejected(self, client):
        r = client.get("/health", headers={
            "Host": "mcp.example.com", "Origin": "http://evil.example"})
        assert r.status_code == 403

    def test_loopback_origin_and_host_pass(self, client):
        # A page served from localhost hitting a loopback-bound server is the one
        # Origin case that stays allowed without an allowlist entry.
        r = client.get("/health", headers={
            "Host": "127.0.0.1:7130", "Origin": "http://localhost:9999"})
        assert r.status_code not in (403, 421)

    def test_public_host_is_not_421ed(self, client):
        # The tunnel deployment shape: a forwarded public Host must NOT be rejected —
        # that is exactly why MCP_ALLOWED_HOSTS stays empty/opt-in.
        r = client.get("/health", headers={"Host": "mcp.example.com"})
        assert r.status_code != 421
