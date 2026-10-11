"""Original durable result survives platform-stop closure, not fake SDK exit."""
from datetime import timedelta
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant import key
from shared_context.behavior_recovery import (
    ClaimedSdkRecovery, FinishedResultRecoveryScope, ModelIntentState, PlatformStopObservation, stop_endpoint,
)
from shared_context.behavior_operations import control_key
from shared_context.errors import Problem
from test_behavior_finished_sdk_wait import finished_flow
from test_behavior_assistant_claim_boundaries import hosted_flow, reviewed
from test_behavior_assistant_acquire_conflicts import native_acquire, current_rotation
from test_behavior_rotation_cutoff import two_rotations
from test_behavior_rotation_settlement_history import current_cancelled
from test_behavior_runtime_rotation import rotated
from test_behavior_unclaimed_closure import normal_report, read_current
from test_behavior_rotation_settlement_history import next_rotation
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_runtime_rotation_history import validate_retirement
from shared_context.source_materials import digest
from shared_context.behavior_recovery import recovery_key
from shared_context.behavior_store import ControlRequest
from test_behavior_store import parse_batch
from shared_context.behavior_assistant_wire import Answer
from test_behavior_assistant_sse import stream
from test_m3_auth import jwk_client, signing_key


@pytest.fixture(autouse=True)
def sufficient_initial_budget(monkeypatch):
    import test_behavior_migration as migration
    original = migration.BudgetRecord
    def budget(**fields):
        if fields.get("remaining_attempts") == 7 and fields.get("remaining_units") == 11:
            fields.update(remaining_attempts=1000, remaining_units=10000)
        return original(**fields)
    monkeypatch.setattr(migration, "BudgetRecord", budget)


@pytest.fixture
def durable_result(native_acquire, request):
    c = native_acquire
    count = getattr(request, "param", 3)
    reviewed(c)
    requests = []
    def provider(req):
        requests.append("model")
        return stream([{}], finish_reason="stop")
    c.composition.assistant.provider.transport = httpx.MockTransport(provider)
    c.composition.assistant.provider.credential = SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_PROVIDER"))
    c.hosted.acquire()
    for n in range(count):
        body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"synthetic-step-{n}"}],
                "stream": True, "max_tokens": 1024}
        c.hosted.reserve_model(digest(body))
        c.hosted.model(httpx.Request("POST", "https://synthetic.invalid/model", json=body))
    c.hosted.context()
    c.hosted.save(Answer(item_count=3, completed_count=2, remaining_count=1))
    c.hosted.finish()
    c.hosted.stopped("sdk_failed")
    return SimpleNamespace(c=c, model_calls=requests)


def service_for(h):
    c, stops = h.c, []
    inventory = c.store.read_inventory()
    sid = key("session-id", c.starts[0].binding.start_request_id)
    models = sorted((ModelIntentState(request_sha256=record.payload["data"]["request_sha256"],
                        consumed=record.payload["data"]["consumed"])
                    for row_id, record in inventory.rows.items()
                    if isinstance(d := record.payload["data"].get("request_sha256"), str)
                    and row_id == key("model-intent", sid, d)), key=lambda item: item.request_sha256)
    scope = FinishedResultRecoveryScope(recovery_id="original-finished-result", configuration_sha256=c.config.sha256,
        job_id=c.binding.job_id, launch_id=c.launch_id, operator_oid=c.config.identity.human_oid,
        expires_at=c.clock[0] + timedelta(minutes=10), recovery_phase="after_durable_finish", model_intents=tuple(models))
    def stop(target, deadline):
        assert c.clock[0] < deadline and c.native.workers.inspect(c.binding.job_id).cancel_requested
        stops.append(target)
        return PlatformStopObservation(target=target, endpoint=stop_endpoint(target),
            credential_client_id=c.config.identity.managed_identity_client_id, status_code=204, observed_at=c.clock[0])
    return ClaimedSdkRecovery(c.config, c.native, scope, stop, delivery=c.composition.runtime.dep.delivery,
                              clock=lambda: c.clock[0]), stops


