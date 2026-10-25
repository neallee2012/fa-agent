"""Offline Team connector API tests: fake JWT and a fake Foundry with the official toolbox version/default shape."""

from __future__ import annotations

import copy
import json
import logging
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import A, TENANT
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier, BehaviorPrincipal
from shared_context.behavior_capabilities import runtime_grants
from shared_context.behavior_store import BehaviorStore
from shared_context.behavior_team_registry import TeamRegistry
from shared_context.errors import Problem
from shared_context.team_connector import current_team_connector_grants
from shared_context.team_connector_api import PREFIX, install_team_connector_routes
from shared_context.team_connector_wire import TEAM_CONNECTORS_ENABLED_ENV, TeamConnectorChange
from shared_context.team_mcp import PROJECT_SCOPE_ENV, TeamMcpFoundryClient, TeamMcpFoundryError, team_connection_prefix
from test_behavior_store import FakeTable, SCOPE
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token  # noqa: F401
from test_team_mcp_api import ENDPOINT, PROJECT, SECOND_OWNER, FakeFoundry

TOOLBOX = "fa-connector-team-one-teams"
CONNECTION = team_connection_prefix(PROJECT, A.team) + "teams"
APP_ID = "00000000-0000-4000-8000-000000000001"
SECRET = "OFFLINE-oauth-client-secret-5d3e9a"
READ_TOOLS = ["fetch", "fetch_blob", "get_schema", "list_agents", "search_paths"]
WRITE_TOOLS = ["ask", "call_function", "create_entity", "delete_entity", "do_action", "update_entity"]
URL = "https://workiq.svc.cloud.microsoft/mcp"


def entries(read="never", write="always", connection=CONNECTION):
    return [{"type": "mcp", "server_label": "teams_read", "server_url": URL, "require_approval": read,
             "allowed_tools": READ_TOOLS, "project_connection_id": connection},
            {"type": "mcp", "server_label": "teams_write", "server_url": URL, "require_approval": write,
             "allowed_tools": WRITE_TOOLS, "project_connection_id": connection}]


class FakeConnectorFoundry(FakeFoundry):
    """Adds the version metadata read and the OAuth2 connection PUT; a GET reads back only metadata, never keys."""

    def read_default_version(self, name):
        default, tools = self.read_toolbox(name)
        if default is None:
            return None, [], {}
        version = next(item for item in self.toolboxes[name]["versions"] if item["version"] == default)
        return default, tools, dict(version["metadata"])

    def connection(self, name, *, api_version=None):
        return super().connection(name)

    def put_oauth2_connection(self, name, *, metadata, **properties):
        self._maybe("put")
        self.puts.append({"name": name, **properties, "scopes": list(properties["scopes"])})
        self.arm[name] = dict(metadata)
        self._maybe("put_after")


