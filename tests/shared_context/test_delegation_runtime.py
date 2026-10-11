"""SDK public event/hook contract; deterministic doubles, not another agent loop."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from shared_context.behavior_delegation_runtime import DelegationRuntime
from shared_context.behavior_delegation_wire import DelegationReply
from shared_context.errors import Problem


TASK = {"name": "one-child", "description": "Compare supplied material", "prompt": "850 - 320",
        "agent_type": "fa-delegate-v1", "mode": "sync"}


def event(kind, identity, data, agent=None):
    return {"type": kind, "id": identity, "agentId": agent, "data": data}


def run(awaitable):
    return asyncio.run(awaitable)


def test_original_tool_start_id_is_reserved_before_the_hook_allows_sdk_dispatch():
    async def scenario():
        calls = []
        async def current():
            calls.append("current")
            return True
        async def api(action, command):
            calls.append((action, command))
            return DelegationReply(parent_job_id="parent-job", sdk_session_id="parent-sdk",
                delegation_id="original-delegation", tool_call_id=command.tool_call_id,
                input_sha256=hashlib.sha256(json.dumps(TASK, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                child_agent_id=None, status="unknown", result=None, failure_code=None, fresh=True)
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        bridge.observe(event("tool.execution_start", "original-event",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": TASK}))
        response = await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                                         {"session_id": "parent-sdk"})
        assert response["permissionDecision"] == "allow"
        assert calls[0] == "current"
        assert calls[1][0] == "prepare" and calls[1][1].tool_call_id == "original-call"
        assert calls[1][1].arguments.model_dump() == TASK
    run(scenario())


def test_missing_original_id_is_denied_without_attempting_reservation(caplog):
    async def scenario():
        calls = []
        async def api(*args):
            calls.append(args)
        async def current():
            return True
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        response = await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                                         {"session_id": "parent-sdk"})
        assert response["permissionDecision"] == "deny" and calls == []
        assert "reason=tool_call_unmatched" in caplog.text
    run(scenario())


def test_reused_start_id_with_different_arguments_is_never_a_new_dispatch():
    async def scenario():
        calls = []
        async def api(*args):
            calls.append(args)
        async def current():
            return True
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        for arguments in (TASK, {**TASK, "prompt": "Different content under the original ID."}):
            bridge.observe(event("tool.execution_start", "event",
                                 {"toolName": "task", "toolCallId": "original-call", "arguments": arguments}))
        denied = await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": arguments},
                                       {"session_id": "parent-sdk"})
        assert denied["permissionDecision"] == "deny" and calls == []
    run(scenario())


def reply(**updates):
    return DelegationReply(parent_job_id="parent-job", sdk_session_id="parent-sdk",
        delegation_id="original-delegation", tool_call_id="original-call",
        input_sha256=hashlib.sha256(json.dumps(TASK, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        child_agent_id=None, status="unknown", result=None, failure_code=None).model_copy(update=updates)


def test_child_events_are_saved_before_the_original_tool_result_reaches_the_parent():
    async def scenario():
        calls = []
        responses = iter((reply(fresh=True), reply(child_agent_id="original-child", status="running"),
                          reply(child_agent_id="original-child", status="completed", result="Complete public result.")))
        async def api(action, command):
            calls.append((action, command))
            return next(responses)
        async def current():
            return True
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        bridge.observe(event("tool.execution_start", "original-event",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": TASK}))
        assert (await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                                     {"session_id": "parent-sdk"}))["permissionDecision"] == "allow"
        bridge.observe(event("subagent.started", "started",
                             {"toolCallId": "original-call", "agentName": "fa-delegate-v1"}, "original-child"))
        reference = await bridge.model_ref(SimpleNamespace(session_id="parent-sdk", agent_id="original-child",
                                                           parent_agent_id="parent-sdk"))
        assert reference.child_agent_id == "original-child" and reference.tool_call_id == "original-call"
        bridge.observe(event("assistant.message", "child-answer",
                             {"content": "Complete public result.", "toolRequests": []}, "original-child"))
        bridge.observe(event("subagent.completed", "completed",
                             {"toolCallId": "original-call"}, "original-child"))
        result = await bridge.post_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK,
            "toolResult": {"resultType": "success", "textResultForLlm": "Complete public result."}},
            {"session_id": "parent-sdk"})
        receipt = DelegationReply.model_validate_json(result["modifiedResult"]["textResultForLlm"])
        assert receipt.status == "completed" and receipt.result == "Complete public result."
        assert [item.kind for action, item in calls if action == "observe"] == ["started", "result"]
        assert calls[-1][1].event_id == "completed"
        assert calls[-1][1].child_agent_id == "original-child"
        await bridge.require_complete()
        await bridge.request_cancel()
        bridge.observe(event("session.idle", "later-parent-cancel", {"aborted": True}))
        await bridge.drain()
        assert len(calls) == 3 and bridge.reply.status == "completed"
    run(scenario())


def test_post_hook_waits_for_the_original_child_completion_event_when_notifications_arrive_later():
    async def scenario():
        responses = iter((reply(fresh=True), reply(child_agent_id="child", status="running"),
                          reply(child_agent_id="child", status="completed", result="Original child result.")))
        async def api(*args):
            return next(responses)
        async def current():
            return True
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        bridge.observe(event("tool.execution_start", "start",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": TASK}))
        await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                              {"session_id": "parent-sdk"})
        bridge.observe(event("subagent.started", "child-start",
                             {"toolCallId": "original-call", "agentName": "fa-delegate-v1"}, "child"))
        await bridge.drain()
        waiting = asyncio.create_task(bridge.post_tool({
            "sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK,
            "toolResult": {"resultType": "success", "textResultForLlm": "Original child result."},
        }, {"session_id": "parent-sdk"}))
        try:
            await asyncio.sleep(0)
            assert not waiting.done()
            bridge.observe(event("assistant.message", "child-answer",
                                 {"content": "Original child result.", "toolRequests": []}, "child"))
            bridge.observe(event("subagent.completed", "child-complete",
                                 {"toolCallId": "original-call"}, "child"))
            result = await asyncio.wait_for(waiting, timeout=2)
            assert result["modifiedResult"]["resultType"] == "success"
            assert DelegationReply.model_validate_json(result["modifiedResult"]["textResultForLlm"]).status == "completed"
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    run(scenario())


def test_missing_completion_observation_stays_unknown_and_cannot_be_resent(monkeypatch):
    from shared_context import behavior_delegation_runtime as runtime
    monkeypatch.setattr(runtime, "CAPABILITY_CLIENT_SECONDS", 0.01)
    async def scenario():
        responses = iter((reply(fresh=True), reply(child_agent_id="child", status="running")))
        async def api(*args):
            return next(responses)
        async def current():
            return True
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        bridge.observe(event("tool.execution_start", "start",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": TASK}))
        await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                              {"session_id": "parent-sdk"})
        bridge.observe(event("subagent.started", "child-start",
                             {"toolCallId": "original-call", "agentName": "fa-delegate-v1"}, "child"))
        result = await bridge.post_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK,
            "toolResult": {"resultType": "success", "textResultForLlm": "Unconfirmed text."}},
            {"session_id": "parent-sdk"})
        assert result["modifiedResult"]["resultType"] == "failure"
        assert bridge.failure.body["error"]["code"] == "DELEGATION_UNKNOWN"
        assert bridge.tool_call_id == "original-call" and bridge.reply.status == "running"
        with pytest.raises(Problem):
            await bridge.require_complete()
    run(scenario())


def test_model_override_is_rejected_before_the_child_or_ledger_starts(caplog):
    async def scenario():
        calls = []
        async def api(*args):
            calls.append(args)
        async def current():
            return True
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        args = {**TASK, "model": "unapproved-model"}
        bridge.observe(event("tool.execution_start", "event",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": args}))
        denied = await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": args},
                                       {"session_id": "parent-sdk"})
        assert denied["permissionDecision"] == "deny" and calls == []
        assert "reason=unsupported_arguments" in caplog.text
        assert "unapproved-model" not in caplog.text and TASK["prompt"] not in caplog.text
    run(scenario())


def test_parent_effect_tool_uses_its_existing_permission_path_not_a_delegation_fence():
    async def scenario():
        async def api(*args):
            pytest.fail("An ordinary parent tool must not reserve another delegation")
        async def current():
            return True
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        assert await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "execute_operation", "toolArgs": {}},
                                     {"session_id": "parent-sdk"}) is None
    run(scenario())


def test_oversized_task_is_an_explicit_refusal_not_a_poisoned_unknown_parent_turn():
    async def scenario():
        calls = []
        async def api(*args):
            calls.append(args)
            raise Problem(413, "REQUEST_TOO_LARGE", "Synthetic request limit.")
        async def current():
            return True
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        arguments = {**TASK, "prompt": "x" * (192 * 1024)}
        bridge.observe(event("tool.execution_start", "large-event",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": arguments}))
        value = await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": arguments},
                                      {"session_id": "parent-sdk"})
        assert value["permissionDecision"] == "deny"
        assert "size" in value["permissionDecisionReason"].lower()
        assert calls == [] and bridge.failure is None and bridge.tool_call_id is None
        assert await bridge.model_ref(SimpleNamespace(
            session_id="parent-sdk", agent_id="parent-sdk", parent_agent_id=None)) is None
    run(scenario())


def test_typed_server_before_reservation_refusal_leaves_the_parent_usable():
    async def scenario():
        async def current():
            return True
        async def api(*args):
            raise Problem(413, "DELEGATION_PREPARE_TOO_LARGE", "Original server refused before reservation.")
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        bridge.observe(event("tool.execution_start", "event",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": TASK}))
        value = await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                                      {"session_id": "parent-sdk"})
        assert value["permissionDecision"] == "deny" and "size" in value["permissionDecisionReason"].lower()
        assert bridge.failure is None and bridge.tool_call_id is None
        assert await bridge.model_ref(SimpleNamespace(
            session_id="parent-sdk", agent_id="parent-sdk", parent_agent_id=None)) is None
    run(scenario())


@pytest.mark.parametrize("first", ["child", "parent"])
def test_cancellation_records_both_original_observations_not_just_abort_ack(first):
    async def scenario():
        observations = []
        state = reply(fresh=True)
        async def api(action, command):
            nonlocal state
            if action == "prepare":
                return state
            observations.append(command)
            if command.kind == "started":
                state = state.model_copy(update={"fresh": False, "status": "running", "child_agent_id": "child"})
            elif command.kind == "cancel":
                state = state.model_copy(update={"cancel_requested": True, "status": "unknown"})
            elif command.kind == "result":
                state = state.model_copy(update={"child_cancelled": True})
            elif command.kind == "parent_idle":
                state = state.model_copy(update={"parent_idle": True})
            if state.parent_idle and state.child_cancelled:
                state = state.model_copy(update={"status": "cancelled", "stop_confirmed": True})
            return state
        async def current():
            return True
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        bridge.observe(event("tool.execution_start", "start",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": TASK}))
        await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                              {"session_id": "parent-sdk"})
        bridge.observe(event("subagent.started", "child-start",
                             {"toolCallId": "original-call", "agentName": "fa-delegate-v1"}, "child"))
        await bridge.request_cancel()
        assert state.cancel_requested and not state.stop_confirmed
        events = {
            "child": event("subagent.completed", "child-cancelled",
                           {"toolCallId": "original-call", "cancelled": True}, "child"),
            "parent": event("session.idle", "parent-idle", {"aborted": True}),
        }
        bridge.observe(events[first])
        await bridge.drain()
        assert not state.stop_confirmed
        bridge.observe(events["parent" if first == "child" else "child"])
        await bridge.drain()
        assert state.stop_confirmed and state.status == "cancelled"
        assert {item.event_id for item in observations} >= {"child-cancelled", "parent-idle"}
        with pytest.raises(Problem):
            await bridge.require_complete()
    run(scenario())


def test_replayed_intent_cannot_authorize_a_second_child():
    async def scenario():
        async def current():
            return True
        async def api(*args):
            return reply()
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        bridge.observe(event("tool.execution_start", "start",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": TASK}))
        denied = await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                                       {"session_id": "parent-sdk"})
        assert denied["permissionDecision"] == "deny"
        with pytest.raises(Problem):
            await bridge.model_ref(SimpleNamespace(session_id="parent-sdk", agent_id="parent-sdk", parent_agent_id=None))
    run(scenario())


def test_current_work_refusal_denies_before_reserving_or_spawning():
    async def scenario():
        calls = []
        async def current():
            return False
        async def api(*args):
            calls.append(args)
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        bridge.observe(event("tool.execution_start", "start",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": TASK}))
        denied = await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                                       {"session_id": "parent-sdk"})
        assert denied["permissionDecision"] == "deny" and calls == []
    run(scenario())


def test_lost_original_prepare_reply_is_not_retried_under_a_new_tool_call_id():
    async def scenario():
        calls = []
        async def current():
            return True
        async def api(*args):
            calls.append(args)
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic commit response loss.")
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        for identity in ("original-call", "replacement-call"):
            bridge.observe(event("tool.execution_start", "start-" + identity,
                                 {"toolName": "task", "toolCallId": identity, "arguments": TASK}))
            denied = await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                                           {"session_id": "parent-sdk"})
            assert denied["permissionDecision"] == "deny"
        assert len(calls) == 1 and calls[0][1].tool_call_id == "original-call"
        with pytest.raises(Problem):
            await bridge.require_complete()
    run(scenario())


def test_incomplete_child_receipt_is_returned_as_failure_and_never_parent_success():
    async def scenario():
        responses = iter((reply(fresh=True), reply(child_agent_id="child", status="running"),
                          reply(child_agent_id="child", status="incomplete", failure_code="DELEGATION_RESULT_INCOMPLETE")))
        async def current():
            return True
        async def api(*args):
            return next(responses)
        bridge = DelegationRuntime("parent-job", "parent-sdk", api, current)
        bridge.observe(event("tool.execution_start", "start",
                             {"toolName": "task", "toolCallId": "original-call", "arguments": TASK}))
        await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK},
                              {"session_id": "parent-sdk"})
        bridge.observe(event("subagent.started", "child-start",
                             {"toolCallId": "original-call", "agentName": "fa-delegate-v1"}, "child"))
        bridge.observe(event("subagent.completed", "child-end", {"toolCallId": "original-call"}, "child"))
        result = await bridge.post_tool({"sessionId": "parent-sdk", "toolName": "task", "toolArgs": TASK,
                                         "toolResult": {"resultType": "success", "textResultForLlm": ""}},
                                        {"session_id": "parent-sdk"})
        assert result["modifiedResult"]["resultType"] == "failure"
        assert DelegationReply.model_validate_json(result["modifiedResult"]["textResultForLlm"]).status == "incomplete"
        with pytest.raises(Problem):
            await bridge.require_complete()
    run(scenario())


def _digest(task):
    return hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _parallel_bridge(tasks, calls, limit=4):
    async def current():
        return True

    async def api(action, command):
        calls.append((action, command.tool_call_id))
        values = dict(parent_job_id="parent-job", sdk_session_id="parent-sdk", delegation_id="d-" + command.tool_call_id,
                      tool_call_id=command.tool_call_id, input_sha256=_digest(tasks[command.tool_call_id]),
                      failure_code=None)
        if action == "prepare":
            return DelegationReply(**values, child_agent_id=None, status="unknown", result=None, fresh=True)
        if command.kind == "started":
            return DelegationReply(**values, child_agent_id=command.child_agent_id, status="running", result=None)
        return DelegationReply(**values, child_agent_id=command.child_agent_id, status="completed", result=command.text)

    class Parallel(DelegationRuntime):
        max_children = limit

    bridge = Parallel("parent-job", "parent-sdk", api, current)
    for identity, task in tasks.items():
        bridge.observe(event("tool.execution_start", "start-" + identity,
                             {"toolName": "task", "toolCallId": identity, "arguments": task}))
    return bridge


def _hook(task):
    return {"sessionId": "parent-sdk", "toolName": "task", "toolArgs": task}, {"session_id": "parent-sdk"}


def test_parallel_task_calls_each_keep_their_own_child_and_result():
    """Stage B (2026-10-04): a second parallel task was refused as 'reserved or unknown'."""
    async def scenario():
        tasks = {"call-cities": {**TASK, "name": "cities", "prompt": "List five municipalities."},
                 "call-teas": {**TASK, "name": "teas", "prompt": "List three teas."}}
        calls = []
        bridge = _parallel_bridge(tasks, calls)
        decisions = await asyncio.gather(*(bridge.pre_tool(*_hook(task)) for task in tasks.values()))
        assert [decision["permissionDecision"] for decision in decisions] == ["allow", "allow"]
        for identity, child in (("call-cities", "child-a"), ("call-teas", "child-b")):
            bridge.observe(event("subagent.started", "s-" + identity,
                                 {"toolCallId": identity, "agentName": "fa-delegate-v1"}, child))
        for identity, child, text in (("call-teas", "child-b", "Oolong"), ("call-cities", "child-a", "Taipei, Tainan")):
            bridge.observe(event("assistant.message", "m-" + identity, {"content": text}, child))
            bridge.observe(event("subagent.completed", "c-" + identity, {"toolCallId": identity}, child))
        for identity, text in (("call-cities", "Taipei, Tainan"), ("call-teas", "Oolong")):
            data, context = _hook(tasks[identity])
            result = await bridge.post_tool({**data, "toolResult": {"resultType": "success", "textResultForLlm": text}},
                                            context)
            saved = DelegationReply.model_validate_json(result["modifiedResult"]["textResultForLlm"])
            assert result["modifiedResult"]["resultType"] == "success"
            assert (saved.tool_call_id, saved.child_agent_id, saved.result) == (
                identity, "child-a" if identity == "call-cities" else "child-b", text)
        await bridge.require_complete()
        assert sorted(call for action, call in calls if action == "prepare") == ["call-cities", "call-teas"]
    run(scenario())


def test_calls_beyond_the_turn_limit_are_told_they_did_not_run(caplog):
    async def scenario():
        tasks = {f"call-{n}": {**TASK, "name": f"part-{n}", "prompt": f"Part {n}."} for n in range(5)}
        calls = []
        bridge = _parallel_bridge(tasks, calls)
        decisions = [await bridge.pre_tool(*_hook(task)) for task in tasks.values()]
        assert [decision["permissionDecision"] for decision in decisions] == ["allow"] * 4 + ["deny"]
        reason = decisions[-1]["permissionDecisionReason"]
        assert "[child_limit]" in reason and "did not run" in reason and "unknown" not in reason
        assert len([call for action, call in calls if action == "prepare"]) == 4
        assert "reason=child_limit" in caplog.text
    run(scenario())


def test_a_call_denied_before_reservation_is_not_started_and_its_retry_matches_only_itself():
    async def scenario():
        tasks = {"first-call": TASK}
        calls, allowed = [], iter((False, True))
        bridge = _parallel_bridge(tasks, calls)

        async def current():
            return next(allowed)

        bridge.current = current
        denied = await bridge.pre_tool(*_hook(TASK))
        assert denied["permissionDecision"] == "deny" and calls == []
        assert denied["permissionDecisionReason"].startswith("Delegation not started [current_authority_denied]")
        assert "unknown" not in denied["permissionDecisionReason"]
        # The model resends the same subtask under a new SDK call; it matches only the new start.
        tasks["retry-call"] = TASK
        bridge.observe(event("tool.execution_start", "start-retry",
                             {"toolName": "task", "toolCallId": "retry-call", "arguments": TASK}))
        allowed_retry = await bridge.pre_tool(*_hook(TASK))
        assert allowed_retry["permissionDecision"] == "allow" and calls == [("prepare", "retry-call")]
    run(scenario())


def test_the_job_lane_still_runs_one_child_and_says_a_second_call_did_not_run():
    async def scenario():
        tasks = {"first-call": TASK, "second-call": {**TASK, "prompt": "A different subtask."}}
        calls = []
        bridge = _parallel_bridge(tasks, calls, limit=DelegationRuntime.max_children)
        first = await bridge.pre_tool(*_hook(tasks["first-call"]))
        second = await bridge.pre_tool(*_hook(tasks["second-call"]))
        assert first["permissionDecision"] == "allow" and second["permissionDecision"] == "deny"
        assert "[child_limit]" in second["permissionDecisionReason"] and calls == [("prepare", "first-call")]
    run(scenario())


def test_an_unresolved_reservation_keeps_the_unknown_wording_and_blocks_new_calls():
    async def scenario():
        tasks = {"first-call": TASK, "second-call": {**TASK, "prompt": "Another subtask."}}
        calls = []
        bridge = _parallel_bridge(tasks, calls)

        async def lost(action, command):
            calls.append((action, command.tool_call_id))
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic commit response loss.")

        bridge.call = lost
        first = await bridge.pre_tool(*_hook(tasks["first-call"]))
        assert first["permissionDecision"] == "deny"
        assert "unresolved" in first["permissionDecisionReason"] and "not started" not in first["permissionDecisionReason"]
        second = await bridge.pre_tool(*_hook(tasks["second-call"]))
        assert second["permissionDecision"] == "deny" and "unresolved" in second["permissionDecisionReason"]
        assert calls == [("prepare", "first-call")]
        with pytest.raises(Problem):
            await bridge.require_complete()
    run(scenario())


def test_an_unconfirmed_reservation_in_the_approval_hook_stays_unresolved():
    """With HITL the reservation is written inside before_prepare; an unconfirmed write is not 'not started'."""
    async def scenario():
        tasks = {"first-call": TASK, "second-call": {**TASK, "prompt": "Another subtask."}}
        calls = []
        bridge = _parallel_bridge(tasks, calls)

        async def reserve(command):
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "effect reservation is unconfirmed; do not execute")

        bridge.before_prepare = reserve
        first = await bridge.pre_tool(*_hook(tasks["first-call"]))
        assert first["permissionDecision"] == "deny"
        assert "unresolved" in first["permissionDecisionReason"] and "not started" not in first["permissionDecisionReason"]
        assert "first-call" in bridge.starts
        second = await bridge.pre_tool(*_hook(tasks["second-call"]))
        assert second["permissionDecision"] == "deny" and "unresolved" in second["permissionDecisionReason"]
        assert calls == []
        with pytest.raises(Problem):
            await bridge.require_complete()
    run(scenario())


def test_a_waiting_call_is_refused_once_a_sibling_reservation_became_unresolved():
    async def scenario():
        tasks = {"waiting-call": TASK, "lost-call": {**TASK, "prompt": "Another subtask."}}
        calls = []
        bridge = _parallel_bridge(tasks, calls)
        waiting, release = asyncio.Event(), asyncio.Event()

        async def approve(command):
            if command.tool_call_id == "waiting-call":
                waiting.set()
                await release.wait()

        async def lost(action, command):
            calls.append((action, command.tool_call_id))
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic commit response loss.")

        bridge.before_prepare, bridge.call = approve, lost
        pending = asyncio.create_task(bridge.pre_tool(*_hook(tasks["waiting-call"])))
        await waiting.wait()
        lost_decision = await bridge.pre_tool(*_hook(tasks["lost-call"]))
        assert lost_decision["permissionDecision"] == "deny" and calls == [("prepare", "lost-call")]
        release.set()
        decision = await pending
        assert decision["permissionDecision"] == "deny" and "unresolved" in decision["permissionDecisionReason"]
        assert calls == [("prepare", "lost-call")] and "waiting-call" not in bridge.children
    run(scenario())


def test_finish_records_every_running_child_even_when_one_write_fails():
    async def scenario():
        tasks = {"call-a": TASK, "call-b": {**TASK, "prompt": "Another subtask."}}
        calls = []
        bridge = _parallel_bridge(tasks, calls)
        for identity, child in (("call-a", "child-a"), ("call-b", "child-b")):
            assert (await bridge.pre_tool(*_hook(tasks[identity])))["permissionDecision"] == "allow"
            bridge.observe(event("subagent.started", "s-" + identity,
                                 {"toolCallId": identity, "agentName": "fa-delegate-v1"}, child))
        await bridge.drain()
        unknown = []

        async def flaky(action, command):
            unknown.append(command.tool_call_id)
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic commit response loss.")

        bridge.call = flaky
        with pytest.raises(Problem):
            await bridge.finish()
        assert unknown == ["call-a", "call-b"]
    run(scenario())
