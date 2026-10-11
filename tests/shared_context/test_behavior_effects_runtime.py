"""Goal edits must not invalidate an already enqueued runtime job."""

from datetime import timedelta

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_effects import GOAL_REVIEW, WorkGoalUpdate
from shared_context.behavior_operations import AuthorizationRequest
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_behavior_api import CTX, h
from test_behavior_delivery import CONTEXT
from test_behavior_effects import EffectHarness
from test_behavior_store import code
from test_m3_auth import jwk_client, signing_key


def approved_goal_change(h):
    before = h.adapter.inspect("work-one", CTX)
    h.references["goal"] = WorkGoalUpdate(
        work_id="work-one", obligation_id=h.binding.obligation_id,
        expected_etag=before.etag, expected_definition_digest=before.definition_digest,
        goal=GOAL_REVIEW,
    )
    prepared = h.post("/operation/prepare", wire.OperationPrepare(request_id="prepare", input_ref="goal"), "agent")
    assert prepared.status_code == 200, prepared.text
    descriptor = prepared.json()["result"]
    approval = wire.OperationApproval(
        request_id="approval", decision_id="decision",
        description_digest=descriptor["description_digest"],
        adapter_contract_digest=descriptor["adapter_contract_digest"],
        expires_at=h.time + timedelta(minutes=2),
    )
    approved = h.post("/operation/approve", approval)
    assert approved.status_code == 200, approved.text
    command = AuthorizationRequest(operation_id="op", attempt_id="attempt", grant_id="grant")
    return before, command


def staged_goal_change(h):
    before, command = approved_goal_change(h)
    h.broker.authorize(command, CTX)
    h.broker.dispatcher._arm(command, CTX)
    h.broker.dispatcher._consume(command, CTX)
    receiver = h.broker.dispatcher.receiver
    return before, receiver, receiver._envelope("op", "attempt").command


@pytest.mark.parametrize("during_start", [False, True], ids=["queued", "executing"])
def test_bound_goal_edit_is_rejected_and_original_job_can_still_complete(h, during_start):
    h.submit()
    before, receiver, command = staged_goal_change(h)

    def reject_edit():
        with pytest.raises(Problem) as refused:
            receiver.receive(command, CTX)
        assert code(refused) == "WORK_EFFECT_EXECUTION_BOUND"
        assert h.adapter.inspect("work-one", CTX) == before

    if during_start:
        start = h.executor.start

        def start_with_goal_edit(request):
            reject_edit()
            return start(request)

        h.executor.start = start_with_goal_edit
        h.workers.adapter = h.executor.adapter()
    else:
        reject_edit()
    result = h.runtime.controller.step("binding-one")
    assert result.state == "succeeded" and result.result_forwarded
    assert len(h.executor.starts) == 1
    assert h.adapter.inspect("work-one", CTX).definition_digest == before.definition_digest


def test_fixed_runtime_goal_is_also_protected_between_admission_and_enqueue(h, monkeypatch):
    enqueue = h.runtime._enqueue

    def pause_enqueue(*_):
        raise Problem(503, "OFFLINE_ENQUEUE_PAUSED", "offline admission/enqueue boundary")

    monkeypatch.setattr(h.runtime, "_enqueue", pause_enqueue)
    admitted = h.post("/work/submit", wire.SubmitWork(request_id="submit-one"))
    assert admitted.status_code == 503
    admission_key = worker_key("admission", h.binding.obligation_id)
    assert h.store.read_snapshot((admission_key,))[admission_key] is None
    before, _ = approved_goal_change(h)
    refused = h.post("/operation/execute", wire.Command(request_id="execute-before-enqueue"), "agent")
    assert refused.status_code == 409
    assert refused.json()["error"]["reconcile_original"] is False
    assert h.adapter.inspect("work-one", CTX) == before
    monkeypatch.setattr(h.runtime, "_enqueue", enqueue)
    h.submit()
    assert h.runtime.controller.step("binding-one").state == "succeeded"
    assert len(h.executor.starts) == 1


def test_concurrent_enqueue_marker_fences_the_goal_edit_transaction():
    fixture = EffectHarness()
    command = fixture.stage()
    before = fixture.work()
    admission_key = worker_key("admission", fixture.ob)

    def enqueue_marker():
        fixture.store.transact(
            ControlRequest(request_id="concurrent-enqueue", request_digest="c" * 64),
            lambda _: ControlChanges(writes=(ControlWrite(
                row_id=admission_key, mode="create", data={"job_id": "concurrent-job"},
            ),), result={"offline": True}),
        )

    fixture.client.before_submit = enqueue_marker
    with pytest.raises(Problem) as conflict:
        fixture.receiver.receive(command, CONTEXT)
    assert code(conflict) == "BEHAVIOR_CONFLICT"
    assert fixture.work() == before
    assert fixture.store.read_snapshot((admission_key,))[admission_key] is not None
    with pytest.raises(Problem) as refused:
        fixture.receiver.receive(command, CONTEXT)
    assert code(refused) == "WORK_EFFECT_EXECUTION_BOUND"
    assert fixture.work() == before


def test_applied_receipt_remains_reconcilable_after_later_enqueue():
    fixture = EffectHarness()
    command = fixture.stage()
    receipt = fixture.receiver.receive(command, CONTEXT)
    changed = fixture.work()
    fixture.store.transact(
        ControlRequest(request_id="later-enqueue", request_digest="d" * 64),
        lambda _: ControlChanges(writes=(ControlWrite(
            row_id=worker_key("admission", fixture.ob), mode="create", data={"job_id": "later-job"},
        ),), result={"offline": True}),
    )
    assert fixture.receiver.receive(command, CONTEXT) == receipt
    assert fixture.receiver.reconcile("op-a", "a", CONTEXT) == receipt
    assert fixture.work() == changed
