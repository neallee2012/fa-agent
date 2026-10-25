"""ADR-0011 phase 3: the API's Cosmos memory relay over signed local HTTP.

The Cosmos container is an offline fake shaped like ``azure-cosmos`` 4.17.1 (``execute_item_batch``,
``query_items``, ``patch_item`` and the ``status_code`` / ``error_index`` / ``operation_responses`` attributes of its
errors, with the phase 0c probe's statuses). ``azure-cosmos`` itself is not installed here; no Azure call runs.
"""

import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import A, B, C, TENANT
from shared_context import memory_relay
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.memory_relay import (
    PREFIX, CosmosMemoryContainer, install_memory_relay_routes, supersede_predicate,
)
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token

ENDPOINT = "https://fa-memory-test.documents.azure.com:443/"
NOW = 1_790_000_000
PRIVATE_A = "fap1-cfb96a2f81ea661708121aa8ea3f451f1894ca32ced6a3e0"
PRIVATE_B = "fap1-e7f598795c4261818e84677176b5465068403ab8020d1b48"
TEAM_ONE = "fat1-923c94b5bae6343ce7319d2f3fcbc5ce41b4c08375008ea0"
TEAM_TWO = "fat1-aab16dd49cd81fd8eb6be65e29484705650bbeb9ab58cee9"
P0, P1, E0 = ("fam1-305f02bbcd13e72315777bd16442d3cea0d7c18a", "fam1-e435a71f246d1d4d786e601f3f457c80b789831c",
              "fam1-6ccbf0c726d7e92f25f29fa66e2067672faad060")
P0_TWO = "fam1-691a5af128b6959e899b065b3d35fa4901f1edbe"
UPDATE_ONE, UPDATE_TWO = ("fau1-4bee38a7d87cf939edd6fab2d037e4307b8db44a",
                          "fau1-49036eb2a1088a4c2d0ad17c1c9198a1051b657e")
NOTE = "fam1-22a16d5464fe06e59f5783d54a3faed30f11e48c"
VECTOR = [0.5] * 1536


class CosmosError(Exception):
    """The attribute shape of azure.cosmos.exceptions.CosmosHttpResponseError / CosmosBatchOperationError."""

    def __init__(self, status_code, *, error_index=None, operation_responses=None):
        super().__init__(f"synthetic Cosmos {status_code}")
        self.status_code, self.error_index, self.operation_responses = status_code, error_index, operation_responses


class FakeContainer:
    def __init__(self):
        self.items, self.calls, self.rows, self.fail = {}, [], [], {}

    def read(self):
        self.calls.append(("read",))
        return {"id": "memories"}

    def execute_item_batch(self, batch_operations, partition_key):
        self.calls.append(("batch", partition_key, list(batch_operations)))
        if "batch" in self.fail:
            raise CosmosError(self.fail["batch"])
        documents = [operation[1][0] for operation in batch_operations]
        assert all(operation[0] == "create" for operation in batch_operations)
        taken = [index for index, document in enumerate(documents) if (partition_key, document["id"]) in self.items]
        if taken:
            # The probe: the failing create answers 409, every other operation 424, and nothing is written.
            statuses = [424] * len(documents)
            statuses[taken[0]] = 409
            raise CosmosError(409, error_index=taken[0], operation_responses=[{"statusCode": s} for s in statuses])
        for document in documents:
            self.items[(partition_key, document["id"])] = dict(document)
        return [{"statusCode": 201} for _ in documents]

    def query_items(self, query, parameters, partition_key):
        self.calls.append(("query", query, parameters, partition_key))
        if "query" in self.fail:
            raise CosmosError(self.fail["query"])
        return iter(self.rows)

    def patch_item(self, item, partition_key, patch_operations, *, filter_predicate):
        self.calls.append(("patch", item, partition_key, patch_operations, filter_predicate))
        document = self.items.get((partition_key, item))
        if document is None:
            raise CosmosError(404)
        matched = re.fullmatch(r"FROM c WHERE c\.kind = 'user_profile' AND NOT IS_DEFINED\(c\.superseded_by\)"
                               r" AND c\.updated_at <= ([0-9]+)", filter_predicate)
        # An unbound "@n" or any other text never matches: the probe's silent 412.
        if (matched is None or document["kind"] != "user_profile" or "superseded_by" in document
                or document["updated_at"] > int(matched.group(1))):
            raise CosmosError(412)
        for operation in patch_operations:
            assert operation["op"] == "add"
            document[operation["path"].lstrip("/")] = operation["value"]


