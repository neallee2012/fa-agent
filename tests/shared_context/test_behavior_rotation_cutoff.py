"""Offline native-mode producers across two committed rotation cutoffs."""

from dataclasses import replace
from datetime import timedelta
import copy
import json
import os
from types import SimpleNamespace

import pytest

from shared_context import behavior_workers as w
from shared_context import behavior_delivery as d, behavior_inbox as inbox, behavior_foundry as native
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_operations import control_key
from shared_context.behavior_assistant_wire import Task
from shared_context.behavior_runtime_rotation import RotationHistory, rotation_key, _snapshot_store
from shared_context.behavior_runtime_rotation_history import historical_shared_row
from shared_context.behavior_runtime_transition import _payload_hash
from shared_context.records import Record
from shared_context.source_materials import digest
from shared_context.errors import Problem
from test_behavior_rotation_settlement_history import current_cancelled, next_rotation, settle
from test_behavior_runtime_rotation import rotated
from test_m3_auth import jwk_client, signing_key


def publish_worker(store, config, now, evidence_id):
    worker = w.WorkerCore(store, config.workers, adapter=None, clock=lambda: now)
    proof = w.WorkerReadiness(
        evidence_id=evidence_id, configuration_digest=worker.configuration_digest(),
        control_binding=config.workers.control_binding, protection_version=config.workers.protection_version,
        executor_id=config.workers.executor_id, executor_version=config.workers.executor_version,
        native_binding_reference="offline-native-binding", supervisor_reference="offline-supervisor",
        protection_reference="offline-protection", observed_at=now, expires_at=now + timedelta(minutes=10),
        proof=b"OFFLINE independent readiness observation, no provider calls",
    )
    def forbidden(*args):
        pytest.fail("readiness test cannot dispatch an executor")
    worker.adapter = w.ExecutorAdapter(forbidden, forbidden, forbidden, forbidden,
                                        verify_readiness=lambda _: proof)
    worker.publish_readiness(evidence_id)
    return worker, proof


def publish_inbox(store, config, now, worker, evidence_id):
    def forbidden(*args):
        pytest.fail("readiness test cannot invoke an external service")
    channel = inbox.PullInbox(store, inbox.InboxRegistration(config.authentication_version, forbidden, forbidden))
    core = d.DeliveryCore(store, clock=lambda: now, registration=d.RuntimeRegistration(
        config.delivery, verify_work_result=forbidden, channel_adapter=channel))
    channel.attach(core)
    ready = d.ReadinessEvidence(
        evidence_id=evidence_id + "-delivery", configuration_digest=core.configuration_digest(),
        control_binding=config.delivery.control_binding, protection_version=config.delivery.protection_version,
        consumer_version=config.delivery.consumer_version, timer_version=config.delivery.timer_version,
        coverage=("atomic_control", "inline_retention", "durable_consumer", "timer_redrive", "bounded_scan",
                  "ownership_recovery"),
        observed_at=now, expires_at=now + timedelta(minutes=10), proof=b"OFFLINE deployment witness")
    proof = inbox.InboxDeploymentEvidence(
        evidence_id=evidence_id, configuration_digest=ready.configuration_digest,
        control_binding=config.delivery.control_binding, authentication_version=config.authentication_version,
        protection_scope=config.table.scope.partition_key, protection_evidence_id=config.sources.protection.generation,
        worker_evidence_id=worker.evidence_id, worker_workspace_id=config.workers.workspace_id,
        worker_configuration_digest=worker.configuration_digest, readiness=ready, proof=b"OFFLINE inbox witness")
    channel.registration = replace(channel.registration, verify_deployment=lambda _: proof)
    channel.publish_readiness(proof.evidence_id)
    return core, channel, proof


@pytest.fixture
def two_rotations(current_cancelled):
    c = current_cancelled
    _, worker = publish_worker(c.store, c.config, c.clock[0], "before-second-cutoff")
    core, _, _ = publish_inbox(c.store, c.config, c.clock[0], worker, "inbox-before-second-cutoff")
    settle(c)
    b = c.config.bindings[0]
    d.DeliveryConsumer(core).drain(b.owner.root_budget_id, lambda _: b.owner, "offline-old-cursor", limit=1)
    service = next_rotation(c)
    before = c.store.read_inventory()
    plan = service.plan(c.principal, rotation_id="cutoff-second")
    approval = service.authorize(c.principal, plan, decision_id="cutoff-second-approved",
                                 expires_at=c.clock[0] + timedelta(minutes=5))
    service.apply(c.principal, plan, approval)
    service.verify(c.principal, plan, approval)
    return SimpleNamespace(c=c, service=service, before=before, plan=plan, approval=approval)