@pytest.mark.parametrize("finished_flow", ["history", "late-finish-commit"], indirect=True)
@pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)
def test_original_finished_result_survives_one_platform_stop_without_sdk_exit_claim(finished_flow):
    h, _, calls, phases, completed, _ = finished_flow
    c = h.c
    assert completed.is_set() and h.supervisor.observe(c.launch_id).phase == "spawn_unknown"
    service, stops = service_for(h)
    before = c.store.read_inventory()
    plan = service.plan(c.principal)
    approval = service.authorize(c.principal, plan, "explicit-original-result-closure")
    result = service.execute(c.principal, approval)
    assert result["state"] == "succeeded" and result["execution_closure"] == "platform_stop_after_durable_finish"
    assert result["sdk_graceful_exit_claim"] is False and result["model_outcome"] == "unknown"
    assert len(stops) == 1 and len(calls) == 3
    proof = c.native.verify(result["platform_stop_evidence_id"])
    original = before.rows[key("result", c.binding.work.work_id)]
    assert proof.result.model_dump(mode="json") == original.payload["data"]
    assert c.native.launch(c.launch_id).child_pid is None
    assert h.supervisor.observe(c.launch_id).phase == "spawn_unknown"
    normal_report(c)
    after = c.store.read_inventory()
    for k in (key("result", c.binding.work.work_id), key("artifact", c.binding.work.work_id),
              control_key("budget", c.binding.owner.root_budget_id)):
        assert after.rows[k] == before.rows[k]
    assert service.execute(c.principal, approval)["stop_resent"] is False and len(stops) == 1


@pytest.mark.parametrize("durable_result", [1, 3, 6], indirect=True)
def test_finished_result_every_commit_readable_and_next_rotation_preserves_unknown_models(durable_result, monkeypatch):
    h, c = durable_result, durable_result.c
    service, stops = service_for(h)
    before = c.store.read_inventory()
    plan = service.plan(c.principal)
    sizes, inline_sizes, posts, ops_counts, stages = [], [], [], [], []
    entity, transact, send = c.store._entity, c.store.transact, c.transport.send
    def encoded(*args, **kwargs):
        result = entity(*args, **kwargs)
        sizes.append(len(result["Payload"]))
        if getattr(args[1], "material", None) is not None:
            inline_sizes.append(len(args[1].material.data))
        return result
    def committed(request, decide, **kwargs):
        result = transact(request, decide, **kwargs)
        read_current(c)
        stages.append(request.request_id)
        return result
    def transport(request, **kwargs):
        if request.method == "POST":
            posts.append(len(request.body))
            ops_counts.append(len(parse_batch(request)[0]))
        return send(request, **kwargs)
    monkeypatch.setattr(c.store, "_entity", encoded)
    monkeypatch.setattr(c.store, "transact", committed)
    monkeypatch.setattr(c.transport, "send", transport)
    approval = service.authorize(c.principal, plan, "original-finish-approved")
    done = service.execute(c.principal, approval)
    assert done["state"] == "succeeded" and done["model_outcome"] == "unknown"
    normal_report(c)
    final = c.store.read_inventory()
    _, liability = validate_retirement(final, c.config)
    assert liability["business_result"] == "succeeded" and liability["model_outcome"] == "unknown"
    assert len(liability["model_intents"]) == len(h.model_calls)
    assert liability["finished_result"]["sdk_outcome"] == "not_observed"
    successor = next_rotation(SimpleNamespace(config=c.config, clock=c.clock, store=c.store,
                                              first_plan=c.h.plan, h=c.h.c.h), suffix="-after-finished")
    rotation = successor.plan(c.principal, rotation_id="after-finished")
    allowed = successor.authorize(c.principal, rotation, decision_id="after-finished-approved",
                                  expires_at=c.clock[0] + timedelta(minutes=5))
    successor.apply(c.principal, rotation, allowed)
    successor.verify(c.principal, rotation, allowed)
    fresh = MigrationStore(c.composition.clients[0], c.store._scope)
    assert installed_origin(fresh, successor.new) == c.h.c.h.original
    inventory = fresh.read_inventory()
    for k in (key("result", c.binding.work.work_id), key("artifact", c.binding.work.work_id),
              control_key("budget", c.binding.owner.root_budget_id)):
        assert inventory.rows[k] == before.rows[k]
    assert all(inventory.receipts[k] == value for k, value in before.receipts.items())
    assert len(stops) == 1 and len(stages) >= 10 and max(sizes) <= 49152 and max(posts) <= 4 * 1024 * 1024
    assert max(inline_sizes) <= 32768 and max(ops_counts) <= 100
    if directory := os.environ.get("FINISHED_RESULT_SHAPES"):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        shape = {"scope": service.scope.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
                 "authorization": approval.model_dump(mode="json"), "execute_result": done,
                 "liability": rotation.liability.model_dump(mode="json"),
                 "result_sha256": plan.finished_result.result_image_sha256,
                 "size": {"max_entity": max(sizes), "max_inline": max(inline_sizes),
                          "max_multipart": max(posts), "max_operations": max(ops_counts)}}
        (path / f"models-{len(h.model_calls)}.json").write_text(json.dumps(shape, indent=2) + "\n", encoding="utf-8")
    print(f"models={len(h.model_calls)} max_entity={max(sizes)} max_inline={max(inline_sizes)} "
          f"max_multipart={max(posts)} max_ops={max(ops_counts)} stop_calls=1")