@pytest.fixture
def relay(auth, jwk_client, signing_key, monkeypatch):
    monkeypatch.setattr(memory_relay, "_AGENT_NAMES", {})
    resolved = []
    # Configured targets: agent_id differs from the actor name, as in a real configuration.
    scopes = {name: SimpleNamespace(tenant_id=TENANT, team_id=actor.team, identity=actor,
                                    target=SimpleNamespace(agent_name="fa-agent-" + name))
              for name, actor in (("native-a", A), ("native-b", B), ("native-c", C))}

    def agent_scope(agent_id):
        resolved.append(agent_id)
        return scopes[agent_id]

    config = SimpleNamespace(targets=tuple(SimpleNamespace(agent_id=name, agent_oid=scope.identity.oid)
                                           for name, scope in scopes.items()), agent_scope=agent_scope)
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client)
    composition = SimpleNamespace(runtime=SimpleNamespace(store=None), config=config, verifier=verifier)
    fake = FakeContainer()
    environ = {"FA_MEMORY_BACKEND": "cosmos", "FA_COSMOS_ENDPOINT": ENDPOINT, "FA_TEAM_MEMORY_ENABLED": "1"}

    def build(env=environ):
        app = FastAPI()
        _install_error_handlers(app)
        install_memory_relay_routes(app, composition, environ=env, connect=lambda: CosmosMemoryContainer(fake),
                                    clock=lambda: NOW)
        return TestClient(app)

    def headers(actor=A):
        if actor is None:
            return {"Authorization": "Bearer " + token(signing_key, {"scp": "Behavior.Read"})}
        claims = {"oid": actor.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}
        return {"Authorization": "Bearer " + token(signing_key, claims, remove=("scp",))}

    return SimpleNamespace(client=build(), build=build, headers=headers, fake=fake, resolved=resolved,
                           environ=environ, scopes=scopes)


def search(r, actor=A, **body):
    return r.client.post(PREFIX + "/search", json={"scope": PRIVATE_A, "mode": "static", "top": 5, **body},
                         headers=r.headers(actor))


def item(slot="p0", kind="user_profile", content="The user lives in Kaohsiung.", updated_at=NOW, **extra):
    return {"slot": slot, "kind": kind, "content": content, "embedding": VECTOR, "updated_at": updated_at, **extra}


def write(r, items, scope=PRIVATE_A, rid="rid-one", actor=A):
    return r.client.post(PREFIX + "/items", json={"scope": scope, "source_request_id": rid, "items": items},
                         headers=r.headers(actor))


def test_the_routes_exist_only_on_a_configured_cosmos_backend_and_have_no_delete(relay, caplog):
    routes = {(method, route.path) for route in relay.client.app.routes if route.path.startswith(PREFIX)
              for method in route.methods}
    assert routes == {("POST", PREFIX + "/search"), ("POST", PREFIX + "/items")}
    assert relay.client.delete(PREFIX + "/items", headers=relay.headers()).status_code == 405
    for env in ({}, {"FA_MEMORY_BACKEND": "foundry", "FA_COSMOS_ENDPOINT": ENDPOINT},
                {"FA_MEMORY_BACKEND": "cosmos"},
                {"FA_MEMORY_BACKEND": "cosmos", "FA_COSMOS_ENDPOINT": "https://attacker.example.com/"},
                {"FA_MEMORY_BACKEND": "cosmos", "FA_COSMOS_ENDPOINT": ENDPOINT, "FA_MEMORY_EPISODE_DAYS": "0"}):
        off = relay.build(env)
        assert off.post(PREFIX + "/search", json={}, headers=relay.headers()).status_code == 404
    assert caplog.text.count("FA_MEMORY_RELAY_INACTIVE reason=config_invalid") == 3
    assert relay.fake.calls == []


