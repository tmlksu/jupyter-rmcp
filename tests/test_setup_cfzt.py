"""Unit tests for scripts/setup_cfzt.py — the request bodies it sends.

The script is not importable as a package module (scripts/ is not on the path),
so it is loaded from its file. Nothing here touches the network: `api` is
replaced with a recorder, which is the point — what these tests protect is the
*shape* of the calls, and specifically the three shapes whose absence has
silently failed open before:

- an Access app update is a full-body PUT that must re-send policies AND
  oauth_configuration, because the API drops what you omit;
- a service token is only honored by a `non_identity` policy;
- a tunnel ingress list must end with the catch-all rule.
"""
import importlib.util
import pathlib

import pytest

SPEC = importlib.util.spec_from_file_location(
    "setup_cfzt", pathlib.Path(__file__).parent.parent / "scripts" / "setup_cfzt.py"
)
cfzt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cfzt)


@pytest.fixture()
def cfg():
    return {
        "CF_API_TOKEN": "t",
        "APP_NAME": "jupyter-mcp",
        "TUNNEL_NAME": "jupyter-rmcp",
        "POLICY_NAME": "jupyter-rmcp-human",
        "SVC_POLICY_NAME": "jupyter-rmcp-svc",
        "SVC_TOKEN_NAME": "claude-code-jupyter-rmcp",
        "SESSION_DURATION": "24h",
        "MCP_LOCAL_PORT": "7130",
        "ACCESS_EMAIL": "me@example.com",
    }


@pytest.fixture()
def recorder(monkeypatch):
    """Replace the API layer; queue canned results per (method, path-prefix)."""
    calls = []
    responses = {}

    def fake_api(cfg, method, path, body=None, need=None, allow_404=False):
        calls.append({"method": method, "path": path, "body": body})
        for (m, prefix), result in responses.items():
            if m == method and path.startswith(prefix):
                return result
        return {"id": "generated-id"}

    monkeypatch.setattr(cfzt, "api", fake_api)
    return type("R", (), {"calls": calls, "responses": responses})


def body_of(recorder, method, contains):
    return next(
        c["body"] for c in recorder.calls if c["method"] == method and contains in c["path"]
    )


# --------------------------------------------------------------------------- #
def test_env_file_parsing(tmp_path):
    f = tmp_path / "x.env"
    f.write_text('# comment\nA=1\nB="two"\n\nBAD_LINE\nC=has=equals\n')
    assert cfzt.read_env_file(f) == {"A": "1", "B": "two", "C": "has=equals"}
    assert cfzt.read_env_file(tmp_path / "missing.env") == {}


