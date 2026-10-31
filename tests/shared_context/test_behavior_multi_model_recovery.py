"""Mixed consumed/reserved original intents from real native-mode producers."""
from datetime import timedelta
from dataclasses import replace
import json
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
from shared_context.behavior_store import ControlRequest
from test_behavior_store import parse_batch
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_behavior_assistant_claim_boundaries import reviewed
from test_behavior_assistant_sse import stream
from test_m3_auth import jwk_client, signing_key


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


@pytest.mark.parametrize("stage", ["authorization", "cancel-model-set", "stop-intent", "stop-observation",
                                 "sealed", "reconcile-sealed", "observe", "prepare-forward", "work-result", "forwarded"])
def test_every_recovery_commit_loss_reconciles_without_resending_provider_stop(mixed_models, monkeypatch, stage):
    from test_behavior_unclaimed_closure import read_current, normal_report
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
        assert len(calls) == 1
    assert posts.count(lost[0].request_id) == 1 and len(h.sends) == 1


@pytest.mark.parametrize("stage", ["fetch", "ack", "confirmation"])
def test_model_set_report_loss_preserves_pending_until_original_ack(mixed_models, monkeypatch, stage):
    from shared_context import behavior_delivery as delivery, behavior_wire as wire
    from test_behavior_unclaimed_closure import read_current
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