def test_a_static_search_reads_newest_profiles_in_one_partition_without_vectors(relay):
    relay.fake.rows = [{"id": P0, "kind": "user_profile", "content": "Lives in Kaohsiung.", "updated_at": NOW},
                       {"id": P1, "kind": "user_profile", "content": "Prefers short replies.", "updated_at": NOW - 9}]
    found = search(relay)
    assert found.status_code == 200, found.text
    assert found.json() == {"memories": [
        {"memory_item": {"memory_id": P0, "kind": "user_profile", "content": "Lives in Kaohsiung.", "updated_at": NOW}},
        {"memory_item": {"memory_id": P1, "kind": "user_profile", "content": "Prefers short replies.",
                         "updated_at": NOW - 9}}]}
    assert relay.fake.calls == [("query", "SELECT TOP @n c.id, c.kind, c.content, c.updated_at FROM c WHERE "
                                 "c.kind = 'user_profile' AND NOT IS_DEFINED(c.superseded_by) ORDER BY c.updated_at DESC",
                                 [{"name": "@n", "value": 5}], PRIVATE_A)]
    assert found.headers["cache-control"] == "no-store"


def test_a_private_contextual_search_ages_out_old_episodes_and_a_team_search_does_not(relay):
    assert search(relay, mode="contextual", vector=VECTOR, top=10).status_code == 200
    assert search(relay, scope=TEAM_ONE, mode="contextual", vector=VECTOR, top=3).status_code == 200
    private, team = relay.fake.calls
    assert private == ("query", "SELECT TOP @n c.id, c.kind, c.content, c.updated_at FROM c WHERE NOT IS_DEFINED("
                       "c.superseded_by) AND (c.kind != 'chat_summary' OR c.updated_at >= @episode_since) ORDER BY "
                       "VectorDistance(c.embedding, @vector)",
                       [{"name": "@n", "value": 10}, {"name": "@vector", "value": VECTOR},
                        {"name": "@episode_since", "value": NOW - 90 * 86400}], PRIVATE_A)
    assert team == ("query", "SELECT TOP @n c.id, c.kind, c.content, c.updated_at FROM c WHERE NOT IS_DEFINED("
                    "c.superseded_by) ORDER BY VectorDistance(c.embedding, @vector)",
                    [{"name": "@n", "value": 3}, {"name": "@vector", "value": VECTOR}], TEAM_ONE)
    # The projection never returns the vector itself (15-20 KB of JSON each).
    assert all(call[1].split(" FROM c")[0] == "SELECT TOP @n c.id, c.kind, c.content, c.updated_at"
               for call in relay.fake.calls)


def test_the_episode_age_is_an_api_setting(relay):
    client = relay.build({**relay.environ, "FA_MEMORY_EPISODE_DAYS": "30"})
    assert client.post(PREFIX + "/search", json={"scope": PRIVATE_A, "mode": "contextual", "vector": VECTOR,
                                                 "top": 5}, headers=relay.headers()).status_code == 200
    assert relay.fake.calls[0][2][2] == {"name": "@episode_since", "value": NOW - 30 * 86400}


def test_only_the_callers_own_scope_and_its_teams_are_served(relay, caplog):
    for actor, scope in ((A, PRIVATE_B), (A, TEAM_TWO), (C, TEAM_ONE), (C, PRIVATE_A), (B, PRIVATE_A)):
        denied = search(relay, actor, scope=scope)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "MEMORY_SCOPE_DENIED"
    assert caplog.text.count("FA_MEMORY_RELAY_DENIED agent_id=agent-a reason=scope_mismatch") == 2
    assert PRIVATE_B not in caplog.text and TEAM_TWO not in caplog.text
    human = search(relay, None)
    assert human.status_code == 403 and human.json()["error"]["code"] == "MEMORY_AGENT_ONLY"
    assert relay.fake.calls == []
    assert search(relay, B, scope=PRIVATE_B).status_code == 200
    assert search(relay, B, scope=TEAM_ONE).status_code == 200
    # Without the Team switch only the private scope remains.
    off = relay.build({k: v for k, v in relay.environ.items() if k != "FA_TEAM_MEMORY_ENABLED"})
    refused = off.post(PREFIX + "/search", json={"scope": TEAM_ONE, "mode": "static", "top": 3},
                       headers=relay.headers())
    assert refused.status_code == 403


