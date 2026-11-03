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


def _started(bridge, identity, child, event_id=None):
    bridge.observe(event("subagent.started", event_id or "s-" + identity,
                         {"toolCallId": identity, "agentName": "fa-delegate-v1"}, child))


def _spy(bridge):
    seen, inner = [], bridge.call

    async def spy(action, command):
        if action == "observe":
            seen.append(command)
        return await inner(action, command)

    bridge.call = spy
    return seen


def test_an_event_is_saved_once_and_an_event_without_an_id_is_never_saved():
    async def scenario():
        bridge = _parallel_bridge({"call-a": TASK}, [])
        seen = _spy(bridge)
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
        _started(bridge, "call-a", "child-a", "same-event")
        _started(bridge, "call-a", "child-a", "same-event")
        bridge.observe(event("subagent.completed", "", {"toolCallId": "call-a"}, "child-a"))
        await bridge.drain()
        assert [item.event_id for item in seen] == ["same-event"] and bridge.failure is None
    run(scenario())


def test_a_second_child_identity_for_one_call_is_an_invalid_observation():
    async def scenario():
        bridge = _parallel_bridge({"call-a": TASK}, [])
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
        _started(bridge, "call-a", "child-a")
        _started(bridge, "call-a", "child-b", "s-second")
        with pytest.raises(Problem) as caught:
            await bridge.drain()
        assert caught.value.body["error"]["code"] == "DELEGATION_OBSERVATION_INVALID"
        assert bridge.children["call-a"].completion_observed.is_set()
    run(scenario())


def test_one_sdk_agent_cannot_be_the_child_of_two_calls():
    async def scenario():
        tasks = {"call-a": TASK, "call-b": {**TASK, "prompt": "Another subtask."}}
        bridge = _parallel_bridge(tasks, [])
        for task in tasks.values():
            assert (await bridge.pre_tool(*_hook(task)))["permissionDecision"] == "allow"
        _started(bridge, "call-a", "shared")
        _started(bridge, "call-b", "shared")
        with pytest.raises(Problem) as caught:
            await bridge.drain()
        assert caught.value.body["error"]["code"] == "DELEGATION_OBSERVATION_INVALID"
    run(scenario())


def test_a_start_event_must_name_the_delegate_agent_type():
    async def scenario():
        bridge = _parallel_bridge({"call-a": TASK}, [])
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
        bridge.observe(event("subagent.started", "s", {"toolCallId": "call-a", "agentName": "other-agent"}, "child-a"))
        with pytest.raises(Problem) as caught:
            await bridge.drain()
        assert caught.value.body["error"]["code"] == "DELEGATION_OBSERVATION_INVALID"
        assert bridge.children["call-a"].child_id is None
    run(scenario())


@pytest.mark.parametrize("requests, ended_in_tool_call", [
    ({"toolRequests": [{"name": "read"}]}, True), ({"tool_requests": [{"name": "read"}]}, True),
    ({"toolRequests": []}, False), ({}, False)])
def test_the_result_observation_says_whether_the_childs_last_message_asked_for_tools(requests, ended_in_tool_call):
    async def scenario():
        bridge = _parallel_bridge({"call-a": TASK}, [])
        seen = _spy(bridge)
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
        _started(bridge, "call-a", "child-a")
        bridge.observe(event("assistant.message", "m", {"content": "partial", **requests}, "child-a"))
        bridge.observe(event("subagent.completed", "c", {"toolCallId": "call-a"}, "child-a"))
        await bridge.drain()
        result = seen[-1]
        assert (result.kind, result.text, result.ended_in_tool_call) == ("result", "partial", ended_in_tool_call)
    run(scenario())


def test_a_child_message_that_is_not_text_is_an_invalid_observation():
    async def scenario():
        bridge = _parallel_bridge({"call-a": TASK}, [])
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
        _started(bridge, "call-a", "child-a")
        bridge.observe(event("assistant.message", "m", {"content": {"not": "text"}}, "child-a"))
        with pytest.raises(Problem) as caught:
            await bridge.drain()
        assert caught.value.body["error"]["code"] == "DELEGATION_OBSERVATION_INVALID"
    run(scenario())


