"""Mixed consumed/reserved original intents from real native-mode producers."""
from datetime import timedelta
from dataclasses import replace
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant import key, _Session, _row
from shared_context.behavior_recovery import (
    ClaimedSdkRecovery, RecoveryScope, MultiModelRecoveryScope, ModelIntentState, PlatformStopObservation, stop_endpoint,
)
from shared_context.errors import Problem
from shared_context.source_materials import digest
from shared_context.behavior_recovery import recovery_key
from shared_context.behavior_operations import control_key
from shared_context.behavior_foundry import native_key
from shared_context.behavior_workers import worker_key
from shared_context.behavior_store import ControlRequest
from test_behavior_store import parse_batch
from test_behavior_assistant_acquire_conflicts import native_acquire, current_rotation
from test_behavior_rotation_cutoff import two_rotations
from test_behavior_rotation_settlement_history import current_cancelled
from test_behavior_runtime_rotation import rotated
from test_behavior_assistant_claim_boundaries import reviewed
from test_behavior_assistant_sse import stream
from test_m3_auth import jwk_client, signing_key
from test_behavior_model_diagnostics import sixteen_queries


@pytest.fixture
def mixed_models(native_acquire, request):
    c = native_acquire
    reviewed(c)
    sends = []
    def provider(request):
        sends.append(request)
        return stream([{"tool_calls": [{"index": 0, "function": {"name": "read_context", "arguments": "{}"}}]}])
    c.composition.assistant.provider.transport = httpx.MockTransport(provider)
    c.composition.assistant.provider.credential = SimpleNamespace(
        get_token=lambda _: SimpleNamespace(token="SYNTHETIC_PROVIDER"))
    c.hosted.acquire()
    first = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "Synthetic metrics"}],
             "stream": True, "max_tokens": 1024}
    c.hosted.reserve_model(digest(first))
    c.hosted.model(httpx.Request("POST", "https://synthetic.invalid/model", json=first))
    context = c.hosted.context()
    second = {**first, "messages": [*first["messages"], {"role": "user", "content": context.model_dump_json()}]}
    count = getattr(request, "param", 2)
    exhausted = count == "exhausted"
    count = 2 if exhausted else count
    bodies = [first, second]
    bodies.extend({**second, "temperature": number / 10} for number in range(2, count))
    for body in bodies[1:]:
        c.hosted.reserve_model(digest(body))
    c.hosted.stopped("sdk_failed")
    if exhausted:
        from shared_context import behavior_wire as wire
        binding = c.starts[0].binding
        def query(original_binding):
            probe = c.native.probe(original_binding)
            launch = c.native.launch(c.launch_id)
            report = wire.HostedReport(request_id=probe.probe_id, invocation_id=launch.native_invocation_id,
                identity=c.identity, process_id=launch.process_id, claim_id=launch.claim_id,
                report_token=c.native_claim.report_token, probe_id=probe.probe_id, supervisor_id=launch.process_id,
                process_generation=launch.process_generation, binding_digest=digest(binding.model_dump(mode="json")),
                phase="spawn_unknown", sequence=1)
            return c.native.report(c.binding.binding_id, c.launch_id, probe.probe_id, report, c.agent).evidence_id
        c.native.workers.adapter = replace(c.native.workers.adapter, query=query)
        for _ in range(16):
            c.native.workers.reconcile(binding.job_id, binding.owner)
        assert c.native.workers.inspect(binding.job_id).state == "capacity_uncertain"
        assert c.native.workers.inspect(binding.job_id).queries_reserved == 16
        assert c.native.launch(c.launch_id).probes_reserved == 16
    inventory = c.store.read_inventory()
    sid = c.assignment.session_id
    session = _row(inventory.rows, key("session", sid), _Session)
    assert session.model_requests == count and session.context_read and session.state == "waiting"
    assert inventory.rows[key("model-intent", sid, digest(first))].payload["data"]["consumed"] is True
    assert inventory.rows[key("model-intent", sid, digest(second))].payload["data"]["consumed"] is False
    assert len(sends) == 1
    return SimpleNamespace(c=c, first=digest(first), second=digest(second), bodies=tuple(bodies), sends=sends)