def test_the_agent_name_is_resolved_once_per_identity(relay):
    for _ in range(3):
        assert search(relay).status_code == 200
    assert search(relay, B, scope=PRIVATE_B).status_code == 200
    assert relay.resolved == ["native-a", "native-b"]


@pytest.mark.parametrize("field, value", [("team_id", C.team), ("identity", B)])
def test_a_registry_answer_for_another_team_or_identity_is_refused(relay, caplog, field, value):
    relay.scopes["native-a"] = SimpleNamespace(**{**vars(relay.scopes["native-a"]), field: value})
    denied = search(relay)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "MEMORY_SCOPE_DENIED"
    assert "FA_MEMORY_RELAY_DENIED agent_id=agent-a reason=agent_mismatch" in caplog.text
    assert relay.fake.calls == [] and memory_relay._AGENT_NAMES == {}


@pytest.mark.parametrize("body", [
    {"mode": "contextual", "top": 5},
    {"mode": "static", "vector": VECTOR, "top": 5},
    {"mode": "contextual", "vector": VECTOR[:-1], "top": 5},
    {"mode": "contextual", "vector": [True] + VECTOR[1:], "top": 5},
    {"mode": "contextual", "vector": ["0.5"] + VECTOR[1:], "top": 5},
    {"mode": "static", "top": 11},
    {"mode": "static", "top": 0},
    {"mode": "static", "top": 5, "scope": "fap1-short"},
    {"mode": "static", "top": 5, "id": P0},
])
def test_search_bodies_are_validated_before_anything_is_read(relay, body):
    assert search(relay, **body).status_code == 422
    assert relay.fake.calls == []


def test_a_non_finite_or_integer_vector_value(relay):
    raw = ('{"scope":"%s","mode":"contextual","top":5,"vector":[1e999%s]}' % (PRIVATE_A, ",0.5" * 1535))
    assert relay.client.post(PREFIX + "/search", content=raw, headers={
        **relay.headers(), "content-type": "application/json"}).status_code == 422
    assert search(relay, mode="contextual", vector=[0] * 1536).status_code == 200


def test_a_private_write_is_one_insert_only_batch_with_api_computed_ids(relay):
    written = write(relay, [item(), item("p1", content="Prefers short replies."),
                            item("e0", "chat_summary", "Talked about moving.")])
    assert written.status_code == 200, written.text
    assert written.json() == {"update_id": UPDATE_ONE, "items": [
        {"slot": "p0", "id": P0, "status": 201}, {"slot": "p1", "id": P1, "status": 201},
        {"slot": "e0", "id": E0, "status": 201}], "corrections": []}
    (call,) = relay.fake.calls
    assert call[:2] == ("batch", PRIVATE_A)
    assert [operation[0] for operation in call[2]] == ["create", "create", "create"]
    assert call[2][0][1][0] == {
        "id": P0, "scope": PRIVATE_A, "kind": "user_profile", "content": "The user lives in Kaohsiung.",
        "updated_at": NOW, "embedding": VECTOR, "source_request_id": "rid-one", "update_id": UPDATE_ONE,
        "schema": "fa-memory-item-v1"}


def test_the_same_rid_is_never_written_twice_and_sends_no_correction(relay):
    assert write(relay, [item()]).status_code == 200
    before = dict(relay.fake.items)
    again = write(relay, [item("p1"), item(supersedes=P0_TWO)])
    assert again.status_code == 200
    assert again.json() == {"update_id": UPDATE_ONE, "items": [
        {"slot": "p1", "id": P1, "status": 409}, {"slot": "p0", "id": P0, "status": 409}],
        "corrections": [{"slot": "p0", "supersedes": P0_TWO, "result": "skipped"}]}
    # The probe's rollback: [424, 409] and the other item reads back as missing.
    assert relay.fake.items == before and (PRIVATE_A, P1) not in relay.fake.items
    assert [call[0] for call in relay.fake.calls] == ["batch", "batch"]


