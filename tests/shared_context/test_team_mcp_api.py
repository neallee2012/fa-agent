"""Offline Team MCP owner API tests: fake JWT, a fake Foundry with the official toolbox version/default shape."""

from __future__ import annotations

import copy
import json
import logging
import threading
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import A, TENANT
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier, BehaviorPrincipal
from shared_context.behavior_capabilities import CapabilityGrant, runtime_grants
from shared_context.behavior_store import BehaviorStore
from shared_context.behavior_team_registry import TeamRegistry
from shared_context.team_mcp import (
    PROJECT_SCOPE_ENV, TeamMcpFoundryClient, TeamMcpFoundryError, team_connection_name, team_connection_prefix,
)
from shared_context.team_mcp_api import PREFIX, install_team_mcp_routes
from shared_context.team_mcp_wire import TEAM_MCP_ENABLED_ENV, TEAM_MCP_SINGLE_TEAM_ENV
from shared_context.tool_catalog import parse_entry_file
from test_behavior_store import FakeTable, SCOPE
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token  # noqa: F401

SECOND_OWNER = "99999999-9999-9999-9999-999999999999"
WEBIQ_TOOLBOX_SHA = "e27ef360fe9cd94c6e45d26e0d9b753d64ca7b0d5f51917754c72c5ae3bbdb01"
ENDPOINT = "https://acct.services.ai.azure.com/api/projects/proj"
PROJECT = ("/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
           "/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj")
TOOLBOX = "fa-team-team-one"
SECRET = "OFFLINE-team-key-4f9c2d71"


class FakeFoundry:
    """Official toolbox semantics: each create makes a new immutable version; the first one becomes the default."""

    def __init__(self):
        self.connections = [
            {"name": "safe-aad", "connection_type": "RemoteTool", "credential_type": "AAD"},
            {"name": "secret-key", "connection_type": "CustomKeys", "credential_type": "CustomKeys"},
        ]
        self.toolboxes, self.created, self.promoted, self.puts, self.arm = {}, [], [], [], {}
        self.fail, self.reads = {}, 0

    def _maybe(self, stage):
        if stage in self.fail:
            raise TeamMcpFoundryError(self.fail.pop(stage))

    def list_connections(self):
        from shared_context.team_mcp import _connection
        return tuple(_connection(item) for item in self.connections)

    def read_toolbox(self, name):
        self.reads += 1
        self._maybe("read")
        box = self.toolboxes.get(name)
        if box is None or box["default"] is None:
            return None, []
        version = next(item for item in box["versions"] if item["version"] == box["default"])
        return box["default"], copy.deepcopy(version["tools"])

    def default_version(self, name):
        box = self.toolboxes.get(name)
        return None if box is None else box["default"]

    def request_version(self, name, request_id):
        for item in reversed(self.toolboxes.get(name, {}).get("versions", [])):
            if item["metadata"].get("fa_request_id") == request_id:
                return {"version": item["version"], "metadata": dict(item["metadata"])}
        return None

    def create_version(self, name, *, tools, metadata):
        self._maybe("create_before")
        box = self.toolboxes.setdefault(name, {"default": None, "versions": []})
        version = f"v{len(box['versions']) + 1}"
        box["versions"].append({"version": version, "metadata": dict(metadata), "tools": copy.deepcopy(tools)})
        if box["default"] is None:
            box["default"] = version
        self.created.append({"toolbox": name, "version": version, "tools": copy.deepcopy(tools),
                             "metadata": dict(metadata)})
        self._maybe("create_after")
        return version

    def promote(self, name, version):
        self._maybe("promote")
        self.toolboxes[name]["default"] = version
        self.promoted.append((name, version))

    def connection(self, name):
        return None if name not in self.arm else dict(self.arm[name])

    def put_connection(self, name, *, target, keys, metadata):
        self._maybe("put")
        self.puts.append({"name": name, "target": target, "keys": dict(keys)})
        self.arm[name] = dict(metadata)
        self.connections.append({"name": name, "connection_type": "RemoteTool", "credential_type": "CustomKeys"})


