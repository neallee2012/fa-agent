"""Console proxy for the Connectors tab and each Agent's connector switch; the Team routes are the real
/team-connectors/v1 API on a fake Foundry (offline). The two-Agent switch through the real API is in
test_web_test_console_agent_policy.py."""

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from web_test_console.adapter import ConsoleError, PullAdapter
from web_test_console.team_connectors import TeamConnectors, install_team_connector_routes, is_connector_write
from test_m3_auth import jwk_client, signing_key  # noqa: F401  (fixtures used by connectors)
from test_team_connector_api import SECRET, connectors  # noqa: F401
from test_web_test_console_frozen_interface import real_interface  # noqa: F401

DISCLOSURE = "Microsoft Teams 經 Work IQ 連線。Agent 能讀取的不只 Teams，也可能包含你的 Microsoft 365 郵件與檔案。"
AGENT_ORIGIN = "http://agent-api.test"


def console_for(api, identity="owner"):
    token = api.headers(identity)["Authorization"].removeprefix("Bearer ")
    scopes = {"owner": {"Behavior.Read", "Behavior.Approve"}, "read_only": {"Behavior.Read"},
              "agent": {"Behavior.Read"}}[identity]
    sent = []

    def forward(request):
        sent.append((request.method, request.url.path, request.content))
        response = api.client.request(request.method, request.url.path, headers=dict(request.headers),
                                      content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    console = SimpleNamespace(
        config=SimpleNamespace(backend_origin="http://connector-api.test"),
        backend=httpx.Client(transport=httpx.MockTransport(forward)),
        vault=SimpleNamespace(get=lambda: token, allows=lambda scope: scope in scopes),
        active=lambda: None)
    return TeamConnectors(console), sent


def teams_card(version, status, settings):
    return {"connector_id": "teams", "display_name": "Microsoft Teams", "disclosure": DISCLOSURE,
            "revoke_url": "https://entra.microsoft.com", "version": version, "status": status, "settings": settings}


def command(request_id, version, *, enabled=True, read="never", write="always", secret=None):
    body = {"schema_version": "fa-team-connector-change-v1", "request_id": request_id, "connector_id": "teams",
            "expected_version": version, "enabled": enabled, "read_approval": read, "write_approval": write}
    return body if secret is None else {**body, "client_secret": secret}


def posts(sent):
    return [content for method, _, content in sent if method == "POST"]


def test_owner_adds_teams_with_the_secret_once_and_a_lost_reply_resend_reuses_the_version(connectors, caplog):
    caplog.set_level(logging.DEBUG)
    owner, sent = console_for(connectors)
    assert owner.connectors() == {"schema_version": "fa-console-team-connectors-v1", "can_manage": True,
                                  "connectors": [teams_card(None, "not-added", None)]}
    add = command("a" * 32, None, secret=SECRET)
    connectors.foundry.fail["create_after"] = None
    with pytest.raises(ConsoleError) as unknown:
        owner.change(add)
    assert (unknown.value.code, unknown.value.status) == ("TEAM_CONNECTOR_OUTCOME_UNKNOWN", 502)
    resent = owner.change(add)
    assert resent == {"schema_version": "fa-console-team-connector-change-v1", "request_id": "a" * 32,
                      "state": "applied", "toolbox_version": "v1", "original_request_resent": False,
                      "connector": teams_card("v1", "enabled",
                                              {"enabled": True, "read_approval": "never", "write_approval": "always"})}
    assert (len(connectors.foundry.created), len(connectors.foundry.puts)) == (1, 1), "one version, one connection"
    assert connectors.foundry.puts[0]["client_secret"] == SECRET
    assert [SECRET.encode() in body for body in posts(sent)] == [True, True]
    shown = [str(unknown.value), json.dumps(resent), json.dumps(owner.connectors()), caplog.text]
    assert [SECRET in text for text in shown] == [False, False, False, False]
    assert "client_id" not in json.dumps(resent) and "fa-connector-" not in json.dumps(resent)


def test_approval_change_disable_and_re_enable_each_write_one_version_without_a_secret(connectors):
    owner, sent = console_for(connectors)
    owner.change(command("1" * 32, None, secret=SECRET))
    approval = owner.change(command("2" * 32, "v1", read="always"))
    assert approval["connector"]["settings"] == {"enabled": True, "read_approval": "always", "write_approval": "always"}
    disabled = owner.change(command("3" * 32, "v2", enabled=False, read="always"))
    assert (disabled["toolbox_version"], disabled["connector"]["status"]) == ("v3", "disabled")
    enabled = owner.change(command("4" * 32, "v3", read="always"))
    assert (enabled["toolbox_version"], enabled["connector"]["status"]) == ("v4", "enabled")
    assert [item["metadata"]["fa_connector_enabled"] for item in connectors.foundry.created] == [
        "true", "true", "false", "true"]
    assert len(connectors.foundry.puts) == 1, "only the first add writes the connection"
    assert [b"client_secret" in body for body in posts(sent)] == [True, False, False, False]


def test_a_stale_version_or_a_missing_first_secret_is_refused_before_any_write(connectors):
    owner, _ = console_for(connectors)
    with pytest.raises(ConsoleError) as missing:
        owner.change(command("5" * 32, None))
    assert (missing.value.code, missing.value.status) == ("TEAM_CONNECTOR_SECRET_REQUIRED", 422)
    owner.change(command("6" * 32, None, secret=SECRET))
    with pytest.raises(ConsoleError) as stale:
        owner.change(command("7" * 32, None, enabled=False))
    assert (stale.value.code, stale.value.status) == ("TEAM_CONNECTOR_CHANGED", 409)
    with pytest.raises(ConsoleError) as reused:
        owner.change(command("6" * 32, None, enabled=False, secret=SECRET))
    assert reused.value.code == "TEAM_CONNECTOR_COMMAND_CONFLICT"
    assert len(connectors.foundry.created) == 1


def test_a_private_network_refusal_reaches_the_page_with_its_own_code(connectors):
    connectors.environ["FA_NETWORK_MODE"] = "private"
    owner, _ = console_for(connectors)
    with pytest.raises(ConsoleError) as refused:
        owner.change(command("8" * 32, None, secret=SECRET))
    assert (refused.value.code, refused.value.status) == ("TEAM_CONNECTOR_PRIVATE_NETWORK", 422)
    assert connectors.foundry.created == [] and connectors.foundry.puts == []


def test_a_read_only_login_sees_the_card_but_cannot_change_and_a_non_owner_sees_nothing(connectors):
    reader, sent = console_for(connectors, "read_only")
    assert reader.connectors() == {"schema_version": "fa-console-team-connectors-v1", "can_manage": False,
                                   "connectors": [teams_card(None, "not-added", None)]}
    with pytest.raises(ConsoleError) as denied:
        reader.change(command("8" * 32, None, secret=SECRET))
    assert (denied.value.code, denied.value.status) == ("TEAM_CONNECTOR_OWNER_ONLY", 403)
    assert posts(sent) == []
    stranger, _ = console_for(connectors, "agent")
    with pytest.raises(ConsoleError) as hidden:
        stranger.connectors()
    assert (hidden.value.code, hidden.value.status) == ("TEAM_CONNECTOR_OWNER_ONLY", 403)


def test_malformed_changes_are_not_sent_and_never_echo_the_secret(connectors):
    owner, sent = console_for(connectors)
    for body in ({**command("not-hex", None), "client_secret": SECRET}, {**command("9" * 32, None), "extra": 1},
                 command("9" * 32, None, secret=SECRET + "\nline-two"), command("9" * 32, None, secret="")):
        with pytest.raises(ConsoleError) as invalid:
            owner.change(body)
        assert (invalid.value.code, SECRET in str(invalid.value)) == ("TEAM_CONNECTOR_INPUT_INVALID", False)
    assert sent == []


def test_an_api_without_connector_routes_reads_as_unavailable():
    def absent(request):
        return httpx.Response(404, json={"detail": "Not Found"})

    console = SimpleNamespace(config=SimpleNamespace(backend_origin=AGENT_ORIGIN),
                              backend=httpx.Client(transport=httpx.MockTransport(absent)),
                              vault=SimpleNamespace(get=lambda: "token", allows=lambda scope: True), active=lambda: None)
    console.adapter = PullAdapter(console.config, console.backend)
    codes = []
    for read in (TeamConnectors(console).connectors, TeamConnectors(console).agent_connectors):
        with pytest.raises(ConsoleError) as unavailable:
            read()
        codes.append((unavailable.value.code, unavailable.value.upstream_status))
    assert codes == [("TEAM_CONNECTOR_UNAVAILABLE", 404), ("AGENT_CONNECTORS_UNAVAILABLE", 404)]


def agent_console(replies, scopes=("Behavior.Read", "Behavior.Approve")):
    """An Agent's prefix on a fake API: each request takes the next reply (a dict, a status pair or an exception)."""
    sent = []

    def handle(request):
        sent.append((request.method, request.url.path, request.content))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, value = reply if isinstance(reply, tuple) else (200, reply)
        return httpx.Response(status, json=value, headers={"cache-control": "no-store"})

    console = SimpleNamespace(config=SimpleNamespace(backend_origin=AGENT_ORIGIN),
                              backend=httpx.Client(transport=httpx.MockTransport(handle)),
                              vault=SimpleNamespace(get=lambda: "token", allows=lambda scope: scope in scopes),
                              active=lambda: None)
    console.adapter = PullAdapter(console.config, console.backend, api_prefix="/agents/agent-b")
    return TeamConnectors(console), sent


def view(revision, disabled):
    return {"schema_version": "fa-agent-connectors-v1", "revision": revision, "disabled_connector_ids": disabled,
            "applies_to": "new-conversations-only"}


def switch(request_id, expected, disabled):
    return {"schema_version": "fa-agent-connectors-command-v1", "request_id": request_id,
            "expected_revision": expected, "disabled_connector_ids": disabled}


def test_an_agent_switch_resends_only_its_original_body_after_a_lost_reply():
    agent, sent = agent_console([view(0, []), httpx.ReadTimeout("synthetic lost reply"), view(1, ["teams"]),
                                 (409, {"error": {"code": "AGENT_CONNECTORS_CHANGED"}})])
    assert agent.agent_connectors() == {"schema_version": "fa-console-agent-connectors-v1", "revision": 0,
                                        "applies_to": "new-conversations-only", "can_manage": True,
                                        "connectors": [{"connector_id": "teams", "display_name": "Microsoft Teams",
                                                        "on": True}]}
    off = switch("b" * 32, 0, ["teams"])
    with pytest.raises(ConsoleError) as lost:
        agent.select_agent_connectors(off)
    assert lost.value.code == "BACKEND_OFFLINE"
    assert agent.select_agent_connectors(off)["connectors"] == [
        {"connector_id": "teams", "display_name": "Microsoft Teams", "on": False}]
    with pytest.raises(ConsoleError) as stale:
        agent.select_agent_connectors(switch("c" * 32, 0, []))
    assert (stale.value.code, stale.value.status) == ("AGENT_CONNECTORS_CHANGED", 409)
    bodies = [content for method, _, content in sent if method == "POST"]
    assert bodies[0] == bodies[1] and len(bodies) == 3
    assert {path for _, path, _ in sent} == {"/agents/agent-b/work-policy/v1/agent-connectors"}


def test_an_agent_switch_is_read_only_without_approve_and_refuses_a_reply_for_another_choice():
    reader, sent = agent_console([view(1, ["teams"])], scopes=("Behavior.Read",))
    assert reader.agent_connectors()["can_manage"] is False
    with pytest.raises(ConsoleError) as denied:
        reader.select_agent_connectors(switch("d" * 32, 1, []))
    assert (denied.value.code, [method for method, _, _ in sent]) == ("AGENT_CONNECTORS_OWNER_ONLY", ["GET"])
    owner, _ = agent_console([view(2, [])])
    with pytest.raises(ConsoleError) as mismatch:
        owner.select_agent_connectors(switch("e" * 32, 1, ["teams"]))
    assert mismatch.value.code == "AGENT_CONNECTORS_RESPONSE_INVALID"


def test_bridge_admits_only_bounded_csrf_checked_connector_writes(real_interface):
    c = real_interface
    ui, headers = c.browser.ui, c.browser.headers
    for path, body, code in (("/api/team-connectors/changes", command("f" * 32, None), "TEAM_CONNECTOR_INPUT_INVALID"),
                             ("/api/agent-connectors", switch("f" * 32, 0, ["teams"]), "AGENT_CONNECTORS_INPUT_INVALID")):
        assert ui.post(path, json=body, headers={"Origin": headers["Origin"]}).json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
        oversized = ui.post(path, json={**body, "padding": "x" * (8 * 1024)}, headers=headers)
        assert (oversized.status_code, oversized.json()["error"]["code"]) == (413, code)
        assert ui.post(path, json=body, headers=headers).json()["error"]["code"] not in {"LOCAL_REQUEST_DENIED", code}
    assert is_connector_write("/api/agent-connectors") and not is_connector_write("/api/team-connectors")


def test_console_routes_have_no_delete_or_resume():
    from fastapi import FastAPI
    app = FastAPI()
    install_team_connector_routes(app, lambda: None)
    routes = sorted((route.path, *sorted(route.methods)) for route in app.routes if "connectors" in route.path)
    assert routes == [("/api/agent-connectors", "GET"), ("/api/agent-connectors", "POST"),
                      ("/api/team-connectors", "GET"), ("/api/team-connectors/changes", "POST")]
