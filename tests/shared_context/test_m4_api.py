from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from azure.ai.projects import AIProjectClient
from fastapi.testclient import TestClient

from conftest import A, B, C, TENANT, FakeMemory
from shared_context.api import create_app
from shared_context.errors import Problem
from shared_context.m2_gateway import M2Gateway
from shared_context.m3_models import ApprovalInput, CreateArtifact, VersionInput
from shared_context.m4_api import MAX_M4_BODY_BYTES
from shared_context.m4_config import ENV_NAMES, MAX_REQUESTS, MEMORY_STORE, MODEL, POLICY_SHA256, M4Settings
from shared_context.m4_context import FIXTURE_ACTORS, M4ContextService, M4FoundryMemory, SeedFixture
from shared_context.m4_fixtures import (
    ALIASES, CASES, CONTEXT_BYTES_LIMIT, FIXTURE_SHA256, GOLD, POLICY_ARTIFACT,
    POLICY_TEXT, POLICY_VERSION, PROMPT, PROMPT_TOKENS_LIMIT, SUITE, encoded, fixtures,
)
from shared_context.models import CreateWork
from shared_context.storage import Record
from test_adapters import WireTransport
from test_m2_gateway import Credential, NATIVE, PROJECT
from test_m3_artifacts import HUMAN, m3

SETTINGS = M4Settings(SUITE, MODEL, MAX_REQUESTS, MEMORY_STORE, FIXTURE_SHA256)
BASE = "/m4/teams/team-one"
WORK = SUITE + "-short-cold"
SEED = {"fixture_id": "short_team", "suite_sha256": FIXTURE_SHA256}
M4_ENV = dict(zip(ENV_NAMES, (SUITE, MODEL, str(MAX_REQUESTS), MEMORY_STORE, FIXTURE_SHA256)))


@pytest.fixture
def m4(m3):
    artifacts, repository, _ = m3
    created = artifacts.create(A, A.team, POLICY_ARTIFACT, CreateArtifact(
        kind="sop", title="Existing approved SOP", source_work="source-work",
    ))
    uploaded = artifacts.upload(A, A.team, POLICY_ARTIFACT, POLICY_VERSION, created.etag, VersionInput(
        filename="sop.txt", media_type="text/plain", sha256=POLICY_SHA256,
    ), POLICY_TEXT.encode("utf-8"))
    artifacts.decide(HUMAN, A.team, POLICY_ARTIFACT, uploaded.etag, ApprovalInput(
        version_id=POLICY_VERSION, sha256=POLICY_SHA256, decision="approve", decision_id="a" * 32,
    ))
    memory = FakeMemory()
    service = M4ContextService(artifacts.context, memory, SETTINGS, artifacts)
    artifacts.context.create(A, A.team, WORK, CreateWork(
        goal="Project Lyra context", progress=fixtures()["seed_data"]["current_work_progress"],
    ))
    with ExitStack() as resources:
        clients = {}

        def verifier(selected):
            def verified_identity():
                return selected
            return verified_identity

        for actor in (A, B, C):
            gateway = M2Gateway(artifacts.context, SETTINGS.model_settings, PROJECT, Credential(),
                                httpx.AsyncClient(transport=httpx.MockTransport(
                                    lambda _request: httpx.Response(200, json=NATIVE),
                                )), milestone="m4")
            app = create_app(
                artifacts.context, verifier(actor), artifact_service=artifacts,
                verified_artifact_identity_dependency=lambda: A, m4_service=service, m4_model_gateway=gateway,
            )
            clients[actor.actor] = resources.enter_context(TestClient(app))
        yield service, repository, memory, clients, artifacts


