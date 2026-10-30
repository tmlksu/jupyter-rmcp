"""Regression tests for audit batch 2.

- #9  fetch_to_workspace: streaming abort at the byte cap, SSRF refusal of
      non-public targets (incl. IPv4-mapped v6), per-hop redirect revalidation,
      and the connection pinned to the validated IP (rebind can't bypass)
- #13 one workspace-path validator: accept/reject table
- #16 download_from_colab: stat-first size refusal + the same chunked upload
-     chunked Contents-API upload numbering (1..N, -1)
- #17 item 2: orphaned execution workers go to a bounded dedicated pool and are
      counted — they can't starve the default executor

No network: DNS is stubbed at workspace._resolve_ips and HTTP at a MockTransport
client injected through workspace._fetch_client; the Contents API is a fake
local_jupyter._http.
"""
import asyncio
import base64
import ipaddress
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from httpx._content import AsyncIteratorByteStream

import config
import reaper
import state
import workspace
from backends import colab_cli, local_jupyter
from registry import KernelRegistry
from util import _check_workspace_path


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "_locks", {})
    monkeypatch.setattr(state, "_inflight", {})
    monkeypatch.setattr(state, "_last_exec", {})
    monkeypatch.setattr(state, "_registry", KernelRegistry(str(tmp_path / "kernels.json")))
    monkeypatch.setattr(reaper, "_reaper_started", True)
    monkeypatch.setattr(local_jupyter, "_orphaned_exec_workers", 0)


def _aiter(data: bytes) -> AsyncIteratorByteStream:
    """A one-shot async body — AsyncClient.send(stream=True) requires an
    AsyncByteStream, so MockTransport responses can't use plain content=..."""
    async def _gen():
        yield data
    return AsyncIteratorByteStream(_gen())


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._body


class _Contents:
    """Fake for local_jupyter._http(): records every contents PUT; GETs 404 unless
    `exists` is set (the pre-write overwrite check)."""

    def __init__(self, exists=False):
        self.exists = exists
        self.puts = []      # (path, json-dict)
        self.gets = []

    async def get(self, path, params=None, **_kw):
        self.gets.append(path)
        return _Resp(200 if self.exists else 404)

    async def put(self, path, json=None, **_kw):
        self.puts.append((path, json))
        return _Resp(201)

    def file_puts(self):
        """Just the file uploads — auto-created parent directories also land in puts."""
        return [(p, j) for p, j in self.puts if (j or {}).get("type") == "file"]

    def written_bytes(self):
        """The file content reassembled from the recorded upload chunks."""
        return b"".join(base64.b64decode(j["content"]) for _p, j in self.file_puts())

    def chunk_sequence(self):
        return [j["chunk"] for _p, j in self.file_puts()]


def _stub_dns(monkeypatch, mapping):
    """workspace._resolve_ips -> canned answers keyed by host; records lookups."""
    calls = []

    async def _resolve(host, port):
        calls.append(host)
        return [ipaddress.ip_address(a) for a in mapping[host]]
    monkeypatch.setattr(workspace, "_resolve_ips", _resolve)
    return calls


def _mock_transport(monkeypatch, handler):
    """Route fetch_to_workspace through a MockTransport client."""
    requests = []

    def _handler(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        workspace, "_fetch_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler),
                                  follow_redirects=False))
    return requests


# ---- #13: the shared workspace-path validator --------------------------------
class TestWorkspacePathValidator:
    @pytest.mark.parametrize("path", [
        "", ".", "./", "./x.ipynb", "sub/dir/x.ipynb", "a/b/c.txt",
        "a..b/x", "..x/y", "x..", "a//b", "name with space.txt",
        "100%.csv", "a%2Fb",            # decodes to a/b — still clean
        "data/../x".replace("../", "@"),  # literal '@' has no meaning
    ])
    def test_accepts_legitimate_paths(self, path):
        assert _check_workspace_path(path) == path

    @pytest.mark.parametrize("path", [
        "/etc/passwd", "/abs", "..", "../x", "a/../b", "a/../../z", "./../x",
        "a\x00b", "x\x00", "a\\b", "..\\x", "C:\\win", "\\\\server\\share",
        "%2e%2e/x", "a%2f..%2fb", "%00", "..%2f..",
    ])
    def test_rejects_escapes(self, path):
        with pytest.raises(RuntimeError):
            _check_workspace_path(path)