def test_bearer_must_be_empty_under_oauth(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(cfzt, "ENV_PATH", env)

    env.write_text("MCP_BEARER=deadbeef\n")
    good, msg = cfzt.check_local_bearer()
    assert not good and "401" in msg

    env.write_text("MCP_BEARER=\n")
    assert cfzt.check_local_bearer()[0]


def test_app_put_resends_policies_and_oauth(cfg, recorder):
    """A PUT that omits either one fails open (policies) or breaks mobile (oauth)."""
    recorder.responses[("GET", "/accounts/A/access/apps?")] = [
        {"id": "app1", "domain": "mcp.example.com"}
    ]
    recorder.responses[("PUT", "/accounts/A/access/apps/app1")] = {
        "policies": [{"name": "human", "decision": "allow"}],
        "oauth_configuration": {"enabled": True},
    }

    cfzt.ensure_app(cfg, "A", "mcp.example.com", [{"id": "p1", "precedence": 1}])

    assert not any(c["method"] == "POST" for c in recorder.calls), "existing app must be reused"
    body = body_of(recorder, "PUT", "/access/apps/app1")
    assert body["policies"] == [{"id": "p1", "precedence": 1}]
    assert body["oauth_configuration"]["enabled"] is True
    dcr = body["oauth_configuration"]["dynamic_client_registration"]
    assert dcr["enabled"] is True
    assert dcr["allowed_uris"] == ["https://claude.ai/api/mcp/auth_callback"]
    assert body["self_hosted_domains"] == ["mcp.example.com"]
    assert body["type"] == "self_hosted"


def test_app_is_created_when_absent(cfg, recorder):
    recorder.responses[("GET", "/accounts/A/access/apps?")] = []
    recorder.responses[("POST", "/accounts/A/access/apps")] = {"id": "new1"}
    recorder.responses[("PUT", "/accounts/A/access/apps/new1")] = {"policies": []}

    cfzt.ensure_app(cfg, "A", "mcp.example.com", [{"id": "p1", "precedence": 1}])
    assert body_of(recorder, "POST", "/access/apps")["domain"] == "mcp.example.com"
    assert body_of(recorder, "PUT", "/access/apps/new1")["policies"]


def test_email_policy_is_an_allowlist_of_named_addresses(cfg, recorder):
    recorder.responses[("GET", "/accounts/A/access/policies?")] = []
    cfg["ACCESS_EMAIL"] = "a@example.com, b@example.com"

    cfzt.ensure_email_policy(cfg, "A")
    body = body_of(recorder, "POST", "/access/policies")
    assert body["decision"] == "allow"
    assert body["include"] == [
        {"email": {"email": "a@example.com"}},
        {"email": {"email": "b@example.com"}},
    ]


def test_email_policy_updates_in_place(cfg, recorder):
    recorder.responses[("GET", "/accounts/A/access/policies?")] = [
        {"id": "pol9", "name": "jupyter-rmcp-human"}
    ]
    assert cfzt.ensure_email_policy(cfg, "A") == "pol9"
    assert body_of(recorder, "PUT", "/access/policies/pol9")["include"] == [
        {"email": {"email": "me@example.com"}}
    ]


def test_ingress_keeps_other_hostnames_and_ends_with_catch_all(cfg, recorder):
    recorder.responses[("GET", "/accounts/A/cfd_tunnel/T/configurations")] = {
        "config": {
            "ingress": [
                {"hostname": "other.example.com", "service": "http://localhost:9999"},
                {"hostname": "mcp.example.com", "service": "http://localhost:1"},  # stale
                {"service": "http_status:404"},
            ]
        }
    }
    cfzt.ensure_ingress(cfg, "A", "T", "mcp.example.com")
    rules = body_of(recorder, "PUT", "/configurations")["config"]["ingress"]

    assert rules[-1] == {"service": "http_status:404"}, "catch-all must be last"
    assert len([r for r in rules if r.get("hostname") == "mcp.example.com"]) == 1
    assert {"hostname": "other.example.com", "service": "http://localhost:9999"} in rules
    assert rules[-2] == {"hostname": "mcp.example.com", "service": "http://localhost:7130"}


def test_dns_record_points_at_the_tunnel(cfg, recorder):
    recorder.responses[("GET", "/zones/Z/dns_records?name.exact=")] = []
    cfzt.ensure_dns(cfg, "Z", "mcp.example.com", "TUN")

    # `?name=` is the legacy filter form and silently matches nothing, which would
    # make every run believe no record exists and create a duplicate.
    lookup = next(c for c in recorder.calls if c["method"] == "GET")
    assert "name.exact=mcp.example.com" in lookup["path"]

    body = body_of(recorder, "POST", "/dns_records")
    assert body == {
        "type": "CNAME",
        "name": "mcp.example.com",
        "content": "TUN.cfargotunnel.com",
        "proxied": True,
    }


def test_dns_refuses_to_clobber_a_non_cname(cfg, recorder):
    recorder.responses[("GET", "/zones/Z/dns_records?name.exact=")] = [
        {"id": "r1", "type": "A", "content": "1.2.3.4"}
    ]
    with pytest.raises(SystemExit):
        cfzt.ensure_dns(cfg, "Z", "mcp.example.com", "TUN")


def test_tunnel_is_reused_by_name(cfg, recorder):
    recorder.responses[("GET", "/accounts/A/cfd_tunnel?")] = [{"id": "t7", "name": "jupyter-rmcp"}]
    assert cfzt.ensure_tunnel(cfg, "A") == "t7"
    assert not any(c["method"] == "POST" for c in recorder.calls)


def test_tunnel_is_created_with_remote_config(cfg, recorder):
    recorder.responses[("GET", "/accounts/A/cfd_tunnel?")] = []
    recorder.responses[("POST", "/accounts/A/cfd_tunnel")] = {"id": "t8"}
    assert cfzt.ensure_tunnel(cfg, "A") == "t8"
    # config_src=cloudflare is what makes the /configurations endpoint authoritative.
    assert body_of(recorder, "POST", "/cfd_tunnel")["config_src"] == "cloudflare"
