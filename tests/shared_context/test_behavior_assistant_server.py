"""Production composition through real SDKs and local transports; NOT Azure readiness."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shared_context.behavior_assistant import AssistantAuthority, AssistantRuntime
from shared_context.behavior_foundry import NativeExecutionGrant, native_key
from shared_context.behavior_server import application, bootstrap, build_server
from shared_context.behavior_assistant_wire import Task
from shared_context.behavior_runtime import executor_job_input
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
from test_m3_auth import jwk_client
from test_m3_auth import signing_key


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


def test_legacy_default_serialization_is_unchanged():
    assert synthetic_configuration().sha256 == "e853591b3bfb0e941a212792d2aff967d303c21481a499924647d8bbf9811067"
    raw = synthetic_configuration().model_dump(mode="json")
    assert "assistant" not in raw
    raw["assistant"] = None
    assert ServerConfiguration.model_validate_json(json.dumps(raw)).sha256 == synthetic_configuration().sha256


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
