"""Elapsed-time acceptance for a distinct steady/per-work policy, not v1 renewal."""
from datetime import timedelta
import base64
from dataclasses import replace
import pytest
from shared_context.errors import Problem
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import AssistantSubmit
from shared_context.behavior_work_policy import WorkPolicyControl
from test_behavior_assistant import assistant,runtime_path
from test_work_cost_policy import standing
from test_m3_auth import jwk_client,signing_key


def enabled(assistant,*,after_global=False):
    h,policy,observed,_,human=standing(assistant)
    if after_global:h.time=h.policy.valid_until+timedelta(seconds=1)
    facts=[observed.model_copy(update={"observed_at":h.time})]
    control=WorkPolicyControl(h.runtime,policy,lambda:facts[0])
    control.install_offline(human,"offline-once-standing-policy",h.time+timedelta(minutes=10))
    initial=h.readiness()
    h.delivery.registration=replace(h.delivery.registration,verify_readiness=lambda:initial.model_copy(update={
        "observed_at":h.time,"expires_at":h.time+timedelta(minutes=5)}))
    return h,control,human,facts


def test_new_work_is_not_bound_to_one_global_test_deadline(assistant):
    h,control,human,facts=enabled(assistant,after_global=True)
    binding,status=control.submit(human,AssistantSubmit(request_id="new-work-after-old-global-time"))
    assert status.state=="queued" and binding.job_id!=h.binding.job_id
    assert control.policy.legacy_template_policy.valid_until==h.policy.valid_until
    h.time=h.policy.valid_until+timedelta(seconds=1)
    assert control.policy.active and h.time>h.policy.valid_until


def test_saved_result_and_ack_are_not_execution_deadline_permissions(assistant):
    h,control,human,facts=enabled(assistant,after_global=True)
    binding,_=control.submit(human,AssistantSubmit(request_id="original-work"))
    state=h.runtime.controller.step(binding.binding_id)
    assert state.state=="succeeded" and state.result_forwarded
    h.time+=timedelta(days=1)
    path="/behavior/v1/bindings/"+binding.binding_id
    fetched=h.client.post(path+"/delivery/pull",content=wire.Command(request_id="read-after-work-deadline").model_dump_json(),headers=h.headers())
    assert fetched.status_code==200,fetched.text
    delivery=wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(fetched.headers["x-behavior-binding"]))
    ack=h.client.post(path+"/delivery/acknowledge",content=wire.PullAcknowledgment(request_id="ack-after-work-deadline",
        exchange_id=fetched.headers["x-behavior-exchange"],acknowledgment_token=fetched.headers["x-behavior-ack-token"],binding=delivery).model_dump_json(),headers=h.headers())
    assert ack.status_code==200 and ack.json()["result"]["fulfillment"]=="fulfilled"
    assert len(h.endpoint.requests)==3


def test_terminal_unacknowledged_work_does_not_hold_capacity_or_block_next_work(assistant):
    from test_behavior_assistant import call
    from shared_context.behavior_workers import worker_key
    from shared_context.behavior_operations import control_key
    h,control,human,facts=enabled(assistant,after_global=True)
    first,_=control.submit(human,AssistantSubmit(request_id="first-work"))
    done=h.runtime.controller.step(first.binding_id)
    assert done.state=="succeeded" and not done.obligation_fulfilled
    old=h.store.read_snapshot((control_key("delivery-obligation",first.obligation_id),))[control_key("delivery-obligation",first.obligation_id)]
    second,status=control.submit(human,AssistantSubmit(request_id="independent-second-work"))
    assert second.job_id!=first.job_id and status.state=="queued"
    assert h.store.read_snapshot((control_key("delivery-obligation",first.obligation_id),))[control_key("delivery-obligation",first.obligation_id)]==old
    h.endpoint.replies=iter([call("read_context",{},4),call("save_result",{"item_count":3,"completed_count":2,"remaining_count":1},5),
                             call("finish_task",{},6)])
    other=h.runtime.controller.step(second.binding_id)
    assert other.state=="succeeded" and not other.obligation_fulfilled and len(h.endpoint.requests)==6
    assert h.runtime.job_status(first.binding_id,human).state=="succeeded"


