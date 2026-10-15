"""ADR-0006 option C relay: the agent token picks the team; offline fakes, not Azure proof."""
import hashlib
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import A, B, C, InMemoryRepository, TENANT
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.team_data_relay import PREFIX, TeamDataRelay, install_team_data_routes
from test_m3_auth import CLIENT_ID, HUMAN_OID, UNKNOWN_OID, jwk_client, signing_key, token
from test_behavior_server import workspace

VERSION = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def relay_app(auth, jwk_client, signing_key):
    records, objects = InMemoryRepository(), InMemoryArtifactStore(TENANT)
    app = FastAPI()
    _install_error_handlers(app)
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client)
    install_team_data_routes(app, TeamDataRelay(records, objects, TENANT), verifier)

    def headers(oid=None, **extra):
        claims = ({"scp": "Behavior.Read"} if oid is None else
                  {"oid": oid, "idtyp": "app", "roles": ["Behavior.Agent"]})
        value = token(signing_key, claims, remove=() if oid is None else ("scp",))
        return {"Authorization": "Bearer " + value, **extra}

    return TestClient(app), headers, records, objects


def _octets(data):
    return {"content-type": "application/octet-stream", "x-fa-content-sha256": hashlib.sha256(data).hexdigest()}


def _reasons(caplog):
    return [record.getMessage().split('"code":"')[1].split('"')[0] for record in caplog.records
            if "BEHAVIOR_REQUEST_FAILED" in record.getMessage()]


def test_agent_token_selects_the_team_and_other_teams_cannot_reach_its_data(relay_app):
    client, headers, records, objects = relay_app
    record = PREFIX + "/records/result/turn-1"
    created = client.put(record, json={"data": {"answer": "A team only"}}, headers=headers(A.oid, **{"If-None-Match": "*"}))
    assert created.status_code == 201
    data = b"team-one object"
    obj = PREFIX + "/objects/checkpoint/conv-1/versions/" + VERSION
    put = client.put(obj, content=data, headers=headers(A.oid, **_octets(data)))
    assert put.status_code == 201
    receipt = put.json()

    same_team = client.get(record, headers=headers(B.oid))
    assert same_team.status_code == 200 and same_team.json()["data"] == {"answer": "A team only"}
    assert same_team.json()["writer"] == A.actor
    assert client.post(obj + "/read", json={"receipt": receipt}, headers=headers(B.oid)).content == data

    # The same names from another team resolve only to that team's own namespace.
    assert client.get(record, headers=headers(C.oid)).status_code == 404
    assert client.get(obj + "/receipt", headers=headers(C.oid)).status_code == 404
    assert client.post(obj + "/read", json={"receipt": receipt}, headers=headers(C.oid)).status_code == 404
    assert client.put(record, json={"data": {"answer": "team two"}},
                      headers=headers(C.oid, **{"If-None-Match": "*"})).status_code == 201
    assert client.get(record, headers=headers(A.oid)).json()["data"] == {"answer": "A team only"}
    assert client.get(record, headers=headers(C.oid)).json()["data"] == {"answer": "team two"}
    assert {partition for partition, _ in records.data} == {
        f"team-data-v1-{TENANT}-team-one", f"team-data-v1-{TENANT}-team-two"}
    assert set(objects._versions) == {("team-one", "td-checkpoint-conv-1", VERSION)}


def test_non_agents_are_refused_before_any_storage_access(relay_app, caplog):
    client, headers, records, objects = relay_app
    record = PREFIX + "/records/result/turn-1"
    obj = PREFIX + "/objects/result/out/versions/" + VERSION
    caplog.set_level(logging.WARNING)
    for response in (
        client.get(record, headers=headers()),
        client.put(record, json={"data": {}}, headers=headers(**{"If-None-Match": "*"})),
        client.put(obj, content=b"x", headers=headers(**_octets(b"x"))),
        client.get(obj + "/receipt", headers=headers()),
        client.post(obj + "/read", json={"receipt": {}}, headers=headers()),
    ):
        assert response.status_code == 403
    assert _reasons(caplog) == ["TEAM_DATA_AGENT_ONLY"] * 5
    assert client.get(record).status_code == 401
    assert client.get(record, headers=headers(UNKNOWN_OID)).status_code == 403
    assert not records.data and not objects._versions


