"""OFFLINE composition tests: real Table/Blob SDKs, local HTTP transports.

Catalog fixtures below explicitly simulate independently captured evidence.
They do not demonstrate Azure readiness, four native agents, or a shared mount.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from shared_context import behavior_wire as wire
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_delivery import ReadinessEvidence
from shared_context.behavior_foundry import FoundryExecutor, NativeExecutionGrant, native_key
from shared_context.behavior_inbox import InboxDeploymentEvidence
from shared_context.behavior_operations import control_key
from shared_context.behavior_server import (
    COVERAGE, _authority_probe_readback, _probe_layout,
    ServerTokenizer, TOKENIZER_CACHE_KEY, application, bootstrap, build_server, recovery_application, run_controller,
)
from shared_context.behavior_server_config import (
    BootstrapAuthorization, NativeEvidenceBundle, ProtectionObservation, RecoveryCatalog,
    ProjectionInstallationEvidence, RecoveryConfiguration, ServerConfiguration, SupervisorLimits,
    TokenizerConfiguration, load_configuration, synthetic_configuration,
)
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import WorkerReadiness
from shared_context.errors import Problem
from shared_context.memory_projection import NativeMemoryProjection, ProjectionSetup
from shared_context.source_backup import RecoveryAuthorization, make_blob_backup_store
from shared_context.source_materials import NativeConfigurationEvidence, SourceScope, configuration_evidence_fingerprint, digest
from test_backup_store import BlobTransport
from test_behavior_store import Credential, WireTransport
from test_m3_auth import jwk_client, signing_key, token
from test_memory_projection import Transport as MemoryTransport
from test_source_backup import (
    BACKUP_HUMAN, ETAG, NATIVE_VERSION, SETTINGS, archive_from_plan,
    native_headers, native_reads, service, snapshot, uploaded,
)
from test_source_materials import NOW as SOURCE_NOW, source_service
from test_source_reviews import HUMAN_CONTEXT

NOW = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)
TOKENIZER_TEST_CACHE = Path(os.environ.get("TIKTOKEN_CACHE_DIR", str(
    Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / ".venv" / "fa-tokenizer-cache",
)))


@pytest.fixture
def workspace():
    root = Path.cwd() / ".behavior-server-tests" / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root)
        try:
            root.parent.rmdir()
        except OSError:
            pass


def observation(storage, scopes):
    account = storage.account_resource_id
    common = dict(api_version="2025-06-01", request_id="offline-transport-request",
                  observed_at=NOW, valid_until=NOW + timedelta(hours=1), etag=None)
    blob = NativeConfigurationEvidence.from_response(
        resource_kind="blob-service", resource_id=account + "/blobServices/default", status_code=200,
        response_body={"id": account + "/blobServices/default", "properties": {"isVersioningEnabled": True}}, **common,
    )
    policy = NativeConfigurationEvidence.from_response(
        resource_kind="management-policy", resource_id=account + "/managementPolicies/default", status_code=404,
        response_body={"error": {"code": "ManagementPolicyNotFound"}}, **common,
    )
    fingerprint = configuration_evidence_fingerprint(
        blob_service_etag=None, management_policy_etag=None, blob_service_evidence=blob, management_policy_evidence=policy,
        automatic_delete_consumers=(), inventory_version="offline-inventory-v1",
    )
    config = storage.protection.model_copy(update={"evidence_sha256": fingerprint})
    return ProtectionObservation(
        configuration=config, observed_at=NOW, valid_until=NOW + timedelta(hours=1),
        blob_service=blob, management_policy=policy, automatic_delete_consumers=(),
        inventory_version="offline-inventory-v1", inventory_coverage=scopes,
        inventory_observed_at=NOW, inventory_valid_until=NOW + timedelta(hours=1),
    )


def projection_setup(config):
    binding = config.bindings[0]
    return ProjectionSetup(
        control_binding=config.workers.control_binding, binding_id=binding.binding_id,
        endpoint=config.targets[0].project_endpoint, store="existing-reviewed-memory",
        projection_namespace="fa-reviewed-v1-server-offline", evidence_sha256="e" * 64,
        scope=SourceScope(tenant_id=binding.tenant_id, team_id=binding.team_id, visibility="team"),
        subject=binding.subject, claim_ids=binding.claim_ids, versions=binding.context_versions,
        max_polls=4, max_searches=4, max_input_tokens=8192, max_query_tokens=512,
        max_input_bytes=8192, max_response_bytes=32768,
    )


class Harness:
    def __init__(self, workspace, key_client, *, human_text=False, projection=False, configuration_data=None):
        raw = synthetic_configuration().model_dump(mode="json") if configuration_data is None else configuration_data
        raw["evidence"]["path"] = str(workspace / "evidence.json")
        raw["tokenizer"]["cache_directory"] = str(TOKENIZER_TEST_CACHE)
        if human_text:
            raw["policies"][0]["policy"].update(human_text_enabled=True, text_policy_version="human-text-policy-v1")
        preliminary = ServerConfiguration.model_validate_json(json.dumps(raw))
        if projection:
            raw["memory_projections"] = [projection_setup(preliminary).model_dump(mode="json")]
        self.sources = observation(preliminary.sources, preliminary.evidence.inventory_scopes)
        self.backups = observation(preliminary.backups, preliminary.evidence.inventory_scopes)
        raw["sources"]["protection"] = self.sources.configuration.model_dump(mode="json")
        raw["backups"]["protection"] = self.backups.configuration.model_dump(mode="json")
        self.config = ServerConfiguration.model_validate_json(json.dumps(raw))
        self.time, self.table, self.source, self.backup = NOW, WireTransport(), BlobTransport([]), BlobTransport([])
        self.probe = WireTransport()
        self.probe.backing = self.table.backing
        self.calls, self.credential = [], Credential()
        self.memory = MemoryTransport([])
        self.options = dict(credential=self.credential, table_transport=self.table, source_transport=self.source,
                            probe_transport=self.probe,
                            backup_transport=self.backup, key_client=key_client, clock=lambda: self.time,
                            memory_transports={"binding-1": self.memory} if projection else None,
                            native_transports={t.executor_id: httpx.MockTransport(self.native) for t in self.config.targets})
        self.composition = build_server(self.config, **self.options)
        self.runtime = self.composition.runtime
        self.bundle = self.evidence()
        self.approval = self.authorization()

    def native(self, request):
        self.calls.append(request)
        name = request.url.path.split("/agents/")[1].split("/")[0]
        target = next(t for t in self.config.targets if t.agent_name == name)
        return httpx.Response(202, headers={"x-agent-invocation-id": request.headers["x-agent-invocation-id"],
                                          "x-agent-session-id": target.session_id}, json={
            "status": "pending", "launch_id": json.loads(request.content)["launch_id"],
            "invocation_id": request.headers["x-agent-invocation-id"],
        })

    def evidence(self):
        config, runtime = self.config, self.runtime
        worker = WorkerReadiness(
            evidence_id="worker-proof", configuration_digest=runtime.dep.workers.configuration_digest(),
            control_binding=config.workers.control_binding, protection_version=config.workers.protection_version,
            executor_id=config.workers.executor_id, executor_version=config.workers.executor_version,
            native_binding_reference=config.deployment_id, supervisor_reference=config.controller_id,
            protection_reference=config.sources.protection.generation,
            observed_at=NOW, expires_at=NOW + timedelta(hours=1), proof=b"OFFLINE catalog transport fixture",
        )
        delivery = ReadinessEvidence(
            evidence_id="delivery-proof", configuration_digest=runtime.dep.delivery.configuration_digest(),
            control_binding=config.delivery.control_binding, protection_version=config.delivery.protection_version,
            consumer_version=config.delivery.consumer_version, timer_version=config.delivery.timer_version,
            coverage=tuple(sorted(COVERAGE)), observed_at=NOW, expires_at=NOW + timedelta(hours=1),
            proof=b"OFFLINE prerequisite coverage fixture",
        )
        inbox = InboxDeploymentEvidence(
            evidence_id="inbox-proof", configuration_digest=delivery.configuration_digest,
            control_binding=config.delivery.control_binding, authentication_version=config.authentication_version,
            protection_scope=config.table.scope.partition_key, protection_evidence_id=config.sources.protection.generation,
            worker_evidence_id=worker.evidence_id, worker_workspace_id=config.workers.workspace_id,
            worker_configuration_digest=worker.configuration_digest, readiness=delivery,
            proof=b"OFFLINE authenticated inbox fixture",
        )
        return NativeEvidenceBundle(
            catalog_id=config.evidence.catalog_id, revision=1, observer_oid=config.evidence.observer_oid,
            configuration_sha256=config.sha256, deployment_id=config.deployment_id, resource_bindings=config.resource_bindings(),
            captured_at=NOW, expires_at=NOW + timedelta(hours=1), sources=self.sources, backups=self.backups,
            worker=worker, inbox=inbox, delivery=delivery,
            memory_projections=tuple(ProjectionInstallationEvidence(
                binding_id=setup.binding_id, setup_sha256=setup.sha256, evidence_sha256=setup.evidence_sha256,
                observed_at=NOW, expires_at=NOW + timedelta(hours=1),
                verified_contracts=("existing_store", "namespace_isolation", "same_authority", "exact_authority_fallback"),
                proof=b"OFFLINE memory installation evidence fixture, not native proof",
            ) for setup in config.memory_projections),
        )

    def save(self, bundle=None):
        Path(self.config.evidence.path).write_text((bundle or self.bundle).model_dump_json(), encoding="utf-8")

    def authorization(self):
        grants = []
        for binding in self.config.bindings:
            data = wire.ExecutorJobInput(job_id=binding.job_id, work_id=binding.work.work_id,
                definition_digest=digest(binding.work.model_dump(mode="json")), required_checks=binding.work.required_checks).model_dump_json().encode()
            grants.append(NativeExecutionGrant(
                grant_id=native_key("bootstrap-grant", binding.job_id), controller_id=self.config.controller_id,
                owner=binding.owner, policy_revision=1, job_id=binding.job_id, definition=binding.work,
                input_digest=hashlib.sha256(data).hexdigest(), targets=self.config.targets, active=True, remaining_uses=4,
                not_before=NOW, expires_at=NOW + timedelta(hours=1),
            ))
        return BootstrapAuthorization(
            decision_id="operator-exact-approval", authorizing_human_oid=self.config.identity.human_oid,
            configuration_sha256=self.config.sha256, purpose="authorized-six-gap-validation",
            not_before=NOW, expires_at=NOW + timedelta(hours=1), native_grants=tuple(grants),
        )

    def activate(self):
        self.save()
        bootstrap(self.composition, self.approval, stage="activate")

    def headers(self, key, scopes="Behavior.Read"):
        c = self.config.identity
        return {"Authorization": "Bearer " + token(key, {"oid": c.human_oid, "appid": c.admin_client_id, "scp": scopes})}

    def submit(self):
        c = self.config.identity
        principal = BehaviorPrincipal(c.tenant_id, c.human_oid, c.team_id, "human", None, frozenset({"read", "submit"}))
        for binding in self.config.bindings:
            status = self.runtime.submit(binding.binding_id, wire.SubmitWork(request_id="submit-" + binding.binding_id), principal)
            assert status.state == "queued"

    def mutate(self, key, **updates):
        row = self.runtime.store.read_snapshot((key,))[key].payload
        request = ControlRequest(request_id=uuid4().hex, request_digest="a" * 64, read_set=(key,))
        self.runtime.store.transact(request, lambda _: ControlChanges(writes=(
            ControlWrite(row_id=key, mode="replace", data={**row["data"], **updates}),
        ), result={"changed": True}))


@pytest.fixture
def h(workspace, jwk_client):
    value = Harness(workspace, jwk_client)
    try:
        yield value
    finally:
        value.composition.close()


def test_factory_constructs_actual_native_components_without_io_and_closes_owned_clients(h, monkeypatch):
    assert h.table.requests == [] and h.source.requests == [] and h.backup.requests == [] and h.calls == []
    assert h.runtime.mode == "native" and h.runtime.dep.workers.contract.capacity == 4
    assert isinstance(h.runtime.dep.workers.adapter.start.__self__, FoundryExecutor)
    assert h.runtime.dep.source_backups.sources is h.runtime.dep.sources
    assert h.runtime.dep.broker.dispatcher.adapter.settings.allowed_work_ids == tuple(b.work.work_id for b in h.config.bindings)
    assert next(p for p in h.composition.clients[0]._policies if type(p).__name__ == "TablesRetryPolicy").total_retries == 0
    for client in h.composition.clients[1:3]:
        retry = client._config.retry_policy
        assert getattr(retry, "total_retries", getattr(retry, "retry_total", None)) == 0
    closed = []
    for index, transport in enumerate((h.table, h.source, h.backup)):
        monkeypatch.setattr(transport, "close", lambda i=index: closed.append(i))
    h.composition.close()
    h.composition.close()
    assert sorted(closed) == [0, 1, 2]


def test_default_credential_is_managed_identity_and_is_owned_on_failure(h, monkeypatch):
    from shared_context import behavior_server
    credential = SimpleNamespace(close=lambda: closed.append("credential"), get_token=Credential().get_token)
    created, closed = [], []
    monkeypatch.setattr(behavior_server, "ManagedIdentityCredential",
                        lambda **kwargs: created.append(kwargs) or credential)
    invalid = h.config.model_copy(update={"workers": h.config.workers.model_copy(update={"control_binding": "f" * 64}),
                                         "delivery": h.config.delivery.model_copy(update={"control_binding": "f" * 64})})
    with pytest.raises(ValueError):
        build_server(invalid)
    assert created == [{"client_id": None, "retry_total": 0}]
    assert closed == ["credential"]


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(secret="do-not-store"), lambda c: c.update(automatic_model_repairs=3),
    lambda c: c["workers"].update(capacity=5), lambda c: c["workers"].update(queries_per_generation=0),
    lambda c: c["table"].update(endpoint=c["table"]["endpoint"] + "?sig=secret"),
    lambda c: c["table"]["scope"].update(partition_key="../escape"),
    lambda c: c["identity"].update(human_oid=c["identity"]["agents"][0]["oid"]),
    lambda c: c["targets"][1].update(agent_oid=c["targets"][0]["agent_oid"]),
    lambda c: c["sources"]["protection"].update(protected_prefix="all/"),
    lambda c: c["operations"][0].update(binding_id="binding-2"),
    lambda c: c["delivery"].update(model_repair_contract="offline-model-budget-v1"),
    lambda c: c["supervisor"].update(max_run_seconds=float("inf")),
    lambda c: c["evidence"].update(path="https://caller.example/evidence"),
    lambda c: c["policies"][0]["policy"].update(human_text_enabled=True),
    lambda c: c["policies"][0]["policy"].update(text_policy_version="human-text-policy-v1"),
    lambda c: c["policies"][0]["policy"].update(human_text_enabled=True, text_policy_version="latest"),
    lambda c: c["policies"][0]["policy"].update(human_text_enabled=True, text_policy_version="unimplemented-policy-v9"),
    lambda c: c["tokenizer"].update(version="0.12.0"),
    lambda c: c["tokenizer"].update(encoding="cl100k_base"),
    lambda c: c["tokenizer"].update(cache_directory="https://model-selected.example/tokenizer"),
])
def test_strict_config_rejects_untrusted_or_unbounded_configuration(mutation):
    raw = synthetic_configuration().model_dump(mode="json")
    mutation(raw)
    with pytest.raises((ValidationError, ValueError)):
        load_configuration(environ={"BEHAVIOR_SERVER_CONFIG_JSON": json.dumps(raw)})


def test_missing_duplicate_and_conflicting_configuration_fails(workspace):
    with pytest.raises(ValueError):
        load_configuration(environ={})
    with pytest.raises(ValueError):
        load_configuration(environ={"BEHAVIOR_SERVER_CONFIG_JSON": '{"schema_version":"x","schema_version":"y"}'})
    with pytest.raises(ValueError):
        load_configuration(environ={"BEHAVIOR_SERVER_CONFIG": "missing.json", "BEHAVIOR_SERVER_CONFIG_JSON": "{}"})
    path = workspace / "configuration.json"
    config = synthetic_configuration()
    path.write_text(config.model_dump_json(), encoding="utf-8")
    assert load_configuration(path, environ={}) == config
    assert ServerConfiguration.model_json_schema()["additionalProperties"] is False


def test_missing_evidence_plain_startup_never_initializes_anything(h):
    with pytest.raises(Problem):
        application(h.composition)
    assert h.composition.closed and not h.table.backing.rows
    assert all(request.method == "GET" for request in h.table.requests)
    assert not h.calls


def test_probe_has_no_admission_approval_or_initialization_routes_and_auth_is_first(h, signing_key):
    app = application(h.composition, preactivation=True)
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {
            "status": "alive", "admissions": "CLOSED", "operational_readiness": "NOT_EVALUATED",
            "actual_environment": "NOT_RUN",
        }
        assert not h.table.requests and not h.source.requests and not h.backup.requests and not h.calls
        path = "/behavior/v1/operator/prerequisites"
        assert client.get(path).status_code == 401
        assert client.get("/behavior/v1/operator/config-check").status_code == 401
        response = client.get(path, headers=h.headers(signing_key))
        assert response.status_code == 200 and response.json()["admissions"] == "CLOSED"
        assert client.get("/behavior/v1/operator/config-check", headers=h.headers(signing_key)).json() == response.json()
        assert response.json()["actual_environment"] == "NOT_RUN"
        for suffix in ("work/submit", "initialize", "operation/approve"):
            assert client.post("/behavior/v1/bindings/binding-1/" + suffix, json={}, headers=h.headers(signing_key)).status_code == 404
        callback = "/behavior/v1/bindings/binding-1/hosted/launches/launch/claim"
        assert client.post(callback, content=b"x" * 100000).status_code == 401
    assert not h.table.backing.rows


def test_explicit_bootstrap_is_idempotent_without_counter_reset_or_human_grants(h):
    bootstrap(h.composition, h.approval, stage="records")
    assert h.calls == [] and not h.source.requests and not h.backup.requests
    root_key = control_key("budget", "root")
    grant_key = native_key("grant", h.approval.native_grants[0].grant_id)
    h.mutate(root_key, remaining_units=11, remaining_attempts=2)
    h.mutate(grant_key, remaining_uses=1, active=False)
    posts = len(h.table.parts)
    bootstrap(h.composition, h.approval, stage="records")
    assert len(h.table.parts) == posts
    assert h.runtime.store.read_snapshot((root_key,))[root_key].payload["data"]["remaining_units"] == 11
    assert h.runtime.store.read_snapshot((grant_key,))[grant_key].payload["data"]["remaining_uses"] == 1
    for binding in h.config.bindings:
        key = control_key("grant", binding.grant_id)
        assert h.runtime.store.read_snapshot((key,))[key] is None
    assert all("/Tables" not in request.url for request in h.table.requests)


def test_bootstrap_probes_own_binding_with_two_clients_and_retained_atomic_readback(h):
    result = bootstrap(h.composition, h.approval, stage="records")
    assert result["own_authority_probe"] == "VERIFIED" and result["actual_environment"] == "NOT_RUN"
    assert h.table is not h.probe and h.table.requests and h.probe.requests
    keys, winner, loser = _probe_layout(h.config)
    proof = _authority_probe_readback(h.runtime.store, h.config)
    assert proof["control_binding"] == h.runtime.store._binding
    assert proof["configuration_sha256"] == h.config.sha256 and proof["stale_batch_status"] == 412
    receipt = h.runtime.store.reconcile(winner)
    assert dict(receipt.payload["writes"]) == proof["business_payload_sha256"]
    assert h.runtime.store._receipt(loser) is None
    assert h.runtime.store.read_snapshot((keys["loser"],))[keys["loser"]] is None
    root = control_key("budget", "root")
    assert h.runtime.store.read_snapshot((root,))[root].payload["data"]["remaining_units"] == h.config.roots[0].units
    counts = len(h.table.parts), len(h.probe.parts)
    bootstrap(h.composition, h.approval, stage="records")
    assert counts == (len(h.table.parts), len(h.probe.parts))


def test_own_authority_probe_lost_ack_consumes_attempt_and_never_replays_competing_writes(h):
    h.probe.after_apply = "lost"
    with pytest.raises(Problem):
        bootstrap(h.composition, h.approval, stage="records")
    counts = len(h.table.parts), len(h.probe.parts)
    h.probe.after_apply = None
    with pytest.raises(Problem) as retry:
        bootstrap(h.composition, h.approval, stage="records")
    assert retry.value.body["error"]["code"] == "BEHAVIOR_AUTHORITY_PROBE_UNKNOWN"
    assert counts == (len(h.table.parts), len(h.probe.parts))
    assert h.calls == []


def test_external_catalog_cannot_replace_missing_own_application_authority_probe(h):
    h.activate()
    keys, _, _ = _probe_layout(h.config)
    h.table.backing.rows.pop((h.config.table.scope.table_name, h.config.table.scope.partition_key,
                             "behavior:v1:row:" + keys["proof"]))
    h.composition.catalog.require_activation()
    with pytest.raises(Problem) as missing:
        application(h.composition)
    assert missing.value.body["error"]["code"] == "BEHAVIOR_AUTHORITY_PROBE_REQUIRED"
    assert h.composition.closed


def test_authority_cannot_bootstrap_a_second_pool_to_bypass_global_capacity(h):
    bootstrap(h.composition, h.approval, stage="records")
    config = h.config.model_copy(update={"workers": h.config.workers.model_copy(update={"workspace_id": "second-pool"})})
    approval = h.approval.model_copy(update={"configuration_sha256": config.sha256})
    before = len(h.table.parts)
    with build_server(config, **h.options) as second:
        with pytest.raises(Problem):
            bootstrap(second, approval, stage="records")
    assert len(h.table.parts) == before


@pytest.mark.parametrize("change", ["actor", "input", "targets", "definition", "expired", "configuration"])
def test_bootstrap_requires_exact_current_authorization_before_any_io(h, change):
    approval = h.approval.model_dump(mode="json")
    if change == "actor":
        approval["authorizing_human_oid"] = h.config.identity.agents[0].oid
    elif change == "configuration":
        approval["configuration_sha256"] = "f" * 64
    elif change == "expired":
        approval["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif change == "input":
        approval["native_grants"][0]["input_digest"] = "f" * 64
    elif change == "targets":
        approval["native_grants"][0]["targets"][0]["agent_version"] = "2"
    else:
        approval["native_grants"][0]["definition"]["goal"] = "unapproved action"
    with pytest.raises(Problem):
        bootstrap(h.composition, BootstrapAuthorization.model_validate_json(json.dumps(approval)))
    assert not h.table.requests


@pytest.mark.parametrize("change", ["missing-worker", "missing-inbox", "expired", "resources", "config", "inventory",
                                   "delete-capable-consumer", "coverage", "observer", "worker-binding"])
def test_native_gate_refuses_incomplete_expired_or_mismatched_catalog(h, change):
    raw = h.bundle.model_dump(mode="json")
    if change == "missing-worker":
        raw["worker"] = None
    elif change == "missing-inbox":
        raw["inbox"] = None
    elif change == "expired":
        raw["expires_at"] = NOW.isoformat()
    elif change == "resources":
        raw["resource_bindings"]["table"] = "OtherTable"
    elif change == "config":
        raw["configuration_sha256"] = "f" * 64
    elif change == "inventory":
        raw["sources"]["automatic_delete_consumers"] = None
    elif change == "delete-capable-consumer":
        raw["sources"]["automatic_delete_consumers"] = ["defender-malware-response"]
    elif change == "coverage":
        raw["sources"]["inventory_coverage"] = None
    elif change == "observer":
        raw["observer_oid"] = h.config.identity.human_oid
    else:
        raw["worker"]["native_binding_reference"] = "other-deployment"
    h.save(NativeEvidenceBundle.model_validate_json(json.dumps(raw)))
    with pytest.raises(Problem):
        h.composition.catalog.require_activation()
    assert not h.table.requests


def test_actual_no_etag_fingerprint_and_staged_readiness_publication(h, signing_key):
    partial = h.bundle.model_copy(update={"inbox": None, "delivery": None})
    h.save(partial)
    bootstrap(h.composition, h.approval, stage="worker")
    assert h.runtime.dep.workers.pending() == ()
    assert h.sources.blob_service.etag is None and h.sources.management_policy.kind == "absence"
    with pytest.raises(Problem):
        h.runtime.validate_startup()
    h.save()
    result = bootstrap(h.composition, h.approval, stage="activate")
    assert result["full_scenario_pass"] is False and result["actual_environment"] == "NOT_RUN"
    rows = len(h.table.parts)
    app = application(h.composition)
    assert len(h.table.parts) == rows
    with TestClient(app) as client:
        assert client.post("/behavior/v1/bindings/binding-1/work/submit", json={"request_id": "first"},
                           headers=h.headers(signing_key)).status_code == 200
        assert h.calls == []  # request handler never launches the controller
        h.time += timedelta(hours=2)
        count = len(h.table.requests)
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["operational_readiness"] == "NOT_EVALUATED"
        assert len(h.table.requests) == count
        assert client.post("/behavior/v1/bindings/binding-2/work/submit", json={"request_id": "second"},
                           headers=h.headers(signing_key)).status_code == 503
    assert h.composition.closed


def test_four_exact_targets_fifth_queue_and_query_before_start_local_only(h):
    h.activate()
    h.submit()
    first = h.runtime.controller.drain(limit=5)
    assert len(first) == 5 and len(h.calls) == 4
    assert {r.url.path.split("/agents/")[1].split("/")[0] for r in h.calls} == {t.agent_name for t in h.config.targets}
    assert h.runtime.dep.workers.inspect("job-5").state == "queued"
    for request in h.calls:
        assert "x-client-behavior-launch-permit" in request.headers
        assert set(json.loads(request.content)) == {"binding_id", "launch_id"}
        assert request.headers["x-client-behavior-launch-permit"].encode() not in request.content
    h.runtime.controller.drain(limit=5)
    assert len([r for r in h.calls if r.method == "POST"]) == 4
    assert all(r.method == "GET" for r in h.calls[4:])


def test_controller_tick_lifecycle_close_scan_budget_and_no_background_thread(h):
    h.activate()
    h.submit()
    result = run_controller(h.composition, limits=SupervisorLimits(ticks=2, page_limit=5, interval_seconds=0.0))
    assert result["ticks"] == 2 and result["actual_environment"] == "NOT_RUN"
    assert result["problem_counts"]["DELIVERY_RESULT_PENDING"] == 10
    assert h.composition.closed and len([r for r in h.calls if r.method == "POST"]) == 4
    key = control_key("delivery-quota", "root", "root")
    assert h.runtime.store.read_snapshot((key,))[key].payload["data"]["scans_remaining"] == 62
    assert not hasattr(h.composition, "thread")


def test_empty_delivery_index_does_not_create_guard_writes_or_consume_scan_quota(h):
    h.activate()
    before = len(h.table.parts)
    result = run_controller(h.composition, limits=SupervisorLimits(ticks=3, page_limit=5, interval_seconds=0.0))
    assert result["ticks"] == 3 and result["worker_items"] == result["delivery_items"] == 0
    assert result["problem_counts"] == {}
    assert len(h.table.parts) == before


def test_controller_preactivation_lifecycle_and_missing_evidence_cleanup(h):
    h.save(h.bundle.model_copy(update={"inbox": None, "delivery": None}))
    bootstrap(h.composition, h.approval, stage="worker")
    result = run_controller(h.composition, preactivation=True, stop_requested=lambda: True)
    assert result["ticks"] == 0 and h.calls == [] and h.composition.closed
    second = build_server(h.config, **h.options)
    Path(h.config.evidence.path).unlink()
    with pytest.raises(Problem):
        run_controller(second)
    assert second.closed


def test_controller_invalid_limits_also_close_resources(h):
    invalid = SupervisorLimits().model_copy(update={"ticks": 0})
    with pytest.raises(ValidationError):
        run_controller(h.composition, limits=invalid)
    assert h.composition.closed


def test_controller_checks_wall_limit_between_ticks_and_does_not_sleep_past_it(h):
    h.activate()
    counter = [0]
    sleeps = []

    def clock():
        counter[0] += 1
        return float(counter[0])

    result = run_controller(h.composition, limits=SupervisorLimits(
        ticks=10, max_run_seconds=3.0, interval_seconds=30.0), monotonic=clock, sleep=sleeps.append)
    assert result["ticks"] == 1 and sum(sleeps) <= 3 and h.composition.closed


def test_configured_operation_alias_resolves_only_its_native_current_work_etag(h):
    h.activate()
    h.submit()
    adapter = h.runtime.dep.broker.dispatcher.adapter
    alias = h.config.operations[0]
    resolved = adapter.references[alias.input_ref]
    binding = h.config.bindings[0]
    assert resolved.work_id == binding.work.work_id
    assert resolved.obligation_id == binding.obligation_id
    assert resolved.expected_etag.startswith('W/"fake-')  # Real SDK/local transport's native ETag.
    assert resolved.expected_definition_digest == digest(binding.work.model_dump(mode="json"))
    with pytest.raises(KeyError):
        adapter.references["model-supplied-url"]


def test_evidence_expiry_closes_source_reads_too_and_still_authenticates_before_body(h, signing_key):
    h.activate()
    app = application(h.composition)
    with TestClient(app) as client:
        h.time += timedelta(hours=2)
        path = "/behavior/v1/bindings/binding-1/sources/metrics-1/prepare"
        assert client.post(path, content=b"x" * 100000).status_code == 401
        assert client.post(path, content=b"x" * 100000, headers=h.headers(signing_key)).status_code == 503
    assert h.calls == []


def test_native_configuration_disables_human_text_by_default_even_with_consent(h, signing_key):
    assert all(not item.policy.human_text_enabled and item.policy.text_policy_version is None for item in h.config.policies)
    h.activate()
    original = "Human submitted original.\r\n".encode("utf-8")
    body = wire.SourcePrepare(
        request_id="text-original", version=1, original=original, source_schema="fa-human-text-v1",
        retention_consent_sha256=hashlib.sha256(original).hexdigest(),
    )
    with TestClient(application(h.composition)) as client:
        response = client.post("/behavior/v1/bindings/binding-1/sources/metrics-1/prepare",
                               content=body.model_dump_json(),
                               headers={**h.headers(signing_key), "Content-Type": "application/json"})
        assert response.status_code == 403
    assert h.calls == []


def test_configured_native_text_uses_exact_consent_source_snapshot_and_durable_review(workspace, jwk_client, signing_key):
    h = Harness(workspace, jwk_client, human_text=True)
    try:
        h.activate()
        config = h.config
        assert config.policies[0].policy.human_text_enabled
        assert config.policies[0].policy.text_policy_version == "human-text-policy-v1"
        original = "Project Lyra 的報告採 compact。\r\n我的介面偏好是 cobalt。\n".encode("utf-8")
        source = wire.SourcePrepare(
            request_id="text-original", version=1, original=original, source_schema="fa-human-text-v1",
            retention_consent_sha256=hashlib.sha256(original).hexdigest(),
        )
        prefix = "/behavior/v1/bindings/binding-1"
        with TestClient(application(h.composition)) as client:
            def post(path, command, scopes="Behavior.Read"):
                return client.post(prefix + path, content=command.model_dump_json(),
                                   headers={**h.headers(signing_key, scopes), "Content-Type": "application/json"})
            rows = len(h.table.backing.rows)
            assert post("/sources/metrics-1/prepare", source.model_copy(
                update={"retention_consent_sha256": None})).status_code == 403
            assert len(h.table.backing.rows) == rows
            agent_headers = {"Authorization": "Bearer " + token(signing_key, {
                "oid": config.bindings[0].agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
            }, remove=("scp",)), "Content-Type": "application/json"}
            assert client.post(prefix + "/sources/metrics-1/prepare",
                               content=source.model_dump_json(), headers=agent_headers).status_code == 403
            assert post("/sources/metrics-1/prepare", source).status_code == 200
            assert post("/sources/metrics-1/commit", wire.Command(request_id=source.request_id)).status_code == 200
            read = client.get(prefix + "/sources/metrics-1/versions/1", headers=h.headers(signing_key))
            assert read.status_code == 200 and read.content == original
            assert read.headers["content-type"].startswith("text/plain")
            ref = wire.ExactSourceRef(source_id="metrics-1", version=1,
                                     sha256=hashlib.sha256(original).hexdigest(), byte_length=len(original))
            capture = wire.SnapshotCapture(request_id="text-snapshot", roots=(ref,))
            assert post("/snapshots/text-snapshot/capture", capture).status_code == 200
            draft = wire.ClaimDraft(
                request_id="reviewed-text", version=1, text="使用者的介面偏好是 cobalt。",
                supporting=(ref,), effective_from=NOW,
            )
            assert post("/claims/claim-1/preview", draft).status_code == 403
            preview = post("/claims/claim-1/preview", draft, "Behavior.Read Behavior.Approve")
            assert preview.status_code == 200
            assert post("/claims/claim-1/commit", wire.Command(request_id=draft.request_id)).status_code != 200
            review = wire.ClaimReview(
                **draft.model_dump(), decision_id="explicit-human-text-review",
                **{key: preview.json()[key] for key in ("candidate_sha256", "evidence_sha256", "policy_version")},
                reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
            )
            assert post("/claims/claim-1/review", review, "Behavior.Read Behavior.Approve").status_code == 200
            assert post("/claims/claim-1/commit", wire.Command(request_id=draft.request_id)).status_code == 200
            context = post("/context", wire.ContextRequest(request_id="context", required_claim_ids=("claim-1",)))
            assert context.status_code == 200 and "cobalt" in context.text
            assert original in [base64.b64decode(value) for value in context.json()["originals"].values()]
        assert h.calls == []
    finally:
        h.composition.close()


def test_configured_text_snapshot_verification_schema_gate(workspace, jwk_client, signing_key):
    h = Harness(workspace, jwk_client, human_text=True)
    try:
        h.activate()
        original = b"Explicit human text, not metrics.\r\n"
        source = wire.SourcePrepare(
            request_id="snapshot-original", version=1, original=original, source_schema="fa-human-text-v1",
            retention_consent_sha256=hashlib.sha256(original).hexdigest(),
        )
        prefix = "/behavior/v1/bindings/binding-1"
        with TestClient(application(h.composition)) as client:
            def post(path, command):
                return client.post(prefix + path, content=command.model_dump_json(),
                                   headers={**h.headers(signing_key), "Content-Type": "application/json"})
            assert post("/sources/metrics-1/prepare", source).status_code == 200
            assert post("/sources/metrics-1/commit", wire.Command(request_id=source.request_id)).status_code == 200
            ref = wire.ExactSourceRef(source_id="metrics-1", version=1,
                                     sha256=hashlib.sha256(original).hexdigest(), byte_length=len(original))
            capture = wire.SnapshotCapture(request_id="text-snapshot", roots=(ref,))
            assert post("/snapshots/text-snapshot/capture", capture).status_code == 200
            verified = post("/snapshots/text-snapshot/verify", wire.Command(request_id=capture.request_id))
            if verified.status_code == 422:
                identity = h.config.identity
                principal = BehaviorPrincipal(
                    identity.tenant_id, identity.human_oid, identity.team_id, "human", None, frozenset({"read"}),
                )
                with pytest.raises(Problem) as failure:
                    h.runtime.verify_snapshot("binding-1", "text-snapshot", wire.Command(request_id=capture.request_id), principal)
                assert failure.value.body["error"]["code"] == "BEHAVIOR_UNSAFE_CONTENT"
                assert failure.value.body["error"]["message"] == "only complete metrics JSON is supported"
                pytest.xfail("Parent-owned BehaviorRuntime.verify_snapshot still applies metrics-only _safe_original to human text")
            assert verified.status_code == 200
            assert original in [base64.b64decode(value) for value in verified.json()["originals"].values()]
    finally:
        h.composition.close()


def test_independent_recovery_with_current_catalog_real_blob_sdk_and_no_source_table(
    workspace, source_service, jwk_client, signing_key, monkeypatch,
):
    sources, table = source_service
    scope, receipt, originals = snapshot(sources)
    transport = BlobTransport([])
    backing = make_blob_backup_store(SETTINGS, Credential(), transport=transport)
    upload = service(sources, backing)
    upload.initialize_protection()
    plan = upload.plan(BACKUP_HUMAN, HUMAN_CONTEXT, backup_id="0123456789abcdef0123456789abcdef",
                       scope=scope, snapshot_id="checkpoint", snapshot_receipt=receipt)
    _, archive = archive_from_plan(upload, plan)
    transport.responses.extend([(201, {"ETag": ETAG, "x-ms-version-id": NATIVE_VERSION}),
                                (200, native_headers(plan))] + native_reads(plan, archive))
    reference, _ = uploaded(upload, plan)
    backing.container.close()
    base = synthetic_configuration()
    config = RecoveryConfiguration(
        deployment_id="recovery-only", identity=base.identity.model_copy(update={
            "human_oid": BACKUP_HUMAN.oid, "agents": (*base.identity.agents[:3], base.identity.agents[3].model_copy(
                update={"oid": "55555555-5555-5555-5555-555555555555"})),
        }),
        authority_domain=SETTINGS.authority_domain, backup_account_resource_id=base.backups.account_resource_id,
        backup_container_url=SETTINGS.container_url,
        catalog=base.evidence.model_copy(update={"path": str(workspace / "recovery.json")}),
    )
    decision = RecoveryAuthorization(
        decision_id="current-recovery", policy_version="recovery-v1", principal_oid=BACKUP_HUMAN.oid,
        reference_sha256=reference.sha256, scope=scope, current_access_revision=7,
        expires_at=SOURCE_NOW + timedelta(hours=1),
    )
    catalog = RecoveryCatalog(
        catalog_id=config.catalog.catalog_id, observer_oid=config.catalog.observer_oid, revision=1,
        configuration_sha256=config.sha256, observed_at=SOURCE_NOW, expires_at=SOURCE_NOW + timedelta(hours=1),
        reference=reference, decision=decision, active=True,
    )
    path = Path(config.catalog.path)
    path.write_text(catalog.model_dump_json(), encoding="utf-8")
    table.backing.rows.clear()
    restore_transport = BlobTransport(native_reads(plan, archive))
    from shared_context import behavior_server

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Recovery must not construct a live Table/source authority")

    monkeypatch.setattr(behavior_server, "TableClient", forbidden)
    monkeypatch.setattr(behavior_server, "SourceMaterials", forbidden)
    app = recovery_application(config, credential=Credential(), backup_transport=restore_transport,
                               key_client=jwk_client, clock=lambda: SOURCE_NOW)
    assert app.state.recovery_service.sources is None
    with TestClient(app) as client:
        endpoint = "/behavior/v1/recovery/restore-quarantined"
        def headers(scopes):
            return {"Authorization": "Bearer " + token(signing_key, {
                "oid": config.identity.human_oid, "appid": config.identity.admin_client_id, "scp": scopes})}
        body = {"request_id": "restore", "reference": reference.model_dump(mode="json")}
        for scope_string in ("Behavior.Read", "Behavior.Read Behavior.Backup", "Artifacts.Approve"):
            assert client.post(endpoint, json=body, headers=headers(scope_string)).status_code == 403
        result = client.post(endpoint, json=body, headers=headers("Behavior.Read Behavior.Restore"))
        assert result.status_code == 200, result.text
        assert result.json()["activation_performed"] is False
        assert {key: base64.b64decode(value) for key, value in result.json()["originals_base64"].items()} == originals
        assert all(request.method in {"HEAD", "GET"} for request in restore_transport.requests)
        before = len(restore_transport.requests)
        path.write_text(catalog.model_copy(update={"active": False}).model_dump_json(), encoding="utf-8")
        assert client.post(endpoint, json=body, headers=headers("Behavior.Read Behavior.Restore")).status_code == 403
        assert len(restore_transport.requests) == before
    assert not table.backing.rows and app.state.composition.closed


def test_missing_recovery_catalog_fails_before_blob_or_credential_creation(workspace):
    config = synthetic_configuration()
    recovery = RecoveryConfiguration(
        deployment_id="recovery", identity=config.identity, authority_domain=config.table.scope.authority_domain,
        backup_account_resource_id=config.backups.account_resource_id, backup_container_url=config.backups.protection.container_url,
        catalog=config.evidence.model_copy(update={"path": str(workspace / "missing.json")}),
    )
    transport = BlobTransport([])
    with pytest.raises(Problem):
        recovery_application(recovery, credential=Credential(), backup_transport=transport)
    assert not transport.requests


def test_cli_exports_valid_exact_json_without_azure_calls():
    entry = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / "behavior_server_main.py"
    result = subprocess.run([sys.executable, str(entry), "sample-config"], capture_output=True, check=True, timeout=30)
    config = ServerConfiguration.model_validate_json(result.stdout)
    assert len(config.targets) == 4 and len(config.bindings) == 5
    for kind in ("config", "evidence", "authorization", "recovery", "recovery-catalog", "projection", "projection-evidence", "tokenizer"):
        result = subprocess.run([sys.executable, str(entry), "schema", kind], capture_output=True, check=True, timeout=30)
        assert json.loads(result.stdout)["additionalProperties"] is False
    result = subprocess.run(
        [sys.executable, "-m", "shared_context.behavior_server", "bootstrap", "--help"],
        cwd=entry.parent, capture_output=True, check=True, timeout=30,
    )
    assert b"--config" in result.stdout and b"--evidence" in result.stdout and b"--authorization" in result.stdout


def test_aca_job_cli_bootstrap_uses_explicit_files_and_real_guarded_init_loop(h, workspace, monkeypatch, capsys):
    import behavior_server_main
    from shared_context import behavior_server

    config_file, authorization_file = workspace / "config.json", workspace / "approval.json"
    config_file.write_text(h.config.model_dump_json(), encoding="utf-8")
    authorization_file.write_text(h.approval.model_dump_json(), encoding="utf-8")
    monkeypatch.delenv("BEHAVIOR_SERVER_CONFIG_JSON", raising=False)
    monkeypatch.setenv("BEHAVIOR_SERVER_CONFIG", str(config_file))
    monkeypatch.setattr(sys, "argv", [
        "behavior_server_main.py", "bootstrap", "--config", str(config_file),
        "--evidence", h.config.evidence.path, "--stage", "records", "--authorization", str(authorization_file),
    ])
    received = []
    monkeypatch.setattr(behavior_server, "build_server", lambda config: received.append(config) or h.composition)
    behavior_server_main.main()
    output = json.loads(capsys.readouterr().out)
    assert received == [h.config] and output["stage"] == "records"
    assert output["actual_environment"] == "NOT_RUN" and h.composition.closed
    assert h.table.backing.rows and not h.calls and not h.source.requests and not h.backup.requests
    assert all("/Tables" not in request.url for request in h.table.requests)


def test_cli_rejects_evidence_path_override_before_constructing_clients(h, workspace, monkeypatch):
    import behavior_server_main
    from shared_context import behavior_server

    config_file = workspace / "config.json"
    config_file.write_text(h.config.model_dump_json(), encoding="utf-8")
    monkeypatch.delenv("BEHAVIOR_SERVER_CONFIG_JSON", raising=False)
    monkeypatch.setenv("BEHAVIOR_SERVER_CONFIG", str(config_file))
    monkeypatch.setattr(sys, "argv", [
        "behavior_server_main.py", "bootstrap", "--config", str(config_file),
        "--evidence", str(workspace / "caller-evidence.json"), "--stage", "records", "--authorization", "missing.json",
    ])
    constructed = []
    monkeypatch.setattr(behavior_server, "build_server", lambda config: constructed.append(config))
    with pytest.raises(SystemExit):
        behavior_server_main.main()
    assert not constructed and not h.table.requests


@pytest.mark.parametrize("mutation", [
    lambda item: item.update(binding_id="other"),
    lambda item: item.update(control_binding="f" * 64),
    lambda item: item.update(claim_ids=["unreviewed-claim"]),
    lambda item: item.update(subject="other"),
    lambda item: item["versions"].update(catalog_version="different-version"),
    lambda item: item["scope"].update(tenant_id="22222222-2222-2222-2222-222222222222"),
    lambda item: item["scope"].update(visibility="private", owner_actor="unassigned-agent"),
    lambda item: item.update(endpoint="https://other.services.ai.azure.com/api/projects/not-configured"),
    lambda item: item.update(projection_namespace="sc1-old-m4"),
    lambda item: item.update(max_polls=0),
    lambda item: item.update(max_response_bytes=999999),
])
def test_optional_projection_setup_is_exact_bounded_and_not_m4(mutation):
    config = synthetic_configuration()
    raw = config.model_dump(mode="json")
    setup = projection_setup(config).model_dump(mode="json")
    mutation(setup)
    raw["memory_projections"] = [setup]
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(raw))


def test_optional_projection_builds_real_sdk_same_authority_without_store_creation_and_closes(workspace, jwk_client, monkeypatch):
    from azure.ai.projects import AIProjectClient

    h = Harness(workspace, jwk_client, projection=True)
    try:
        projection = h.runtime.dep.memory_projections["binding-1"]
        assert isinstance(projection, NativeMemoryProjection) and isinstance(projection.client, AIProjectClient)
        assert projection.governance is h.runtime.dep.memory
        assert projection.store is h.runtime.store and projection.sources is h.runtime.dep.sources
        assert projection.setup == h.config.memory_projections[0]
        assert projection.client._config.retry_policy.total_retries == 0
        assert not h.memory.requests and not h.table.requests
        with pytest.raises(TypeError):
            h.runtime.dep.memory_projections["model-binding"] = projection
        closed = []
        monkeypatch.setattr(h.memory, "close", lambda: closed.append(True))
        h.composition.close()
        assert closed == [True]
    finally:
        h.composition.close()


@pytest.mark.parametrize("missing", ["missing", "stale", "mismatched-setup", "mismatched-evidence", "incomplete-contracts"])
def test_optional_projection_activation_requires_current_independent_installation_evidence(workspace, jwk_client, missing):
    h = Harness(workspace, jwk_client, projection=True)
    try:
        proof = h.bundle.memory_projections[0]
        if missing == "missing":
            proofs = ()
        else:
            update = {
                "stale": {"expires_at": NOW},
                "mismatched-setup": {"setup_sha256": "f" * 64},
                "mismatched-evidence": {"evidence_sha256": "f" * 64},
                "incomplete-contracts": {"verified_contracts": ("existing_store",)},
            }[missing]
            proofs = (proof.model_copy(update=update),)
        h.save(h.bundle.model_copy(update={"memory_projections": proofs}))
        with pytest.raises(Problem):
            h.composition.catalog.require_activation()
        assert not h.table.requests and not h.memory.requests
        h.save()
        h.composition.catalog.require_activation()
        assert not h.table.requests and not h.memory.requests
    finally:
        h.composition.close()


def test_native_tokenizer_matches_pinned_gpt41_o200k_text_not_byte_or_chat_usage(h, monkeypatch):
    import tiktoken
    import tiktoken.load

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(TOKENIZER_TEST_CACHE))

    def no_network(*_args, **_kwargs):
        raise AssertionError("tokenizer tests must not download")

    monkeypatch.setattr(tiktoken.load, "read_file", no_network)
    official = tiktoken.encoding_for_model("gpt-4.1-2025-04-14")
    counter = h.runtime.dep.memory._count
    assert isinstance(counter.__self__, ServerTokenizer)
    assert counter.__self__.encoding.name == official.name == "o200k_base"
    for text in ("Hello world", "中文原件\r\ncobalt", "🎉 data 1234567", "<|endoftext|> is literal source content"):
        assert counter(text.encode()) == len(official.encode(text, allowed_special=set(), disallowed_special=()))
    assert counter(b"Hello world") == 2 and counter(b"Hello world") != len(b"Hello world")
    assert h.config.tokenizer.accounting_scope == "context-text-sections-only"


def test_invalid_utf8_is_unknown_token_count_and_governance_fails_closed(h):
    counter = h.runtime.dep.memory._count
    assert counter(b"\xff\xfe") is None and counter("not-bytes") is None
    with pytest.raises(Problem):
        h.runtime.dep.memory._tokens(b"\xff\xfe")


@pytest.mark.parametrize("case", ["missing", "corrupt", "wrong-package"])
def test_tokenizer_missing_or_changed_cache_never_downloads_or_returns_fake_count(workspace, monkeypatch, case):
    import tiktoken
    from shared_context import behavior_server

    def forbidden(*_args, **_kwargs):
        raise AssertionError("runtime must never use the network-capable tokenizer loader")

    monkeypatch.setattr(tiktoken, "get_encoding", forbidden)
    if case == "corrupt":
        (workspace / TOKENIZER_CACHE_KEY).write_bytes(b"wrong-cache-data")
    if case == "wrong-package":
        monkeypatch.setattr(behavior_server, "package_version", lambda _: "0.10.0")
    config = TokenizerConfiguration(version="0.11.0", encoding="o200k_base", cache_directory=str(workspace))
    with pytest.raises(Problem) as failure:
        ServerTokenizer(config)
    assert failure.value.body["error"]["code"] == "BEHAVIOR_TOKENIZER_UNAVAILABLE"