def test_a_correction_marks_the_older_profile_once_after_the_batch_commits(relay, caplog):
    assert write(relay, [item(updated_at=NOW - 60)], rid="rid-two").status_code == 200
    corrected = write(relay, [item(content="The user moved to Taipei.", supersedes=P0_TWO)])
    assert corrected.json()["corrections"] == [{"slot": "p0", "supersedes": P0_TWO, "result": "superseded"}]
    patch = relay.fake.calls[-1]
    assert patch == ("patch", P0_TWO, PRIVATE_A, [
        {"op": "add", "path": "/superseded_by", "value": P0}, {"op": "add", "path": "/superseded_at", "value": NOW}],
        "FROM c WHERE c.kind = 'user_profile' AND NOT IS_DEFINED(c.superseded_by) AND c.updated_at <= 1790000000")
    assert "@" not in patch[4]
    assert relay.fake.items[(PRIVATE_A, P0_TWO)]["superseded_by"] == P0
    assert relay.fake.items[(PRIVATE_A, P0)]["supersedes"] == P0_TWO
    assert [call[0] for call in relay.fake.calls] == ["batch", "batch", "patch"]


@pytest.mark.parametrize("seed, reason", [
    ({"kind": "user_profile", "updated_at": NOW + 60}, "precondition_failed"),   # the old item is newer
    ({"kind": "user_profile", "updated_at": NOW - 60, "superseded_by": E0}, "precondition_failed"),
    ({"kind": "chat_summary", "updated_at": NOW - 60}, "precondition_failed"),   # an episode is never superseded
    (None, "not_found"),
])
def test_a_correction_that_does_not_apply_is_skipped_once_and_the_new_item_stays(relay, caplog, seed, reason):
    if seed is not None:
        relay.fake.items[(PRIVATE_A, P0_TWO)] = {"id": P0_TWO, **seed}
    original = dict(relay.fake.items.get((PRIVATE_A, P0_TWO), {}))
    corrected = write(relay, [item(supersedes=P0_TWO)])
    assert corrected.status_code == 200
    assert corrected.json()["corrections"] == [{"slot": "p0", "supersedes": P0_TWO, "result": "skipped"}]
    assert corrected.json()["items"] == [{"slot": "p0", "id": P0, "status": 201}]
    assert relay.fake.items.get((PRIVATE_A, P0_TWO), {}) == original
    assert [call[0] for call in relay.fake.calls] == ["batch", "patch"]
    assert f"FA_MEMORY_RELAY_SUPERSEDE status=skipped reason={reason} slot=p0" in caplog.text


def test_the_correction_predicate_inlines_only_a_validated_integer():
    assert supersede_predicate(0) == (
        "FROM c WHERE c.kind = 'user_profile' AND NOT IS_DEFINED(c.superseded_by) AND c.updated_at <= 0")
    assert supersede_predicate(2**53 - 1).endswith("c.updated_at <= 9007199254740991")
    for value in (-1, 1.5, True, "5", 2**53, None):
        with pytest.raises(ValueError):
            supersede_predicate(value)