def test_writes_keep_create_only_etag_and_no_delete(relay_app, caplog):
    client, headers, _, _ = relay_app
    caplog.set_level(logging.WARNING)
    record = PREFIX + "/records/memory/notes"
    first = client.put(record, json={"data": {"v": 1}}, headers=headers(A.oid, **{"If-None-Match": "*"}))
    assert first.status_code == 201
    assert client.put(record, json={"data": {"v": 9}}, headers=headers(B.oid, **{"If-None-Match": "*"})).status_code == 409
    assert client.put(record, json={"data": {"v": 2}}, headers=headers(A.oid, **{"If-Match": 'W/"stale"'})).status_code == 412
    assert client.get(record, headers=headers(A.oid)).json()["data"] == {"v": 1}
    second = client.put(record, json={"data": {"v": 2}}, headers=headers(B.oid, **{"If-Match": first.json()["etag"]}))
    assert second.status_code == 200 and client.get(record, headers=headers(A.oid)).json()["data"] == {"v": 2}
    assert client.put(record, json={"data": {"v": 3}}, headers=headers(A.oid)).status_code == 422

    done = PREFIX + "/records/memory/kept"
    assert client.put(done, json={"data": {"v": 1}}, headers=headers(A.oid, **{"If-None-Match": "*"})).status_code == 201

    obj = PREFIX + "/objects/result/out/versions/" + VERSION
    receipt = client.put(obj, content=b"one", headers=headers(A.oid, **_octets(b"one"))).json()
    assert client.put(obj, content=b"one", headers=headers(A.oid, **_octets(b"one"))).status_code == 409
    assert client.get(obj + "/receipt", headers=headers(A.oid)).json() == receipt
    assert _reasons(caplog) == ["ALREADY_EXISTS", "ETAG_CONFLICT", "ARTIFACT_VERSION_EXISTS"]
    for path in (record, done, obj):
        assert client.delete(path, headers=headers(A.oid)).status_code == 405
    assert client.get(record, headers=headers(A.oid)).json()["data"] == {"v": 2}


def test_turn_complete_is_read_only_over_http_and_stays_immutable_in_process(relay_app, caplog):
    from shared_context.behavior_auth import BehaviorPrincipal
    client, headers, records, _ = relay_app
    caplog.set_level(logging.WARNING)
    done = PREFIX + "/records/turn-complete/rid-1"
    for precondition in ({"If-None-Match": "*"}, {"If-Match": 'W/"any"'}):
        refused = client.put(done, json={"data": {"state": "succeeded"}}, headers=headers(A.oid, **precondition))
        assert refused.status_code == 403
    assert _reasons(caplog) == ["TEAM_DATA_KIND_READ_ONLY"] * 2
    assert not records.data

    # The turn receipt API writes it in process; the HTTP read still sees it and it cannot be replaced.
    relay = TeamDataRelay(records, None, TENANT)
    agent = BehaviorPrincipal(TENANT, A.oid, A.team, "agent", A.actor, frozenset({"read", "submit"}))
    saved = relay.write_record(agent, "turn-complete", "rid-1", {"state": "succeeded"})
    assert client.get(done, headers=headers(B.oid)).json()["data"] == {"state": "succeeded"}
    with pytest.raises(Exception) as immutable:
        relay.write_record(agent, "turn-complete", "rid-1", {"state": "failed"}, etag=saved.etag)
    assert immutable.value.body["error"]["code"] == "TEAM_DATA_IMMUTABLE"


@pytest.mark.parametrize("path", [
    "/records/unknown/key", "/records/result/bad key", "/objects/unknown/x/versions/" + VERSION,
    "/objects/result/x/versions/not-hex",
])
def test_kinds_and_keys_are_fixed_by_the_server(relay_app, path):
    client, headers, records, objects = relay_app
    response = client.get(PREFIX + path + ("/receipt" if "/objects/" in path else ""), headers=headers(A.oid))
    assert response.status_code in {404, 422}
    assert not records.data and not objects._versions


