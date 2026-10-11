"""Production composition through real SDKs and local transports; NOT Azure readiness."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from shared_context.behavior_assistant import AssistantAuthority, AssistantRuntime
from shared_context.behavior_foundry import NativeExecutionGrant, native_key
from shared_context.behavior_server import application, bootstrap, build_server
from shared_context.behavior_assistant_wire import AssistantSubmit, Task
from shared_context import behavior_assistant_wire as aw, behavior_wire as wire
from shared_context.behavior_runtime import executor_job_input
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from shared_context.behavior_server_config import ServerConfiguration, synthetic_configuration
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_store import BlobArtifactStore
from shared_context.service import ContextService
from shared_context.storage import TableRepository, serialize
from shared_context.source_materials import digest
from test_behavior_server import Harness, NOW, workspace
from test_behavior_api import METRICS
from test_behavior_store import parse_batch
from test_m3_auth import jwk_client, signing_key, token


def assistant_configuration():
    raw = synthetic_configuration().model_dump(mode="json")
    raw["workload_version"] = "assistant-v1.2"
    raw["identity"]["managed_identity_client_id"] = "33333333-3333-3333-3333-333333333333"
    raw["workers"].update(
        capacity=1, executor_version="assistant-sdk-1-0-13-runtime-1-0-83",
        protection_version="assistant-inline-and-m3-retention-v1",
    )
    raw["delivery"]["protection_version"] = "assistant-inline-and-m3-retention-v1"
    for field in ("bindings", "policies", "operations"):
        raw[field] = raw[field][:1]
    binding = raw["bindings"][0]
    binding["work"].update(
        goal="Use the reviewed metrics to compute remaining items and save my result.",
        criteria_version="assistant-criteria-v1",
        required_checks=["answer_verified", "artifact_verified", "finish_verified"],
    )
    raw["policies"][0]["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", binding])
    raw["assistant"] = {
        "profiles": [{
            "binding_id": binding["binding_id"],
            "source": {"source_id": "metrics-1", "version": 1,
                       "sha256": hashlib.sha256(METRICS).hexdigest(), "byte_length": len(METRICS)},
            "claim_id": "claim-1", "legacy_source_work": "legacy-source",
            "artifact_id": "assistant-report", "artifact_version": "b" * 32,
            "policy_version": "assistant-own-result-v1", "max_model_requests": 6, "require_operation": False,
        }],
        "provider_endpoint": raw["targets"][0]["project_endpoint"] + "/openai/v1/chat/completions",
        "model": "gpt-4.1-mini",
        "credential_binding": "server-api-uami",
        "m3_context": {
            "account_resource_id": raw["table"]["account_resource_id"],
            "endpoint": raw["table"]["endpoint"], "table_name": raw["table"]["scope"]["table_name"],
        },
        "artifact_container_url": raw["sources"]["protection"]["container_url"],
    }
    return raw


def test_assistant_profile_has_explicit_pinned_serializable_resource_bindings():
    raw = assistant_configuration()
    config = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert config.assistant.profiles[0].legacy_source_work == "legacy-source"
    assert config.active_targets == config.targets[:1]
    assert config.model_dump(mode="json")["assistant"] == raw["assistant"]
    assert config.resource_bindings()["assistant_sha256"] == digest(raw["assistant"])
    assert ServerConfiguration.model_validate_json(config.model_dump_json()) == config


FROZEN_C1 = Path(__file__).resolve().parents[2] / ".azure" / "behavior-migration-native" / "config-new.json"


def frozen_c1_config():
    # A private deployment snapshot that is not in the repository; its exact bytes are checked below when present.
    if not FROZEN_C1.is_file():
        pytest.skip("needs the private C1 snapshot .azure/behavior-migration-native/config-new.json "
                    "(sha256 f5b4b89c...) at the repository root")
    return FROZEN_C1.read_bytes()


def test_legacy_default_serialization_is_unchanged():
    assert synthetic_configuration().sha256 == "e853591b3bfb0e941a212792d2aff967d303c21481a499924647d8bbf9811067"
    raw = synthetic_configuration().model_dump(mode="json")
    assert "assistant" not in raw
    raw["assistant"] = None
    assert ServerConfiguration.model_validate_json(json.dumps(raw)).sha256 == synthetic_configuration().sha256


def test_frozen_c1_serialization_is_unchanged():
    frozen = frozen_c1_config()
    config = ServerConfiguration.model_validate_json(frozen)
    assert hashlib.sha256(frozen).hexdigest() == "f5b4b89c23672d6140448e447af99eb58fcb21e5f78e5cf4f7601173d034d60d"
    assert config.sha256 == "d22c9ebc091c0cf21472a6a15c579b60e67f9aa0a73e8eea7fe3c3d064fb9c6a"
    assert hashlib.sha256(config.model_dump_json().encode()).hexdigest() == "4e1f9a696d0c4a3d1f9558a2a554a41ae97e0d1b9085cb2a899c85ab9cc81d96"


def test_transition_shaped_config_keeps_existing_c1_resources_roots_and_old_policy():
    frozen = frozen_c1_config()
    original = ServerConfiguration.model_validate_json(frozen)
    raw = original.model_dump(mode="json")
    sample = assistant_configuration()
    raw.update(deployment_id="assistant-config-shape", workload_version="assistant-v1.2",
               assistant=sample["assistant"])
    raw["workers"].update(
        executor_version="assistant-sdk-1-0-13-runtime-1-0-83",
        protection_version="assistant-inline-and-m3-retention-v1",
    )
    raw["delivery"].update(
        protection_version="assistant-inline-and-m3-retention-v1", verifier_version="assistant-verifier-v1",
    )
    raw["targets"][0].update(agent_version=str(int(original.targets[0].agent_version) + 1),
                             session_id="assistant-session-shape")
    binding = raw["bindings"][0]
    binding.pop("child_jobs", None)
    binding.update(
        binding_id="assistant-binding", event_id="assistant-event", job_id="assistant-job",
        operation_id="assistant-operation", attempt_id="assistant-attempt", grant_id="assistant-grant",
        ingress_id="assistant-authenticated-api", source_ids=["assistant-source"],
        claim_ids=["assistant-claim"], allowed_input_refs=["assistant-goal"],
    )
    binding["owner"].update(owner_record_id="assistant-owner", conversation_id="assistant-conversation",
                            policy_id="assistant-policy")
    binding["route"]["thread_id"] = "assistant-thread"
    binding["work"] = {**sample["bindings"][0]["work"], "work_id": "assistant-work"}
    raw["bindings"] = [binding]
    raw["policies"] = [{
        "binding_id": binding["binding_id"],
        "policy": {**raw["policies"][0]["policy"],
                   "binding_digest": digest(["fa-behavior-api-v1", "native", binding])},
    }]
    raw["operations"] = [{
        **raw["operations"][0], "input_ref": "assistant-goal", "binding_id": binding["binding_id"],
    }]
    settings = raw["assistant"]
    settings["profiles"][0].update(binding_id=binding["binding_id"], claim_id="assistant-claim")
    settings["profiles"][0]["source"]["source_id"] = "assistant-source"
    settings["provider_endpoint"] = original.targets[0].project_endpoint + "/openai/v1/chat/completions"
    settings["m3_context"] = {
        "account_resource_id": original.table.account_resource_id, "endpoint": original.table.endpoint,
        "table_name": original.table.scope.table_name,
    }
    settings["artifact_container_url"] = original.sources.protection.container_url
    candidate = ServerConfiguration.model_validate_json(json.dumps(raw))
    old, new = original.model_dump(), candidate.model_dump()
    assert original.assistant is None and "assistant" not in old
    assert set(new["assistant"]) == {
        "profiles", "provider_endpoint", "model", "credential_binding", "m3_context", "artifact_container_url",
    }
    assert {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)} == {
        "deployment_id", "workload_version", "assistant", "bindings", "policies", "operations",
        "workers", "delivery", "targets",
    }
    for section, allowed in (
        ("workers", {"executor_version", "protection_version"}),
        ("delivery", {"protection_version", "verifier_version"}),
    ):
        assert {key for key in old[section] if old[section][key] != new[section][key]} == allowed
    assert {key for key in old["targets"][0] if old["targets"][0][key] != new["targets"][0][key]} == {
        "agent_version", "session_id",
    }
    assert new["targets"][1:] == old["targets"][1:]
    assert candidate.workers.capacity == original.workers.capacity == 1
    assert candidate.bindings[0].root_request_id == original.roots[0].root_request_id
    assert candidate.bindings[0].owner.root_budget_id == original.roots[0].root_budget_id
    assert candidate.policies[0].policy.valid_until == original.policies[0].policy.valid_until
    assert candidate.bindings[0].binding_id not in {item.binding_id for item in original.bindings}
    assert ServerConfiguration.model_validate(candidate.model_dump()) == candidate
    assert original.model_dump_json() == ServerConfiguration.model_validate_json(frozen).model_dump_json()
    assert path.read_bytes() == frozen


class AssistantHarness(Harness):
    def __init__(self, workspace, key_client):
        super().__init__(workspace, key_client, configuration_data=assistant_configuration())

    def authorization(self):
        from datetime import timedelta
        from shared_context.behavior_server_config import BootstrapAuthorization

        grants = tuple(NativeExecutionGrant(
            grant_id=native_key("bootstrap-grant", binding.job_id), controller_id=self.config.controller_id,
            owner=binding.owner, policy_revision=1, job_id=binding.job_id, definition=binding.work,
            input_digest=hashlib.sha256(self.runtime.job_input(binding)).hexdigest(),
            targets=self.config.active_targets, active=True, remaining_uses=4,
            not_before=NOW, expires_at=NOW + timedelta(hours=1), workload_version="assistant-v1.2",
        ) for binding in self.config.bindings)
        return BootstrapAuthorization(
            decision_id="assistant-exact-approval", authorizing_human_oid=self.config.identity.human_oid,
            configuration_sha256=self.config.sha256, purpose="authorized-six-gap-validation",
            not_before=NOW, expires_at=NOW + timedelta(hours=1), native_grants=grants,
        )

    def legacy(self, owner=None):
        # Fixture represents an existing M3 work; no production provisioning path.
        self.table.backing.apply("Behavior", [("create", {
            "PartitionKey": self.config.identity.team_id, "RowKey": "work:legacy-source",
            "Payload": serialize({
                "work_id": "legacy-source", "team": self.config.identity.team_id,
                "owner": owner or self.config.identity.agents[0].actor, "status": "published", "revision": 1,
            }),
        })])


@pytest.fixture
def assistant_server(workspace, jwk_client):
    harness = AssistantHarness(workspace, jwk_client)
    try:
        yield harness
    finally:
        harness.composition.close()


def test_factory_defers_legacy_lookup_and_owns_real_m3_clients_without_io(assistant_server, monkeypatch):
    h = assistant_server
    assert isinstance(h.runtime, AssistantRuntime)
    assert not h.table.requests and not h.source.requests and not h.backup.requests and not h.calls
    services = h.composition.assistant
    assert services.authority is None
    assert isinstance(services.artifacts, ArtifactService)
    assert isinstance(services.artifacts.context, ContextService)
    assert isinstance(services.artifacts.repository, TableRepository)
    assert isinstance(services.artifacts.store, BlobArtifactStore)
    assert services.artifacts.repository.client is h.composition.clients[0]
    assert services.artifacts.store.container is h.composition.clients[1]
    assert services.provider.credential is h.composition.clients[0].credential
    closed = []
    for index, transport in enumerate((h.table, h.source, h.backup)):
        monkeypatch.setattr(transport, "close", lambda i=index: closed.append(i))
    h.composition.close()
    h.composition.close()
    assert sorted(closed) == [0, 1, 2]


def test_bootstrap_grant_pins_actual_assistant_input_not_checksum_bytes(assistant_server):
    h = assistant_server
    binding = h.config.bindings[0]
    body = h.runtime.job_input(binding)
    task = Task.model_validate_json(body)
    assert task.profile == "assistant-v1.2" and task.artifact_id == "assistant-report"
    assert body != executor_job_input(binding.job_id, binding.work)
    bootstrap(h.composition, h.approval, stage="records")
    key = native_key("grant", h.approval.native_grants[0].grant_id)
    saved = h.runtime.store.read_snapshot((key,))[key].payload["data"]
    assert saved["input_digest"] == hashlib.sha256(body).hexdigest()
    assert saved["workload_version"] == "assistant-v1.2"
    assert h.composition.assistant.authority is None
    assert not h.source.requests and not h.calls


@pytest.mark.parametrize("change", ["checksum-bytes", "checksum-profile", "checkpoint"])
def test_inexact_assistant_grant_is_rejected_before_io(assistant_server, change):
    h = assistant_server
    binding = h.config.bindings[0]
    updates = {
        "checksum-bytes": {"input_digest": hashlib.sha256(executor_job_input(binding.job_id, binding.work)).hexdigest()},
        "checksum-profile": {"workload_version": "checksum-count-v1"},
        "checkpoint": {"checkpoint_after_bytes": 10},
    }[change]
    approval = h.approval.model_copy(update={
        "native_grants": (h.approval.native_grants[0].model_copy(update=updates),),
    })
    with pytest.raises(Problem):
        bootstrap(h.composition, approval)
    assert not h.table.requests and not h.calls


def test_installation_initializes_concrete_authority_on_the_original_pool(assistant_server):
    h = assistant_server
    h.legacy()
    bootstrap(h.composition, h.approval, stage="records")
    assert h.composition.assistant.authority is None
    before = len(h.table.parts)
    h.composition.installation()
    authority = h.composition.assistant.authority
    assert isinstance(authority, AssistantAuthority)
    assert authority.store is h.runtime.store
    assert authority is h.runtime.dep.native_launches.assistant
    assert authority.runtime.dep.workers is h.runtime.dep.native_launches.workers
    assert authority.artifacts is h.composition.assistant.artifacts
    assert len(h.table.parts) == before
    assert any("work%3Alegacy-source" in r.url or "work:legacy-source" in r.url for r in h.table.requests)


def test_production_assistant_routes_share_auth_current_catalog_and_installation_gates(assistant_server, signing_key):
    h = assistant_server
    h.legacy()
    h.activate()
    assert isinstance(h.composition.assistant.authority, AssistantAuthority)
    with TestClient(application(h.composition)) as client:
        prefix = "/behavior/v1/bindings/binding-1/assistant"
        command = AssistantSubmit(request_id="production-assistant-submit")
        headers = {**h.headers(signing_key), "Content-Type": "application/json"}
        assert client.post(prefix + "/submit", content=b"x" * 100000).status_code == 401
        accepted = client.post(prefix + "/submit", content=command.model_dump_json(), headers=headers)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["state"] == "queued"
        h.runtime.controller.step("binding-1")
        assert len(h.calls) == 1
        assert h.runtime.dep.workers.inspect("job-1").spec.input == h.runtime.job_input(h.config.bindings[0])
        grant_key = native_key("grant", h.approval.native_grants[0].grant_id)
        assert h.runtime.store.read_snapshot((grant_key,))[grant_key].payload["data"]["remaining_uses"] == 3
        h.save(h.bundle.model_copy(update={"worker": None}))
        endpoints = ["/submit", "/launches/launch/claim"] + [
            "/sessions/" + "a" * 64 + "/" + suffix for suffix in (
                "model", "acquire", "context", "reserve-model", "save", "reconcile", "finish",
                "operation", "stopped", "status", "checkpoint", "restore",
            )
        ]
        before = len(h.table.parts)
        for endpoint in endpoints:
            response = client.post(prefix + endpoint, content=b"x" * 100000, headers=headers)
            assert response.status_code == 503, (endpoint, response.text)
        assert len(h.table.parts) == before and len(h.calls) == 1
        assert client.get("/healthz").status_code == 200
    assert h.composition.closed


def test_probe_keeps_assistant_admission_model_and_tools_closed_without_io(assistant_server):
    h = assistant_server
    with TestClient(application(h.composition, preactivation=True)) as client:
        assert client.get("/healthz").status_code == 200
        for suffix in ("submit", "sessions/session/model", "sessions/session/save", "launches/launch/claim"):
            assert client.post("/behavior/v1/bindings/binding-1/assistant/" + suffix, json={}).status_code == 404
    assert h.composition.assistant.authority is None
    assert not h.table.requests and not h.source.requests and not h.calls


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(assistant=None),
    lambda c: c.update(workload_version="checksum-count-v1"),
    lambda c: c["workers"].update(capacity=4),
    lambda c: c["workers"].update(executor_version="checksum-count-v1"),
    lambda c: c["workers"].update(protection_version="retention-v1"),
    lambda c: c["delivery"].update(protection_version="retention-v1"),
    lambda c: c["bindings"][0]["work"].update(required_checks=["answer_verified"]),
    lambda c: c["bindings"][0]["work"].update(human_checks=["artifact_verified"]),
    lambda c: c["bindings"][0]["work"].update(result_kind="artifact"),
    lambda c: c["bindings"][0].update(child_jobs=[{"job_id": "child", "work": {
        **c["bindings"][0]["work"], "work_id": "child-work",
    }}]),
    lambda c: c["targets"].reverse(),
    lambda c: c["bindings"][0]["owner"].update(principal_id=c["identity"]["human_oid"]),
    lambda c: c["identity"].update(managed_identity_client_id=None),
    lambda c: c["assistant"].update(profiles=[]),
    lambda c: c["assistant"].update(profiles=c["assistant"]["profiles"] * 17),
    lambda c: c["assistant"]["profiles"][0].update(binding_id="other-binding"),
    lambda c: c["assistant"]["profiles"][0].update(legacy_source_work=c["bindings"][0]["work"]["work_id"]),
    lambda c: c["assistant"]["profiles"][0].update(claim_id="unreviewed"),
    lambda c: c["assistant"]["profiles"][0]["source"].update(source_id="unassigned"),
    lambda c: c["assistant"]["profiles"][0].update(max_model_requests=9),
    lambda c: c["assistant"]["profiles"][0].update(artifact_version="latest"),
    lambda c: c["assistant"].update(provider_endpoint="https://arbitrary.example/openai/v1/chat/completions"),
    lambda c: c["assistant"].update(provider_endpoint=c["assistant"]["provider_endpoint"] + "?key=secret"),
    lambda c: c["assistant"].update(model="gpt-4.1"),
    lambda c: c["assistant"].update(credential_binding="hosted-identity"),
    lambda c: c["assistant"].update(api_key="SYNTHETIC_NOT_A_SECRET"),
    lambda c: c["assistant"]["m3_context"].update(table_name="UnpinnedTable"),
    lambda c: c["assistant"]["m3_context"].update(endpoint="https://other.table.core.windows.net"),
    lambda c: c["assistant"].update(artifact_container_url=c["backups"]["protection"]["container_url"]),
])
def test_assistant_configuration_rejects_invalid_checks_identity_or_unpinned_backends(mutation):
    raw = assistant_configuration()
    mutation(raw)
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("change", ["profile", "source", "provider", "table", "container", "credential", "actor"])
def test_all_assistant_bindings_change_both_configuration_and_resource_digest(change):
    raw = assistant_configuration()
    original = ServerConfiguration.model_validate_json(json.dumps(raw))
    if change == "profile":
        raw["assistant"]["profiles"][0]["legacy_source_work"] = "another-existing-work"
    elif change == "source":
        raw["assistant"]["profiles"][0]["source"]["sha256"] = "f" * 64
    elif change == "provider":
        raw["targets"][0]["project_endpoint"] = "https://other.services.ai.azure.com/api/projects/project"
        raw["assistant"]["provider_endpoint"] = raw["targets"][0]["project_endpoint"] + "/openai/v1/chat/completions"
    elif change == "table":
        raw["table"]["scope"]["table_name"] = raw["assistant"]["m3_context"]["table_name"] = "ExistingContext"
    elif change == "container":
        url = "https://synthetic.blob.core.windows.net/existing-artifacts"
        raw["sources"]["protection"]["container_url"] = raw["assistant"]["artifact_container_url"] = url
    elif change == "credential":
        raw["identity"]["managed_identity_client_id"] = "44444444-4444-4444-4444-444444444444"
    else:
        raw["identity"]["agents"][0]["actor"] = "another-owned-actor"
    changed = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert original.sha256 != changed.sha256
    assert digest(original.resource_bindings()) != digest(changed.resource_bindings())


def test_uninstalled_assistant_cannot_start_or_create_authority(assistant_server):
    h = assistant_server
    with pytest.raises(Problem) as error:
        application(h.composition)
    assert error.value.body["error"]["code"] == "BEHAVIOR_NOT_INITIALIZED"
    assert h.composition.closed and h.composition.assistant.authority is None
    assert not h.table.backing.rows and not h.source.requests and not h.calls


@pytest.mark.parametrize("change", ["missing-catalog", "missing-worker", "old-executor", "old-protection", "old-config"])
def test_installed_assistant_requires_current_readiness_without_mutation_or_dispatch(assistant_server, change):
    h = assistant_server
    h.legacy()
    h.activate()
    before = deepcopy(h.table.backing.rows)
    if change == "missing-catalog":
        Path(h.config.evidence.path).unlink()
    elif change == "missing-worker":
        h.save(h.bundle.model_copy(update={"worker": None}))
    elif change == "old-executor":
        h.save(h.bundle.model_copy(update={"worker": h.bundle.worker.model_copy(update={"executor_version": "checksum-count-v1"})}))
    elif change == "old-protection":
        h.save(h.bundle.model_copy(update={"worker": h.bundle.worker.model_copy(update={"protection_version": "retention-v1"})}))
    elif change == "old-config":
        h.save(h.bundle.model_copy(update={"configuration_sha256": synthetic_configuration().sha256}))
    with pytest.raises(Problem) as error:
        application(h.composition)
    assert error.value.body["error"]["code"] == "BEHAVIOR_NATIVE_EVIDENCE_REQUIRED"
    assert h.composition.closed and h.table.backing.rows == before
    assert not h.source.requests and not h.calls


@pytest.mark.parametrize("owner", [None, "agent-2"])
def test_legacy_source_must_exist_and_be_owned_at_installation(assistant_server, owner):
    h = assistant_server
    if owner is not None:
        h.legacy(owner)
    bootstrap(h.composition, h.approval, stage="records")
    h.save()
    with pytest.raises(Problem) as error:
        application(h.composition)
    assert error.value.body["error"]["code"] == ("NOT_FOUND" if owner is None else "ASSISTANT_LEGACY_OWNER")
    assert h.composition.closed and h.composition.assistant.authority is None
    assert not h.source.requests and not h.calls


def test_assistant_lifespan_rechecks_evidence_and_closes_on_failure(assistant_server):
    h = assistant_server
    h.legacy()
    h.activate()
    app = application(h.composition)
    h.save(h.bundle.model_copy(update={"worker": None}))
    with pytest.raises(Problem), TestClient(app):
        pass
    assert h.composition.closed and not h.calls


def test_assistant_routes_recheck_missing_installation_after_startup(assistant_server, signing_key):
    from shared_context.behavior_migration import installation_record

    h = assistant_server
    h.legacy()
    h.activate()
    with TestClient(application(h.composition)) as client:
        key, _ = installation_record(h.config)
        h.table.backing.rows.pop(("Behavior", h.config.table.scope.partition_key, "behavior:v1:row:" + key))
        response = client.post("/behavior/v1/bindings/binding-1/assistant/submit",
                               json={"request_id": "fenced"}, headers=h.headers(signing_key))
        assert response.status_code >= 400
        assert response.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
        assert response.json()["error"]["automatic_retry"] is False
    assert not h.calls


def test_assistant_bootstrap_still_refuses_a_migrated_authority(assistant_server):
    from shared_context.behavior_migration import MigrationStore, migration_key

    h = assistant_server
    bootstrap(h.composition, h.approval, stage="records")
    key = migration_key(h.config.table.scope.authority_domain, "current")
    # Existing nonempty migration pointer fixture; bootstrap must not inspect or replace it.
    installer = MigrationStore(h.composition.clients[0], h.config.table.scope)
    installer.transact(
        ControlRequest(request_id="existing-migration-fixture", request_digest="a" * 64),
        lambda _: ControlChanges(writes=(ControlWrite(
            row_id=key, mode="create", data={"existing": "migration"},
        ),), result={"fixture": True}),
    )
    before = len(h.table.parts)
    with pytest.raises(Problem) as error:
        bootstrap(h.composition, h.approval, stage="records")
    assert error.value.body["error"]["code"] == "MIGRATION_BOOTSTRAP_DISABLED"
    assert len(h.table.parts) == before and not h.calls


@pytest.mark.parametrize("revocation", ["catalog", "installation"])
def test_model_last_gate_rechecks_authority_after_consumption_without_refund_or_send(
    assistant_server, signing_key, monkeypatch, revocation,
):
    from shared_context.behavior_migration import installation_record

    h = assistant_server
    h.legacy()
    h.activate()
    provider_requests = []

    def forbidden_send(request):
        provider_requests.append(request)
        raise AssertionError("revoked authority must not send any model request")

    h.composition.assistant.provider.transport = httpx.MockTransport(forbidden_send)
    prefix = "/behavior/v1/bindings/binding-1"
    agent_headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": h.config.bindings[0].agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",)), "Content-Type": "application/json"}
    human_headers = {**h.headers(signing_key, "Behavior.Read Behavior.Approve"), "Content-Type": "application/json"}
    with TestClient(application(h.composition)) as client:
        def post(suffix, command, headers=human_headers):
            response = client.post(prefix + suffix, content=command.model_dump_json(), headers=headers)
            assert response.status_code == 200, (suffix, response.text)
            return response

        prepare = wire.SourcePrepare(request_id="source", version=1, original=METRICS)
        post("/sources/metrics-1/prepare", prepare)
        post("/sources/metrics-1/commit", wire.Command(request_id="source"))
        draft = wire.ClaimDraft(
            request_id="review", version=1, text="The accepted metrics source has been reviewed.",
            supporting=(h.config.assistant.profiles[0].source,), effective_from=NOW,
        )
        preview = post("/claims/claim-1/preview", draft).json()
        post("/claims/claim-1/review", wire.ClaimReview(
            **draft.model_dump(), decision_id="explicit-review",
            **{field: preview[field] for field in ("candidate_sha256", "evidence_sha256", "policy_version")},
            reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        ))
        post("/claims/claim-1/commit", wire.Command(request_id="review"))
        post("/assistant/submit", AssistantSubmit(request_id="human-admission"))
        h.runtime.controller.step("binding-1")
        launch_id = json.loads(h.calls[0].content)["launch_id"]
        native = h.runtime.dep.native_launches
        assignment, ticket = h.composition.assistant.authority.issue(native.launch(launch_id).start)
        headers = {**agent_headers, "x-behavior-assistant-ticket": ticket.get_secret_value()}
        session = "/assistant/sessions/" + assignment.session_id
        post(session + "/acquire", wire.Command(request_id="sdk-start"), headers)
        body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "Count remaining items."}],
                "max_tokens": 1024, "stream": True}
        request_id = digest(body)
        post(session + "/reserve-model", aw.Reservation(request_id=request_id, request_sha256=request_id), headers)
        budget_key = control_key("budget", h.config.bindings[0].owner.root_budget_id)
        budget = h.runtime.store.read_snapshot((budget_key,))[budget_key].payload["data"]
        assert budget["remaining_attempts"] == h.config.roots[0].attempts - 2
        assert budget["remaining_units"] == h.config.roots[0].units - 2
        installation_key, _ = installation_record(h.config)
        physical_key = ("Behavior", h.config.table.scope.partition_key, "behavior:v1:row:" + installation_key)
        installed = h.table.backing.rows[physical_key]
        original_send = h.table.send
        revoked = False

        def revoke_after_commit(request, **kwargs):
            nonlocal revoked
            response = original_send(request, **kwargs)
            if request.method == "POST" and not revoked:
                operations, _ = parse_batch(request)
                if any(b'"consumed":true' in op[1]["Payload"] for op in operations):
                    revoked = True
                    if revocation == "catalog":
                        h.save(h.bundle.model_copy(update={"worker": None}))
                    else:
                        h.table.backing.rows.pop(physical_key)
            return response

        monkeypatch.setattr(h.table, "send", revoke_after_commit)
        command = aw.ModelRequest(request_id=request_id, body=body)
        response = client.post(prefix + session + "/model", content=command.model_dump_json(), headers=headers)
        assert revoked and response.status_code in {403, 409, 503}, response.text
        assert not provider_requests
        h.save()
        h.table.backing.rows[physical_key] = installed
        replay = client.post(prefix + session + "/model", content=command.model_dump_json(), headers=headers)
        assert replay.status_code == 409, replay.text
        assert replay.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
        assert replay.json()["error"]["automatic_retry"] is False
        assert h.runtime.store.read_snapshot((budget_key,))[budget_key].payload["data"] == budget
        assert not provider_requests