@pytest.fixture
def team_mcp(auth, jwk_client, signing_key):
    table = FakeTable()
    store = BehaviorStore(table, SCOPE)
    store.initialize()
    teams = TeamRegistry(
        store, tenant_id=TENANT, operator_oid=HUMAN_OID, legacy_team_id=A.team,
        legacy_owner_oid=HUMAN_OID, agent_oids=tuple(auth.actors))
    operator = BehaviorPrincipal(TENANT, HUMAN_OID, A.team, "human", None,
                                 frozenset({"read", "submit", "approve"}))
    teams.register_owner(operator, "second-owner", "team-two", SECOND_OWNER)
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client, teams=teams)
    foundry = FakeFoundry()
    app = FastAPI()
    _install_error_handlers(app)
    runtime = SimpleNamespace(store=store, capability_grants={},
                              bindings=MappingProxyType({"b": SimpleNamespace(tenant_id=TENANT, team_id=A.team)}))
    composition = SimpleNamespace(runtime=runtime, verifier=verifier,
                                  config=SimpleNamespace(targets=(SimpleNamespace(project_endpoint=ENDPOINT),)))
    install_team_mcp_routes(app, composition, environ={TEAM_MCP_ENABLED_ENV: "1", PROJECT_SCOPE_ENV: PROJECT},
                            foundry_factory=lambda: foundry)

    def headers(identity="owner"):
        if identity == "agent":
            return {"Authorization": "Bearer " + token(
                signing_key, {"oid": A.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",)),
                "Content-Type": "application/json"}
        oid = SECOND_OWNER if identity == "second" else HUMAN_OID
        scopes = "Behavior.Read" if identity == "read_only" else "Behavior.Read Behavior.Approve"
        return {"Authorization": "Bearer " + token(signing_key, {"oid": oid, "scp": scopes}),
                "Content-Type": "application/json"}

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, foundry=foundry, store=store, headers=headers, runtime=runtime,
                              service=app.state.team_mcp)


def view(c, identity="owner"):
    response = c.client.get(PREFIX + "/servers", headers=c.headers(identity))
    assert response.status_code == 200, response.text
    return response.json()


def change(c, request_id, operation, label, *, server_url="https://mcp.example.test/sse", tools=("search",),
           connection="safe-aad", approval="never", expected=..., new_connection=None, identity="owner"):
    expected = view(c, identity)["default_version"] if expected is ... else expected
    body = {"schema_version": "fa-team-mcp-change-v2", "request_id": request_id, "expected_version": expected,
            "operation": operation, "server_label": label, "server": None}
    if operation in {"add", "modify"}:
        body["server"] = {"server_label": label, "server_url": server_url, "project_connection_id": connection,
                          "allowed_tools": list(tools), "require_approval": approval}
    if new_connection is not None:
        body["new_connection"] = new_connection
    return body


def post(c, body, *, identity="owner"):
    return c.client.post(PREFIX + "/changes", headers=c.headers(identity), json=body)


def code(response):
    return response.json()["error"]["code"]


def test_default_off_installs_no_routes(team_mcp):
    app = FastAPI()
    install_team_mcp_routes(app, SimpleNamespace(), environ={})
    with TestClient(app) as client:
        assert client.get(PREFIX + "/servers").status_code == 404


@pytest.mark.parametrize("environ", [{TEAM_MCP_ENABLED_ENV: "true"}, {TEAM_MCP_SINGLE_TEAM_ENV: "yes", TEAM_MCP_ENABLED_ENV: "1"}])
def test_invalid_settings_fail_explicitly(environ, team_mcp):
    with pytest.raises(ValueError):
        install_team_mcp_routes(FastAPI(), SimpleNamespace(), environ=environ)


def test_owner_add_writes_one_official_version_and_the_same_request_reuses_it(team_mcp):
    c = team_mcp
    first = view(c)
    assert (first["schema_version"], first["toolbox"], first["default_version"], first["servers"]) == (
        "fa-team-mcp-v2", TOOLBOX, None, [])
    body = change(c, "a" * 32, "add", "Alpha", tools=("beta", "alpha"), approval="always")
    added = post(c, body)
    assert added.status_code == 200, added.text
    data = added.json()
    assert (data["schema_version"], data["toolbox_version"], data["view"]["default_version"]) == (
        "fa-team-mcp-change-result-v2", "v1", "v1")
    assert data["view"]["servers"] == [{
        "server_label": "Alpha", "server_url": "https://mcp.example.test/sse", "project_connection_id": "safe-aad",
        "allowed_tools": ["alpha", "beta"], "require_approval": "always", "version": "v1"}]
    assert c.foundry.created[0]["tools"] == [{
        "type": "mcp", "server_label": "Alpha", "server_url": "https://mcp.example.test/sse",
        "require_approval": "always", "allowed_tools": ["alpha", "beta"], "project_connection_id": "safe-aad"}]
    metadata = c.foundry.created[0]["metadata"]
    assert metadata["fa_request_id"] == "a" * 32 and metadata["fa_team"] == A.team and len(metadata["fa_command"]) == 64
    resent = post(c, body)
    assert resent.status_code == 200 and resent.json()["toolbox_version"] == "v1"
    assert len(c.foundry.created) == 1
    conflict = post(c, {**body, "server": {**body["server"], "allowed_tools": ["other"]}})
    assert conflict.status_code == 409 and code(conflict) == "TEAM_MCP_COMMAND_CONFLICT"


@pytest.mark.parametrize("stage", ["create_after", "promote"])
def test_a_lost_reply_is_resent_with_the_same_request_and_never_makes_a_second_version(team_mcp, stage):
    c = team_mcp
    assert post(c, change(c, "b" * 32, "add", "Alpha")).status_code == 200
    c.foundry.fail[stage] = None
    body = change(c, "c" * 32, "add", "Beta", tools=("browse",))
    unknown = post(c, body)
    assert unknown.status_code == 503 and code(unknown) == "TEAM_MCP_OUTCOME_UNKNOWN"
    assert len(c.foundry.created) == 2 and view(c)["default_version"] == "v1"
    applied = post(c, body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["toolbox_version"] == "v2" and len(c.foundry.created) == 2
    assert [server["server_label"] for server in view(c)["servers"]] == ["Alpha", "Beta"]


def test_a_stale_read_or_a_change_that_lost_the_race_is_never_promoted(team_mcp):
    c = team_mcp
    assert post(c, change(c, "d" * 32, "add", "Alpha")).status_code == 200
    stale = post(c, change(c, "e" * 32, "add", "Beta", expected=None))
    assert stale.status_code == 409 and code(stale) == "TEAM_MCP_CHANGED" and len(c.foundry.created) == 1
    c.foundry.fail["promote"] = None
    first = change(c, "f" * 32, "add", "Beta")
    assert code(post(c, first)) == "TEAM_MCP_OUTCOME_UNKNOWN"
    assert post(c, change(c, "1" * 32, "add", "Gamma")).status_code == 200
    late = post(c, first)
    assert late.status_code == 409 and code(late) == "TEAM_MCP_CHANGED"
    assert view(c)["default_version"] == "v3" and ("fa-team-team-one", "v2") not in c.foundry.promoted


def test_disable_makes_a_new_version_and_the_last_server_cannot_be_disabled(team_mcp):
    c = team_mcp
    assert post(c, change(c, "2" * 32, "add", "Alpha")).status_code == 200
    assert post(c, change(c, "3" * 32, "add", "Beta", tools=("browse",))).status_code == 200
    disabled = post(c, change(c, "4" * 32, "disable", "Alpha"))
    assert disabled.status_code == 200, disabled.text
    assert [(item["server_label"], item["version"]) for item in disabled.json()["view"]["servers"]] == [("Beta", "v3")]
    last = post(c, change(c, "5" * 32, "disable", "Beta"))
    assert last.status_code == 409 and code(last) == "TEAM_MCP_LAST_SERVER"
    assert len(c.foundry.created) == 3 and view(c)["default_version"] == "v3"


def test_tools_this_page_does_not_manage_are_kept_and_never_edited(team_mcp):
    c = team_mcp
    other = [{"type": "web_search", "name": "web"},
             {"type": "mcp", "server_label": "Portal", "server_url": "https://portal.example.test/mcp",
              "require_approval": "never", "headers": {"x": "y"}}]
    c.foundry.toolboxes[TOOLBOX] = {"default": "v1", "versions": [
        {"version": "v1", "metadata": {}, "tools": copy.deepcopy(other)}]}
    current = view(c)
    assert current["servers"] == [] and current["other_tools"] == 2
    assert code(post(c, change(c, "6" * 32, "modify", "Portal"))) == "TEAM_MCP_MISSING"
    assert code(post(c, change(c, "7" * 32, "add", "Portal"))) == "TEAM_MCP_EXISTS"
    assert post(c, change(c, "8" * 32, "add", "Alpha")).status_code == 200
    assert c.foundry.created[-1]["tools"][:2] == other
    assert post(c, change(c, "9" * 32, "disable", "Alpha")).status_code == 200
    assert c.foundry.created[-1]["tools"] == other


def test_multiteam_hides_and_refuses_secret_connections_but_offers_the_teams_own(team_mcp):
    c = team_mcp
    owned = team_connection_name(PROJECT, A.team, "Docs")
    other_team = team_connection_name(PROJECT, "team-two", "Docs")
    c.foundry.connections += [{"name": owned, "connection_type": "RemoteTool", "credential_type": "CustomKeys"},
                              {"name": other_team, "connection_type": "RemoteTool", "credential_type": "CustomKeys"}]
    listed = c.client.get(PREFIX + "/connections", headers=c.headers()).json()
    assert [(item["name"], item["team_owned"]) for item in listed["connections"]] == [("safe-aad", False), (owned, True)]
    assert listed["hidden_secret_connections"] == 2
    for name in ("secret-key", other_team):
        denied = post(c, change(c, "aa" * 16, "add", "Alpha", connection=name))
        assert denied.status_code == 422 and code(denied) == "TEAM_MCP_CONNECTION_DENIED"
    assert post(c, change(c, "ab" * 16, "add", "Alpha", connection=owned)).status_code == 200


@pytest.mark.parametrize("kind", ["AppInsights", "ContainerRegistry", "AzureOpenAI", "unknown-category"])
def test_mcp_connections_exclude_non_tool_types_and_refuse_a_posted_reference(team_mcp, kind):
    c = team_mcp
    c.service.single_team = True
    c.foundry.connections.append({"name": "wrong-kind", "connection_type": kind, "credential_type": "ApiKey"})
    connections = c.client.get(PREFIX + "/connections", headers=c.headers())
    assert {item["name"] for item in connections.json()["connections"]} == {"safe-aad", "secret-key"}
    refused = post(c, change(c, "ae" * 16, "add", "Alpha", connection="wrong-kind"))
    assert refused.status_code == 422 and code(refused) == "TEAM_MCP_CONNECTION_DENIED"
    assert c.foundry.created == []


@pytest.mark.parametrize("kind", ["RemoteTool", "RemoteTool_Preview", "CustomKeys"])
def test_mcp_connection_categories_keep_existing_authentication_policy(team_mcp, kind):
    c = team_mcp
    c.service.single_team = True
    c.foundry.connections = [{"name": "mcp-auth", "connection_type": kind, "credential_type": "CustomKeys"}]
    available = c.client.get(PREFIX + "/connections", headers=c.headers())
    assert [item["name"] for item in available.json()["connections"]] == ["mcp-auth"]
    response = post(c, change(c, "af" * 16, "add", "Alpha", connection="mcp-auth"))
    assert response.status_code == 200, response.text
    assert c.foundry.created[0]["tools"][0]["project_connection_id"] == "mcp-auth"


def test_plaintext_secret_url_is_refused_with_team_mcp_code(team_mcp):
    c = team_mcp
    body = change(c, "ac" * 16, "add", "Alpha",
                  server_url="https://mcp.example.test/sse?api_key=Zx9Qm2Lp7Rt4Vw8Ky3Nb6Hc1Jd5Fg0Sa")
    refused = post(c, body)
    assert refused.status_code == 422 and code(refused) == "TEAM_MCP_PLAINTEXT_SECRET"


def test_foundry_403_and_definite_400_create_nothing(team_mcp):
    c = team_mcp
    c.foundry.fail["create_before"] = 403
    body = change(c, "ad" * 16, "add", "Alpha")
    forbidden = post(c, body)
    assert forbidden.status_code == 403 and code(forbidden) == "TEAM_MCP_FORBIDDEN"
    c.foundry.fail["create_before"] = 400
    refused = post(c, body)
    assert refused.status_code == 422 and code(refused) == "TEAM_MCP_REFUSED"
    assert c.foundry.created == [] and post(c, body).status_code == 200


def test_owner_only_read_and_write(team_mcp):
    c = team_mcp
    assert post(c, change(c, "ba" * 16, "add", "Alpha"), identity="read_only").status_code == 403
    agent = c.client.get(PREFIX + "/servers", headers=c.headers("agent"))
    assert agent.status_code == 403 and code(agent) == "TEAM_MCP_OWNER_ONLY"


def test_turn_grants_follow_the_default_version_and_keep_a_selection_across_unrelated_changes(monkeypatch, team_mcp):
    c = team_mcp
    monkeypatch.setenv(TEAM_MCP_ENABLED_ENV, "1")
    assert runtime_grants(c.runtime) == {}
    post(c, change(c, "bb" * 16, "add", "Alpha", approval="never"))
    alpha = runtime_grants(c.runtime)["team-Alpha"]
    assert alpha.kind == "mcp-toolbox" and (alpha.toolbox.toolbox, alpha.toolbox.version) == (TOOLBOX, "v1")
    post(c, change(c, "bc" * 16, "add", "Beta", tools=("browse",)))
    post(c, change(c, "bd" * 16, "modify", "Alpha", approval="always"))
    merged = runtime_grants(c.runtime)
    # Pinned to the current default version, but the selection (entry_sha256) survives other servers and approval.
    assert merged["team-Alpha"].entry_sha256 == alpha.entry_sha256 and merged["team-Alpha"].toolbox.version == "v3"
    post(c, change(c, "be" * 16, "modify", "Alpha", server_url="https://other.example.test/mcp"))
    assert runtime_grants(c.runtime)["team-Alpha"].entry_sha256 != alpha.entry_sha256
    post(c, change(c, "bf" * 16, "disable", "Alpha"))
    assert "team-Alpha" not in runtime_grants(c.runtime)

    static = CapabilityGrant(entry_id="team-Beta", entry_sha256="0" * 64, kind="fa-builtin", tools=("static-tool",),
                             vision=False)
    c.runtime.capability_grants = {"team-Beta": static}
    assert runtime_grants(c.runtime)["team-Beta"] == static


def test_turn_grants_are_cached_briefly_and_a_failed_read_keeps_the_last_servers(monkeypatch, team_mcp, caplog):
    c = team_mcp
    monkeypatch.setenv(TEAM_MCP_ENABLED_ENV, "1")
    post(c, change(c, "ca" * 16, "add", "Alpha"))
    first = runtime_grants(c.runtime)
    reads = c.foundry.reads
    assert runtime_grants(c.runtime) == first and c.foundry.reads == reads
    monkeypatch.setattr("shared_context.team_mcp.GRANTS_SECONDS", -1.0)
    c.service.forget(TENANT, A.team)
    runtime_grants(c.runtime)
    c.foundry.fail["read"] = None
    assert runtime_grants(c.runtime) == first and "TEAM_MCP_GRANTS_UNAVAILABLE" in caplog.text
    c.service.forget(TENANT, A.team)
    c.foundry.fail["read"] = 503
    assert "team-Alpha" not in runtime_grants(c.runtime)


def test_a_new_connection_key_is_sent_only_to_foundry_once_under_the_teams_own_name(team_mcp, caplog):
    caplog.set_level(logging.DEBUG)
    c = team_mcp
    secret = {"header_name": "x-apikey", "secret": SECRET}
    body = change(c, "cb" * 16, "add", "Docs", connection=None, approval="always", new_connection=secret)
    # The connection is written, then the version's outcome is unknown: the resend must not write the key again.
    c.foundry.fail["create_before"] = None
    unknown = post(c, body)
    assert code(unknown) == "TEAM_MCP_OUTCOME_UNKNOWN"
    applied = post(c, body)
    assert applied.status_code == 200, applied.text
    name = team_connection_name(PROJECT, A.team, "cb" * 16)
    assert applied.json()["connection"] == name and name.startswith(team_connection_prefix(PROJECT, A.team))
    assert len(name) <= 33 and name != team_connection_name(PROJECT, "team-two", "cb" * 16)
    assert c.foundry.puts == [{"name": name, "target": "https://mcp.example.test/sse", "keys": {"x-apikey": SECRET}}]
    assert c.foundry.arm[name] == {"fa_team": A.team, "fa_request_id": "cb" * 16, "fa_server_label": "Docs"}
    assert len(c.foundry.created) == 1 and c.foundry.created[0]["tools"][0]["project_connection_id"] == name
    # The key is in no response, metadata, read or log.
    texts = [unknown.text, applied.text, json.dumps(c.foundry.created), caplog.text,
             c.client.get(PREFIX + "/servers", headers=c.headers()).text,
             c.client.get(PREFIX + "/connections", headers=c.headers()).text]
    assert all(SECRET not in text for text in texts)
    for bad in ({**body, "request_id": "cc" * 16, "server": {**body["server"], "project_connection_id": "secret-key"}},
                {**body, "request_id": "cd" * 16, "new_connection": {"header_name": "x", "secret": "a\nb"}}):
        refused = post(c, bad)
        assert code(refused) == "TEAM_MCP_INVALID" and SECRET not in refused.text and "a\nb" not in refused.text
    assert len(c.foundry.puts) == 1


def test_a_new_key_never_overwrites_the_connection_a_live_version_uses(team_mcp):
    c = team_mcp
    first = {"header_name": "x-apikey", "secret": SECRET}
    assert post(c, change(c, "d1" * 16, "add", "Docs", connection=None, new_connection=first)).status_code == 200
    live = team_connection_name(PROJECT, A.team, "d1" * 16)
    before = dict(c.foundry.arm[live])
    # Modify with a new key, then Foundry refuses the version: the live server keeps its own connection and key.
    c.foundry.fail["create_before"] = 400
    second = {"header_name": "x-apikey", "secret": "OFFLINE-second-key-9a8b"}
    refused = post(c, change(c, "d2" * 16, "modify", "Docs", server_url="https://other.example.test/mcp",
                             connection=None, new_connection=second))
    assert refused.status_code == 422 and code(refused) == "TEAM_MCP_REFUSED"
    assert c.foundry.arm[live] == before and [put["name"] for put in c.foundry.puts].count(live) == 1
    assert view(c)["servers"][0]["project_connection_id"] == live
    assert view(c)["servers"][0]["server_url"] == "https://mcp.example.test/sse"
    # A name already owned by another change is never written over.
    clash = team_connection_name(PROJECT, A.team, "d3" * 16)
    c.foundry.arm[clash] = {"fa_request_id": "someone-else"}
    puts = len(c.foundry.puts)
    conflict = post(c, change(c, "d3" * 16, "add", "Wiki", connection=None, new_connection=second))
    assert conflict.status_code == 409 and code(conflict) == "TEAM_MCP_COMMAND_CONFLICT"
    assert len(c.foundry.puts) == puts


def test_another_owner_writes_only_its_own_teams_connection(team_mcp):
    c = team_mcp
    secret = {"header_name": "x-apikey", "secret": SECRET}
    response = post(c, change(c, "ce" * 16, "add", "Docs", connection=None, new_connection=secret, identity="second"),
                    identity="second")
    assert response.status_code == 200, response.text
    assert c.foundry.puts[0]["name"] == team_connection_name(PROJECT, "team-two", "ce" * 16)
    assert "fa-team-team-two" in c.foundry.toolboxes and TOOLBOX not in c.foundry.toolboxes
    mine = c.client.get(PREFIX + "/connections", headers=c.headers()).json()["connections"]
    assert not any(item["team_owned"] for item in mine)


def test_without_the_project_scope_no_connection_is_created(team_mcp):
    c = team_mcp
    c.service.project_scope = None
    assert view(c)["can_create_connection"] is False
    refused = post(c, change(c, "cf" * 16, "add", "Docs", connection=None,
                             new_connection={"header_name": "x-apikey", "secret": SECRET}))
    assert refused.status_code == 422 and code(refused) == "TEAM_MCP_CONNECTION_CREATE_UNAVAILABLE"
    assert c.foundry.puts == [] and c.foundry.created == []


FEED_URL = "https://feeds.example.test/rss/OFFLINEa1B2c3D4e5F6"


def test_a_feed_url_connection_carries_the_apis_signature_of_exactly_that_url(team_mcp, caplog):
    caplog.set_level(logging.DEBUG)
    from shared_context.feed_signature import generate_keypair, load_public_key, load_signing_key, verify_feed_url
    private, public = generate_keypair()
    c = team_mcp
    c.service.feed_signing_key = load_signing_key(private)
    feed = {"header_name": "X-Feed-URL", "secret": FEED_URL}
    applied = post(c, change(c, "fa" * 16, "add", "Stratechery", server_url="https://feed.example.test/mcp",
                             tools=("list_items", "read_item"), connection=None, new_connection=feed))
    assert applied.status_code == 200, applied.text
    [put] = c.foundry.puts
    assert set(put["keys"]) == {"x-feed-url", "x-fa-feed-signature"} and put["keys"]["x-feed-url"] == FEED_URL
    signature = put["keys"]["x-fa-feed-signature"]
    assert verify_feed_url(load_public_key(public), FEED_URL, signature)
    assert not verify_feed_url(load_public_key(public), FEED_URL + "x", signature)
    # The URL and its signature go to Foundry only: no response, version, metadata, read or log carries them.
    texts = [applied.text, json.dumps(c.foundry.created), json.dumps(c.foundry.arm), caplog.text,
             c.client.get(PREFIX + "/servers", headers=c.headers()).text]
    assert all(FEED_URL not in text and signature not in text for text in texts)


@pytest.mark.parametrize("bad", ["http://feeds.example.test/rss", "https://user:pw@feeds.example.test/rss",
                                 "https://feeds.example.test:8443/rss", "https://feeds.example.test/rss#x",
                                 "not a url", "https://"])
def test_a_feed_url_must_be_one_https_url_and_is_never_echoed(team_mcp, bad):
    c = team_mcp
    c.service.feed_signing_key = object()
    refused = post(c, change(c, "fb" * 16, "add", "Feed", connection=None,
                             new_connection={"header_name": "x-feed-url", "secret": bad}))
    assert refused.status_code == 422 and code(refused) == "TEAM_MCP_FEED_URL_INVALID"
    assert bad not in refused.text and c.foundry.puts == [] and c.foundry.created == []


def test_without_the_signing_key_no_feed_connection_is_created(team_mcp):
    c = team_mcp
    assert c.service.feed_signing_key is None
    refused = post(c, change(c, "fc" * 16, "add", "Feed", connection=None,
                             new_connection={"header_name": "x-feed-url", "secret": FEED_URL}))
    assert refused.status_code == 422 and code(refused) == "TEAM_MCP_FEED_UNAVAILABLE"
    assert FEED_URL not in refused.text and c.foundry.puts == [] and c.foundry.created == []


def test_the_signing_key_setting_is_optional_and_never_logged(caplog):
    from shared_context.feed_signature import SIGNING_KEY_ENV, generate_keypair
    from shared_context.team_mcp_api import feed_signing_key_setting
    private, _ = generate_keypair()
    assert feed_signing_key_setting({}) is None
    assert feed_signing_key_setting({SIGNING_KEY_ENV: private}) is not None
    assert feed_signing_key_setting({SIGNING_KEY_ENV: "OFFLINE-not-a-key"}) is None
    assert "OFFLINE-not-a-key" not in caplog.text and private not in caplog.text


def test_a_project_scope_for_another_project_is_ignored():
    from shared_context.team_mcp_api import project_scope_setting
    composition = SimpleNamespace(config=SimpleNamespace(targets=(SimpleNamespace(project_endpoint=ENDPOINT),)))
    assert project_scope_setting(composition, {PROJECT_SCOPE_ENV: PROJECT}) == PROJECT
    assert project_scope_setting(composition, {PROJECT_SCOPE_ENV: PROJECT.replace("/proj", "/other")}) is None
    assert project_scope_setting(composition, {}) is None


def test_arm_connection_client_puts_the_key_once_and_never_logs_it(caplog):
    caplog.set_level(logging.DEBUG)
    seen = []

    def arm(request):
        seen.append((request.method, str(request.url), request.content))
        if request.method == "GET":
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        return httpx.Response(500, json={"error": {"code": "InternalError"}})

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="OFFLINE-arm-token"))
    client = TeamMcpFoundryClient(ENDPOINT, credential, project_scope=PROJECT, transport=httpx.MockTransport(arm))
    assert client.connection("tabc-Docs") is None
    with pytest.raises(TeamMcpFoundryError) as failed:
        client.put_connection("tabc-Docs", target="https://mcp.example.test/sse", keys={"x-apikey": SECRET},
                              metadata={"fa_team": A.team})
    assert failed.value.status == 500 and SECRET not in repr(failed.value) and SECRET not in caplog.text
    method, url, content = seen[-1]
    assert (method, url) == ("PUT", "https://management.azure.com" + PROJECT
                             + "/connections/tabc-Docs?api-version=2025-06-01")
    assert json.loads(content)["properties"] == {
        "category": "RemoteTool", "authType": "CustomKeys", "target": "https://mcp.example.test/sse",
        "isSharedToAll": False, "credentials": {"keys": {"x-apikey": SECRET}}, "metadata": {"fa_team": A.team}}
    assert TeamMcpFoundryClient(ENDPOINT, credential).project_scope is None


