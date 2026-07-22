"""COLAB_ONLY mode: local backend stores notebooks but never executes code.

The flag is read as `config.COLAB_ONLY` (module attribute, not a from-import) so
these tests can monkeypatch it per-case. Every test that reaches a colab code path
also has to register the colab backend and stub the colab-cli subprocess — conftest
forces COLAB_AVAILABLE=False for determinism.
"""
import pytest

import backends
import config
import kernels
import reaper
import state
from config import COLAB, LOCAL


@pytest.fixture()
def colab_only(monkeypatch):
    """Colab-only server with a registered, stubbed colab backend."""
    monkeypatch.setattr(config, "COLAB_ONLY", True)
    monkeypatch.setattr(state, "COLAB_AVAILABLE", True)
    monkeypatch.setitem(state._backends, COLAB, {"kind": "cli"})
    monkeypatch.setattr(reaper, "_reaper_started", True)   # don't spawn the reaper task


@pytest.fixture()
def tracked(monkeypatch):
    """Track kernel ids in the registry and undo it afterwards."""
    added: list[str] = []

    def _track(kernel_id: str, backend: str) -> str:
        state._registry.track(kernel_id, backend)
        added.append(kernel_id)
        return kernel_id

    yield _track
    for kid in added:
        state._registry.forget(kid)


class TestStartKernel:
    async def test_explicit_local_is_refused(self, colab_only):
        with pytest.raises(RuntimeError, match="disabled"):
            await kernels.start_kernel(backend=LOCAL)

    async def test_default_backend_is_colab(self, colab_only, monkeypatch):
        monkeypatch.setattr(kernels.local_jupyter, "_resolve_notebook_path",
                            _async_return("work.ipynb"))
        monkeypatch.setattr(kernels.local_jupyter, "_ensure_notebook", _async_return(None))
        monkeypatch.setattr(kernels.colab_cli, "_colab_cli", _async_return((0, "created")))
        monkeypatch.setattr(kernels.colab_cli, "mark_session_live", lambda _s: None)

        result = await kernels.start_kernel()

        assert result["backend"] == COLAB
        assert result["kernel_id"].startswith("rmcp-")
        state._registry.forget(result["kernel_id"])

    async def test_default_backend_is_local_without_the_flag(self, monkeypatch):
        monkeypatch.setattr(config, "COLAB_ONLY", False)
        monkeypatch.setattr(reaper, "_reaper_started", True)
        monkeypatch.setattr(kernels.local_jupyter, "_resolve_notebook_path", _async_return(None))
        monkeypatch.setattr(kernels.local_jupyter, "_ensure_notebook", _async_return(None))
        monkeypatch.setattr(kernels.reaper, "_enforce_capacity", _async_return(None))
        monkeypatch.setattr(kernels.local_jupyter, "_rest_post",
                            _async_return({"id": "kid-local", "name": "python3"}))
        monkeypatch.setattr(kernels.local_jupyter, "_wait_ready", _async_return(None))

        result = await kernels.start_kernel()

        assert result["backend"] == LOCAL
        state._registry.forget(result["kernel_id"])


class TestExecutionRouting:
    async def test_local_kernel_cannot_execute(self, colab_only, tracked):
        kid = tracked("kid-local-exec", LOCAL)
        with pytest.raises(RuntimeError, match="disabled"):
            await backends._resolve_backend(kid)

    async def test_colab_kernel_still_executes(self, colab_only, tracked):
        kid = tracked("rmcp-abcd1234", COLAB)
        assert await backends._resolve_backend(kid) == COLAB

    async def test_local_kernel_routes_normally_without_the_flag(self, monkeypatch, tracked):
        monkeypatch.setattr(config, "COLAB_ONLY", False)
        kid = tracked("kid-local-ok", LOCAL)
        assert await backends._resolve_backend(kid) == LOCAL

    async def test_list_variables_is_refused(self, colab_only, tracked):
        kid = tracked("kid-local-vars", LOCAL)
        with pytest.raises(RuntimeError, match="disabled"):
            await kernels.list_variables(kid)


class TestStartupGuard:
    def test_ensure_background_raises_without_colab(self, monkeypatch):
        monkeypatch.setattr(config, "COLAB_ONLY", True)
        monkeypatch.setattr(state, "COLAB_AVAILABLE", False)
        monkeypatch.delitem(state._backends, COLAB, raising=False)
        monkeypatch.setattr(reaper, "_reaper_started", True)
        with pytest.raises(RuntimeError, match="COLAB_ONLY"):
            reaper._ensure_background()


class TestListBackends:
    async def test_local_reports_exec_disabled(self, colab_only, monkeypatch):
        monkeypatch.setattr(kernels.local_jupyter, "_http", _raising_http())

        entries = {e["backend"]: e for e in await kernels.list_backends()}

        assert entries[LOCAL]["exec_enabled"] is False
        assert "COLAB_ONLY" in entries[LOCAL]["note"]
        assert entries[COLAB]["exec_enabled"] is True


# ---- helpers ----------------------------------------------------------------
def _async_return(value):
    async def _fn(*_args, **_kwargs):
        return value
    return _fn


def _raising_http():
    """Stand in for local_jupyter._http so list_backends records local as unreachable
    instead of touching the network."""
    class _Client:
        async def get(self, _path):
            raise RuntimeError("no jupyter in unit tests")

    return lambda _name: _Client()