def test_a_team_note_is_one_create_only_item_and_never_supersedes(relay):
    shared = write(relay, [item("note", "chat_summary", "[native-a, 2026-10-24 09:00 UTC, src rid-note] Ship Friday.")],
                   scope=TEAM_ONE, rid="rid-note")
    assert shared.json() == {"update_id": None, "items": [{"slot": "note", "id": NOTE, "status": 201}],
                             "corrections": []}
    assert "update_id" not in relay.fake.items[(TEAM_ONE, NOTE)]
    calls = len(relay.fake.calls)
    for scope, items in ((TEAM_ONE, [item("note", supersedes=P0)]), (TEAM_ONE, [item("p0")]),
                         (PRIVATE_A, [item("note")]), (PRIVATE_A, [item("e0", "chat_summary", supersedes=P0_TWO)]),
                         (PRIVATE_A, [item("p0", "chat_summary")]), (PRIVATE_A, [item("e0")]),
                         (PRIVATE_A, [item(), item()]), (PRIVATE_A, []),
                         (PRIVATE_A, [item(content="x" * 601)]),
                         (PRIVATE_A, [item(), item("p1"), item("p2"), item("e0", "chat_summary"), item("p0")]),
                         (PRIVATE_A, [item(updated_at=-1)]), (PRIVATE_A, [item(updated_at=True)]),
                         (PRIVATE_A, [item(id=P0)])):
        assert write(relay, items, scope=scope).status_code == 422, (scope, items)
    assert write(relay, [item("p1", supersedes=P1)]).status_code == 422  # its own new ID
    assert len(relay.fake.calls) == calls


def test_a_full_size_write_is_accepted(relay):
    vector = [-0.012345678901234567] * 1536
    items = [dict(item(slot), embedding=vector, content="字" * 600) for slot in ("p0", "p1", "p2")]
    items.append(dict(item("e0", "chat_summary"), embedding=vector, content="字" * 600))
    assert write(relay, items).status_code == 200
    assert len(relay.fake.calls[0][2]) == 4


def test_a_throttled_or_failed_backend_is_503_and_nothing_is_retried(relay, caplog):
    relay.fake.fail = {"batch": 429, "query": 500}
    refused = write(relay, [item()])
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "MEMORY_BACKEND_UNAVAILABLE"
    assert "FA_MEMORY_RELAY_THROTTLED stage=batch" in caplog.text
    assert search(relay).status_code == 503
    assert [call[0] for call in relay.fake.calls] == ["batch", "query"]


def test_the_wrapper_has_no_replace_upsert_or_delete():
    names = {name for name in dir(CosmosMemoryContainer) if not name.startswith("_")}
    assert names == {"create_batch", "query", "patch", "read_metadata"}


def fake_azure_cosmos(monkeypatch, build=None):
    """``azure.cosmos`` as the relay imports it: 4.17.1's ``ConnectionPolicy`` defaults and a recording client."""
    built = []

    class RetryOptions:
        def __init__(self, max_retry_attempt_count=9, fixed_retry_interval_in_milliseconds=None,
                     max_wait_time_in_seconds=30):
            self.MaxRetryAttemptCount, self.MaxWaitTimeInSeconds = max_retry_attempt_count, max_wait_time_in_seconds

    class ConnectionPolicy:
        def __init__(self):
            self.EnableEndpointDiscovery, self.RetryOptions = True, RetryOptions()

    class CosmosClient:
        def __init__(self, url, **kwargs):
            built.append((url, kwargs))
            if build is not None:
                build()

        def get_database_client(self, name):
            return SimpleNamespace(get_container_client=lambda container: FakeContainer())

    monkeypatch.setitem(sys.modules, "azure.cosmos", SimpleNamespace(CosmosClient=CosmosClient))
    monkeypatch.setitem(sys.modules, "azure.cosmos.documents", SimpleNamespace(ConnectionPolicy=ConnectionPolicy))
    return built


def test_the_client_turns_off_every_sdk_resend_it_can(monkeypatch):
    built = fake_azure_cosmos(monkeypatch)
    credential = object()
    shared = memory_relay.SharedCosmos(ENDPOINT, credential)
    assert shared.container() is shared.container()
    ((url, kwargs),) = built
    policy = kwargs.pop("connection_policy")
    assert url == ENDPOINT and kwargs == {"credential": credential, "retry_total": 0, "connection_timeout": 5,
                                          "read_timeout": 15}
    assert policy.EnableEndpointDiscovery is False
    assert (policy.RetryOptions.MaxRetryAttemptCount, policy.RetryOptions.MaxWaitTimeInSeconds) == (0, 0)


