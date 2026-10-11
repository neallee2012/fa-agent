"""Server-internal delegation ledger; SDK/admission capability wiring is not enabled."""

import copy
import hashlib
from types import SimpleNamespace

import pytest

from shared_context.behavior_delegation import CHILD_AGENT_TYPE, DelegationLedger, capture_parent
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn
from test_work_policy_lifecycle import enabled


TASK = {
    "agent_type": "fa-delegate-v1",
    "mode": "sync",
    "name": "compare-fragments",
    "description": "Compare the approved Team fragments",
    "prompt": "Compare Team fragment A v1 (850 total, 320 assigned) and B v1 (530 remaining).",
}


@pytest.fixture
def delegation(assistant, monkeypatch):
    monkeypatch.delenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", raising=False)
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    turn = open_turn(h, control, human, "delegation-parent")
    parent = capture_parent(h.assistant, turn.session, capability_sha256="e" * 64)
    try:
        yield SimpleNamespace(h=h, control=control, human=human, turn=turn, parent=parent,
                              ledger=DelegationLedger(h.store))
    finally:
        turn.client.close()


def test_first_intent_is_durable_and_replay_or_readback_never_returns_a_new_dispatch(delegation):
    c = delegation
    assert CHILD_AGENT_TYPE == TASK["agent_type"]
    first = c.ledger.prepare(c.parent, "task-call-1", TASK)
    assert first.fresh is True
    assert first.view.status == "unknown"
    assert first.view.child_agent_id is None and first.view.result is None
    assert first.view.parent == c.parent
    assert c.parent.execution.work_id == c.turn.binding.work.work_id
    assert c.parent.parent_request_id == "delegation-parent"
    assert c.parent.execution.start_request_id == c.turn.start.binding.start_request_id
    assert c.parent.execution.owner.root_budget_id == c.turn.binding.owner.root_budget_id

    saved = copy.deepcopy(c.h.transport.backing.rows)
    restarted = DelegationLedger(c.h.store)
    replay = restarted.prepare(c.parent, "task-call-1", TASK)
    assert replay.fresh is False and replay.view == first.view
    assert restarted.read(c.parent) == first.view
    assert c.h.transport.backing.rows == saved


def test_complete_child_result_is_durable_but_does_not_finish_the_parent(delegation):
    c = delegation
    c.ledger.prepare(c.parent, "task-call-1", TASK)
    parent_job = c.h.workers.inspect(c.turn.binding.job_id)
    ledger = c.h.store.read_snapshot((c.control.cost_key,))[c.control.cost_key]
    started = c.ledger.record_started(c.parent, "task-call-1", "child-agent-1", event_id="child-start-1")
    assert started.status == "running" and started.child_agent_id == "child-agent-1"
    text = "Complete approved result: 530 remain; refs=team-a-v1,team-b-v1.\n" * 4000
    completed = c.ledger.record_result(c.parent, "task-call-1", "child-agent-1", event_id="child-end-1",
                                       outcome="completed", text=text)
    assert completed.status == "completed" and completed.result == text
    assert DelegationLedger(c.h.store).read(c.parent) == completed
    before = copy.deepcopy(c.h.transport.backing.rows)
    assert c.ledger.record_result(c.parent, "task-call-1", "child-agent-1", event_id="child-end-1",
                                 outcome="completed", text=text) == completed
    assert c.h.transport.backing.rows == before
    assert c.h.workers.inspect(c.turn.binding.job_id) == parent_job
    assert c.h.store.read_snapshot((c.control.cost_key,))[c.control.cost_key] == ledger
    assert not c.h.runtime.job_status(c.turn.binding.binding_id, c.human).obligation_fulfilled


@pytest.mark.parametrize(("text", "ended_in_tool_call"), [
    (None, False), (" \n", False), ("Only the unfinished tool call.", True),
])
def test_empty_or_tool_only_child_output_is_incomplete_not_completed(delegation, text, ended_in_tool_call):
    c = delegation
    c.ledger.prepare(c.parent, "task-call-1", TASK)
    c.ledger.record_started(c.parent, "task-call-1", "child-agent-1", event_id="child-start-1")
    value = c.ledger.record_result(c.parent, "task-call-1", "child-agent-1", event_id="child-end-1",
                                   outcome="completed", text=text, ended_in_tool_call=ended_in_tool_call)
    assert value.status == "incomplete" and value.failure_code == "DELEGATION_RESULT_INCOMPLETE"
    assert value.result == text
    assert DelegationLedger(c.h.store).read(c.parent) == value