def test_concurrent_sends_of_one_request_create_one_version(team_mcp):
    c = team_mcp
    owner = BehaviorPrincipal(TENANT, HUMAN_OID, A.team, "human", None, frozenset({"read", "submit", "approve"}))
    entered, release = threading.Event(), threading.Event()
    original = c.foundry.create_version

    def slow(name, **kwargs):
        entered.set()
        release.wait(5)
        return original(name, **kwargs)

    c.foundry.create_version = slow
    from shared_context.team_mcp_wire import TeamMcpChange
    command = TeamMcpChange.model_validate_json(json.dumps(change(c, "da" * 16, "add", "Alpha")))
    results = []
    threads = [threading.Thread(target=lambda: results.append(c.service.submit(owner, command))) for _ in range(2)]
    threads[0].start()
    assert entered.wait(5)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join(10)
    assert len(c.foundry.created) == 1 and {item.toolbox_version for item in results} == {"v1"}


def test_org_webiq_toolbox_sha_stays_byte_identical():
    from pathlib import Path
    path = Path("tool_catalog") / "webiq-toolbox.json"
    entry = parse_entry_file(path.name, (Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / path).read_bytes())
    assert entry.entry_sha256 == WEBIQ_TOOLBOX_SHA
    assert "version" not in entry.config.model_dump(mode="json")