def test_the_real_sdk_builds_a_policy_with_every_resend_off(monkeypatch):
    """The relay's own kwargs through azure-cosmos's ``_build_connection_policy``; only the client is replaced."""
    cosmos = pytest.importorskip("azure.cosmos")
    from azure.cosmos.cosmos_client import _build_connection_policy
    built = []

    class CosmosClient:
        def __init__(self, url, credential, **kwargs):
            built.append(kwargs)

        def get_database_client(self, name):
            return SimpleNamespace(get_container_client=lambda container: FakeContainer())

    monkeypatch.setattr(cosmos, "CosmosClient", CosmosClient)
    memory_relay.SharedCosmos(ENDPOINT, object()).container()
    policy = _build_connection_policy(built.pop())
    # retry_total=0 is falsy in the SDK's or-chain, so the throttle count is the relay's preset 0, not the default 9.
    assert policy.RetryOptions.MaxRetryAttemptCount == 0
    assert policy.ConnectionRetryConfiguration.total_retries == 0
    assert policy.EnableEndpointDiscovery is False
    assert not policy.RetryNonIdempotentWrites


def test_a_failed_client_build_is_a_logged_503_and_is_not_retried_for_30_seconds(monkeypatch, caplog):
    failures = [ConnectionError("account read failed")]

    def build():
        if failures:
            raise failures.pop()

    built = fake_azure_cosmos(monkeypatch, build=build)
    now = [100.0]
    shared = memory_relay.SharedCosmos(ENDPOINT, object(), clock=lambda: now[0])
    for seconds in (0, 29.9):
        now[0] = 100.0 + seconds
        with pytest.raises(memory_relay.Problem) as refused:
            shared.container()
        assert refused.value.status == 503 and refused.value.body["error"]["code"] == "MEMORY_BACKEND_UNAVAILABLE"
    assert len(built) == 1
    assert "FA_MEMORY_RELAY_FAILED stage=connect status=None exception_type=ConnectionError" in caplog.text
    assert "FA_MEMORY_RELAY_FAILED stage=connect reason=recent_failure" in caplog.text
    now[0] = 130.0
    assert shared.container() is shared.container() and len(built) == 2


def test_a_request_never_waits_behind_a_slow_build_and_start_up_waits_one_connect_timeout(monkeypatch, caplog):
    release = threading.Event()
    built = fake_azure_cosmos(monkeypatch, build=release.wait)
    shared = memory_relay.SharedCosmos(ENDPOINT, object())
    started = time.monotonic()
    shared.warm(wait=0.2)
    assert time.monotonic() - started < 2 and "FA_MEMORY_RELAY_WARMUP status=pending" in caplog.text
    with pytest.raises(memory_relay.Problem) as refused:
        shared.container()
    assert refused.value.status == 503
    assert "FA_MEMORY_RELAY_FAILED stage=connect reason=build_in_progress" in caplog.text
    release.set()
    for _ in range(100):
        if "FA_MEMORY_RELAY_WARMUP status=ready" in caplog.text:
            break
        time.sleep(0.05)
    assert shared.container() is not None and len(built) == 1


def test_shutdown_never_waits_behind_a_build_and_closes_a_built_client(monkeypatch, caplog):
    fake_azure_cosmos(monkeypatch)
    shared = memory_relay.SharedCosmos(ENDPOINT, object())
    container = shared.container()
    closed = []
    shared._client = SimpleNamespace(close=lambda: closed.append(True))
    shared._lock.acquire()
    try:
        started = time.monotonic()
        shared.close()
        assert time.monotonic() - started < 1 and shared._container is container and not closed
    finally:
        shared._lock.release()
    assert "FA_MEMORY_RELAY_CLOSE status=skipped reason=build_in_progress" in caplog.text
    shared.close()
    assert closed == [True] and shared._container is None and shared._client is None


def test_the_relay_imports_without_the_hosted_sdk_or_azure_cosmos():
    code = """
import importlib.abc
import sys
class Reject(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('copilot', 'azure.cosmos') or fullname.startswith(('copilot.', 'azure.ai.agentserver',
                                                                           'azure.cosmos.')):
            raise AssertionError('import: ' + fullname)
sys.meta_path.insert(0, Reject())
import shared_context.memory_relay
print('independent')
"""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc")}
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "independent"