def test_unknown_then_explicit_child_failure_keeps_original_parent_and_complete_failure(delegation):
    c = delegation
    first = c.ledger.prepare(c.parent, "task-call-1", TASK)
    unknown = c.ledger.record_unknown(c.parent, "task-call-1", event_id="dispatch-reply-lost",
                                      reason_code="SDK_DISPATCH_UNKNOWN")
    assert unknown.status == "unknown" and unknown.child_agent_id is None
    assert not c.ledger.prepare(c.parent, "task-call-1", TASK).fresh
    failed = c.ledger.record_result(c.parent, "task-call-1", "original-child", event_id="original-failure",
                                    outcome="failed", text="Original public child failure.", failure_code="SDK_CHILD_FAILED")
    assert failed.status == "failed" and failed.failure_code == "SDK_CHILD_FAILED"
    assert failed.result == "Original public child failure."
    assert failed.parent == first.view.parent and failed.tool_call_id == first.view.tool_call_id


@pytest.mark.parametrize("first_proof", ["child", "parent"])
def test_cancel_ack_requires_cancelled_child_and_original_parent_idle(delegation, first_proof):
    c = delegation
    c.ledger.prepare(c.parent, "task-call-1", TASK)
    c.ledger.record_started(c.parent, "task-call-1", "child-agent-1", event_id="child-start-1")
    ack = c.ledger.request_cancel(c.parent, request_id="original-cancel")
    assert ack.status == "unknown" and ack.cancel_requested and not ack.stop_confirmed
    assert not c.ledger.prepare(c.parent, "task-call-1", TASK).fresh

    def child():
        return c.ledger.record_result(c.parent, "task-call-1", "child-agent-1",
            event_id="child-cancelled", outcome="cancelled", text="Original partial public output.")

    def parent():
        return c.ledger.record_parent_idle(c.parent, "task-call-1", "child-agent-1", event_id="parent-idle")

    first, second = (child, parent) if first_proof == "child" else (parent, child)
    pending = first()
    assert pending.status == "unknown" and not pending.stop_confirmed
    assert DelegationLedger(c.h.store).read(c.parent) == pending
    stopped = second()
    assert stopped.status == "cancelled" and stopped.stop_confirmed
    assert stopped.child_cancelled and stopped.parent_idle
    assert stopped.result == "Original partial public output."
    saved = copy.deepcopy(c.h.transport.backing.rows)
    assert child() == parent() == stopped
    assert c.h.transport.backing.rows == saved


def test_child_model_usage_uses_the_existing_parent_cost_and_never_adds_a_second_charge(delegation):
    from shared_context.behavior_assistant_wire import ModelRequest
    from shared_context.behavior_instruction_cost import record_usage
    from shared_context.source_materials import digest
    c = delegation
    c.ledger.prepare(c.parent, "task-call-1", TASK)
    c.ledger.record_started(c.parent, "task-call-1", "child-agent-1", event_id="child-start-1")
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "Synthetic child model request."}],
            "stream": True}
    request_sha256 = digest(body)
    c.turn.client.reserve_model(request_sha256)
    agent = c.h.verifier(c.h.client.build_request("GET", "/", headers=c.h.headers("agent")))
    c.h.assistant.consume_model(c.turn.session, ModelRequest(request_id=request_sha256, body=body), agent)
    original_cost = c.h.store.read_snapshot((c.control.cost_key,))[c.control.cost_key]
    linked = c.ledger.link_model_request(c.parent, "task-call-1", "child-agent-1", request_sha256)
    assert linked.root_budget_id == c.parent.execution.owner.root_budget_id
    assert linked.parent_work_id == c.parent.execution.work_id
    assert linked.session_id == c.parent.session_id and linked.job_id == c.parent.execution.job_id
    pending = c.ledger.read_model_usage(c.parent, request_sha256)
    assert pending.stage == "pending" and pending.input_tokens is None and pending.output_tokens is None
    saved = copy.deepcopy(c.h.transport.backing.rows)
    assert c.ledger.link_model_request(c.parent, "task-call-1", "child-agent-1", request_sha256) == linked
    assert c.h.transport.backing.rows == saved
    assert c.h.store.read_snapshot((c.control.cost_key,))[c.control.cost_key] == original_cost

    record_usage(c.h.assistant, c.turn.session, request_sha256, (37, 11))
    actual = DelegationLedger(c.h.store).read_model_usage(c.parent, request_sha256)
    assert actual.stage == "usage" and (actual.input_tokens, actual.output_tokens) == (37, 11)
    metered = c.h.store.read_snapshot((c.control.cost_key,))[c.control.cost_key]
    c.ledger.record_result(c.parent, "task-call-1", "child-agent-1", event_id="child-end-1",
                           outcome="completed", text="Original child result.")
    assert c.h.store.read_snapshot((c.control.cost_key,))[c.control.cost_key] == metered


@pytest.mark.parametrize("override", [
    {"agent_type": "general-purpose"}, {"mode": "background"}, {"model": "configured-second-model"},
    {"model": None}, {"reasoning_effort": "high"}, {"context_tier": "long_context"}, {"prompt": " \n"},
])
def test_server_fixed_task_rejects_extra_capabilities_before_writing_an_intent(delegation, override):
    c = delegation
    before = copy.deepcopy(c.h.transport.backing.rows)
    with pytest.raises(Problem) as denied:
        c.ledger.prepare(c.parent, "task-call-1", {**TASK, **override})
    assert denied.value.body["error"]["code"] == "DELEGATION_TASK_INVALID"
    assert c.h.transport.backing.rows == before