def test_frozen_fixture_exports_match_original_and_oracle_is_separate():
    path = Path(__file__).resolve().parents[2] / "controller" / "m4_context_eval.py"
    spec = importlib.util.spec_from_file_location("original_m4_fixture", path)
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    bundle = fixtures()
    assert encoded(bundle) == original.encoded(original.fixtures())
    assert hashlib.sha256(encoded(bundle)).hexdigest() == FIXTURE_SHA256
    assert len(encoded(bundle["seed_data"]["short_team"])) == 272
    assert len(bundle["seed_data"]["long_team"]) == 128
    assert len(encoded(bundle["seed_data"]["long_team"])) == 52147
    for name in ("SUITE", "POLICY_VERSION", "POLICY_ARTIFACT", "POLICY_TEXT", "CASES", "GOLD",
                 "ALIASES", "PROMPT", "CONTEXT_BYTES_LIMIT", "PROMPT_TOKENS_LIMIT"):
        assert globals()[name] == getattr(original, name)
    bundle["seed_data"]["short_team"].append("local mutation")
    assert len(fixtures()["seed_data"]["short_team"]) == 3


@pytest.mark.parametrize("changes", [
    {"fixture_id": "arbitrary"}, {"fixture_id": "current_work_progress"}, {"fixture_id": 1},
    {"suite_sha256": "0" * 64}, {"suite_sha256": FIXTURE_SHA256.upper()},
    {"suite_sha256": None}, {"items": []}, {"text": "arbitrary upload"}, {"actor": "agent-a"},
    {"target": "agent-a"}, {"marker": "x", "value": "amber"},
])
def test_seed_strict_schema_rejects_arbitrary_upload_and_claimed_identity(m4, changes):
    _, repository, memory, clients, _ = m4
    before = copy.deepcopy(repository.data)
    assert clients[A.actor].post(BASE + "/memory/seed", json={**SEED, **changes}).status_code == 422
    assert repository.data == before and memory.calls == []


@pytest.mark.parametrize("body", [
    {}, {"fixture_id": "short_team"}, {"suite_sha256": FIXTURE_SHA256}, None, ["short_team"],
])
def test_seed_requires_exact_two_fields(m4, body):
    assert m4[3][A.actor].post(BASE + "/memory/seed", json=body).status_code in (415, 422)
    assert m4[2].calls == []


@pytest.mark.parametrize("actor,fixture,status", [
    (A, "short_team", 202), (A, "long_team", 202), (A, "private_a", 202),
    (B, "private_b", 202), (A, "private_b", 403), (B, "private_a", 403),
    (B, "short_team", 403), (B, "long_team", 403), (C, "private_b", 403),
])
def test_fixed_fixture_scopes_use_only_verified_allowlisted_identity(m4, actor, fixture, status):
    service, repository, memory, clients, _ = m4
    response = clients[actor.actor].post(BASE + "/memory/seed", json={**SEED, "fixture_id": fixture})
    assert response.status_code == status
    if status != 202:
        assert memory.calls == []
        return
    expected_scope = f"private:{A.team}:{actor.actor}" if fixture.startswith("private_") else f"team:{A.team}"
    result = response.json()
    assert set(result) == {"operation_id", "scope", "native", "receipt_etag"}
    assert result["scope"] == expected_scope
    assert response.headers["x-should-retry"] == "false"
    call = memory.calls[0]
    assert call[:2] == ("start", expected_scope)
    assert [item["content"][0]["text"] for item in call[2]] == fixtures()["seed_data"][fixture]
    assert all(set(item) == {"role", "type", "content"} for item in call[2])
    assert "oracle" not in json.dumps(call) and "Mira" not in json.dumps(call) and PROMPT not in json.dumps(call)
    row = repository.get(*service._memory_operation_key(A.team, result["operation_id"]))
    assert row.payload["submission_attempts"] == 1
    assert row.payload["memory_store"] == MEMORY_STORE and row.payload["suite_sha256"] == FIXTURE_SHA256
    assert row.etag == result["receipt_etag"]
    assert service.partition == "m4-" + TENANT
    assert (A.team, "work:" + WORK) in repository.data
    assert not any(partition.startswith("m4") and key.startswith("work:") for partition, key in repository.data)