def service_for(h, selected=None):
    c, calls = h.c, []
    fields = dict(recovery_id="mixed-original-cancel", configuration_sha256=c.config.sha256,
        job_id=c.binding.job_id, launch_id=c.launch_id,
        operator_oid=c.config.identity.human_oid, expires_at=c.clock[0] + timedelta(minutes=10))
    scope = (RecoveryScope(**fields, model_request_sha256=selected) if selected else MultiModelRecoveryScope(
        **fields, recovery_phase="after_sdk_model_requests", model_intents=tuple(sorted(
            (ModelIntentState(request_sha256=digest(body), consumed=index == 0) for index, body in enumerate(h.bodies)),
            key=lambda item: item.request_sha256))))
    def stop(target, deadline):
        calls.append(target)
        return PlatformStopObservation(target=target, endpoint=stop_endpoint(target),
            credential_client_id=c.config.identity.managed_identity_client_id,
            status_code=204, observed_at=c.clock[0])
    return ClaimedSdkRecovery(c.config, c.native, scope, stop,
        delivery=c.composition.runtime.dep.delivery, clock=lambda: c.clock[0]), calls


@pytest.mark.parametrize("selected", ["first", "second"])
def test_old_single_digest_scope_cannot_discard_the_other_intent(mixed_models, selected):
    h = mixed_models
    service, calls = service_for(h, getattr(h, selected))
    with pytest.raises(Problem, match="exact consumed model intent differs"):
        service.plan(h.c.principal)
    assert calls == []


@pytest.mark.parametrize("mixed_models", [2, 6], indirect=True)
def test_multiple_original_intents_cancel_without_refund_or_fake_model_evidence(mixed_models, monkeypatch):
    from test_behavior_unclaimed_closure import read_current, normal_report
    from shared_context.behavior_runtime_rotation_history import validate_retirement
    from test_behavior_rotation_settlement_history import next_rotation
    from shared_context.behavior_migration import MigrationStore, installed_origin
    h = mixed_models
    c = h.c
    service, calls = service_for(h)
    plan = service.plan(h.c.principal)
    expected = {digest(body) for body in h.bodies}
    assert set(item.request_sha256 for item in plan.model_intents) == expected
    original = c.store.transact
    raw_entity = c.store._entity
    entity_sizes, inline_sizes = [], []
    def entity(*args, **kwargs):
        material = getattr(args[1], "material", None)
        if material is not None:
            inline_sizes.append(len(material.data))
        value = raw_entity(*args, **kwargs)
        entity_sizes.append(len(value["Payload"]))
        return value
    monkeypatch.setattr(c.store, "_entity", entity)
    requests_before = len(c.transport.requests)
    original_inventory = c.store.read_inventory()
    checked = []
    def transact(request, decide, **kwargs):
        receipt = original(request, decide, **kwargs)
        read_current(c)
        checked.append(request.request_id)
        return receipt
    monkeypatch.setattr(c.store, "transact", transact)
    approval = service.authorize(h.c.principal, plan, "explicit-mixed-cancel")
    result = service.execute(h.c.principal, approval)
    assert result["state"] == "cancelled" and result["model_outcome"] == "unknown"
    assert len(calls) == 1
    proof = h.c.native.verify(result["platform_stop_evidence_id"])
    assert set(proof.result.unresolved_operations) == expected
    assert service.execute(h.c.principal, approval)["stop_resent"] is False and len(calls) == 1
    normal_report(c)
    _, liability = validate_retirement(c.store.read_inventory(), c.config)
    assert liability["model_outcome"] == "unknown" and liability["liability_kind"] == "multiple_model_requests"
    assert {item["request_sha256"] for item in liability["model_intents"]} == expected
    successor = next_rotation(SimpleNamespace(config=c.config, clock=c.clock, store=c.store,
                                              first_plan=c.h.plan, h=c.h.c.h), suffix="-after-model-set")
    next_plan = successor.plan(c.principal, rotation_id="models-next")
    approved = successor.authorize(c.principal, next_plan, decision_id="models-next-approved",
                                   expires_at=c.clock[0] + timedelta(minutes=5))
    successor.apply(c.principal, next_plan, approved)
    successor.verify(c.principal, next_plan, approved)
    assert installed_origin(MigrationStore(c.composition.clients[0], c.store._scope), successor.new) == c.h.c.h.original
    assert len(checked) >= 9 and len(h.sends) == 1
    with pytest.raises(Problem, match="writer configuration is no longer installed"):
        c.store.read_inventory()
    final = MigrationStore(c.composition.clients[0], c.store._scope).read_inventory()
    assert all(final.receipts[key] == row for key, row in original_inventory.receipts.items())
    budget_key = control_key("budget", c.binding.owner.root_budget_id)
    assert final.rows[budget_key] == original_inventory.rows[budget_key]
    for body in h.bodies:
        k = key("model-intent", c.assignment.session_id, digest(body))
        assert final.rows[k] == original_inventory.rows[k]
    posts = [req for req in c.transport.requests[requests_before:] if req.method == "POST"]
    lengths = [len(req.body) for req in posts]
    operations = [len(parse_batch(req)[0]) for req in posts]
    assert max(entity_sizes) <= 49152 and max(lengths) <= 4 * 1024 * 1024 and max(operations) <= 100
    assert max(inline_sizes) <= 32768
    assert "model_request_sha256" not in next_plan.liability.model_dump(mode="json")
    assert all(item.consumed == (item.request_sha256 == h.first) for item in next_plan.liability.model_intents)
    if directory := os.environ.get("MULTI_MODEL_SHAPES"):
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        shapes = {
            "scope": service.scope.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
            "authorization": approval.model_dump(mode="json"), "execute_result": result,
            "result_content": json.loads(proof.result.content), "unresolved_operations": list(proof.result.unresolved_operations),
            "liability": next_plan.liability.model_dump(mode="json"),
            "size": {"max_encoded_entity": max(entity_sizes), "max_actual_multipart": max(lengths),
                     "max_inline_bytes": max(inline_sizes), "max_EGT_ops": max(operations), "stop_calls": len(calls)},
        }
        (target / f"intents-{len(expected)}.json").write_text(json.dumps(shapes, indent=2) + "\n", encoding="utf-8")
    print(f"intents={len(expected)}; max_encoded_entity={max(entity_sizes)}; "
          f"max_inline_bytes={max(inline_sizes)}; max_actual_multipart={max(lengths)}; "
          f"max_EGT_ops={max(operations)}; stop_calls={len(calls)}")