@pytest.mark.parametrize("kind, cancelled, outcome, failure_code", [
    ("subagent.completed", False, "completed", None),
    ("subagent.failed", False, "failed", "SDK_CHILD_FAILED"),
    ("subagent.failed", True, "cancelled", None),
    ("subagent.completed", True, "cancelled", None)])
def test_the_result_observation_names_how_the_child_ended(kind, cancelled, outcome, failure_code):
    async def scenario():
        bridge = _parallel_bridge({"call-a": TASK}, [])
        seen = _spy(bridge)
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
        _started(bridge, "call-a", "child-a")
        bridge.observe(event("assistant.message", "m", {"content": "last words"}, "child-a"))
        bridge.observe(event(kind, "end", {"toolCallId": "call-a", **({"cancelled": True} if cancelled else {})}, "child-a"))
        await bridge.drain()
        assert (seen[-1].kind, seen[-1].outcome, seen[-1].failure_code) == ("result", outcome, failure_code)
        assert bridge.cancelled is cancelled and bridge.children["call-a"].finished is (not cancelled)
        assert bridge.children["call-a"].completion_observed.is_set()
    run(scenario())


def test_a_child_cancelled_after_the_parent_went_idle_also_records_that_idle():
    async def scenario():
        bridge = _parallel_bridge({"call-a": TASK}, [])
        seen, inner = [], bridge.call
        final = DelegationReply(parent_job_id="parent-job", sdk_session_id="parent-sdk", delegation_id="d-call-a",
                                tool_call_id="call-a", input_sha256=_digest(TASK), child_agent_id="child-a",
                                status="completed", result="last words", failure_code=None)

        async def record(action, command):
            if action == "observe":
                seen.append(command)
                if command.kind == "parent_idle":
                    return final
            return await inner(action, command)

        bridge.call = record
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
        _started(bridge, "call-a", "child-a")
        bridge.observe(event("assistant.message", "m", {"content": "last words"}, "child-a"))
        bridge.observe(event("session.idle", "parent-idle", {"aborted": True}))
        await bridge.drain()
        assert [item.kind for item in seen] == ["started"]
        bridge.observe(event("subagent.completed", "end", {"toolCallId": "call-a", "cancelled": True}, "child-a"))
        await bridge.drain()
        assert [(item.kind, item.event_id) for item in seen[1:]] == [("result", "end"), ("parent_idle", "parent-idle")]
    run(scenario())


def test_a_parent_idle_that_is_not_an_abort_or_belongs_to_another_agent_records_nothing():
    async def scenario():
        bridge = _parallel_bridge({"call-a": TASK}, [])
        seen = _spy(bridge)
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
        _started(bridge, "call-a", "child-a")
        bridge.observe(event("session.idle", "plain-idle", {}))
        bridge.observe(event("session.idle", "child-idle", {"aborted": True}, "child-a"))
        await bridge.drain()
        assert [item.kind for item in seen] == ["started"] and bridge.children["call-a"].idle_event_id is None
    run(scenario())


async def _running_child(read_user_files=True):
    bridge = _parallel_bridge({"call-a": TASK}, [])
    bridge.read_user_files = read_user_files
    assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
    _started(bridge, "call-a", "child-a")
    await bridge.drain()
    return bridge


def _child_hook(session="child-a", tool="read_user_file"):
    return {"sessionId": session, "toolName": tool}, {"session_id": "parent-sdk"}


def test_the_running_child_may_read_user_files_when_its_original_call_is_still_current():
    async def scenario():
        bridge = await _running_child()
        assert await bridge.pre_tool(*_child_hook()) == {"permissionDecision": "allow"}
    run(scenario())


@pytest.mark.parametrize("case, reason", [
    ("lane_off", "child_tool_denied"), ("other_tool", "child_tool_denied"), ("unknown_child", "child_tool_denied"),
    ("cancelled", "child_tool_denied"), ("not_running", "original_child_inactive"),
    ("other_agent", "original_child_inactive"), ("not_current", "original_child_inactive"),
    ("truthy_current", "original_child_inactive"), ("no_reply", "original_child_inactive"),
    ("unconfirmed", "original_child_unconfirmed")])
