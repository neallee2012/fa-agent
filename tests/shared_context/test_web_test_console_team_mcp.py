"""Console proxy for owner Team MCP management, wired to the real /team-mcp/v1 routes (offline, fake Foundry)."""

import logging
from types import SimpleNamespace

import httpx
import pytest

from web_test_console.adapter import ConsoleError
from web_test_console.team_mcp import TeamMcp, install_team_mcp_routes, is_team_mcp_write
from test_m3_auth import jwk_client, signing_key  # noqa: F401  (fixtures used by team_mcp)
from test_team_mcp_api import SECRET, team_mcp  # noqa: F401
from test_web_test_console_frozen_interface import real_interface  # noqa: F401


def console_for(api, identity="owner"):
    token = api.headers(identity)["Authorization"].removeprefix("Bearer ")
    scopes = {"owner": {"Behavior.Read", "Behavior.Approve"}, "read_only": {"Behavior.Read"}}[identity]
    sent = []

    def forward(request):
        sent.append((request.method, request.url.path, request.content))
        response = api.client.request(request.method, request.url.path, headers=dict(request.headers),
                                      content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    console = SimpleNamespace(
        config=SimpleNamespace(backend_origin="http://mcp-api.test"),
        backend=httpx.Client(transport=httpx.MockTransport(forward)),
        vault=SimpleNamespace(get=lambda: token, allows=lambda scope: scope in scopes),
        active=lambda: None)
    return TeamMcp(console), sent


def add_command(view, request_id="a" * 32, label="Docs", connection="safe-aad", approval="always", **extra):
    return {"schema_version": "fa-team-mcp-change-v2", "request_id": request_id,
            "expected_version": view["default_version"], "operation": "add", "server_label": label,
            "server": {"server_label": label, "server_url": "https://mcp.example.test/docs",
                       "project_connection_id": connection, "allowed_tools": ["search"],
                       "require_approval": approval}, **extra}


def test_console_adds_once_and_a_resend_reuses_the_same_version(team_mcp):
    mcp, sent = console_for(team_mcp)
    view = mcp.servers()
    assert view["default_version"] is None and view["servers"] == [] and view["can_create_connection"] is True
    connections = mcp.connections()
    assert [item["name"] for item in connections["connections"]] == ["safe-aad"]
    assert connections["hidden_secret_connections"] == 1, "a multi-Team environment never lists a secret connection"
    applied = mcp.change(add_command(view))
    assert applied["state"] == "applied" and applied["original_request_resent"] is False
    assert [(item["server_label"], item["require_approval"]) for item in applied["view"]["servers"]] == [("Docs", "always")]
    team_mcp.foundry.fail["create_after"] = None
    current = mcp.servers()
    with pytest.raises(ConsoleError) as unknown:
        mcp.change(add_command(current, "b" * 32, "Wiki"))
    assert unknown.value.code == "TEAM_MCP_OUTCOME_UNKNOWN"
    resent = mcp.change(add_command(current, "b" * 32, "Wiki"))
    assert resent["toolbox_version"] == "v2" and len(team_mcp.foundry.created) == 2, "no second version for one ID"
    assert not any(path.endswith("/resume") for _, path, _ in sent)


def test_a_new_connection_key_goes_once_to_the_api_and_never_back(team_mcp, caplog):
    caplog.set_level(logging.DEBUG)
    mcp, sent = console_for(team_mcp)
    view = mcp.servers()
    result = mcp.change(add_command(view, connection=None, new_connection={"header_name": "x-apikey", "secret": SECRET}))
    assert result["connection"] and SECRET not in str(result) and SECRET not in caplog.text
    posts = [content for method, _, content in sent if method == "POST"]
    assert len(posts) == 1 and SECRET.encode() in posts[0]
    assert team_mcp.foundry.puts[0]["keys"] == {"x-apikey": SECRET} and len(team_mcp.foundry.puts) == 1


def test_a_bad_feed_url_is_refused_before_anything_is_sent(team_mcp):
    owner, sent = console_for(team_mcp)
    view = owner.servers()
    with pytest.raises(ConsoleError) as invalid:
        owner.change(add_command(view, connection=None,
                                 new_connection={"header_name": "x-feed-url", "secret": "http://feeds.example.test/rss"}))
    assert invalid.value.code == "TEAM_MCP_FEED_URL_INVALID"
    assert all(method == "GET" for method, _, _ in sent) and team_mcp.foundry.puts == []


def test_read_only_and_invalid_changes_are_not_sent(team_mcp):
    reader, sent = console_for(team_mcp, "read_only")
    view = reader.servers()
    with pytest.raises(ConsoleError) as denied:
        reader.change(add_command(view))
    assert denied.value.code == "TEAM_MCP_OWNER_ONLY"
    owner, owner_sent = console_for(team_mcp)
    for body in ({**add_command(view), "request_id": "not-hex"}, {**add_command(view), "extra": 1},
                 {**add_command(view), "server": None},
                 add_command(view, connection="safe-aad", new_connection={"header_name": "x-apikey", "secret": SECRET})):
        with pytest.raises(ConsoleError) as invalid:
            owner.change(body)
        assert invalid.value.code == "TEAM_MCP_INPUT_INVALID"
    assert all(method == "GET" for method, _, _ in sent + owner_sent)


def test_bridge_admits_only_bounded_csrf_checked_team_mcp_writes(real_interface):
    c = real_interface
    ui, headers = c.browser.ui, c.browser.headers
    body = add_command({"default_version": None})
    path = "/api/team-mcp/changes"
    assert ui.post(path, json=body, headers={"Origin": headers["Origin"]}).json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
    oversized = ui.post(path, json={**body, "padding": "x" * (16 * 1024)}, headers=headers)
    assert (oversized.status_code, oversized.json()["error"]["code"]) == (413, "TEAM_MCP_INPUT_INVALID")
    admitted = ui.post(path, json=body, headers=headers).json()["error"]["code"]
    assert admitted not in {"LOCAL_REQUEST_DENIED", "TEAM_MCP_INPUT_INVALID"}
    assert is_team_mcp_write(path) and not is_team_mcp_write("/api/team-mcp/pending/resume")
    assert not is_team_mcp_write("/api/team-mcp/servers")


def test_console_routes_have_no_delete_or_resume():
    from fastapi import FastAPI
    app = FastAPI()
    install_team_mcp_routes(app, lambda: None)
    paths = sorted(route.path for route in app.routes if route.path.startswith("/api/team-mcp"))
    assert paths == ["/api/team-mcp/changes", "/api/team-mcp/connections", "/api/team-mcp/servers"]
    assert not any(getattr(route, "methods", set()) & {"DELETE", "PUT", "PATCH"}
                   for route in app.routes if route.path.startswith("/api/team-mcp"))