@pytest.mark.parametrize("stage", ["cancel", "reserve", "before-stop", "seal"])
@pytest.mark.parametrize("mutation", ["late-consume", "extra-intent"])
def test_original_set_races_fail_closed_without_second_stop(mixed_models, monkeypatch, stage, mutation):
    from shared_context.behavior_assistant_wire import ModelRequest
    from test_behavior_settlement import write
    h, c = mixed_models, mixed_models.c
    service, calls = service_for(h)
    plan = service.plan(c.principal)
    approval = service.authorize(c.principal, plan, "race-approved")
    ids = {name: recovery_key(value, plan.recovery_id) for name, value in (
        ("cancel", "cancel-model-set"), ("reserve", "stop-intent"), ("before-stop", "stop-intent"), ("seal", "sealed"))}
    old = c.store.transact
    injected = []
    def change():
        injected.append(stage)
        current = _row(c.store.read_inventory().rows, key("session", c.assignment.session_id), _Session)
        if mutation == "late-consume" and stage == "cancel":
            c.authority.consume_model(current, ModelRequest(request_id=h.second, body=h.bodies[1]), c.agent)
        elif mutation == "late-consume":
            with pytest.raises(Problem):
                c.authority.consume_model(current, ModelRequest(request_id=h.second, body=h.bodies[1]), c.agent)
            k = key("model-intent", c.assignment.session_id, h.second)
            write(c.competitor, k, {"request_sha256": h.second, "consumed": True})
        else:
            write(c.competitor, key("model-intent", c.assignment.session_id, "e" * 64),
                  {"request_sha256": "e" * 64, "consumed": False})
    def transact(request, decide, **kwargs):
        selected = request.request_id == ids[stage] and not injected
        if selected and stage != "before-stop":
            change()
        result = old(request, decide, **kwargs)
        if selected and stage == "before-stop":
            change()
        return result
    monkeypatch.setattr(c.store, "transact", transact)
    with pytest.raises(Problem):
        service.execute(c.principal, approval)
    assert injected == [stage] and len(calls) == (1 if stage == "seal" else 0)
    assert c.native.launch(c.launch_id).phase == "claimed"
    assert c.native.workers.inspect(c.binding.job_id).state != "cancelled"
    assert len(h.sends) == 1


@pytest.mark.parametrize("fault", ["read-failure", "missing-reserve", "reserve-readset", "reserve-image",
                                    "missing-consume", "consume-readset", "consume-hash", "wrong-scope"])