# ---- #9: fetch_to_workspace ---------------------------------------------------
class TestFetchSsrf:
    @pytest.mark.parametrize("ip", [
        "10.0.0.1", "127.0.0.1", "169.254.169.254", "192.168.1.1", "172.16.0.1",
        "0.0.0.0", "100.64.0.1", "198.18.0.1", "192.0.2.1",
        "::1", "fe80::1", "ff02::1", "224.0.0.1", "::", "2001:db8::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
        "::ffff:224.0.0.1",   # multicast hiding behind an IPv4-mapped v6
    ])
    async def test_non_public_targets_refused(self, monkeypatch, ip):
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        _stub_dns(monkeypatch, {"evil.example": [ip]})
        requests = _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=_aiter(b"x")))

        with pytest.raises(RuntimeError, match="non-public"):
            await workspace.fetch_to_workspace("http://evil.example/f", "f.bin", overwrite=True)

        assert requests == []          # nothing was ever sent

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:2800:220:1:248:1893:25c8:1946"])
    async def test_public_targets_allowed(self, monkeypatch, ip):
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        _stub_dns(monkeypatch, {"ok.example": [ip]})
        _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=_aiter(b"hi")))

        r = await workspace.fetch_to_workspace("http://ok.example/f", "f.bin", overwrite=True)
        assert r["bytes"] == 2

    async def test_allow_private_env_lifts_the_filter(self, monkeypatch):
        monkeypatch.setattr(config, "FETCH_ALLOW_PRIVATE", True)
        contents = _Contents()
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: contents)
        _stub_dns(monkeypatch, {"lan.example": ["10.0.0.1"]})
        _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=_aiter(b"lan")))

        r = await workspace.fetch_to_workspace("http://lan.example/f", "f.bin", overwrite=True)
        assert r["bytes"] == 3
        assert contents.written_bytes() == b"lan"

    async def test_connection_pinned_to_validated_ip(self, monkeypatch):
        """The dial goes to the resolved IP while Host stays the original name —
        a second DNS answer between check and connect cannot redirect it."""
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        _stub_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
        requests = _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=_aiter(b"x")))

        await workspace.fetch_to_workspace("http://example.com/p?q=1", "f.bin", overwrite=True)

        req = requests[0]
        assert req.url.host == "93.184.216.34"      # the dial is the PINNED ip
        assert req.headers["host"] == "example.com"  # host header preserved
        assert str(req.url).endswith("/p?q=1")

    async def test_https_keeps_sni_hostname(self, monkeypatch):
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        _stub_dns(monkeypatch, {"secure.example": ["93.184.216.34"]})
        requests = _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=_aiter(b"x")))

        await workspace.fetch_to_workspace("https://secure.example/f", "f.bin", overwrite=True)

        assert requests[0].extensions.get("sni_hostname") == "secure.example"

    async def test_non_default_port_kept_in_host_header(self, monkeypatch):
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        _stub_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
        requests = _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=_aiter(b"x")))

        await workspace.fetch_to_workspace("http://example.com:8080/f", "f.bin", overwrite=True)

        assert requests[0].headers["host"] == "example.com:8080"
        assert requests[0].url.port == 8080

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.example/f", "gopher://x/1"])
    async def test_non_http_schemes_refused(self, monkeypatch, url):
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        calls = _stub_dns(monkeypatch, {})
        _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=_aiter(b"x")))

        with pytest.raises(RuntimeError, match="scheme"):
            await workspace.fetch_to_workspace(url, "f.bin", overwrite=True)
        assert calls == []

    async def test_redirect_hop_is_revalidated(self, monkeypatch):
        """A 302 into a private target must be refused — each hop re-resolves."""
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        calls = _stub_dns(monkeypatch, {"a.example": ["8.8.8.8"],
                                        "internal.example": ["10.0.0.1"]})

        def _handler(req):
            if req.url.host == "8.8.8.8":
                return httpx.Response(302, headers={"location": "http://internal.example/x"})
            return httpx.Response(200, stream=_aiter(b"unreachable-in-test"))
        requests = _mock_transport(monkeypatch, _handler)

        with pytest.raises(RuntimeError, match="non-public"):
            await workspace.fetch_to_workspace("http://a.example/go", "f.bin", overwrite=True)

        assert calls == ["a.example", "internal.example"]   # every hop resolved
        assert len(requests) == 1                            # hop 2 never sent

    async def test_public_redirect_followed_and_repinned(self, monkeypatch):
        contents = _Contents()
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: contents)
        _stub_dns(monkeypatch, {"a.example": ["8.8.8.8"], "b.example": ["9.9.9.9"]})

        def _handler(req):
            if req.url.host == "8.8.8.8":
                return httpx.Response(301, headers={"location": "http://b.example/real"})
            return httpx.Response(200, stream=_aiter(b"payload"))
        requests = _mock_transport(monkeypatch, _handler)

        r = await workspace.fetch_to_workspace("http://a.example/go", "f.bin", overwrite=True)

        assert r["bytes"] == 7
        assert requests[1].url.host == "9.9.9.9"          # re-pinned to hop-2's ip
        assert requests[1].headers["host"] == "b.example"
        assert contents.written_bytes() == b"payload"

    async def test_redirect_to_bad_scheme_refused(self, monkeypatch):
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        _stub_dns(monkeypatch, {"a.example": ["8.8.8.8"]})
        _mock_transport(monkeypatch, lambda _r: httpx.Response(
            302, headers={"location": "file:///etc/passwd"}))

        with pytest.raises(RuntimeError, match="scheme"):
            await workspace.fetch_to_workspace("http://a.example/go", "f.bin", overwrite=True)

    async def test_too_many_redirects(self, monkeypatch):
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        _stub_dns(monkeypatch, {"loop.example": ["8.8.8.8"]})
        _mock_transport(monkeypatch, lambda _r: httpx.Response(
            302, headers={"location": "http://loop.example/next"}))

        with pytest.raises(RuntimeError, match="redirected"):
            await workspace.fetch_to_workspace("http://loop.example/x", "f.bin", overwrite=True)