def test_sdk_connections_read_the_auth_type_from_credentials():
    from azure.ai.projects.models import Connection
    from shared_context.team_mcp import _connection, _secret_connection

    def sdk(name, kind, auth):
        return Connection({"name": name, "id": "/c/" + name, "type": kind, "target": "https://x.test", "isDefault": False,
                           "credentials": {"type": auth}, "metadata": {}})

    key, aad = _connection(sdk("stage-webiq-key", "RemoteTool", "CustomKeys")), _connection(sdk("mi", "RemoteTool", "AAD"))
    assert (key.connection_type, key.credential_type) == ("RemoteTool", "CustomKeys") and _secret_connection(key)
    assert aad.credential_type == "AAD" and not _secret_connection(aad)
    assert not _secret_connection(_connection(sdk("open", "RemoteTool", "None")))


class _FakeToolboxes:
    def __init__(self):
        self.versions, self.default, self.calls = [], None, []

    def list_versions(self, name, *, limit=None, order=None):
        self.calls.append(("list_versions", name, limit, order))
        return iter(reversed(self.versions))

    def create_version(self, name, body):
        from azure.ai.projects.models import ToolboxVersionObject
        self.calls.append(("create_version", name, body))
        created = ToolboxVersionObject({"object": "toolbox.version", "id": "id", "name": name,
                                        "version": str(len(self.versions) + 1), "metadata": body["metadata"],
                                        "tools": body["tools"], "created_at": 1})
        self.versions.append(created)
        return created

    def get(self, name):
        from azure.ai.projects.models import ToolboxObject
        self.calls.append(("get", name))
        return ToolboxObject({"object": "toolbox", "id": "id", "name": name, "default_version": self.default})

    def get_version(self, name, version):
        self.calls.append(("get_version", name, version))
        return next(item for item in self.versions if item.version == version)

    def update(self, name, *, default_version):
        self.calls.append(("update", name, default_version))
        self.default = default_version