def test_model_set_requires_full_exact_receipts_not_operator_hashes(mixed_models, monkeypatch, fault):
    from test_behavior_rotation_normal_pull import changed_inventory
    h, c = mixed_models, mixed_models.c
    service, calls = service_for(h)
    original = c.store.read_inventory
    def inventory():
        if fault == "read-failure":
            raise Problem(503, "BEHAVIOR_READ_FAILED", "synthetic unavailable read")
        value = original()
        r = key("model-send" if "consume" in fault else "model", c.assignment.session_id, h.first)
        if fault.startswith("missing"):
            return changed_inventory(value, missing_receipt=r)
        payload = copy.deepcopy(value.receipts[r].payload)
        if fault.endswith("readset"):
            payload["request"]["read_set"] = payload["request"]["read_set"][:-1]
        elif fault == "reserve-image":
            payload["writes"][0][1] = "0" * 64
        elif fault == "wrong-scope":
            payload["binding"] = "0" * 64
        else:
            payload["request"]["request_digest"] = "f" * 64
        return changed_inventory(value, receipt_change=(r, payload))
    if fault == "consume-hash":
        plan = service.plan(c.principal)
        approval = service.authorize(c.principal, plan, "receipt-bound-approved")
        monkeypatch.setattr(c.store, "read_inventory", inventory)
        with pytest.raises(Problem):
            service.execute(c.principal, approval)
    else:
        monkeypatch.setattr(c.store, "read_inventory", inventory)
        with pytest.raises(Problem):
            service.plan(c.principal)
    assert calls == []


@pytest.mark.parametrize("stage", ["authorization", "cancel-model-set", "stop-intent", "stop-observation",
                                 "sealed", "reconcile-sealed", "observe", "prepare-forward", "work-result", "forwarded"])
def test_every_recovery_commit_loss_reconciles_without_resending_provider_stop(mixed_models, monkeypatch, stage):
    from test_behavior_unclaimed_closure import read_current, normal_report
    from shared_context.behavior_runtime_rotation_history import validate_retirement
    h, c = mixed_models, mixed_models.c
    service, calls = service_for(h)
    plan = service.plan(c.principal)
    old = c.transport.send
    lost, posts = [], []
    def send(request, **kwargs):
        candidate = None
        if request.method == "POST":
            ops, _ = parse_batch(request)
            candidate = next(json.loads(op[1]["Payload"]) for op in ops if "behavior:v1:receipt:" in op[1]["RowKey"])
            posts.append(candidate["request"]["request_id"])
        response = old(request, **kwargs)
        if candidate is not None and not lost:
            result = candidate["result"]
            rid = candidate["request"]["request_id"]
            selected = (rid == recovery_key(stage, plan.recovery_id) if stage in {
                "authorization", "cancel-model-set", "stop-intent", "stop-observation", "sealed"} else
                set(result) == {"evidence_id"} if stage == "reconcile-sealed" else
                result.get("status") == "cancelled" if stage == "observe" else
                "plan_key" in result if stage == "prepare-forward" else
                result.get("work_state") == "cancelled" if stage == "work-result" else
                result.get("result_forwarded") is True)
            if selected:
                from azure.core.exceptions import ServiceRequestError
                lost.append(ControlRequest.model_validate_json(json.dumps(candidate["request"])))
                raise ServiceRequestError("synthetic committed acknowledgement loss")
        return response
    monkeypatch.setattr(c.transport, "send", send)
    if stage == "authorization":
        with pytest.raises(Problem):
            service.authorize(c.principal, plan, "loss-approved")
        from shared_context.behavior_recovery import RecoveryAuthorization
        approval = RecoveryAuthorization.model_validate_json(json.dumps(
            c.store.read_snapshot((recovery_key(stage, plan.recovery_id),))[recovery_key(stage, plan.recovery_id)].payload["data"]))
    else:
        approval = service.authorize(c.principal, plan, "loss-approved")
        with pytest.raises(Problem):
            service.execute(c.principal, approval)
    assert len(lost) == 1
    c.store.reconcile(lost[0])
    read_current(c)
    if stage == "stop-intent":
        assert service.execute(c.principal, approval)["stop_resent"] is False
        assert not calls and c.native.workers.inspect(c.binding.job_id).state != "cancelled"
    else:
        if stage in {"authorization", "cancel-model-set"}:
            result = service.execute(c.principal, approval)
        else:
            assert service.execute(c.principal, approval)["stop_resent"] is False
            result = service.finish(c.principal, approval)
        assert result["state"] == "cancelled"
        normal_report(c)
        assert validate_retirement(c.store.read_inventory(), c.config)[1]["model_outcome"] == "unknown"
        assert len(calls) == 1
    assert posts.count(lost[0].request_id) == 1 and len(h.sends) == 1