def test_a_child_file_read_is_refused_unless_the_original_child_is_running_and_current(case, reason, caplog):
    async def scenario():
        bridge = await _running_child(read_user_files=case != "lane_off")
        child = bridge.children["call-a"]
        hook = _child_hook("other-child" if case == "unknown_child" else "child-a",
                           "write_file" if case == "other_tool" else "read_user_file")
        if case == "cancelled":
            bridge.cancelled = True
        elif case == "not_running":
            child.reply = child.reply.model_copy(update={"status": "completed"})
        elif case == "other_agent":
            child.reply = child.reply.model_copy(update={"child_agent_id": "child-b"})
        elif case == "no_reply":
            child.reply = None
        elif case in {"not_current", "truthy_current", "unconfirmed"}:
            async def current():
                if case == "unconfirmed":
                    raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic.")
                return False if case == "not_current" else "yes"
            bridge.current = current
        decision = await bridge.pre_tool(*hook)
        assert decision["permissionDecision"] == "deny" and f"[{reason}]" in decision["permissionDecisionReason"]
        assert f"reason={reason}" in caplog.text
    run(scenario())


def test_a_hook_from_another_session_is_refused_before_anything_else():
    async def scenario():
        bridge = await _running_child()
        data, _ = _child_hook()
        decision = await bridge.pre_tool(data, {"session_id": "other-session"})
        assert decision["permissionDecision"] == "deny" and "[session_mismatch]" in decision["permissionDecisionReason"]
        assert await bridge.pre_tool({"sessionId": "parent-sdk", "toolName": "other"}, {"session_id": "parent-sdk"}) is None
    run(scenario())


def _held_approval(bridge):
    """before_prepare that parks the first call until released, like a human deciding."""
    waiting, release = asyncio.Event(), asyncio.Event()

    async def approve(command):
        waiting.set()
        await release.wait()

    bridge.before_prepare = approve
    return waiting, release


def test_a_second_call_cannot_take_the_slot_of_a_call_still_waiting_for_approval():
    async def scenario():
        tasks = {"first-call": TASK, "second-call": {**TASK, "prompt": "Another subtask."}}
        bridge = _parallel_bridge(tasks, [], limit=1)
        waiting, release = _held_approval(bridge)
        pending = asyncio.create_task(bridge.pre_tool(*_hook(tasks["first-call"])))
        await waiting.wait()
        second = await asyncio.wait_for(bridge.pre_tool(*_hook(tasks["second-call"])), 5)
        assert second["permissionDecision"] == "deny" and "[child_limit]" in second["permissionDecisionReason"]
        assert "second-call" not in bridge.starts
        release.set()
        assert (await pending)["permissionDecision"] == "allow"
    run(scenario())


def test_the_same_call_cannot_be_reserved_twice_while_its_first_hook_is_waiting():
    async def scenario():
        bridge = _parallel_bridge({"only-call": TASK}, [])
        waiting, release = _held_approval(bridge)
        pending = asyncio.create_task(bridge.pre_tool(*_hook(TASK)))
        await waiting.wait()
        repeat = await asyncio.wait_for(bridge.pre_tool(*_hook(TASK)), 5)
        assert repeat["permissionDecision"] == "deny" and "[original_reserved_or_unknown]" in repeat["permissionDecisionReason"]
        assert "only-call" in bridge.starts and bridge.failure is None
        release.set()
        assert (await pending)["permissionDecision"] == "allow"
        assert list(bridge.children) == ["only-call"]
    run(scenario())


def test_a_task_start_is_recorded_only_when_it_comes_from_the_parent_session():
    bridge = _parallel_bridge({}, [])
    for identity, tool, agent in (("from-parent", "task", "parent-sdk"), ("from-nobody", "task", None),
                                  ("from-child", "task", "child-x"), ("not-a-task", "read", None)):
        bridge.observe(event("tool.execution_start", "e-" + identity,
                             {"toolName": tool, "toolCallId": identity, "arguments": TASK}, agent))
    assert set(bridge.starts) == {"from-parent", "from-nobody"}