def test_two_rotations_then_real_readiness_publish_can_reopen_current_installation(two_rotations):
    h = two_rotations
    c, config = h.c, h.service.new
    key = w.worker_key("readiness", config.workers.workspace_id)
    assert key in h.before.rows and key not in {image.row_id for image in h.plan.images}
    store = MigrationStore(c.h.table, c.store._scope)
    installed_origin(store, config)
    original = store.read_inventory()
    publish_worker(store, config, c.clock[0] + timedelta(seconds=1), "after-second-cutoff")
    updated = store.read_inventory()
    assert updated.rows[key] != original.rows[key]
    count = len(c.h.table.submissions)
    assert installed_origin(MigrationStore(c.h.table, c.store._scope), config) == c.h.original
    assert store.read_inventory().entity_sha256 == updated.entity_sha256
    assert len(c.h.table.submissions) == count


@pytest.mark.parametrize("lane", ["inbox", "cursor", "admission-start"])
def test_other_mutable_producers_do_not_substitute_current_payloads_at_old_cutoffs(two_rotations, lane):
    h, c = two_rotations, two_rotations.c
    config, now = h.service.new, c.clock[0] + timedelta(seconds=1)
    store = MigrationStore(c.h.table, c.store._scope)
    installed_origin(store, config)
    worker, readiness = publish_worker(store, config, now, "current-worker")
    core, channel, _ = publish_inbox(store, config, now, readiness, "current-inbox")
    b = config.bindings[0]
    if lane == "cursor":
        d.DeliveryConsumer(core).drain(b.owner.root_budget_id, lambda _: b.owner, "offline-current-cursor", limit=1)
    elif lane == "admission-start":
        profile = config.assistant.profiles[0]
        task = Task(job_id=b.job_id, work_id=b.work.work_id,
                    definition_digest=digest(b.work.model_dump(mode="json")),
                    profile_digest=digest(profile.model_dump(mode="json")), source=profile.source,
                    artifact_id=profile.artifact_id, artifact_version=profile.artifact_version, operation_id=b.operation_id)
        worker.enqueue(w.JobSpec(job_id=b.job_id, work_id=b.work.work_id, input=task.model_dump_json().encode()), b.owner)
        def stopped_before_external_send(command):
            raise RuntimeError("OFFLINE stop after the real durable worker debit")
        worker.adapter = replace(worker.adapter, start=stopped_before_external_send)
        with pytest.raises(Problem) as error:
            worker.start(b.job_id, b.owner)
        assert error.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
    before = store.read_inventory()
    count = len(c.h.table.submissions)
    assert installed_origin(MigrationStore(c.h.table, c.store._scope), config) == c.h.original
    assert store.read_inventory().entity_sha256 == before.entity_sha256
    assert len(c.h.table.submissions) == count


@pytest.fixture
def runtime_rotations(monkeypatch, request):
    import test_behavior_runtime_transition as transition
    original = transition.synthetic_configuration
    def configuration():
        config = original()
        return config.model_copy(update={"tokenizer": config.tokenizer.model_copy(
            update={"cache_directory": os.environ["TIKTOKEN_CACHE_DIR"]})})
    monkeypatch.setattr(transition, "synthetic_configuration", configuration)
    return request.getfixturevalue("two_rotations")