@pytest.mark.parametrize("stage", ["fetch", "ack", "confirmation"])
def test_model_set_report_loss_preserves_pending_until_original_ack(mixed_models, monkeypatch, stage):
    from shared_context import behavior_delivery as delivery, behavior_wire as wire
    from test_behavior_unclaimed_closure import read_current
    from shared_context.behavior_runtime_rotation_history import validate_retirement
    from azure.core.exceptions import ServiceRequestError
    h, c = mixed_models, mixed_models.c
    service, calls = service_for(h)
    approval = service.authorize(c.principal, service.plan(c.principal), "report-loss-approved")
    service.execute(c.principal, approval)
    runtime, binding = c.composition.runtime, c.binding
    delivery.DeliveryConsumer(runtime.dep.delivery).consume(binding.obligation_id, binding.owner, "report-only")
    old = c.transport.send
    lost, posts = [], []
    def send(request, **kwargs):
        candidate = None
        if request.method == "POST":
            operations, _ = parse_batch(request)
            candidate = next(json.loads(op[1]["Payload"]) for op in operations if "behavior:v1:receipt:" in op[1]["RowKey"])
            posts.append(candidate["request"]["request_id"])
        response = old(request, **kwargs)
        if candidate is not None and not lost:
            result = candidate["result"]
            if (set(result) == {"exchange_id"} if stage == "fetch" else
                result.get("confirmation_level") == "client_received" if stage == "ack" else
                result.get("fulfillment") == "fulfilled"):
                lost.append(ControlRequest.model_validate_json(json.dumps(candidate["request"])))
                raise ServiceRequestError("synthetic report commit response loss")
        return response
    monkeypatch.setattr(c.transport, "send", send)
    def retained(action):
        try:
            return action()
        except Problem as error:
            assert error.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN" and len(lost) == 1
            c.store.reconcile(lost[0])
            read_current(c)
            return action()
    got = retained(lambda: runtime.fetch(binding.binding_id, wire.Command(request_id="original-fetch"), c.principal))
    read_current(c)
    command = wire.PullAcknowledgment(request_id="original-ack", exchange_id=got.exchange_id,
        acknowledgment_token=got.acknowledgment_token,
        binding=wire.DeliveryBinding.model_validate_json(got.binding.model_dump_json()))
    ack = retained(lambda: runtime.acknowledge(binding.binding_id, command, c.principal))
    assert ack.payload["result"]["fulfillment"] == "fulfilled"
    read_current(c)
    assert len(lost) == 1 and posts.count(lost[0].request_id) == 1 and len(calls) == 1
    assert validate_retirement(c.store.read_inventory(), c.config)[1]["liability_kind"] == "multiple_model_requests"


@pytest.mark.parametrize("fault", ["unknown-row", "claimed-report", "guard", "policy", "result"])
def test_unrelated_changes_are_not_normalized_as_own_recovery(mixed_models, fault):
    from test_behavior_settlement import write
    h, c = mixed_models, mixed_models.c
    service, calls = service_for(h)
    approval = service.authorize(c.principal, service.plan(c.principal), "unchanged-approved")
    if fault == "unknown-row":
        write(c.competitor, "unexpected-history-row", {"unknown": True})
    elif fault == "guard":
        from shared_context.behavior_store import ControlChanges
        c.competitor.transact(ControlRequest(request_id="unrelated-controller-tick", request_digest="b" * 64),
                              lambda _: ControlChanges(result={"tick": True}))
    else:
        k = (native_key("launch", c.launch_id) if fault == "claimed-report" else
             control_key("policy", c.binding.owner.policy_id) if fault == "policy" else
             key("result", c.binding.work.work_id))
        data = c.store.read_snapshot((k,))[k]
        value = dict(data.payload["data"]) if data is not None else {"unexpected": True}
        if fault == "claimed-report":
            value["evidence_sequence"] += 1
        elif fault == "policy":
            value["active"] = False
        write(c.competitor, k, value)
    with pytest.raises(Problem):
        service.execute(c.principal, approval)
    assert not calls and not c.native.workers.inspect(c.binding.job_id).cancel_requested