def test_a_completion_from_another_agent_is_an_invalid_observation():
    async def scenario():
        bridge = _parallel_bridge({"call-a": TASK}, [])
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "allow"
        _started(bridge, "call-a", "child-a")
        bridge.observe(event("subagent.completed", "end", {"toolCallId": "call-a"}, "child-x"))
        with pytest.raises(Problem) as caught:
            await bridge.drain()
        assert caught.value.body["error"]["code"] == "DELEGATION_OBSERVATION_INVALID"
        assert bridge.children["call-a"].finished is False
    run(scenario())


def test_two_starts_with_the_same_arguments_are_ambiguous_and_never_dispatch():
    async def scenario():
        calls = []
        bridge = _parallel_bridge({"call-a": TASK, "call-b": TASK}, calls)
        decision = await bridge.pre_tool(*_hook(TASK))
        assert decision["permissionDecision"] == "deny" and "[tool_call_ambiguous]" in decision["permissionDecisionReason"]
        assert calls == [] and set(bridge.starts) == {"call-a", "call-b"}
        unmatched = await _parallel_bridge({}, calls).pre_tool(*_hook(TASK))
        assert "[tool_call_unmatched]" in unmatched["permissionDecisionReason"]
    run(scenario())


def test_a_cancelled_turn_starts_no_further_subtask_and_forgets_the_call():
    async def scenario():
        calls = []
        bridge = _parallel_bridge({"call-a": TASK}, calls)
        await bridge.request_cancel()
        decision = await bridge.pre_tool(*_hook(TASK))
        assert decision["permissionDecision"] == "deny" and decision["permissionDecisionReason"].startswith(
            "Delegation not started [cancelled]")
        assert calls == [] and "call-a" not in bridge.starts and not bridge.children
    run(scenario())


def test_a_call_after_an_unresolved_reservation_is_refused_before_anyone_is_asked():
    async def scenario():
        tasks = {"first-call": TASK, "second-call": {**TASK, "prompt": "Another subtask."}}
        bridge = _parallel_bridge(tasks, [])

        async def lost(action, command):
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic commit response loss.")

        bridge.call = lost
        assert (await bridge.pre_tool(*_hook(tasks["first-call"])))["permissionDecision"] == "deny"
        asked = []

        async def current():
            asked.append("current")
            return True

        async def approve(command):
            asked.append("approval")

        bridge.current, bridge.before_prepare = current, approve
        second = await bridge.pre_tool(*_hook(tasks["second-call"]))
        assert second["permissionDecision"] == "deny" and "[original_reserved_or_unknown]" in second["permissionDecisionReason"]
        assert asked == [] and "second-call" in bridge.starts
    run(scenario())


@pytest.mark.parametrize("outcome, reason", [
    (RuntimeError("authority store down"), "authority_unavailable"), (False, "current_authority_denied"),
    (None, "current_authority_denied"), ("yes", "current_authority_denied")])
def test_a_call_whose_current_authority_is_not_exactly_true_is_not_started_and_is_forgotten(outcome, reason):
    async def scenario():
        calls = []
        bridge = _parallel_bridge({"only-call": TASK}, calls)

        async def current():
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        bridge.current = current
        decision = await bridge.pre_tool(*_hook(TASK))
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"].startswith(f"Delegation not started [{reason}]")
        assert calls == [] and "only-call" not in bridge.starts and bridge.failure is None and not bridge.children
    run(scenario())


def test_a_denied_approval_forgets_the_call_but_any_other_hook_error_leaves_it_unresolved():
    async def scenario():
        bridge = _parallel_bridge({"denied-call": TASK}, [])

        async def refuse(command):
            raise Problem(403, "NATIVE_HITL_DENIED", "the human said no")

        bridge.before_prepare = refuse
        decision = await bridge.pre_tool(*_hook(TASK))
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"] == "This original child action was not approved; do not resend it."
        assert "denied-call" not in bridge.starts and bridge.failure is None and not bridge._pending

        async def broken(command):
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "unconfirmed")

        bridge.observe(event("tool.execution_start", "again", {"toolName": "task", "toolCallId": "again-call", "arguments": TASK}))
        bridge.before_prepare = broken
        assert (await bridge.pre_tool(*_hook(TASK)))["permissionDecision"] == "deny"
        assert bridge.failure is not None and "again-call" in bridge.starts and not bridge._pending
    run(scenario())