def test_sdk_wrapper_uses_official_versions_metadata_and_default_and_never_deletes(monkeypatch):
    import azure.ai.projects as projects

    toolboxes = _FakeToolboxes()

    class FakeProject:
        def __init__(self, **kwargs):
            self.toolboxes = toolboxes

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(projects, "AIProjectClient", FakeProject)
    client = TeamMcpFoundryClient("https://project.test", object())
    tool = {"type": "mcp", "server_label": "Docs", "server_url": "https://mcp.example.test/docs",
            "require_approval": "always", "allowed_tools": ["search"]}
    assert client.request_version("fa-team-t", "a" * 32) is None
    version = client.create_version("fa-team-t", tools=[tool], metadata={"fa_request_id": "a" * 32})
    assert client.request_version("fa-team-t", "a" * 32) == {"version": version, "metadata": {"fa_request_id": "a" * 32}}
    client.promote("fa-team-t", version)
    assert client.read_toolbox("fa-team-t") == (version, [tool]) and client.default_version("fa-team-t") == version
    assert ("list_versions", "fa-team-t", 100, "desc") in toolboxes.calls
    assert not any(call[0].startswith("delete") for call in toolboxes.calls)


def test_migration_and_rotation_classify_retired_team_mcp_rows():
    import inspect
    from shared_context import behavior_migration, behavior_runtime_rotation_history
    from shared_context.team_mcp import _head_key, _intent_key, validate_team_mcp_history

    server = {"server_label": "Alpha", "server_url": "https://mcp.example.test/sse", "project_connection_id": "safe-aad",
              "allowed_tools": ["search"], "version": "v1"}
    command = {"request_id": "e" * 32, "schema_version": "fa-team-mcp-change-v1", "expected_revision": 0,
               "expected_etag": None, "operation": "add", "server_label": "Alpha",
               "server": {key: value for key, value in server.items() if key != "version"}}
    head = {"kind": "team_mcp_head", "tenant_id": TENANT, "team_id": A.team, "toolbox": TOOLBOX, "revision": 1,
            "servers": [server], "pending": None}
    intent = {"kind": "team_mcp_intent", "tenant_id": TENANT, "team_id": A.team, "request_id": "e" * 32,
              "principal_oid": HUMAN_OID, "command": command, "base_revision": 0,
              "desired_servers": [command["server"]], "created_at": "2026-10-03T08:00:00Z", "state": "applied",
              "toolbox_version": "v1"}
    keys = (_head_key(TENANT, A.team), _intent_key(TENANT, A.team, "e" * 32))
    rows = {keys[0]: SimpleNamespace(payload={"data": head}), keys[1]: SimpleNamespace(payload={"data": intent})}
    config = SimpleNamespace(identity=SimpleNamespace(tenant_id=TENANT, team_id=A.team, human_oid=HUMAN_OID))
    assert set(validate_team_mcp_history(SimpleNamespace(rows=rows), config)) == set(keys)
    with pytest.raises(Exception) as invalid:
        validate_team_mcp_history(SimpleNamespace(rows={"wrong-key": rows[keys[0]]}), config)
    assert invalid.value.body["error"]["code"] == "TEAM_MCP_INVALID"
    assert "validate_team_mcp_history" in inspect.getsource(behavior_migration._quiescence)
    assert "validate_team_mcp_history" in inspect.getsource(behavior_runtime_rotation_history.validate_additions)