def test_uncertain_original_executor_keeps_its_slot_and_next_work_stays_queued(assistant):
    from shared_context.behavior_workers import worker_key
    h,control,human,facts=enabled(assistant,after_global=True)
    first,_=control.submit(human,AssistantSubmit(request_id="unknown-first-work"))
    def unknown(_):raise TimeoutError("offline unknown original start")
    h.workers.adapter=replace(h.workers.adapter,start=unknown)
    with pytest.raises(Problem):h.runtime.controller.step(first.binding_id)
    from shared_context.behavior_workers import ExecutorEvidence
    evidence=ExecutorEvidence(evidence_id="offline-original-unknown",binding=h.workers.inspect(first.job_id).binding,
        sequence=1,native_execution_id=None,state="unknown",entire_tree_idle=False,all_inflight_starts_fenced=False,
        proof=b"OFFLINE observer: original executor outcome unknown; not a stop proof")
    h.workers.adapter=replace(h.workers.adapter,query=lambda _:evidence.evidence_id,verify=lambda _:evidence)
    h.workers.reconcile(first.job_id,first.owner)
    job=h.workers.inspect(first.job_id)
    assert job.state=="capacity_uncertain"
    poolkey=worker_key("pool",h.workers.contract.workspace_id)
    before=h.store.read_snapshot((poolkey,))[poolkey].payload["data"]["slots"]
    second,_=control.submit(human,AssistantSubmit(request_id="queued-second-work"))
    h.time+=timedelta(seconds=h.workers.contract.lease_seconds+1)
    with pytest.raises(Problem) as denied:h.runtime.controller.step(second.binding_id)
    assert denied.value.body["error"]["code"]=="WORKER_CAPACITY_FULL"
    assert h.workers.inspect(second.job_id).state=="queued" and h.workers.inspect(second.job_id).generation==0
    assert h.store.read_snapshot((poolkey,))[poolkey].payload["data"]["slots"]==before
    assert h.endpoint.requests==[]


def test_cost_stale_blocks_new_paid_work_but_not_safe_cancellation(assistant):
    h,control,human,facts=enabled(assistant,after_global=True)
    binding,_=control.submit(human,AssistantSubmit(request_id="accepted-but-not-started"))
    h.time+=timedelta(days=1)
    with pytest.raises(Problem) as denied:control.submit(human,AssistantSubmit(request_id="unfunded-new-work"))
    assert "COST_OBSERVATION_STALE" in denied.value.body["error"]["code"]
    result=control.cancel(human,binding.binding_id)
    assert result.payload["result"]["cancel_accepted"]
    assert h.runtime.job_status(binding.binding_id,human).state=="cancelled"
    assert h.endpoint.requests==[]


def test_current_preflight_is_pure_exact_and_existing_work_does_not_need_new_cost(assistant):
    import hashlib
    from shared_context.behavior_work_policy import WorkPreflightQuery
    h,control,human,facts=enabled(assistant,after_global=True)
    command=AssistantSubmit(request_id="same-original-paid-work")
    query=WorkPreflightQuery(read_id="read-before-submit",request_id=command.request_id,
        submit_body_sha256=hashlib.sha256(command.model_dump_json().encode()).hexdigest())
    before=h.store._guard()
    result=control.preflight(human,query)
    assert result.decision=="allow" and not result.authorization and h.store._guard()==before
    assert result.execution_deadline is None and result.monetary_budget.unit=="USD-micro"
    binding,_=control.submit(human,command)
    h.time+=timedelta(days=1)
    control.cost_reader=lambda:pytest.fail("existing original work must not request fresh paid-work approval")
    duplicate,status=control.submit(human,command)
    assert duplicate==binding and status.state=="queued"
    current=control.preflight(human,query.model_copy(update={"read_id":"fresh-original-read"}))
    assert current.decision=="allow" and current.already_admitted and current.monetary_budget.required_microusd==0
    with pytest.raises(Problem):control.preflight(human,query.model_copy(update={"submit_body_sha256":"f"*64}))


def test_wrong_human_budget_high_water_and_revoked_cached_read_deny(assistant):
    from shared_context.behavior_work_policy import CostLedger
    from shared_context.behavior_operations import _row
    h,control,human,facts=enabled(assistant,after_global=True)
    guard=h.store._guard()
    with pytest.raises(Problem):control.submit(replace(human,oid="wrong-human"),AssistantSubmit(request_id="wrong"))
    assert h.store._guard()==guard
    binding,_=control.submit(human,AssistantSubmit(request_id="one-authorized-work"))
    h.runtime.controller.step(binding.binding_id)
    first=h.runtime.fetch(binding.binding_id,wire.Command(request_id="same-read"),human)
    facts[0]=facts[0].model_copy(update={"observed_cumulative_microusd":109_000_000,"observed_at":h.time})
    with pytest.raises(Problem):control.submit(human,AssistantSubmit(request_id="over-cumulative-budget"))
    ledger=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    assert ledger.observed_high_water_microusd==109_000_000 and ledger.reservation_count==1
    facts[0]=facts[0].model_copy(update={"observed_cumulative_microusd":0})
    with pytest.raises(Problem):control.submit(human,AssistantSubmit(request_id="cannot-zero-spending"))
    # Cost trouble does not revoke reading; only the explicit owner change does.
    assert h.runtime.fetch(binding.binding_id,wire.Command(request_id="same-read"),human).payload==first.payload
    control.revoke(human,"explicit-policy-revocation")
    with pytest.raises(Problem):h.runtime.fetch(binding.binding_id,wire.Command(request_id="same-read"),human)
    with pytest.raises(Problem):control.submit(human,AssistantSubmit(request_id="revoked-new-work"))