def test_real_current_admission_native_reservation_and_model_debit_survive_both_cutoffs(runtime_rotations, jwk_client):
    import httpx
    from shared_context.behavior_server import build_server
    from shared_context.behavior_auth import BehaviorPrincipal
    from shared_context import behavior_wire as wire, behavior_assistant_wire as aw
    from shared_context.behavior_assistant import AssistantAuthority, key as assistant_key
    from shared_context.service import ContextService
    from shared_context.m3_artifacts import ArtifactService
    from shared_context.m3_store import InMemoryArtifactStore
    from shared_context.m3_auth import ApprovalSettings
    from shared_context.models import CreateWork
    from test_behavior_runtime_rotation_sources import QueryTransport
    from test_behavior_store import Credential, WireTransport
    from test_behavior_api import METRICS
    from conftest import InMemoryRepository, FakeMemory

    h = runtime_rotations
    c, config = h.c, h.service.new
    transport = QueryTransport()
    transport.backing = c.h.table.backing
    def forbidden(*args, **kwargs):
        pytest.fail("cutoff test cannot contact model, Blob or Foundry services")
    no_io = WireTransport()
    no_io.send = forbidden
    now = c.clock[0] + timedelta(seconds=1)
    with build_server(config, credential=Credential(), table_transport=transport, source_transport=no_io,
                      backup_transport=no_io, assistant_transport=httpx.MockTransport(forbidden),
                      native_transports={target.executor_id: httpx.MockTransport(forbidden) for target in config.active_targets},
                      key_client=jwk_client, clock=lambda: now) as composition:
        runtime = composition.runtime
        runtime.dep.sources._clock = lambda: now
        store = runtime.store
        installed_origin(store, config)
        def reread():
            before = store.read_inventory().entity_sha256
            posts = sum(request.method == "POST" for request in transport.requests)
            assert installed_origin(MigrationStore(composition.clients[0], config.table.scope), config) == c.h.original
            assert store.read_inventory().entity_sha256 == before
            assert sum(request.method == "POST" for request in transport.requests) == posts
        _, worker_proof = publish_worker(store, config, now, "model-worker")
        _, _, inbox_proof = publish_inbox(store, config, now, worker_proof, "model-inbox")
        runtime.dep.workers.adapter = replace(runtime.dep.workers.adapter, verify_readiness=lambda _: worker_proof)
        runtime.dep.inbox.registration = replace(runtime.dep.inbox.registration, verify_deployment=lambda _: inbox_proof)
        runtime.dep.delivery.registration = replace(runtime.dep.delivery.registration,
                                                    verify_readiness=lambda: inbox_proof.readiness)
        b, profile = config.bindings[0], config.assistant.profiles[0]
        principal = replace(c.principal, actions=c.principal.actions | {"submit"})
        prepare = wire.SourcePrepare(request_id="cutoff-source", version=1, original=METRICS)
        runtime.prepare_source(b.binding_id, profile.source.source_id, prepare, principal)
        runtime.commit_source(b.binding_id, profile.source.source_id, wire.Command(request_id=prepare.request_id), principal)
        draft = wire.ClaimDraft(request_id="cutoff-claim", version=1, text="The accepted metrics source has been reviewed.",
                               supporting=(profile.source,), effective_from=now)
        preview = runtime.preview_claim(b.binding_id, profile.claim_id, draft, principal)
        review = wire.ClaimReview(**draft.model_dump(), decision_id="cutoff-review",
                                 candidate_sha256=preview.candidate_sha256, evidence_sha256=preview.evidence_sha256,
                                 policy_version=preview.policy_version,
                                 reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"))
        runtime.review_claim(b.binding_id, profile.claim_id, review, principal)
        runtime.commit_claim(b.binding_id, profile.claim_id, wire.Command(request_id=review.request_id), principal)
        reread()
        auth = config.identity.auth()
        actor = auth.actors[b.agent_oid]
        context = ContextService(InMemoryRepository(), FakeMemory(), auth)
        context.create(actor, b.team_id, profile.legacy_source_work, CreateWork(goal="Offline metrics source"))
        artifacts = ArtifactService(context, InMemoryArtifactStore(auth.tenant_id),
                                    ApprovalSettings(config.identity.human_oid, config.identity.admin_client_id, b.team_id))
        assistant = AssistantAuthority(runtime, artifacts, (actor,))
        runtime.submit(b.binding_id, aw.AssistantSubmit(request_id="cutoff-admission"), principal)
        reread()
        budget_key = control_key("budget", b.owner.root_budget_id)
        before = store.read_snapshot((budget_key,))[budget_key].payload["data"]
        starts = []
        def reserve_without_sending(start):
            starts.append(start)
            runtime.dep.native_launches.prepare(start)
            raise RuntimeError("OFFLINE native reservation retained; no provider request")
        runtime.dep.workers.adapter = replace(runtime.dep.workers.adapter, start=reserve_without_sending)
        with pytest.raises(Problem) as error:
            runtime.dep.workers.start(b.job_id, b.owner)
        assert error.value.body["error"]["code"] == "WORKER_START_UNKNOWN" and len(starts) == 1
        reread()
        assignment, ticket = assistant.issue(starts[0])
        agent = BehaviorPrincipal(b.tenant_id, b.agent_oid, b.team_id, "agent", actor.actor,
                                  frozenset({"read", "consume", "execute"}))
        session = assistant.session(assignment.session_id, ticket, agent)
        assistant.acquire(session, wire.Command(request_id="sdk-start"))
        bodies = ({"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "offline one"}]},
                  {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "offline two"}]})
        for body in bodies:
            request_digest = digest(body)
            assistant.reserve_model(session, aw.Reservation(request_id=request_digest, request_sha256=request_digest), agent)
            reread()
        assistant.consume_model(session, aw.ModelRequest(request_id=digest(bodies[0]), body=bodies[0]), agent)
        reread()
        after = store.read_snapshot((budget_key,))[budget_key].payload["data"]
        assert after["remaining_attempts"] == before["remaining_attempts"] - 3
        assert after["remaining_units"] == before["remaining_units"] - config.workers.reservation_units - 2
        intent_key = assistant_key("model-intent", assignment.session_id, digest(bodies[1]))
        assert store.read_snapshot((intent_key,))[intent_key].payload["data"]["consumed"] is False
        assistant.context(session, wire.Command(request_id="current-context"), agent)
        assistant.save(session, aw.Save(request_id="own-result",
                       answer=aw.Answer(item_count=3, completed_count=2, remaining_count=1)), agent)
        reread()
        saved = store.read_snapshot((budget_key,))[budget_key].payload["data"]
        assert saved["remaining_attempts"] == after["remaining_attempts"] - 2
        assert saved["remaining_units"] == after["remaining_units"] - 2