def test_empty_etag_and_oversized_records_are_refused_before_storage(relay_app, caplog):
    from shared_context.storage import MAX_PAYLOAD_BYTES

    client, headers, records, _ = relay_app
    caplog.set_level(logging.WARNING)
    record = PREFIX + "/records/result/sized"
    first = client.put(record, json={"data": {"v": 1}}, headers=headers(A.oid, **{"If-None-Match": "*"}))
    for etag in ("", "   "):
        assert client.put(record, json={"data": {"v": 2}}, headers=headers(A.oid, **{"If-Match": etag})).status_code == 422
    kept = client.get(record, headers=headers(A.oid)).json()
    assert kept["data"] == {"v": 1} and kept["etag"] == first.json()["etag"]
    # The whole request is over the stored-record ceiling.
    too_big = {"data": {"x": "a" * MAX_PAYLOAD_BYTES}}
    assert client.put(PREFIX + "/records/result/big", json=too_big,
                      headers=headers(A.oid, **{"If-None-Match": "*"})).status_code == 413
    # The request fits, but the stored record (with its server fields) would not.
    body = {"data": {"x": ""}}
    body["data"]["x"] = "a" * (MAX_PAYLOAD_BYTES - len(json.dumps(body, separators=(",", ":"))) - 8)
    raw = json.dumps(body, separators=(",", ":")).encode()
    assert len(raw) <= MAX_PAYLOAD_BYTES
    assert client.put(PREFIX + "/records/result/edge", content=raw,
                      headers=headers(A.oid, **{"If-None-Match": "*", "Content-Type": "application/json"})).status_code == 413
    assert "TEAM_DATA_BODY_LIMIT" in _reasons(caplog)
    assert set(row for _, row in records.data) == {"result:sized"}


def test_agents_cannot_write_published_content(relay_app):
    client, headers, records, objects = relay_app
    assert client.put(PREFIX + "/records/published/sop-1", json={"data": {"sop": "x"}},
                      headers=headers(A.oid, **{"If-None-Match": "*"})).status_code == 404
    assert client.put(PREFIX + "/objects/published/skill-1/versions/" + VERSION, content=b"x",
                      headers=headers(A.oid, **_octets(b"x"))).status_code == 404
    assert not records.data and not objects._versions


def test_live_probe_expectations_hold_against_the_offline_relay(relay_app):
    from shared_context.team_data_probe import probe

    client, headers, records, _ = relay_app
    bearer = lambda oid=None: headers(oid)["Authorization"].removeprefix("Bearer ")
    assert probe(client, "deny", "k1", bearer()) == (
        {"record_read": 403, "record_create": 403, "object_create": 403, "object_receipt": 403}, True)
    assert probe(client, "write", "k1", bearer(A.oid)) == (
        {"record_create": 201, "published_create": 404, "record_delete": 405}, True)
    assert probe(client, "read-own", "k1", bearer(B.oid)) == ({"record_read": 200}, True)
    assert probe(client, "read-other", "k1", bearer(C.oid)) == ({"record_read": 404}, True)
    assert probe(client, "write", "k1", bearer(A.oid))[1] is False
    assert len(records.data) == 1


def test_production_composition_mounts_the_relay_on_the_existing_table(workspace, jwk_client, signing_key):
    from shared_context.behavior_server import application
    from test_behavior_assistant_server import AssistantHarness

    h = AssistantHarness(workspace, jwk_client)
    try:
        h.legacy()
        h.activate()
        config = h.config
        agent = {"Authorization": "Bearer " + token(signing_key, {
            "oid": config.bindings[0].agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",))}
        record = PREFIX + "/records/turn-complete/rid-1"
        app = application(h.composition)
        relay = app.state.team_data
        artifacts = h.composition.assistant.artifacts
        assert relay.records is artifacts.repository and relay.objects is artifacts.store
        assert relay.records.client.table_name == config.table.scope.table_name
        assert relay.tenant_id == str(config.identity.tenant_id)
        with TestClient(app) as client:
            assert client.put(record, json={"data": {"state": "ok"}},
                              headers={**h.headers(signing_key), "If-None-Match": "*"}).status_code == 403
            # The agent passes the existing verifier and reaches the original Table on its team partition.
            assert client.get(record, headers=agent).status_code == 404
        partition = f"team-data-v1-{config.identity.tenant_id}-{config.identity.team_id}"
        assert not [key for key in h.table.backing.rows if key[1] == partition]
    finally:
        h.composition.close()