@pytest.mark.parametrize("fault", ["bytes", "etag", "source-work", "owner", "pending", "late-result", "missing-finish",
                                  "missing-assignment", "missing-acquire", "missing-context", "inventory-readfailure"])
def test_original_artifact_receipts_and_live_m3_are_mandatory(durable_result, monkeypatch, fault):
    from test_behavior_settlement import write
    h, c = durable_result, durable_result.c
    service, stops = service_for(h)
    plan = service.plan(c.principal)
    approval = service.authorize(c.principal, plan, "finished-fault-approved")
    artifacts = c.authority.artifacts
    if fault in {"bytes", "etag"}:
        original = artifacts.download
        def download(*args, **kwargs):
            if fault == "etag":
                raise Problem(412, "ARTIFACT_ETAG_MISMATCH", "synthetic exact version changed")
            record, _ = original(*args, **kwargs)
            return record, b"wrong-original-bytes"
        monkeypatch.setattr(artifacts, "download", download)
    elif fault == "source-work":
        original = artifacts.version
        monkeypatch.setattr(artifacts, "version", lambda *a, **k: original(*a, **k).model_copy(update={"source_work": "different"}))
    elif fault in {"owner", "pending"}:
        original = artifacts.get
        def document(*args):
            record = original(*args)
            payload = copy.deepcopy(record.payload)
            payload["owner_oid" if fault == "owner" else "pending"] = "wrong-original"
            return replace(record, payload=payload)
        monkeypatch.setattr(artifacts, "get", document)
    elif fault == "late-result":
        k = key("result", c.binding.work.work_id)
        value = c.store.read_snapshot((k,))[k]
        write(c.competitor, k, {**value.payload["data"], "state": "failed"})
    elif fault == "inventory-readfailure":
        def unavailable():
            raise Problem(503, "BEHAVIOR_READ_FAILED", "synthetic complete inventory unavailable")
        monkeypatch.setattr(c.store, "read_inventory", unavailable)
    else:
        action, ids = {
            "missing-finish": ("finish", ("finish-task",)),
            "missing-assignment": ("issue", ()),
            "missing-acquire": ("acquire", ("sdk-start",)),
            "missing-context": ("context", ("current-context",)),
        }[fault]
        k = key(action, c.assignment.session_id, *ids)
        c.transport.backing.rows.pop(("Behavior", c.store._scope.partition_key, "behavior:v1:receipt:" + k))
    with pytest.raises((Problem, ValueError)):
        service.execute(c.principal, approval)
    assert stops == [] and not c.native.workers.inspect(c.binding.job_id).cancel_requested