class TestFetchTransfer:
    async def test_body_written_chunked_with_sequence(self, monkeypatch):
        """3 pieces -> Contents-API chunks numbered 1, 2, -1; content reassembles."""
        contents = _Contents()
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: contents)
        monkeypatch.setattr(local_jupyter, "_UPLOAD_CHUNK_BYTES", 1024)
        body = b"a" * 2500
        _stub_dns(monkeypatch, {"ok.example": ["8.8.8.8"]})
        _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=_aiter(body)))

        r = await workspace.fetch_to_workspace("http://ok.example/f", "d/f.bin", overwrite=True)

        assert r["bytes"] == 2500
        assert contents.chunk_sequence() == [1, 2, -1]
        assert contents.written_bytes() == body
        assert contents.file_puts()[0][0].endswith("/d/f.bin")

    async def test_stream_aborts_at_the_cap(self, monkeypatch):
        """The byte counter aborts mid-stream — the rest of the body is never read."""
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())
        monkeypatch.setattr(config, "MAX_FETCH_BYTES", 1000)
        _stub_dns(monkeypatch, {"ok.example": ["8.8.8.8"]})
        served = []

        async def _body():
            for i in range(5):
                served.append(i)
                yield b"x" * 65536     # one transport piece fills httpx's re-chunker
        _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=AsyncIteratorByteStream(_body())))

        with pytest.raises(RuntimeError, match="cap"):
            await workspace.fetch_to_workspace("http://ok.example/big", "f.bin", overwrite=True)

        assert len(served) == 1   # aborted after the first 64KB, not the whole 320KB

    async def test_oversized_content_length_refused_fast(self, monkeypatch):
        """A truthful Content-Length over the cap fails before the body is read."""
        contents = _Contents()
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: contents)
        monkeypatch.setattr(config, "MAX_FETCH_BYTES", 10)
        _stub_dns(monkeypatch, {"ok.example": ["8.8.8.8"]})
        _mock_transport(monkeypatch, lambda _r: httpx.Response(
            200, stream=_aiter(b"x" * 100), headers={"content-length": "100"}))

        with pytest.raises(RuntimeError, match="cap"):
            await workspace.fetch_to_workspace("http://ok.example/big", "f.bin", overwrite=True)

    async def test_existing_target_refused_without_overwrite(self, monkeypatch):
        contents = _Contents(exists=True)
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: contents)
        _stub_dns(monkeypatch, {"ok.example": ["8.8.8.8"]})
        requests = _mock_transport(monkeypatch, lambda _r: httpx.Response(200, stream=_aiter(b"x")))

        with pytest.raises(RuntimeError, match="already exists"):
            await workspace.fetch_to_workspace("http://ok.example/f", "f.bin")
        assert requests == []

    async def test_traversal_path_refused_before_any_io(self, monkeypatch):
        contents = _Contents()
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: contents)
        calls = _stub_dns(monkeypatch, {"ok.example": ["8.8.8.8"]})

        with pytest.raises(RuntimeError, match="invalid path"):
            await workspace.fetch_to_workspace("http://ok.example/f", "../evil", overwrite=True)
        assert calls == [] and contents.puts == []


