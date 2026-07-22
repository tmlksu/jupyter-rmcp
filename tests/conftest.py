"""Test bootstrap: make `import server` work WITHOUT a real environment.

mcp/server.py reads env at import time (JUPYTER_TOKEN is required) and probes
for colab-cli availability. Set dummies BEFORE any test module imports it —
conftest.py is imported by pytest before the test modules, so top-level code
here runs first. `pythonpath = ["mcp"]` in pyproject.toml puts server.py on
sys.path.

Unit tests NEVER touch the network / a live server (that is smoke_test.py's
job); anything that would (httpx, colab-cli subprocess) must be monkeypatched.
"""
import os
import tempfile

os.environ.setdefault("JUPYTER_TOKEN", "test-token")
os.environ.setdefault("MCP_BEARER", "")
# Force COLAB_AVAILABLE deterministic (False) regardless of whether the host/CI
# happens to have colab-cli installed: it requires BOTH the binary and ADC creds.
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
# Deployment mode is per-test (monkeypatch config.COLAB_ONLY); never inherit the host's.
os.environ.pop("COLAB_ONLY", None)
# Keep the module-level kernel registry out of the repo tree during tests.
os.environ.setdefault("MCP_STATE_DIR", tempfile.mkdtemp(prefix="jrmcp-state-"))

import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture()
def colab_sessions_fixture():
    """Load a canned `colab sessions` stdout sample by name."""
    def _load(name: str) -> str:
        return (FIXTURES / "colab_sessions" / f"{name}.txt").read_text()
    return _load