def test_legacy_scope_plan_authorization_and_liability_json_shapes_remain_unchanged(mixed_models):
    from pydantic import TypeAdapter
    from shared_context import behavior_recovery as r
    from shared_context.behavior_runtime_rotation import ModelLiability, PreSdkLiability, RotationPlan
    h, c = mixed_models, mixed_models.c
    service, _ = service_for(h)
    common_scope = {name: value for name, value in service.scope.model_dump(mode="json").items()
                    if name not in {"recovery_phase", "model_intents"}}
    common_plan = dict(recovery_id=service.scope.recovery_id, configuration_sha256=c.config.sha256,
        code_sha256="c" * 64, operator_oid=c.config.identity.human_oid,
        binding=c.starts[0].binding.model_dump(mode="json"), launch_id=c.launch_id,
        target=c.native.launch(c.launch_id).target.model_dump(mode="json"),
        native_invocation_id=c.native.launch(c.launch_id).native_invocation_id, snapshot_sha256="a" * 64)
    pairs = (
        (r.RecoveryScope, {**common_scope, "model_request_sha256": h.first}),
        (r.PreSdkRecoveryScope, {**common_scope, "model_request_sha256": None, "recovery_phase": "before_sdk_acquire"}),
        (r.PreAssignmentRecoveryScope, {**common_scope, "model_request_sha256": None, "recovery_phase": "before_sdk_assignment"}),
        (r.RecoveryPlan, {**common_plan, "model_request_sha256": h.first}),
        (r.PreSdkRecoveryPlan, {**common_plan, "model_request_sha256": None, "recovery_phase": "before_sdk_acquire"}),
        (r.PreAssignmentRecoveryPlan, {**common_plan, "model_request_sha256": None,
            "recovery_phase": "before_sdk_assignment", "native_claim_request": {
                "request_id": "original-native", "request_digest": "d" * 64, "read_set": []},
            "native_claim_receipt_sha256": "b" * 64}),
    )
    scope_union = TypeAdapter(r.RecoveryScope | r.PreSdkRecoveryScope | r.PreAssignmentRecoveryScope | r.MultiModelRecoveryScope)
    for model, body in pairs:
        parsed = model.model_validate_json(json.dumps(body))
        if "code_sha256" in body:
            auth = {"plan": body, "decision_id": "legacy-approved",
                    "approved_at": c.clock[0].isoformat(), "expires_at": (c.clock[0] + timedelta(minutes=1)).isoformat()}
            actual = r.RecoveryAuthorization.model_validate_json(json.dumps(auth)).plan
            assert type(actual) is model and actual.model_dump_json() == parsed.model_dump_json()
        else:
            actual = scope_union.validate_json(json.dumps(body))
            assert type(actual) is model and actual.model_dump_json() == parsed.model_dump_json()
        assert set(parsed.model_dump()) == set(body)
    for legacy in (c.h.c.first_plan, c.h.plan):
        encoded = legacy.model_dump_json()
        assert RotationPlan.model_validate_json(encoded).model_dump_json() == encoded
        assert "liability_kind" not in legacy.liability.model_dump()
        assert isinstance(legacy.liability, (ModelLiability, PreSdkLiability))
    assert c.h.c.first_plan.liability.model_outcome == "unknown"


@pytest.mark.usefixtures("sixteen_queries")
@pytest.mark.parametrize("mixed_models", ["exhausted"], indirect=True)
def test_multi_recovery_after_sixteen_real_unknown_reports_preserves_counters(mixed_models):
    from test_behavior_unclaimed_closure import read_current, normal_report
    from shared_context.behavior_runtime_rotation_history import validate_retirement
    h, c = mixed_models, mixed_models.c
    before = c.store.read_inventory()
    service, stops = service_for(h)
    approval = service.authorize(c.principal, service.plan(c.principal), "exhausted-original-approved")
    result = service.execute(c.principal, approval)
    assert result["state"] == "cancelled" and len(stops) == 1
    normal_report(c)
    read_current(c)
    assert validate_retirement(c.store.read_inventory(), c.config)[1]["model_outcome"] == "unknown"
    job = c.native.workers.inspect(c.binding.job_id)
    assert job.queries_reserved == 16 and job.control_remaining == 0
    assert c.native.launch(c.launch_id).probes_reserved == 16
    after = c.store.read_inventory()
    assert all(after.receipts[key] == row for key, row in before.receipts.items())
    for kind, identifier in (("budget", c.binding.owner.root_budget_id),):
        assert after.rows[control_key(kind, identifier)] == before.rows[control_key(kind, identifier)]