@pytest.mark.parametrize("stop_outcome", ["unknown", "already-stopped", "foreign", "expired"])
def test_finished_stop_does_not_invent_quiescence_or_repeat_unknown_send(durable_result, stop_outcome):
    c = durable_result.c
    service, _ = service_for(durable_result)
    approval = service.authorize(c.principal, service.plan(c.principal), "one-platform-close")
    stops = []
    def stop(target, deadline):
        stops.append(target)
        if stop_outcome == "unknown":
            raise Problem(502, "RECOVERY_PLATFORM_STOP_UNKNOWN", "synthetic original stop response unavailable")
        if stop_outcome == "expired":
            c.clock[0] = deadline
        return PlatformStopObservation(target=target, endpoint=stop_endpoint(target),
            credential_client_id="foreign" if stop_outcome == "foreign" else c.config.identity.managed_identity_client_id,
            status_code=409 if stop_outcome == "already-stopped" else 204,
            error_code="session_already_stopped" if stop_outcome == "already-stopped" else None,
            observed_at=c.clock[0])
    service.stop = stop
    if stop_outcome == "already-stopped":
        assert service.execute(c.principal, approval)["state"] == "succeeded"
        normal_report(c)
        assert validate_retirement(c.store.read_inventory(), c.config)[1]["model_outcome"] == "unknown"
    else:
        with pytest.raises(Problem):
            service.execute(c.principal, approval)
        assert c.native.launch(c.launch_id).phase == "claimed"
        assert c.native.workers.inspect(c.binding.job_id).state != "succeeded"
        read_current(c)
        if stop_outcome == "expired":
            with pytest.raises(Problem):
                service.execute(c.principal, approval)
        else:
            assert service.execute(c.principal, approval)["stop_resent"] is False
    assert len(stops) == 1


@pytest.mark.parametrize("stage", ["authorization", "fence-finished-result", "stop-intent", "stop-observation",
                                 "sealed", "reconcile-sealed", "observe", "prepare-forward", "work-result", "forwarded"])
def test_finished_original_ack_loss_never_repeats_stop_or_work(durable_result, monkeypatch, stage):
    from azure.core.exceptions import ServiceRequestError
    from shared_context.behavior_recovery import RecoveryAuthorization
    h, c = durable_result, durable_result.c
    service, stops = service_for(h)
    plan = service.plan(c.principal)
    original = c.transport.send
    lost, posts = [], []
    def send(request, **kwargs):
        record = None
        if request.method == "POST":
            ops, _ = parse_batch(request)
            record = next(json.loads(op[1]["Payload"]) for op in ops if "behavior:v1:receipt:" in op[1]["RowKey"])
            posts.append(record["request"]["request_id"])
        response = original(request, **kwargs)
        if record is not None and not lost:
            r = record["result"]
            selected = (record["request"]["request_id"] == recovery_key(stage, plan.recovery_id)
                        if stage in {"authorization", "fence-finished-result", "stop-intent", "stop-observation", "sealed"}
                        else set(r) == {"evidence_id"} if stage == "reconcile-sealed"
                        else r.get("status") == "succeeded" if stage == "observe"
                        else "plan_key" in r if stage == "prepare-forward"
                        else r.get("work_state") == "succeeded" if stage == "work-result"
                        else r.get("result_forwarded") is True)
            if selected:
                lost.append(ControlRequest.model_validate_json(json.dumps(record["request"])))
                raise ServiceRequestError("synthetic committed response loss")
        return response
    monkeypatch.setattr(c.transport, "send", send)
    if stage == "authorization":
        with pytest.raises(Problem):
            service.authorize(c.principal, plan, "once-approved")
        k = recovery_key("authorization", plan.recovery_id)
        approval = RecoveryAuthorization.model_validate_json(json.dumps(c.store.read_snapshot((k,))[k].payload["data"]))
    else:
        approval = service.authorize(c.principal, plan, "once-approved")
        with pytest.raises(Problem):
            service.execute(c.principal, approval)
    assert len(lost) == 1
    c.store.reconcile(lost[0])
    read_current(c)
    if stage == "stop-intent":
        assert service.execute(c.principal, approval)["stop_resent"] is False
        assert stops == [] and c.native.workers.inspect(c.binding.job_id).state != "succeeded"
    else:
        if stage in {"authorization", "fence-finished-result"}:
            result = service.execute(c.principal, approval)
        else:
            assert service.execute(c.principal, approval)["stop_resent"] is False
            result = service.finish(c.principal, approval)
        assert result["state"] == "succeeded" and len(stops) == 1
        normal_report(c)
        assert validate_retirement(c.store.read_inventory(), c.config)[1]["liability_kind"] == "finished_result_platform_stop"
    assert posts.count(lost[0].request_id) == 1 and len(h.model_calls) == 3