def test_lost_reservation_response_only_reads_the_original_unknown_intent(delegation):
    c = delegation
    c.h.transport.after_apply = "lost"
    try:
        with pytest.raises(Problem) as lost:
            c.ledger.prepare(c.parent, "task-call-1", TASK)
    finally:
        c.h.transport.after_apply = None
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    before = copy.deepcopy(c.h.transport.backing.rows)
    restarted = DelegationLedger(c.h.store)
    assert restarted.read(c.parent).status == "unknown"
    assert restarted.prepare(c.parent, "task-call-1", TASK).fresh is False
    assert c.h.transport.backing.rows == before


@pytest.mark.parametrize("change", ["tool_call", "input", "attempt", "team"])
def test_original_work_cannot_be_rebound_to_another_call_input_attempt_or_team(delegation, change):
    c = delegation
    original = c.ledger.prepare(c.parent, "task-call-1", TASK).view
    parent, call, arguments = c.parent, "task-call-1", TASK
    if change == "tool_call":
        call = "replacement-call"
    elif change == "input":
        arguments = {**TASK, "prompt": "Different task under the old ID"}
    elif change == "attempt":
        parent = parent.model_copy(update={
            "execution": parent.execution.model_copy(update={"start_request_id": "other-attempt"})})
    else:
        parent = parent.model_copy(update={"team_id": "other-team"})
    before = copy.deepcopy(c.h.transport.backing.rows)
    with pytest.raises(Problem) as denied:
        c.ledger.prepare(parent, call, arguments)
    assert denied.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT"
    assert c.ledger.read(c.parent) == original
    assert c.h.transport.backing.rows == before


def test_result_commit_response_loss_preserves_the_full_original_result(delegation):
    c = delegation
    c.ledger.prepare(c.parent, "task-call-1", TASK)
    c.ledger.record_started(c.parent, "task-call-1", "child-agent-1", event_id="child-start-1")
    c.h.transport.after_apply = "lost"
    try:
        with pytest.raises(Problem) as lost:
            c.ledger.record_result(c.parent, "task-call-1", "child-agent-1", event_id="child-end-1",
                                   outcome="completed", text="The complete original result.")
    finally:
        c.h.transport.after_apply = None
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    before = copy.deepcopy(c.h.transport.backing.rows)
    restarted = DelegationLedger(c.h.store)
    assert restarted.read(c.parent).result == "The complete original result."
    assert restarted.record_result(c.parent, "task-call-1", "child-agent-1", event_id="child-end-1",
                                   outcome="completed", text="The complete original result.").status == "completed"
    assert c.h.transport.backing.rows == before


def test_late_foreign_or_conflicting_child_result_cannot_replace_a_cancelled_child(delegation):
    c = delegation
    c.ledger.prepare(c.parent, "task-call-1", TASK)
    c.ledger.record_started(c.parent, "task-call-1", "child-agent-1", event_id="child-start-1")
    cancelled = c.ledger.record_result(c.parent, "task-call-1", "child-agent-1", event_id="child-cancelled",
                                       outcome="cancelled", text=None)
    assert cancelled.status == "unknown" and cancelled.child_cancelled and not cancelled.stop_confirmed
    before = copy.deepcopy(c.h.transport.backing.rows)
    for child in ("child-agent-1", "other-child"):
        with pytest.raises(Problem):
            c.ledger.record_result(c.parent, "task-call-1", child, event_id="late-completion-" + child,
                                   outcome="completed", text="This is not the original result.")
    assert c.ledger.read(c.parent) == cancelled
    assert c.h.transport.backing.rows == before


@pytest.mark.parametrize("text", [
    "x" * (4 * 1024 * 1024 + 1),
    "".join(hashlib.sha256(str(number).encode()).hexdigest() for number in range(1400)),
], ids=["raw-byte-limit", "compressed-byte-limit"])
def test_oversized_child_result_is_refused_without_truncation_or_a_fake_completed_state(delegation, text):
    c = delegation
    c.ledger.prepare(c.parent, "task-call-1", TASK)
    original = c.ledger.record_started(c.parent, "task-call-1", "child-agent-1", event_id="child-start-1")
    before = copy.deepcopy(c.h.transport.backing.rows)
    with pytest.raises(Problem) as denied:
        c.ledger.record_result(c.parent, "task-call-1", "child-agent-1", event_id="child-end-1",
                               outcome="completed", text=text)
    assert denied.value.body["error"]["code"] == "DELEGATION_RESULT_LIMIT"
    assert c.ledger.read(c.parent) == original
    assert c.h.transport.backing.rows == before