def test_four_create_only_seed_receipts_prevent_replay_across_new_instances(m4):
    service, repository, memory, clients, artifacts = m4
    original = copy.deepcopy(repository.data)
    ids = []
    for fixture, actor_name in FIXTURE_ACTORS.items():
        response = clients[actor_name].post(BASE + "/memory/seed", json={**SEED, "fixture_id": fixture})
        assert response.status_code == 202
        ids.append(response.json()["operation_id"])
    assert len(set(ids)) == 4 and len(memory.calls) == 4
    reopened = M4ContextService(artifacts.context, memory, SETTINGS, artifacts)
    for fixture, actor_name in FIXTURE_ACTORS.items():
        with pytest.raises(Problem) as error:
            reopened.seed(A if actor_name == A.actor else B, A.team, SeedFixture(**{**SEED, "fixture_id": fixture}))
        assert error.value.status == 409
    assert len(memory.calls) == 4
    for key, value in original.items():
        assert repository.data[key] == value
    assert len(repository.data) == len(original) + 4


def test_concurrent_identical_seed_has_one_submission(m4):
    service, _, memory, _, _ = m4

    def seed():
        try:
            return service.seed(A, A.team, SeedFixture(**SEED))
        except Problem as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: seed(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1 and 409 in results
    assert len(memory.calls) == 1


@pytest.mark.parametrize("uncertain", ["upstream", "create_ack", "accepted_ack", "accepted_cas"])
def test_unknown_seed_is_persistently_blocked_without_resubmission(m4, uncertain):
    service, repository, memory, clients, artifacts = m4
    create, replace = repository.create, repository.replace

    def uncertain_create(*args):
        saved = create(*args)
        return Record(saved.payload, "*")

    def uncertain_replace(*args):
        if uncertain == "accepted_cas":
            raise Problem(412, "ETAG_CONFLICT", "synthetic failed acknowledgement")
        replace(*args)
        raise Problem(502, "UPSTREAM_ERROR", "synthetic lost acknowledgement")

    if uncertain == "upstream":
        memory.failure = Problem(502, "UPSTREAM_ERROR", "synthetic Memory failure", upstream_status=503)
    with patch.object(repository, "create", uncertain_create if uncertain == "create_ack" else create), patch.object(
        repository, "replace", uncertain_replace if uncertain in ("accepted_ack", "accepted_cas") else replace,
    ):
        response = clients[A.actor].post(BASE + "/memory/seed", json=SEED)
    assert response.status_code in (412, 502)
    attempts = len(memory.calls)
    assert attempts == (0 if uncertain == "create_ack" else 1)
    memory.failure = None
    reopened = M4ContextService(artifacts.context, memory, SETTINGS, artifacts)
    with pytest.raises(Problem) as exc:
        reopened.seed(A, A.team, SeedFixture(**SEED))
    assert exc.value.status == 409 and len(memory.calls) == attempts
    operation = next(key for key, fixture in service.operations.items() if fixture == "short_team")
    if uncertain != "accepted_ack":
        with pytest.raises(Problem) as exc:
            reopened.poll_memory(A, A.team, operation)
        assert exc.value.body["error"]["code"] == "MEMORY_START_UNKNOWN"


def test_m4_poll_is_store_and_private_scope_isolated_and_rejects_old_operations(m4):
    service, repository, memory, clients, artifacts = m4
    legacy = clients[A.actor].post("/teams/team-one/memory/team", json={"marker": "legacy", "value": "amber"})
    assert legacy.status_code == 202
    before = len(memory.calls)
    assert clients[A.actor].get(BASE + "/memory/operations/" + legacy.json()["operation_id"]).status_code == 404
    old_id = "f" * 32
    repository.create("m3-" + TENANT, "operation:" + old_id, {"native_update_id": "other-store"})
    assert clients[A.actor].get(BASE + "/memory/operations/" + old_id).status_code == 404
    assert len(memory.calls) == before
    seeded = clients[B.actor].post(BASE + "/memory/seed", json={**SEED, "fixture_id": "private_b"}).json()
    path = BASE + "/memory/operations/" + seeded["operation_id"]
    assert clients[A.actor].get(path).status_code == 403
    assert clients[B.actor].get(path).json()["scope"] == "private:team-one:agent-b"
    assert memory.calls[-1] == ("poll", seeded["native"]["update_id"])
    assert len(artifacts.context.memory.calls) == 1
    key = service._memory_operation_key(A.team, seeded["operation_id"])
    saved = repository.get(*key)
    repository.replace(*key, {**saved.payload, "memory_store": "poc-team-memory"}, saved.etag)
    before = len(memory.calls)
    assert clients[B.actor].get(path).json()["error"]["code"] == "M4_RECEIPT_INVALID"
    assert len(memory.calls) == before


@pytest.mark.parametrize("field,value", [
    ("native_update_id", "../other-store"), ("native_update_id", 10),
    ("status", ["queued"]), ("status", "unknown-status"), ("owner", "agent-b"),
    ("run_id", "m3-artifacts-01"), ("scope", "team:team-two"),
])
def test_m4_poll_rejects_invalid_persisted_bindings_before_native_read(m4, field, value):
    service, repository, memory, clients, _ = m4
    started = clients[A.actor].post(BASE + "/memory/seed", json=SEED).json()
    key = service._memory_operation_key(A.team, started["operation_id"])
    saved = repository.get(*key)
    repository.replace(*key, {**saved.payload, field: value}, saved.etag)
    before = len(memory.calls)
    response = clients[A.actor].get(BASE + "/memory/operations/" + started["operation_id"])
    assert response.status_code == 502 and response.json()["error"]["code"] == "M4_RECEIPT_INVALID"
    assert len(memory.calls) == before


@pytest.mark.parametrize("native,status,code", [
    ({"status": "completed"}, 502, "MEMORY_PROTOCOL_ERROR"),
    ({"status": "failed", "error": {"code": "NativeFailed"}}, 502, "MEMORY_EXTRACTION_FAILED"),
    ({"status": "superseded"}, 409, "MEMORY_UPDATE_SUPERSEDED"),
    ({"status": "mystery"}, 502, "MEMORY_PROTOCOL_ERROR"),
])
def test_m4_poll_preserves_fail_stop_native_receipt_semantics(m4, native, status, code):
    _, _, memory, clients, _ = m4
    started = clients[A.actor].post(BASE + "/memory/seed", json=SEED).json()
    memory.results[started["native"]["update_id"]] = native
    response = clients[A.actor].get(BASE + "/memory/operations/" + started["operation_id"])
    assert response.status_code == status and response.json()["error"]["code"] == code


def test_cross_private_probe_denies_before_body_or_upstream_and_search_is_read_only(m4):
    _, repository, memory, clients, _ = m4
    before = copy.deepcopy(repository.data)
    response = clients[A.actor].post(BASE + "/memory/private/agent-b/search", content=b"x" * 100000)
    assert response.status_code == 403 and response.json()["error"]["code"] == "PRIVATE_TARGET_DENIED"
    assert memory.calls == []
    assert clients[B.actor].post(BASE + "/memory/private/agent-b/search", json={"query": "Lyra"}).status_code == 200
    assert memory.calls == [("search", "private:team-one:agent-b", "Lyra")]
    assert repository.data == before
    assert clients[A.actor].post(BASE + "/memory/private/agent-a/search",
                                json={"query": "Lyra", "scope": "private:team-one:agent-b"}).status_code == 422


def test_context_reads_authoritative_work_m4_only_and_pinned_policy_without_mutation(m4):
    _, repository, memory, clients, artifacts = m4
    memory.search_results = {
        "team:team-one": [{"memory_item": {"memory_id": "team-fact", "content": "compact"}}],
        "private:team-one:agent-a": [{"memory_item": {"memory_id": "private-fact", "content": "cobalt"}}],
        "private:team-one:agent-b": [{"memory_item": {"memory_id": "private-b", "content": "saffron"}}],
    }
    before = copy.deepcopy(repository.data)
    response = clients[A.actor].get(BASE + f"/works/{WORK}/context")
    assert response.status_code == 200
    result = response.json()
    assert result["actor"] == "agent-a" and result["team"] == "team-one"
    assert result["work"] == artifacts.context.get(A, A.team, WORK).response()
    assert "Mira" in result["work"]["progress"]
    assert result["approved_policy_context"][0]["version"]["version_id"] == POLICY_VERSION
    assert result["approved_policy_context"][0]["version"]["sha256"] == POLICY_SHA256
    assert result["approved_policy_context"][0]["text"] == POLICY_TEXT
    assert "saffron" not in response.text
    assert response.content == encoded(result) and len(response.content) <= CONTEXT_BYTES_LIMIT
    assert artifacts.context.memory.calls == []
    assert memory.calls == [
        ("search", "team:team-one", "Project Lyra context"),
        ("search", "private:team-one:agent-a", "Project Lyra context"),
    ]
    assert repository.data == before
    assert clients[B.actor].get(BASE + f"/works/{WORK}/context").json()["private_memory"]["scope"].endswith("agent-b")


def test_wrong_work_id_policy_baseline_and_memory_errors_fail_closed(m4):
    service, _, memory, clients, artifacts = m4
    assert clients[A.actor].get(BASE + "/works/source-work/context").status_code == 422
    assert clients[A.actor].get(BASE + "/works/m4-context-01-missing/context").status_code == 404
    policies = service.policy_context(A, A.team)
    for field in ("version_id", "sha256"):
        changed = copy.deepcopy(policies)
        changed[0]["version"][field] = "0" * (32 if field == "version_id" else 64)
        with patch.object(artifacts, "policy_context", return_value=changed):
            for response in (
                clients[A.actor].get(BASE + f"/works/{WORK}/context"),
                clients[A.actor].post(BASE + "/memory/seed", json=SEED),
            ):
                assert response.status_code == 409
                assert response.json()["error"]["code"] == "M4_POLICY_BASELINE_CHANGED"
    assert memory.calls == []
    memory.failure = Problem(502, "UPSTREAM_ERROR", "exact native diagnostic",
                             upstream_status=429, upstream_code="NativeThrottle")
    response = clients[A.actor].get(BASE + f"/works/{WORK}/context")
    assert response.status_code == 502 and response.json() == memory.failure.body
    assert len(memory.calls) == 1


def test_context_and_query_limits_never_truncate(m4):
    _, _, memory, clients, artifacts = m4
    memory.search_results["team:team-one"] = [
        {"memory_item": {"memory_id": "fact-" + str(index), "content": "界" * 2000}} for index in range(6)
    ]
    response = clients[A.actor].get(BASE + f"/works/{WORK}/context")
    assert response.status_code == 413 and response.json()["error"]["code"] == "M4_CONTEXT_TOO_LARGE"
    assert response.json()["error"]["context_bytes"] > CONTEXT_BYTES_LIMIT
    artifacts.context.create(A, A.team, SUITE + "-large-goal", CreateWork(goal="x" * 2049))
    memory.calls.clear()
    response = clients[A.actor].get(BASE + f"/works/{SUITE}-large-goal/context")
    assert response.status_code == 413 and response.json()["error"]["code"] == "M4_QUERY_TOO_LARGE"
    assert memory.calls == []


def test_m4_adapter_reuses_start_but_refuses_legacy_search_truncation():
    project = Mock()
    memory = M4FoundryMemory(project, PROJECT, MEMORY_STORE)

    def begin(**kwargs):
        response = Mock()
        response.http_response.status_code = 202
        response.http_response.json.return_value = {"update_id": "native-m4", "status": "queued"}
        kwargs["raw_response_hook"](response)

    project.beta.memory_stores.begin_update_memories.side_effect = begin
    items = [{"role": "user", "type": "message", "content": [{"type": "input_text", "text": "fixture"}]}]
    assert memory.start("team:team-one", items)["update_id"] == "native-m4"
    arguments = project.beta.memory_stores.begin_update_memories.call_args.kwargs
    assert arguments["name"] == MEMORY_STORE and arguments["items"] == items
    assert arguments["polling"] is False and arguments["update_delay"] == 0
    data = {"memories": [{"memory_item": {"memory_id": "long-native", "content": "x" * 4097}}]}
    project.beta.memory_stores.search_memories.return_value.as_dict.return_value = data
    with pytest.raises(Problem) as exc:
        memory.search("team:team-one", "Lyra")
    assert exc.value.body["error"]["code"] == "M4_MEMORY_CONTEXT_CHANGED"
    data["memories"][0]["memory_item"]["content"] = "compact"
    assert memory.search("team:team-one", "Lyra") == data
    assert project.beta.memory_stores.search_memories.call_args.kwargs["name"] == MEMORY_STORE


@pytest.mark.parametrize("fixture,actor,scope", [
    ("short_team", A, "team:team-one"),
    ("long_team", A, "team:team-one"),
    ("private_a", A, "private:team-one:agent-a"),
    ("private_b", B, "private:team-one:agent-b"),
])
def test_m4_real_sdk_seed_and_poll_wire_contract(m4, fixture, actor, scope):
    _, repository, _, _, artifacts = m4
    operation_id = hashlib.sha256((SUITE + ":" + fixture).encode()).hexdigest()[:32]
    native_id = "native-m4-" + fixture
    partition, row = "m4-" + TENANT, "operation:" + SUITE + ":" + operation_id
    queued = {"update_id": native_id, "status": "queued"}
    completed = {
        "update_id": native_id, "status": "completed",
        "result": {
            "memory_operations": [{"kind": "created", "memory_id": "synthetic-native-fact"}],
            "usage": {"input_tokens": 101, "output_tokens": 12, "embedding_tokens": 7, "total_tokens": 120},
        },
    }

    class ReceiptBoundTransport(WireTransport):
        def __init__(self):
            super().__init__([(202, queued, {}), (200, completed, {})])
            self.wire_responses = []

        def send(self, request, **kwargs):
            reserved = repository.get(partition, row).payload
            assert reserved["scope"] == scope and reserved["memory_store"] == "poc-m4-context-01"
            assert reserved["operation_id"] == operation_id and reserved["submission_attempts"] == 1
            if not self.requests:
                assert reserved["status"] == "starting" and reserved["native_update_id"] is None
            else:
                assert reserved["native_update_id"] == native_id
            response = super().send(request, **kwargs)
            self.wire_responses.append(response)
            return response

    transport, credential = ReceiptBoundTransport(), Credential()
    with AIProjectClient(
        endpoint=PROJECT, credential=credential, transport=transport, api_version="v1",
        retry_total=0, credential_scopes=["https://ai.azure.com/.default"],
    ) as project:
        service = M4ContextService(
            artifacts.context, M4FoundryMemory(project, PROJECT, MEMORY_STORE), SETTINGS, artifacts,
        )
        started = service.seed(actor, actor.team, SeedFixture(fixture_id=fixture, suite_sha256=FIXTURE_SHA256))
        assert started == {
            "operation_id": operation_id, "scope": scope, "native": queued,
            "receipt_etag": repository.get(partition, row).etag,
        }
        assert len(transport.requests) == 1
        if fixture.startswith("private_"):
            with pytest.raises(Problem) as denied:
                service.poll_memory(B if actor == A else A, actor.team, operation_id)
            assert denied.value.status == 403
            assert denied.value.body["error"]["code"] == "PRIVATE_TARGET_DENIED"
            assert len(transport.requests) == 1
        polled = service.poll_memory(actor, actor.team, operation_id)
        assert polled == {"operation_id": operation_id, "scope": scope, "native": completed}

    assert len(transport.requests) == 2 and transport.responses == []
    update, poll = transport.requests
    assert update.method == "POST"
    assert update.url == PROJECT + "/memory_stores/poc-m4-context-01:update_memories?api-version=v1"
    payload = json.loads(update.body)
    wire_scope = "sc1-" + base64.urlsafe_b64encode(scope.encode("ascii")).decode("ascii").rstrip("=")
    assert payload == {
        "scope": wire_scope, "update_delay": 0,
        "items": [
            {"role": "user", "type": "message", "content": [{"type": "input_text", "text": text}]}
            for text in fixtures()["seed_data"][fixture]
        ],
    }
    assert poll.method == "GET" and poll.body in (None, b"", "")
    # The installed SDK supplies this operation URL even with polling=False; do not invent a poll endpoint.
    assert poll.url == transport.wire_responses[0].headers["Operation-Location"]
    assert urlsplit(poll.url).path == (
        urlsplit(PROJECT).path + "/memory_stores/poc-m4-context-01/updates/" + native_id
    )
    assert operation_id != native_id
    for request in transport.requests:
        assert parse_qs(urlsplit(request.url).query) == {"api-version": ["v1"]}
        assert request.headers["Foundry-Features"] == "MemoryStores=V1Preview"
        assert "scope" not in request.headers
        assert "poc-team-memory" not in request.url
        assert operation_id not in request.url
    assert credential.scopes and all(scope == ("https://ai.azure.com/.default",) for scope in credential.scopes)
    assert repository.get(partition, row).payload == {
        **service._details(fixture), "kind": "memory_operation", "operation_id": operation_id,
        "owner": actor.actor, "scope": scope, "status": "queued", "native_update_id": native_id,
        "created_at": repository.get(partition, row).payload["created_at"],
    }
    assert (actor.team, "operation:" + operation_id) not in repository.data


def test_m4_body_guards_after_authentication_and_no_legacy_upload_expansion(m4):
    service, _, memory, clients, artifacts = m4
    client = clients[A.actor]
    for raw in (
        '{"fixture_id":"short_team","fixture_id":"private_b","suite_sha256":"' + FIXTURE_SHA256 + '"}',
        '{"fixture_id":"short_team","suite_sha256":NaN}', '{"fixture_id":',
    ):
        assert client.post(BASE + "/memory/seed", content=raw,
                           headers={"content-type": "application/json"}).status_code == 422
    assert client.post(BASE + "/memory/seed", content=b"x" * (MAX_M4_BODY_BYTES + 1),
                       headers={"content-type": "application/json"}).status_code == 413
    assert client.post(BASE + "/memory/seed", json=SEED,
                       headers={"content-encoding": "gzip"}).status_code == 415
    assert client.post(BASE + "/memory/seed?actor=agent-a", json=SEED).status_code == 422
    assert client.get(BASE + f"/works/{WORK}/context?policy=other").status_code == 422
    assert client.post("/teams/team-one/memory/team", json=SEED).status_code == 422
    assert client.post(BASE + "/memory/team", json={"marker": "x", "value": "amber"}).status_code == 404
    assert memory.calls == []

    def denied():
        raise Problem(401, "TOKEN_REQUIRED", "verified identity required")

    gateway = M2Gateway(artifacts.context, SETTINGS.model_settings, PROJECT, Credential(),
                        httpx.AsyncClient(transport=httpx.MockTransport(
                            lambda _request: httpx.Response(200, json=NATIVE),
                        )), milestone="m4")
    app = create_app(artifacts.context, denied, artifact_service=artifacts,
                     verified_artifact_identity_dependency=lambda: A, m4_service=service, m4_model_gateway=gateway)
    messages = []

    async def receive():
        raise AssertionError("unauthenticated request body must not be read")

    async def send(message):
        messages.append(message)

    try:
        asyncio.run(app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "https", "path": BASE + "/memory/seed",
            "raw_path": (BASE + "/memory/seed").encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-length", b"1000000"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1), "server": ("test", 443),
        }, receive, send))
    finally:
        asyncio.run(gateway.aclose())
    assert messages[0]["status"] == 401