@pytest.mark.parametrize("lost_stage", [None, "plan", "authorization", "fetch", "ack", "confirmation"])
def test_retained_only_original_success_after_policy_expiry_remains_exact(durable_result, monkeypatch, lost_stage):
    from azure.core.exceptions import ServiceRequestError
    from shared_context.behavior_settlement import RetainedResultSettlement, SettlementRegistration
    from shared_context.behavior_settlement import settlement_key
    from test_behavior_settlement import OfflineProtection, approve, fetch_command, ack_command
    c = durable_result.c
    service, stops = service_for(durable_result)
    before = c.store.read_inventory()
    approval = service.authorize(c.principal, service.plan(c.principal), "before-policy-expiry")
    service.execute(c.principal, approval)
    c.clock[0] = c.config.policies[0].policy.valid_until + timedelta(seconds=1)
    observer = OfflineProtection(c.clock[0], "native")
    registration = SettlementRegistration(
        settlement_id="original-finished-report", binding_id=c.binding.binding_id,
        configuration_sha256=c.config.sha256, expires_at=c.clock[0] + timedelta(minutes=10),
        protection_evidence_id="offline-retained-original", protection_verifier_version="offline-retention-v1",
        protection_verifier=observer)
    settlement = RetainedResultSettlement(c.config, c.composition.runtime, registration)
    c.composition.runtime.dep.inbox.register_retained_results(settlement)
    original, transact = c.transport.send, c.store.transact
    lost, posts, stages = [], [], []
    def committed(request, decide, **kwargs):
        result = transact(request, decide, **kwargs)
        read_current(c)
        stages.append(request.request_id)
        return result
    def send(request, **kwargs):
        record = None
        if request.method == "POST":
            operations, _ = parse_batch(request)
            record = next(json.loads(op[1]["Payload"]) for op in operations if "behavior:v1:receipt:" in op[1]["RowKey"])
            posts.append(record["request"]["request_id"])
        response = original(request, **kwargs)
        if record is not None and lost_stage is not None and not lost:
            result = record["result"]
            selected = (record["request"]["request_id"] == settlement_key(lost_stage, registration.settlement_id)
                        if lost_stage in {"plan", "authorization"} else
                        set(result) == {"exchange_id"} if lost_stage == "fetch" else
                        result.get("confirmation_level") == "client_received" if lost_stage == "ack" else
                        result.get("fulfillment") == "fulfilled")
            if selected:
                lost.append(ControlRequest.model_validate_json(json.dumps(record["request"])))
                raise ServiceRequestError("synthetic retained original response loss")
        return response
    monkeypatch.setattr(c.store, "transact", committed)
    monkeypatch.setattr(c.transport, "send", send)
    def use(action):
        try:
            return action()
        except Problem as error:
            assert error.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN" and len(lost) == 1
            c.store.reconcile(lost[0])
            read_current(c)
            return action()
    granted = use(lambda: approve(settlement, c.principal))
    read_current(c)
    received = use(lambda: settlement.fetch(c.principal, fetch_command(granted)))
    read_current(c)
    ack = use(lambda: settlement.acknowledge(c.principal, ack_command(granted, received)))
    read_current(c)
    assert ack.payload["result"]["fulfillment"] == "fulfilled" and len(stops) == 1
    liability = validate_retirement(c.store.read_inventory(), c.config)[1]
    assert liability["business_result"] == "succeeded" and liability["model_outcome"] == "unknown"
    successor = next_rotation(SimpleNamespace(config=c.config, clock=c.clock, store=c.store,
                                              first_plan=c.h.plan, h=c.h.c.h), suffix="-retained-finished")
    rotation = successor.plan(c.principal, rotation_id="retained-finished")
    allowed = successor.authorize(c.principal, rotation, decision_id="retained-finished-approved",
                                  expires_at=c.clock[0] + timedelta(minutes=5))
    successor.apply(c.principal, rotation, allowed)
    successor.verify(c.principal, rotation, allowed)
    fresh = MigrationStore(c.composition.clients[0], c.store._scope)
    assert installed_origin(fresh, successor.new) == c.h.c.h.original
    after = fresh.read_inventory()
    for k in (key("result", c.binding.work.work_id), key("artifact", c.binding.work.work_id),
              control_key("budget", c.binding.owner.root_budget_id)):
        assert after.rows[k] == before.rows[k]
    assert all(after.receipts[k] == value for k, value in before.receipts.items())
    assert len(stages) >= 6 and len(durable_result.model_calls) == 3
    assert len(lost) == (0 if lost_stage is None else 1)
    assert not lost or posts.count(lost[0].request_id) == 1