def test_an_earlier_unresolved_reservation_is_not_replaced_by_a_later_hook_error():
    async def scenario():
        tasks = {"waiting-call": TASK, "lost-call": {**TASK, "prompt": "Another subtask."}}
        bridge = _parallel_bridge(tasks, [])
        waiting, release = asyncio.Event(), asyncio.Event()

        async def approve(command):
            if command.tool_call_id == "waiting-call":
                waiting.set()
                await release.wait()
                raise RuntimeError("approval channel broke")

        async def lost(action, command):
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic commit response loss.")

        bridge.before_prepare, bridge.call = approve, lost
        pending = asyncio.create_task(bridge.pre_tool(*_hook(tasks["waiting-call"])))
        await waiting.wait()
        assert (await bridge.pre_tool(*_hook(tasks["lost-call"])))["permissionDecision"] == "deny"
        sibling_failure = bridge.failure
        assert sibling_failure is not None
        release.set()
        assert (await pending)["permissionDecision"] == "deny"
        assert bridge.failure is sibling_failure
    run(scenario())


def test_a_cancel_that_lands_while_the_reservation_is_written_denies_the_dispatch():
    async def scenario():
        bridge = _parallel_bridge({"only-call": TASK}, [])
        inner = bridge.call

        async def reserve_then_cancel(action, command):
            value = await inner(action, command)
            bridge.cancelled = True
            return value

        bridge.call = reserve_then_cancel
        decision = await bridge.pre_tool(*_hook(TASK))
        assert decision["permissionDecision"] == "deny" and "[cancelled]" in decision["permissionDecisionReason"]
        assert bridge.children["only-call"].reply is not None and bridge.failure is None
    run(scenario())


def test_a_first_reservation_that_is_not_fresh_is_an_unresolved_failure():
    async def scenario():
        bridge = _parallel_bridge({"only-call": TASK}, [])
        inner = bridge.call

        async def reserve(action, command):
            value = await inner(action, command)
            return value.model_copy(update={"fresh": False})

        bridge.call = reserve
        decision = await bridge.pre_tool(*_hook(TASK))
        assert decision["permissionDecision"] == "deny" and "[original_reserved_or_unknown]" in decision["permissionDecisionReason"]
        assert bridge.failure.body["error"]["code"] == "DELEGATION_ORIGINAL_REQUIRED"
        with pytest.raises(Problem):
            await bridge.drain()
    run(scenario())


def test_a_server_size_refusal_on_the_reservation_leaves_nothing_behind_and_the_parent_usable():
    from shared_context.behavior_delegation_wire import DELEGATION_SIZE_REFUSAL

    async def scenario():
        bridge = _parallel_bridge({"only-call": TASK}, [])

        async def too_big(action, command):
            raise Problem(413, DELEGATION_SIZE_REFUSAL, "Synthetic size refusal.")

        bridge.call = too_big
        decision = await bridge.pre_tool(*_hook(TASK))
        assert decision["permissionDecision"] == "deny" and "size" in decision["permissionDecisionReason"].lower()
        assert not bridge.children and "only-call" not in bridge.starts and bridge.failure is None
        await bridge.drain()
    run(scenario())


def test_an_allowed_dispatch_hands_the_sdk_the_normalized_task_and_an_oversized_one_is_forgotten():
    async def scenario():
        bridge = _parallel_bridge({"only-call": TASK}, [])
        decision = await bridge.pre_tool(*_hook(TASK))
        assert decision == {"permissionDecision": "allow", "modifiedArgs": TASK}
        big = {**TASK, "prompt": "x" * (200 * 1024)}
        oversized = _parallel_bridge({"big-call": big}, [])
        denied = await oversized.pre_tool(*_hook(big))
        assert denied["permissionDecision"] == "deny" and "192 KiB" in denied["permissionDecisionReason"]
        assert "big-call" not in oversized.starts and not oversized.children and oversized.failure is None
    run(scenario())