def test_a_failed_grants_read_is_reused_briefly_so_an_outage_waits_once(monkeypatch, team_mcp):
    c = team_mcp
    monkeypatch.setenv(TEAM_MCP_ENABLED_ENV, "1")
    clock = [1000.0]
    monkeypatch.setattr("shared_context.team_mcp.monotonic", lambda: clock[0])
    post(c, change(c, "cb" * 16, "add", "Alpha"))
    c.service.forget(TENANT, A.team)
    c.foundry.fail["read"] = 503  # one failed read; an uncached second capture would read again and succeed
    reads = c.foundry.reads
    assert "team-Alpha" not in runtime_grants(c.runtime)
    assert "team-Alpha" not in runtime_grants(c.runtime) and c.foundry.reads == reads + 1
    clock[0] += 11
    assert "team-Alpha" in runtime_grants(c.runtime) and c.foundry.reads == reads + 2


def test_a_grants_read_that_started_before_a_change_never_stores_its_stale_grants(monkeypatch, team_mcp):
    c = team_mcp
    monkeypatch.setenv(TEAM_MCP_ENABLED_ENV, "1")
    post(c, change(c, "cc" * 16, "add", "Alpha"))
    post(c, change(c, "ce" * 16, "add", "Beta"))  # the last server cannot be disabled
    original = c.foundry.read_toolbox

    def read_then_disable(name):
        read = original(name)  # this read still sees Alpha; the owner disables it before the read stores
        monkeypatch.setattr(c.foundry, "read_toolbox", original)
        assert post(c, change(c, "cd" * 16, "disable", "Alpha")).status_code == 200
        return read

    monkeypatch.setattr(c.foundry, "read_toolbox", read_then_disable)
    assert "team-Alpha" in runtime_grants(c.runtime)
    assert "team-Alpha" not in runtime_grants(c.runtime), "the disable applies to the next capture, not 30 s later"