@pytest.mark.parametrize("lost_stage",["registration","admission","ack"])
def test_original_request_ackloss_does_not_reserve_or_execute_twice(assistant,monkeypatch,lost_stage):
    import json
    from azure.core.exceptions import ServiceRequestError
    from test_behavior_store import parse_batch
    from shared_context.behavior_work_policy import derive_work,work_policy_key,CostLedger,WorkPolicyControl
    from shared_context.behavior_operations import control_key,_row
    from shared_context.behavior_store import ControlRequest
    h,control,human,facts=enabled(assistant,after_global=True)
    command=AssistantSubmit(request_id="one-original-request")
    admission,binding,_=derive_work(control.policy,command.request_id,h.runtime.mode)
    wanted=(work_policy_key("registration",admission) if lost_stage=="registration" else
        control_key("delivery-request","admit",binding.event_id) if lost_stage=="admission" else
        control_key("inbox-acknowledge",control_key("delivery-final-id",binding.obligation_id)))
    send=h.transport.send;lost=[];posts=[]
    def transmit(request,**kwargs):
        response=send(request,**kwargs)
        if request.method=="POST":
            ops,_=parse_batch(request)
            receipt=next(json.loads(op[1]["Payload"]) for op in ops if "behavior:v1:receipt:" in op[1]["RowKey"])
            if receipt["request"]["request_id"]==wanted:
                posts.append(True)
                if not lost:
                    lost.append(receipt["request"]);raise ServiceRequestError("offline committed reply lost")
        return response
    monkeypatch.setattr(h.transport,"send",transmit)
    if lost_stage!="ack":
        with pytest.raises(Problem):control.submit(human,command)
        h.store.reconcile(ControlRequest.model_validate_json(json.dumps(lost[0])))
        cold=WorkPolicyControl(h.runtime,control.policy,lambda:facts[0])
        actual,status=cold.submit(human,command)
        assert actual==binding and status.state=="queued"
        assert control.submit(human,command)[1].state=="queued"
    else:
        control.submit(human,command)
        h.runtime.controller.step(binding.binding_id)
        got=h.runtime.fetch(binding.binding_id,wire.Command(request_id="fetch"),human)
        ack=wire.PullAcknowledgment(request_id="original-ack",exchange_id=got.exchange_id,
            acknowledgment_token=got.acknowledgment_token,binding=wire.DeliveryBinding.model_validate_json(got.binding.model_dump_json()))
        with pytest.raises(Problem):h.runtime.acknowledge(binding.binding_id,ack,human)
        h.store.reconcile(ControlRequest.model_validate_json(json.dumps(lost[0])))
        assert h.runtime.acknowledge(binding.binding_id,ack,human).payload["result"]["fulfillment"]=="fulfilled"
        assert len(h.endpoint.requests)==3
    assert len(posts)==1 and lost
    ledger=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    assert ledger.reservation_count==1 and ledger.work_reservations_microusd==control.policy.work_estimate_microusd


def test_expired_queued_work_cannot_mint_a_new_dispatch_deadline(assistant):
    h,control,human,facts=enabled(assistant,after_global=True)
    binding,_=control.submit(human,AssistantSubmit(request_id="one-original-expiring-work"))
    h.time+=timedelta(seconds=control.policy.queue_seconds+1)
    facts[0]=facts[0].model_copy(update={"observed_at":h.time})
    before=h.store._guard()
    with pytest.raises(Problem) as denied:h.runtime.controller.step(binding.binding_id)
    assert denied.value.body["error"]["code"]=="WORK_POLICY_START_FENCED"
    assert h.workers.inspect(binding.job_id).generation==0 and h.store._guard()==before
    assert h.endpoint.requests==[]