class TestChunkedUpload:
    async def _write(self, pieces, monkeypatch):
        contents = _Contents()
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: contents)

        async def _gen():
            for p in pieces:
                yield p
        n = await local_jupyter._write_file_chunked("f.bin", _gen())
        return n, contents

    async def test_multi_piece_numbering(self, monkeypatch):
        n, c = await self._write([b"aa", b"bb", b"cc"], monkeypatch)
        assert n == 6
        assert c.chunk_sequence() == [1, 2, -1]
        assert c.written_bytes() == b"aabbcc"

    async def test_single_piece_closes_with_minus1(self, monkeypatch):
        n, c = await self._write([b"only"], monkeypatch)
        assert n == 4
        assert c.chunk_sequence() == [1, -1]
        assert c.written_bytes() == b"only"

    async def test_empty_stream_creates_empty_file(self, monkeypatch):
        n, c = await self._write([], monkeypatch)
        assert n == 0
        assert c.chunk_sequence() == [1]
        assert c.written_bytes() == b""


# ---- #16: download_from_colab -------------------------------------------------
class TestDownloadFromColab:
    def _setup_colab(self, monkeypatch, file_bytes: bytes):
        monkeypatch.setitem(state._backends, "colab", {"kind": "cli"})
        state._registry.track("rmcp-x", "colab")
        contents = _Contents()
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: contents)

        async def _cli(*args, **_kw):
            # `colab download -s <session> <remote> <local>` — write the payload.
            assert args[0] == "download"
            with open(args[-1], "wb") as f:
                f.write(file_bytes)
            return 0, ""
        monkeypatch.setattr(colab_cli, "_colab_cli", _cli)
        return contents

    async def test_small_download_written_chunked(self, monkeypatch):
        monkeypatch.setattr(local_jupyter, "_UPLOAD_CHUNK_BYTES", 1024)
        body = b"z" * 2500
        contents = self._setup_colab(monkeypatch, body)

        r = await workspace.download_from_colab("rmcp-x", "/content/ckpt.bin")

        assert r["bytes"] == 2500
        assert r["workspace_path"] == "ckpt.bin"
        assert contents.chunk_sequence() == [1, 2, -1]
        assert contents.written_bytes() == body

    async def test_over_cap_refused(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_FETCH_BYTES", 100)
        contents = self._setup_colab(monkeypatch, b"x" * 200)

        with pytest.raises(RuntimeError, match="cap"):
            await workspace.download_from_colab("rmcp-x", "/content/huge.bin")
        assert contents.puts == []

    async def test_traversal_dest_refused(self, monkeypatch):
        called = []
        monkeypatch.setitem(state._backends, "colab", {"kind": "cli"})
        state._registry.track("rmcp-x", "colab")

        async def _cli(*a, **k):
            called.append(a)
            return 0, ""
        monkeypatch.setattr(colab_cli, "_colab_cli", _cli)

        with pytest.raises(RuntimeError, match="invalid workspace_path"):
            await workspace.download_from_colab("rmcp-x", "/content/x", "../evil")
        assert called == []

    async def test_dotdot_remote_basename_refused(self, monkeypatch):
        monkeypatch.setitem(state._backends, "colab", {"kind": "cli"})
        state._registry.track("rmcp-x", "colab")
        monkeypatch.setattr(local_jupyter, "_http", lambda _b: _Contents())

        with pytest.raises(RuntimeError, match="invalid workspace_path"):
            await workspace.download_from_colab("rmcp-x", "/content/x/..")


# ---- #17 item 2: orphaned execution workers -----------------------------------
class TestOrphanedExecWorkers:
    """A kernel that never answers leaves its execute_interactive worker parked
    until _LIB_DEADLINE_SEC. These tests make that real: a blocked worker, an idle
    kernel, and the liveness watchdog at a fast tick."""

    def _setup_exec(self, monkeypatch, kernel_ids, release):
        monkeypatch.setattr(local_jupyter, "_LIVENESS_INTERVAL_SEC", 0.01)

        async def _k(_backend):
            return [{"id": k, "execution_state": "idle"} for k in kernel_ids]
        monkeypatch.setattr(local_jupyter, "_kernels", _k)

        calls = []

        def _blocking(_kc, code, _sink, _timeout):
            calls.append(code)
            release.wait(timeout=10)      # stands in for a kernel that never replies
            return {"status": "ok", "execution_count": 1, "outputs": []}
        monkeypatch.setattr(local_jupyter, "_execute_streaming", _blocking)

        async def _client(_kid):
            return object()
        monkeypatch.setattr(local_jupyter, "_get_client", _client)

        async def _drop(_kid):
            return None
        monkeypatch.setattr(local_jupyter, "_drop_client", _drop)
        return calls

    async def _wait_for(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("condition never became true")

    async def test_orphan_counted_and_default_executor_unblocked(self, monkeypatch):
        """The waiter gives up after two idle sightings while the worker is still
        parked: it is counted as an orphan, and to_thread (connect/stop/list_variables)
        still answers because the worker sits on the dedicated pool."""
        release = threading.Event()
        self._setup_exec(monkeypatch, ["kid-1"], release)

        with pytest.raises(RuntimeError, match="stopped running"):
            await local_jupyter.exec_local("kid-1", "local", "x = 1", timeout=0)

        assert local_jupyter._orphaned_exec_workers == 1   # parked worker counted
        # A starved default executor is the original bug: this must return fast.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), 1) == 42

        release.set()
        await self._wait_for(lambda: local_jupyter._orphaned_exec_workers == 0)

    async def test_bounded_pool_queues_the_second_worker(self, monkeypatch):
        """With a 1-worker pool the second execution's worker queues — it never runs
        while the orphan holds the slot — and starts only once it unwinds."""
        release = threading.Event()
        calls = self._setup_exec(monkeypatch, ["kid-a", "kid-b"], release)
        monkeypatch.setattr(local_jupyter, "_exec_pool", ThreadPoolExecutor(max_workers=1))

        t_a = asyncio.ensure_future(local_jupyter.exec_local("kid-a", "local", "A", timeout=0))
        await self._wait_for(lambda: calls == ["A"])       # A's worker is parked
        t_b = asyncio.ensure_future(local_jupyter.exec_local("kid-b", "local", "B", timeout=0))

        await asyncio.gather(t_a, t_b, return_exceptions=True)
        assert calls == ["A"]          # B's worker never started while A was parked
        assert local_jupyter._orphaned_exec_workers == 2   # A parked + B queued-abandoned

        release.set()
        await self._wait_for(lambda: calls == ["A", "B"] and
                             local_jupyter._orphaned_exec_workers == 0)