@pytest.mark.parametrize("stage", ["fence-finished-result", "stop-intent", "sealed"])
@pytest.mark.parametrize("fault", ["extra-intent", "artifact", "owner"])
def test_late_authority_changes_fence_finished_closure(durable_result, monkeypatch, stage, fault):
    from test_behavior_settlement import write
    c = durable_result.c
    service, stops = service_for(durable_result)
    approval = service.authorize(c.principal, service.plan(c.principal), "snapshot-approved")
    original = c.store.transact
    changed = []
    def mutate():
        if fault == "extra-intent":
            write(c.competitor, key("model-intent", c.assignment.session_id, "e" * 64),
                  {"request_sha256": "e" * 64, "consumed": False})
        else:
            k = key("artifact", c.binding.work.work_id) if fault == "artifact" else control_key("owner", c.binding.owner.owner_record_id)
            before = c.store.read_snapshot((k,))[k].payload["data"]
            write(c.competitor, k, {**before, **({"artifact_sha256": "f" * 64} if fault == "artifact" else {"active": False})})
    def transact(request, decide, **kwargs):
        if request.request_id == recovery_key(stage, approval.plan.recovery_id) and not changed:
            changed.append(True)
            mutate()
        return original(request, decide, **kwargs)
    monkeypatch.setattr(c.store, "transact", transact)
    with pytest.raises(Problem):
        service.execute(c.principal, approval)
    assert changed == [True] and len(stops) == (1 if stage == "sealed" else 0)
    assert c.native.launch(c.launch_id).phase == "claimed"
    assert c.native.workers.inspect(c.binding.job_id).state != "succeeded"


