"""The existing conversation shows recovery as a related input, never a new human send."""

import hashlib

import pytest

from shared_context.behavior_native_turn_wire import (
    NativeContinuationSubmit, NativeConversation, NativeHistoryPage, NativePublicTurn,
    NativeReceipt, NativeResultPage, NativeResultReference, continuation_request_id,
)
from shared_context.behavior_turn_usage import TURN_USAGE_SCHEMA, ModelUsage, TurnModelUsage
from shared_context.native_continuation_wire import NativeContinuation, NativeContinuationView
from test_web_test_console_native import (
    first, instruction_models, jwk_client, native_api, native_console, post, progress, send,
    signing_key, synthetic_standing_v1,
)


@pytest.mark.parametrize("completed", [False, True])
def test_original_unknown_and_related_attempt_stay_distinct_and_refresh_never_resubmits(native_console, completed):
    c = native_console
    base_draft = first(c)
    assert send(c, base_draft, "First complete context.").status_code == 200
    c.api.remote.completed(base_draft["request_id"])
    assert progress(c, base_draft["thread_id"]).status_code == 200
    base = c.api.remote.records[base_draft["request_id"]]
    base_text = c.api.remote.text
    source_draft = post(c, "/api/instructions/" + base_draft["thread_id"] + "/next").json()["composer"]
    assert send(c, source_draft, "Continue the original context.").status_code == 200
    source = NativeReceipt.model_validate_json(c.api.remote.records[source_draft["request_id"]].model_copy(update={
        "status": "unknown", "turn_number": 2, "failure_code": "NATIVE_TURN_UNKNOWN",
        "continuation_available": True,
    }).model_dump_json())
    c.api.remote.records[source.request_id] = source
    ref = NativeContinuation(
        continuation_of=source.request_id, root_request_id=source.request_id, attempt=1,
        source_submit_digest=source.submit_digest, checkpoint_sha256=base.result.checkpoint_sha256)
    rid = continuation_request_id(source.agent_id, source.conversation_id, source.request_id, 1)
    request = NativeContinuationSubmit(
        command=source.command.model_copy(update={"request_id": rid}), capture=source.capture, continuation=ref)
    text = "391"
    result = NativeResultReference(
        turn_number=3, turn_key="turn:3", result_digest="d" * 64,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_bytes=3, text_parts=1,
        checkpoint_sha256="e" * 64)
    child = NativeReceipt(
        agent_id=source.agent_id, hosted_session_id=source.hosted_session_id,
        conversation_id=source.conversation_id, request_id=rid, command=request.command, capture=request.capture,
        submit_digest=request.sha256, capture_digest=request.capture.sha256, continuation=ref,
        continuation_available=True, status="completed" if completed else "started", turn_number=3,
        result=result if completed else None,
        usage=TurnModelUsage(schema_version=TURN_USAGE_SCHEMA, usage=(ModelUsage(
            model="gpt-4.1-mini", wire_api="completions", requests=1, measured_requests=1,
            input_tokens=20, output_tokens=3),)) if completed else None)
    c.api.remote.records[rid] = child
    conversation = NativeConversation(
        agent_id=source.agent_id, hosted_session_id=source.hosted_session_id,
        conversation_id=source.conversation_id, last_turn=3, last_request_id=rid,
        capture=source.capture, capture_digest=source.capture_digest)

    def read_only(command):
        if command.operation == "read":
            return c.api.remote.records[command.request_id]
        if command.operation == "conversation":
            return conversation
        if command.operation == "continuation":
            assert command.request_id == source.request_id
            return NativeContinuationView(
                agent_id=source.agent_id, conversation_id=source.conversation_id,
                original_request_id=source.request_id, root_request_id=source.request_id,
                request_id=rid, continuation=ref, state="admitted",
                source_hosted_session_id=source.hosted_session_id, target_hosted_session_id=child.hosted_session_id)
        if command.operation == "history":
            turns = [] if not completed or command.after_turn >= 3 else [NativePublicTurn(
                conversation_id=child.conversation_id, request_id=rid, turn_number=3,
                user_text=None, continuation_text=source.command.content, continuation=ref, result=result)]
            return NativeHistoryPage(conversation=conversation, turns=tuple(turns), next_after_turn=None)
        if command.operation == "result":
            record = c.api.remote.records[command.request_id]
            value = text if command.request_id == rid else base_text
            parts = [value] if record.result.text_parts == 1 else [value[:len(value)//2], value[len(value)//2:]]
            return NativeResultPage(
                conversation_id=record.conversation_id, request_id=record.request_id, result=record.result,
                part=command.part, complete=len(parts) == 1, text=parts[command.part],
                next_part=command.part + 1 if command.part + 1 < len(parts) else None)
        pytest.fail("refresh tried a writer or an unrelated operation: " + command.operation)

    c.api.remote.reply_override = read_only
    before = len(c.calls)
    observed = progress(c, source_draft["thread_id"])
    assert observed.status_code == 200, observed.text
    tasks = {task["task_id"]: task for task in observed.json()["tasks"]}
    assert tasks[source.request_id]["native_receipt"]["status"] == "unknown"
    current = tasks[rid]
    assert current["instruction"] is None and current["agent_text"] is None
    assert current["continuation"] == ref.model_dump(mode="json")
    assert current["continuation_text"] == source.command.content
    assert not current["actions"]["submit-retry"]["enabled"]
    assert not current["actions"]["submit-reconcile"]["enabled"]
    # The owner may always write; whether a turn after an unknown one runs is Hosted's answer (it resumes from the last
    # valid checkpoint, ADR-0007 §1, or refuses with the conversation's continuation_blocker).
    assert observed.json()["composer"]["can_follow_up"] is True
    if completed:
        assert current["reply"]["data"]["text"] == text
        usage = tasks[source.request_id]["continuation_usage"]
        assert usage["known_requests"] == 1 and usage["unknown_attempts"] == 1
    assert all(method == "GET" for method, _path, _body in c.calls[before:])
    assert c.api.remote.records[source.request_id] == source