def test_m4_settings_are_disabled_by_default_fixed_and_all_or_none():
    assert M4Settings.from_env({}) is None
    assert M4Settings.from_env(M4_ENV) == SETTINGS
    for name in ENV_NAMES:
        incomplete = dict(M4_ENV)
        del incomplete[name]
        with pytest.raises(ValueError):
            M4Settings.from_env(incomplete)
    for name, value in (
        ("CONTEXT_M4_RUN_ID", "m4-context-02"), ("CONTEXT_M4_MODEL", "gpt-4.1"),
        ("CONTEXT_M4_MAX_REQUESTS", "61"), ("CONTEXT_M4_MAX_REQUESTS", "060"),
        ("CONTEXT_M4_MAX_REQUESTS", "1"), ("CONTEXT_M4_MEMORY_STORE", "poc-team-memory"),
        ("CONTEXT_M4_FIXTURE_SHA256", "0" * 64), ("CONTEXT_M4_AMENDMENT_ID", "new"),
        ("CONTEXT_M4_ADDITIONAL_REQUESTS", "1"),
    ):
        with pytest.raises(ValueError):
            M4Settings.from_env({**M4_ENV, name: value})
    with patch("shared_context.m4_config.fixtures", return_value={"changed": True}):
        with pytest.raises(ValueError, match="fixture bytes"):
            M4Settings.from_env(M4_ENV)