@pytest.fixture
def connectors(auth, jwk_client, signing_key):
    store = BehaviorStore(FakeTable(), SCOPE)
    store.initialize()
    teams = TeamRegistry(store, tenant_id=TENANT, operator_oid=HUMAN_OID, legacy_team_id=A.team,
                         legacy_owner_oid=HUMAN_OID, agent_oids=tuple(auth.actors))
    operator = BehaviorPrincipal(TENANT, HUMAN_OID, A.team, "human", None, frozenset({"read", "submit", "approve"}))
    teams.register_owner(operator, "second-owner", "team-two", SECOND_OWNER)
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client, teams=teams)
    foundry = FakeConnectorFoundry()
    app = FastAPI()
    _install_error_handlers(app)
    runtime = SimpleNamespace(store=store, capability_grants={},
                              bindings=MappingProxyType({"b": SimpleNamespace(tenant_id=TENANT, team_id=A.team)}))
    composition = SimpleNamespace(runtime=runtime, verifier=verifier,
                                  config=SimpleNamespace(targets=(SimpleNamespace(project_endpoint=ENDPOINT),)))
    environ = {TEAM_CONNECTORS_ENABLED_ENV: "1", PROJECT_SCOPE_ENV: PROJECT, "FA_CONNECTOR_TEAMS_CLIENT_ID": APP_ID}
    install_team_connector_routes(app, composition, environ=environ, foundry_factory=lambda: foundry)

    def headers(identity="owner"):
        if identity == "agent":
            return {"Authorization": "Bearer " + token(
                signing_key, {"oid": A.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",))}
        oid = SECOND_OWNER if identity == "second" else HUMAN_OID
        scopes = "Behavior.Read" if identity == "read_only" else "Behavior.Read Behavior.Approve"
        return {"Authorization": "Bearer " + token(signing_key, {"oid": oid, "scp": scopes})}

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, foundry=foundry, headers=headers, runtime=runtime, environ=environ)


def team_grants(c):
    return {grant.entry_id: grant for grant in current_team_connector_grants(c.runtime)}


def view(c, identity="owner"):
    response = c.client.get(PREFIX + "/connectors", headers=c.headers(identity))
    assert response.status_code == 200, response.text
    [teams] = response.json()["connectors"]
    return teams


def change(c, request_id, *, enabled=True, read="never", write="always", secret=SECRET, expected=..., identity="owner"):
    expected = view(c, identity)["default_version"] if expected is ... else expected
    body = {"schema_version": "fa-team-connector-change-v1", "request_id": request_id, "connector_id": "teams",
            "expected_version": expected, "enabled": enabled, "read_approval": read, "write_approval": write}
    if secret is not None:
        body["client_secret"] = secret
    return body


def post(c, body, *, identity="owner"):
    return c.client.post(PREFIX + "/changes", headers=c.headers(identity), json=body)


def code(response):
    return response.json()["error"]["code"]


def test_default_off_installs_no_routes():
    app = FastAPI()
    install_team_connector_routes(app, SimpleNamespace(), environ={})
    with TestClient(app) as client:
        assert client.get(PREFIX + "/connectors").status_code == 404


def test_owner_enables_teams_with_one_oauth_connection_and_one_version_per_request(connectors):
    c = connectors
    assert view(c) == {
        "connector_id": "teams", "display_name": "Microsoft Teams",
        "disclosure": "Microsoft Teams 經 Work IQ 連線。Agent 能讀取的不只 Teams，也可能包含你的 Microsoft 365 郵件與檔案。",
        "revoke_url": "https://entra.microsoft.com", "toolbox": TOOLBOX, "connection": CONNECTION,
        "default_version": None, "settings": None}
    body = change(c, "a" * 32)
    applied = post(c, body)
    assert applied.status_code == 200, applied.text
    data = applied.json()
    assert (data["schema_version"], data["toolbox_version"]) == ("fa-team-connector-change-result-v1", "v1")
    assert (data["connector"]["default_version"], data["connector"]["settings"]) == (
        "v1", {"enabled": True, "read_approval": "never", "write_approval": "always"})
    assert c.foundry.puts == [{
        "name": CONNECTION, "target": URL, "client_id": APP_ID, "client_secret": SECRET,
        "authorization_url": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
        "token_url": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
        "scopes": ["api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask", "offline_access"]}]
    assert c.foundry.arm[CONNECTION] == {"fa_team": A.team, "fa_connector": "teams", "fa_request_id": "a" * 32}
    [created] = c.foundry.created
    assert (created["toolbox"], created["tools"]) == (TOOLBOX, entries())
    metadata = dict(created["metadata"])
    assert len(metadata.pop("fa_command")) == 64
    assert metadata == {"fa_request_id": "a" * 32, "fa_team": A.team, "fa_connector": "teams",
                        "fa_connector_enabled": "true", "fa_connection": CONNECTION}
    resent = post(c, body)
    assert resent.status_code == 200 and resent.json()["toolbox_version"] == "v1"
    assert (len(c.foundry.created), len(c.foundry.puts)) == (1, 1)
    conflict = post(c, {**body, "write_approval": "never"})
    assert conflict.status_code == 409 and code(conflict) == "TEAM_MCP_COMMAND_CONFLICT"


@pytest.mark.parametrize("stage", ["put_after", "create_after"])
def test_a_lost_first_enable_reply_never_rebuilds_the_connection_or_version(connectors, stage):
    c = connectors
    c.foundry.fail[stage] = None
    body = change(c, "b" * 32)
    unknown = post(c, body)
    assert unknown.status_code == 503 and code(unknown) == "TEAM_MCP_OUTCOME_UNKNOWN"
    applied = post(c, body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["toolbox_version"] == "v1" and view(c)["default_version"] == "v1"
    assert (len(c.foundry.puts), len(c.foundry.created)) == (1, 1)


@pytest.mark.parametrize("stage", ["create_after", "promote"])
def test_a_lost_disable_reply_is_resent_into_the_same_version(connectors, stage):
    c = connectors
    assert post(c, change(c, "ba" * 16)).status_code == 200
    c.foundry.fail[stage] = None
    body = change(c, "bb" * 16, enabled=False, secret=None)
    unknown = post(c, body)
    assert unknown.status_code == 503 and code(unknown) == "TEAM_MCP_OUTCOME_UNKNOWN"
    assert view(c)["default_version"] == "v1"
    applied = post(c, body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["toolbox_version"] == "v2" and view(c)["settings"]["enabled"] is False
    assert (len(c.foundry.puts), len(c.foundry.created)) == (1, 2)


def test_disable_and_reenable_make_new_versions_with_the_same_tools_and_connection(monkeypatch, connectors):
    c = connectors
    monkeypatch.setattr("shared_context.team_connector.GRANTS_SECONDS", -1.0)
    assert team_grants(c) == {}
    assert post(c, change(c, "c" * 32)).status_code == 200
    grants = team_grants(c)
    assert runtime_grants(c.runtime) == {}, "connectors are captured per conversation, never selected"
    assert {key: (grant.toolbox.toolbox, grant.toolbox.version, grant.tools) for key, grant in grants.items()} == {
        "connector-teams_read": (TOOLBOX, "v1", tuple(f"connector-teams_read-{tool}" for tool in READ_TOOLS)),
        "connector-teams_write": (TOOLBOX, "v1", tuple(f"connector-teams_write-{tool}" for tool in WRITE_TOOLS))}

    disabled = post(c, change(c, "d" * 32, enabled=False, secret=None))
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["connector"]["settings"] == {"enabled": False, "read_approval": "never",
                                                        "write_approval": "always"}
    assert c.foundry.created[-1]["tools"] == entries()
    assert c.foundry.created[-1]["metadata"]["fa_connector_enabled"] == "false"
    assert team_grants(c) == {}

    approval = post(c, change(c, "e" * 32, enabled=True, read="always", secret=None))
    assert approval.status_code == 200, approval.text
    assert c.foundry.created[-1]["tools"] == entries(read="always")
    assert team_grants(c)["connector-teams_read"].toolbox.version == "v3"
    assert [item["version"] for item in c.foundry.toolboxes[TOOLBOX]["versions"]] == ["v1", "v2", "v3"]
    assert c.foundry.promoted == [(TOOLBOX, "v2"), (TOOLBOX, "v3")]
    assert [put["name"] for put in c.foundry.puts] == [CONNECTION]


def test_the_first_enable_needs_the_secret_and_the_environments_app(connectors):
    c = connectors
    missing = post(c, change(c, "f" * 32, secret=None))
    assert missing.status_code == 422 and code(missing) == "TEAM_CONNECTOR_SECRET_REQUIRED"
    c.environ.pop("FA_CONNECTOR_TEAMS_CLIENT_ID")
    no_app = post(c, change(c, "1" * 32))
    assert no_app.status_code == 422 and code(no_app) == "TEAM_MCP_CONNECTION_CREATE_UNAVAILABLE"
    assert c.foundry.puts == [] and c.foundry.created == []


@pytest.mark.parametrize("mode", [None, "public"])
def test_public_or_unset_network_mode_still_adds_work_iq(connectors, mode):
    c = connectors
    if mode is not None:
        c.environ["FA_NETWORK_MODE"] = mode
    applied = post(c, change(c, "71" * 16))
    assert applied.status_code == 200, applied.text
    assert (len(c.foundry.puts), len(c.foundry.created)) == (1, 1)


def test_private_network_mode_refuses_adding_or_reenabling_work_iq_but_still_disables(connectors, caplog):
    # ADR-0010: Work IQ is not supported in a network-isolated project. An existing grant is never deleted.
    c = connectors
    assert post(c, change(c, "72" * 16)).status_code == 200
    c.environ["FA_NETWORK_MODE"] = "private"
    before = (len(c.foundry.puts), len(c.foundry.created))
    with caplog.at_level(logging.WARNING, logger="shared_context.team_connector"):
        for body in (change(c, "73" * 16), change(c, "74" * 16, read="always", secret=None)):
            refused = post(c, body)
            assert refused.status_code == 422 and code(refused) == "TEAM_CONNECTOR_PRIVATE_NETWORK"
            assert refused.headers["cache-control"] == "no-store"
    assert "TEAM_CONNECTOR_PRIVATE_NETWORK_REFUSED connector=teams" in caplog.text and SECRET not in caplog.text
    assert (len(c.foundry.puts), len(c.foundry.created)) == before
    disabled = post(c, change(c, "75" * 16, enabled=False, secret=None))
    assert disabled.status_code == 200 and disabled.json()["connector"]["settings"]["enabled"] is False
    reenable = post(c, change(c, "76" * 16, secret=None))
    assert reenable.status_code == 422 and code(reenable) == "TEAM_CONNECTOR_PRIVATE_NETWORK"
    assert c.foundry.toolboxes[TOOLBOX]["versions"][-1]["version"] == "v2"


@pytest.mark.parametrize("enabled", [True, False])
def test_private_network_mode_refuses_a_work_iq_secret_before_any_foundry_read(connectors, monkeypatch, enabled):
    # A first-ever enabled=false with a secret would otherwise write the OAuth2 connection.
    c = connectors
    c.environ["FA_NETWORK_MODE"] = "private"
    for name in ("request_version", "read_default_version", "read_toolbox", "connection"):
        monkeypatch.setattr(c.foundry, name, lambda *args, **kwargs: pytest.fail("no Foundry read before the guard"))
    refused = post(c, change(c, "77" * 16, enabled=enabled, expected=None))
    assert refused.status_code == 422 and code(refused) == "TEAM_CONNECTOR_PRIVATE_NETWORK"
    assert (c.foundry.reads, c.foundry.puts, c.foundry.created, c.foundry.arm) == (0, [], [], {})


@pytest.mark.parametrize("status", [400, 403])
def test_a_version_refused_after_this_requests_secret_is_saved_asks_for_the_same_request_again(connectors, status):
    c = connectors
    c.foundry.fail["create_before"] = status
    body = change(c, "4a" * 16)
    refused = post(c, body)
    assert refused.status_code == 503 and code(refused) == "TEAM_MCP_OUTCOME_UNKNOWN"
    assert refused.json()["error"]["upstream_status"] == status
    assert (len(c.foundry.puts), len(c.foundry.created)) == (1, 0)
    assert c.foundry.arm[CONNECTION]["fa_request_id"] == "4a" * 16
    applied = post(c, body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["toolbox_version"] == "v1"
    assert (len(c.foundry.puts), len(c.foundry.created)) == (1, 1), "the resend never writes the secret again"


@pytest.mark.parametrize("status,expected", [(400, (422, "TEAM_MCP_REFUSED")), (403, (403, "TEAM_MCP_FORBIDDEN"))])
def test_a_resend_whose_version_is_refused_again_gets_the_definite_answer(connectors, status, expected):
    c = connectors
    body = change(c, "4e" * 16)
    c.foundry.fail["create_before"] = status
    first = post(c, body)
    assert first.status_code == 503 and code(first) == "TEAM_MCP_OUTCOME_UNKNOWN"
    c.foundry.fail["create_before"] = status
    again = post(c, body)
    assert (again.status_code, code(again)) == expected, "the secret was written by the first send, not this one"
    assert (len(c.foundry.puts), len(c.foundry.created)) == (1, 0)
    assert c.foundry.arm[CONNECTION]["fa_request_id"] == "4e" * 16
    applied = post(c, body)
    assert applied.status_code == 200 and applied.json()["toolbox_version"] == "v1"
    assert len(c.foundry.puts) == 1, "the same request_id writes the secret once"


def test_a_version_refused_without_a_secret_write_in_this_request_stays_a_refusal(connectors):
    c = connectors
    assert post(c, change(c, "4b" * 16)).status_code == 200
    c.foundry.fail["create_before"] = 400
    refused = post(c, change(c, "4c" * 16, enabled=False, secret=None))
    assert refused.status_code == 422 and code(refused) == "TEAM_MCP_REFUSED"
    assert (len(c.foundry.puts), len(c.foundry.created)) == (1, 1)
    assert c.foundry.arm[CONNECTION]["fa_request_id"] == "4b" * 16


def test_the_version_is_built_before_the_connection_write(monkeypatch, connectors):
    c = connectors

    def refuse(*_args):
        raise Problem(422, "TEAM_CONNECTOR_INVALID", "synthetic local refusal")
    monkeypatch.setattr("shared_context.team_connector.connector_tools", refuse)
    assert post(c, change(c, "4d" * 16)).status_code == 422
    assert c.foundry.puts == [] and CONNECTION not in c.foundry.arm and c.foundry.created == []


def test_a_stale_read_is_refused_before_any_write(connectors):
    c = connectors
    assert post(c, change(c, "2" * 32)).status_code == 200
    stale = post(c, change(c, "3" * 32, enabled=False, expected=None))
    assert stale.status_code == 409 and code(stale) == "TEAM_MCP_CHANGED"
    assert (len(c.foundry.created), len(c.foundry.puts)) == (1, 1)


def test_the_secret_is_in_no_response_metadata_digest_readback_or_log(connectors, caplog):
    caplog.set_level(logging.DEBUG)
    c = connectors
    body = change(c, "4" * 32)
    c.foundry.fail["create_after"] = None
    unknown = post(c, body)
    applied = post(c, body)
    assert applied.status_code == 200, applied.text
    command = TeamConnectorChange.model_validate_json(json.dumps(body))
    texts = [unknown.text, applied.text, json.dumps(c.foundry.created), json.dumps(c.foundry.arm), caplog.text,
             c.client.get(PREFIX + "/connectors", headers=c.headers()).text,
             json.dumps(command.material()), command.model_dump_json(), repr(command)]
    assert [SECRET in text for text in texts] == [False] * len(texts)
    assert c.foundry.puts[0]["client_secret"] == SECRET
    bad = post(c, {**change(c, "5" * 32), "client_secret": "a\nOFFLINE-multiline"})
    assert code(bad) == "TEAM_MCP_INVALID" and "OFFLINE-multiline" not in bad.text


@pytest.mark.parametrize("extra", [{"connector_id": "github"}, {"connector_id": "Teams"},
                                   {"server_url": "https://evil.example.test/mcp"},
                                   {"allowed_tools": ["fetch", "send_mail"]},
                                   {"authentication": {"kind": "managed_oauth2"}}])
def test_an_unregistered_connector_url_tool_or_auth_shape_is_refused(connectors, extra):
    c = connectors
    refused = post(c, {**change(c, "6" * 32), **extra})
    assert refused.status_code == 422 and code(refused) == "TEAM_MCP_INVALID"
    assert c.foundry.puts == [] and c.foundry.created == []


@pytest.mark.parametrize("edit", [
    lambda tools: tools[0]["allowed_tools"].append("send_mail"),
    lambda tools: tools[1].update(server_url="https://evil.example.test/mcp"),
    lambda tools: tools[0].update(project_connection_id="someone-else"),
    lambda tools: tools.pop(),
])
def test_a_default_version_fa_did_not_write_offers_no_tools(monkeypatch, connectors, edit):
    c = connectors
    monkeypatch.setattr("shared_context.team_connector.GRANTS_SECONDS", -1.0)
    assert post(c, change(c, "7" * 32)).status_code == 200
    version = c.foundry.toolboxes[TOOLBOX]["versions"][0]
    edit(version["tools"])
    assert view(c)["settings"] is None and team_grants(c) == {}
    # The owner can still write a clean version over it.
    assert post(c, change(c, "8" * 32, secret=None)).status_code == 200
    assert c.foundry.created[-1]["tools"] == entries() and set(team_grants(c)) == {
        "connector-teams_read", "connector-teams_write"}


def test_teams_cannot_read_or_write_each_others_connector(connectors):
    c = connectors
    assert post(c, change(c, "9" * 32)).status_code == 200
    other = view(c, "second")
    assert (other["toolbox"], other["default_version"], other["settings"]) == ("fa-connector-team-two-teams", None, None)
    assert other["connection"] == team_connection_prefix(PROJECT, "team-two") + "teams"
    applied = post(c, change(c, "aa" * 16, identity="second"), identity="second")
    assert applied.status_code == 200, applied.text
    assert [put["name"] for put in c.foundry.puts] == [CONNECTION, other["connection"]]
    assert [item["toolbox"] for item in c.foundry.created] == [TOOLBOX, "fa-connector-team-two-teams"]
    assert view(c)["default_version"] == "v1" and len(c.foundry.toolboxes[TOOLBOX]["versions"]) == 1
    for identity in ("agent", "read_only"):
        denied = c.client.post(PREFIX + "/changes", headers=c.headers(identity), json=change(c, "ab" * 16))
        assert denied.status_code == 403 and code(denied) == "TEAM_MCP_OWNER_ONLY"
    agent = c.client.get(PREFIX + "/connectors", headers=c.headers("agent"))
    assert agent.status_code == 403 and code(agent) == "TEAM_MCP_OWNER_ONLY"


def test_another_teams_toolbox_or_connection_under_the_same_name_is_never_written(connectors):
    c = connectors
    # Team "team-one-teams" owns the generic Team MCP toolbox of the same name.
    c.foundry.toolboxes[TOOLBOX] = {"default": "v1", "versions": [
        {"version": "v1", "metadata": {"fa_team": "team-one-teams"}, "tools": [{"type": "web_search"}]}]}
    taken = post(c, change(c, "ac" * 16))
    assert taken.status_code == 409 and code(taken) == "TEAM_CONNECTOR_TOOLBOX_TAKEN"
    del c.foundry.toolboxes[TOOLBOX]
    c.foundry.arm[CONNECTION] = {"fa_team": "someone-else", "fa_connector": "teams"}
    clash = post(c, change(c, "ad" * 16))
    assert clash.status_code == 409 and code(clash) == "TEAM_MCP_COMMAND_CONFLICT"
    assert c.foundry.puts == [] and c.foundry.created == []


def test_the_connector_does_not_use_the_team_mcp_server_quota(connectors):
    c = connectors
    eight = [{"type": "mcp", "server_label": f"S{index}", "server_url": "https://mcp.example.test/sse",
              "require_approval": "never", "allowed_tools": ["search"]} for index in range(8)]
    c.foundry.toolboxes["fa-team-team-one"] = {"default": "v1", "versions": [
        {"version": "v1", "metadata": {}, "tools": copy.deepcopy(eight)}]}
    assert post(c, change(c, "ae" * 16)).status_code == 200
    assert c.foundry.toolboxes["fa-team-team-one"]["versions"] == [{"version": "v1", "metadata": {}, "tools": eight}]


def test_arm_oauth2_connection_put_matches_the_p0b_readback_shape_and_never_logs_the_secret(caplog):
    caplog.set_level(logging.DEBUG)
    seen = []

    def arm(request):
        seen.append((request.method, str(request.url), request.content))
        return httpx.Response(500, json={"error": {"code": "InternalError"}})

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="OFFLINE-arm-token"))
    client = TeamMcpFoundryClient(ENDPOINT, credential, project_scope=PROJECT, transport=httpx.MockTransport(arm))
    with pytest.raises(TeamMcpFoundryError) as failed:
        client.put_oauth2_connection(
            CONNECTION, target=URL, client_id=APP_ID, client_secret=SECRET,
            authorization_url="https://login.microsoftonline.com/t/oauth2/v2.0/authorize",
            token_url="https://login.microsoftonline.com/t/oauth2/v2.0/token",
            scopes=("api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask", "offline_access"), metadata={"fa_team": A.team})
    assert failed.value.status == 500 and SECRET not in repr(failed.value) and SECRET not in caplog.text
    [(method, url, content)] = seen
    assert (method, url) == ("PUT", "https://management.azure.com" + PROJECT
                             + f"/connections/{CONNECTION}?api-version=2025-04-01-preview")
    assert json.loads(content) == {"properties": {
        "category": "RemoteTool", "authType": "OAuth2", "target": URL, "isSharedToAll": False,
        "authorizationUrl": "https://login.microsoftonline.com/t/oauth2/v2.0/authorize",
        "tokenUrl": "https://login.microsoftonline.com/t/oauth2/v2.0/token",
        "refreshUrl": "https://login.microsoftonline.com/t/oauth2/v2.0/token",
        "scopes": ["api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask", "offline_access"],
        "credentials": {"clientId": APP_ID, "clientSecret": SECRET}, "metadata": {"fa_team": A.team}}}


def test_a_failed_grants_read_is_reused_briefly_so_an_outage_waits_once(monkeypatch, connectors):
    c = connectors
    clock = [1000.0]
    monkeypatch.setattr("shared_context.team_connector.monotonic", lambda: clock[0])
    assert post(c, change(c, "f" * 32)).status_code == 200
    assert team_grants(c) != {}
    c.foundry.fail["read"] = 503  # one failed read; an uncached second capture would read again and succeed
    clock[0] += 31
    reads = c.foundry.reads
    assert team_grants(c) != {}, "the last read grants stay offered"
    assert team_grants(c) != {} and c.foundry.reads == reads + 1
    clock[0] += 11
    assert team_grants(c) != {} and c.foundry.reads == reads + 2


def test_a_grants_read_that_started_before_a_change_never_stores_its_stale_grants(monkeypatch, connectors):
    c = connectors
    assert post(c, change(c, "fa" * 16)).status_code == 200
    original = c.foundry.read_default_version

    def read_then_disable(name):
        read = original(name)  # this read still sees the enabled connector; the owner disables it before it stores
        monkeypatch.setattr(c.foundry, "read_default_version", original)
        assert post(c, change(c, "fb" * 16, enabled=False, secret=None)).status_code == 200
        return read

    monkeypatch.setattr(c.foundry, "read_default_version", read_then_disable)
    assert team_grants(c) != {}
    assert team_grants(c) == {}, "the disable applies to the next capture, not 30 s later"


def test_captures_during_a_failing_read_share_that_one_foundry_read(monkeypatch, connectors):
    from test_team_mcp_api import concurrent_captures_during_one_slow_failing_read
    c = connectors
    assert post(c, change(c, "fc" * 16)).status_code == 200
    c.runtime.team_connectors.forget(TENANT, A.team)
    reads, results = concurrent_captures_during_one_slow_failing_read(
        lambda: team_grants(c), c.foundry, "read_default_version", monkeypatch)
    assert reads == 1 and results == [{}] * 3