@pytest.mark.parametrize("stage", ["fetch", "ack", "confirmation"])
def test_finished_report_loss_is_exact_receipt_reconciled_without_rewriting_result(durable_result, monkeypatch, stage):
    from shared_context import behavior_delivery as delivery, behavior_wire as wire
    from azure.core.exceptions import ServiceRequestError
    c = durable_result.c
    service, stops = service_for(durable_result)
    approval = service.authorize(c.principal, service.plan(c.principal), "original-report-approved")
    service.execute(c.principal, approval)
    runtime, b = c.composition.runtime, c.binding
    delivery.DeliveryConsumer(runtime.dep.delivery).consume(b.obligation_id, b.owner, "original-finished-only")
    original = c.transport.send
    lost, posts = [], []
    def send(request, **kwargs):
        record = None
        if request.method == "POST":
            ops, _ = parse_batch(request)
            record = next(json.loads(op[1]["Payload"]) for op in ops if "behavior:v1:receipt:" in op[1]["RowKey"])
            posts.append(record["request"]["request_id"])
        response = original(request, **kwargs)
        if record is not None and not lost:
            r = record["result"]
            if (set(r) == {"exchange_id"} if stage == "fetch" else
                r.get("confirmation_level") == "client_received" if stage == "ack" else
                r.get("fulfillment") == "fulfilled"):
                lost.append(ControlRequest.model_validate_json(json.dumps(record["request"])))
                raise ServiceRequestError("synthetic committed delivery response loss")
        return response
    monkeypatch.setattr(c.transport, "send", send)
    def use(action):
        try:
            return action()
        except Problem as error:
            assert error.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN" and len(lost) == 1
            c.store.reconcile(lost[0])
            read_current(c)
            return action()
    got = use(lambda: runtime.fetch(b.binding_id, wire.Command(request_id="original-finished-fetch"), c.principal))
    command = wire.PullAcknowledgment(request_id="original-finished-ack", exchange_id=got.exchange_id,
        acknowledgment_token=got.acknowledgment_token,
        binding=wire.DeliveryBinding.model_validate_json(got.binding.model_dump_json()))
    ack = use(lambda: runtime.acknowledge(b.binding_id, command, c.principal))
    assert ack.payload["result"]["fulfillment"] == "fulfilled"
    assert len(lost) == 1 and posts.count(lost[0].request_id) == 1 and len(stops) == 1
    assert validate_retirement(c.store.read_inventory(), c.config)[1]["model_outcome"] == "unknown"


def test_recovery_operator_http_rejects_client_stop_evidence(durable_result, signing_key):
    from fastapi.testclient import TestClient
    from test_m3_auth import token
    from shared_context.behavior_recovery_api import create_recovery_app
    c = durable_result.c
    service, stops = service_for(durable_result)
    app = create_recovery_app(c.composition, service.scope, stop=service.stop)
    bearer = token(signing_key, {"tid": c.config.identity.tenant_id,
        "iss": f"https://sts.windows.net/{c.config.identity.tenant_id}/", "aud": c.config.identity.audience,
        "oid": c.config.identity.human_oid, "appid": c.config.identity.admin_client_id,
        "scp": "Behavior.Read Behavior.Approve"})
    with TestClient(app) as client:
        headers, prefix = {"Authorization": "Bearer " + bearer}, "/operator/v1/sdk-recovery"
        plan = client.post(prefix + "/plan", json={}, headers=headers)
        assert plan.status_code == 200 and plan.json()["recovery_phase"] == "after_durable_finish"
        approved = client.post(prefix + "/authorize", json={"plan": plan.json(), "decision_id": "finished-http-approved"}, headers=headers)
        assert approved.status_code == 200
        assert client.post(prefix + "/execute", json={**approved.json(), "sdk_stop": {"graceful": True}},
                           headers=headers).status_code == 422
        assert stops == []
        result = client.post(prefix + "/execute", json=approved.json(), headers=headers)
        assert result.status_code == 200 and result.json()["state"] == "succeeded" and len(stops) == 1
        assert result.json()["sdk_graceful_exit_claim"] is False
        assert client.post(prefix + "/reconcile", json=approved.json(), headers=headers).json()["stop_resent"] is False