def concurrent_captures_during_one_slow_failing_read(capture, foundry, method, monkeypatch, callers=3):
    """``callers`` captures arrive while the first one's Foundry read hangs and then fails: (Foundry reads, results)."""
    import time
    entered, release, reads, results = threading.Event(), threading.Event(), [], []

    def outage(*_args):
        reads.append(1)
        entered.set()
        release.wait(5)
        raise TimeoutError("synthetic Foundry read timeout")

    monkeypatch.setattr(foundry, method, outage)
    threads = [threading.Thread(target=lambda: results.append(capture())) for _ in range(callers)]
    threads[0].start()
    assert entered.wait(5)
    for thread in threads[1:]:
        thread.start()
    time.sleep(0.2)  # the later captures arrive while the first read still hangs
    release.set()
    for thread in threads:
        thread.join(5)
    return len(reads), results


def test_captures_during_a_failing_read_share_that_one_foundry_read(monkeypatch, team_mcp):
    c = team_mcp
    monkeypatch.setenv(TEAM_MCP_ENABLED_ENV, "1")
    post(c, change(c, "cf" * 16, "add", "Alpha"))
    c.service.forget(TENANT, A.team)
    reads, results = concurrent_captures_during_one_slow_failing_read(
        lambda: c.service.team_grants(TENANT, A.team), c.foundry, "read_toolbox", monkeypatch)
    assert reads == 1 and results == [()] * 3