@pytest.mark.parametrize("mutation", ["missing-archive", "wrong-material", "missing-receipt", "wrong-cutoff"])
def test_legacy_readiness_restoration_requires_original_archived_bytes_and_receipt(two_rotations, mutation):
    h, c = two_rotations, two_rotations.c
    config = h.service.new
    key = w.worker_key("readiness", config.workers.workspace_id)
    old = h.before.rows[key]
    old_evidence = w.worker_key("readiness-evidence", old.payload["data"]["evidence_id"])
    publish_worker(h.service.store, config, c.clock[0] + timedelta(seconds=1), "tamper-new-readiness")
    inventory = h.service.store.read_inventory()
    rows, receipts = copy.deepcopy(dict(inventory.rows)), dict(inventory.receipts)
    cutoff = h.plan.guard.revision
    if mutation == "missing-archive":
        rows.pop(old_evidence)
    elif mutation == "wrong-material":
        row = rows[old_evidence]
        rows[old_evidence] = Record({**row.payload, "material": None}, row.etag)
    elif mutation == "missing-receipt":
        receipts = {rid: row for rid, row in receipts.items() if old_evidence not in dict(row.payload["writes"])}
    else:
        cutoff = 0
    broken = replace(inventory, rows=rows, receipts=receipts)
    count = len(c.h.table.submissions)
    with pytest.raises(Problem):
        historical_shared_row(broken, config, key, _payload_hash(old.payload), cutoff)
    assert len(c.h.table.submissions) == count


@pytest.mark.parametrize("mutation", ["preimage-missing", "preimage-hash", "preimage-material", "current-hash", "unknown-row"])
def test_versioned_shared_preimages_never_hide_incomplete_or_tampered_current_state(two_rotations, mutation):
    h, c = two_rotations, two_rotations.c
    config = h.service.new
    inventory = h.service.store.read_inventory()
    key = rotation_key("history", h.plan.rotation_id)
    parsed = RotationHistory.model_validate_json(json.dumps(inventory.rows[key].payload["data"]))
    assert parsed.schema_version == "fa-runtime-rotation-history-v3" and parsed.shared_preimage_chunks
    rows, hashes = copy.deepcopy(dict(inventory.rows)), dict(inventory.entity_sha256)
    if mutation == "current-hash":
        key = w.worker_key("readiness", config.workers.workspace_id)
        payload = copy.deepcopy(rows[key].payload)
        payload["data"]["evidence_id"] = "unretained-current-pointer"
    elif mutation == "unknown-row":
        payload = {**rows[key].payload, "row_id": "unknown-cutoff-row", "data": {"state": "unknown"}}
        key = "unknown-cutoff-row"
    else:
        payload = copy.deepcopy(rows[key].payload)
        if mutation == "preimage-missing":
            payload["data"]["shared_preimage_chunks"] = []
        elif mutation == "preimage-hash":
            payload["data"]["shared_preimage_chunks"][0]["sha256"] = "f" * 64
        else:
            key = parsed.shared_preimage_chunks[0].row_id
            payload = copy.deepcopy(rows[key].payload)
            payload["material"] = None
    rows[key] = Record(payload, 'W/"tampered"')
    hashes["behavior:v1:row:" + key] = _payload_hash(payload)
    broken = replace(inventory, rows=rows, entity_sha256=hashes)
    count = len(c.h.table.submissions)
    with pytest.raises(Problem):
        installed_origin(_snapshot_store(h.service.store, broken, config), config)
    assert len(c.h.table.submissions) == count
