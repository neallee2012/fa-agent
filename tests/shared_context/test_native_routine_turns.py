"""Real official local admission; only SDK/model I/O is synthetic."""

import pytest

from shared_context.behavior_native_turn_wire import NativeAgentCommand, NativeHistory, NativeRoutineSubmit
from shared_context.errors import Problem
from test_native_routine_wire import routine_origin
from test_native_turn_wire import capture, submit
from test_native_turns import IDENTITY, harness, local_env, native_retry_policy


def routine_request(cid="original-cid"):
    source = routine_origin().model_copy(update={"agent_id": IDENTITY.agent_id, "conversation_id": cid})
    return NativeRoutineSubmit(
        command=NativeAgentCommand(
            request_id=source.request_id, conversation_id=cid, content="Read the original work."),
        capture=capture(), routine=source)


def test_default_off_routine_has_no_native_admission_or_sdk_call(harness, monkeypatch):
    monkeypatch.delenv("FA_NATIVE_ROUTINES_ENABLED", raising=False)
    request = routine_request()
    response = harness.invoke(request)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NATIVE_ROUTINES_DISABLED"
    assert harness.sdk.calls == []
    with pytest.raises(Problem) as caught:
        harness.call(harness.runtime.read, request.command.request_id)
    assert caught.value.body["error"]["code"] == "NATIVE_RID_MISSING"


def test_scheduled_input_resumes_the_original_conversation_without_becoming_human(harness, monkeypatch):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    assert harness.invoke(submit("human-first")).status_code == 202
    first = harness.wait("human-first", "completed")
    request = routine_request(first.conversation_id)
    response = harness.invoke(request)
    assert response.status_code == 202, response.text
    scheduled = harness.wait(request.command.request_id, "completed")
    assert scheduled.routine == request.routine and scheduled.submit_digest == request.sha256
    assert scheduled.turn_number == 2 and scheduled.human_origin is False
    assert harness.sdk.calls[-1][2] is not None
    page = harness.call(harness.runtime.history, NativeHistory(conversation_id=first.conversation_id))
    assert page.turns[0].user_text == "Original user text"
    assert page.turns[1].user_text is None and page.turns[1].agent_text is None
    assert page.turns[1].routine == request.routine
    assert page.turns[1].routine_text == "Read the original work."
    again = harness.invoke(request)
    assert again.json()["result"] == scheduled.result.model_dump(mode="json")
    assert [call[0] for call in harness.sdk.calls] == ["human-first", request.command.request_id]


def test_routine_text_is_not_recalled_or_saved_as_the_humans_private_memory(harness, monkeypatch):
    from test_native_private_memory import private

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    assert harness.invoke(submit("human-first")).status_code == 202
    first = harness.wait("human-first", "completed")
    request = routine_request(first.conversation_id)
    assert harness.invoke(request).status_code == 202
    scheduled = harness.wait(request.command.request_id, "completed")
    sent = []
    memory = private(sent)
    recalled = harness.call(memory.recall, scheduled)
    assert tuple(recalled) == () and sent == []
    assert recalled.observation.reason == "scope_not_applicable"
    harness.call(memory.completed, scheduled, harness.runtime)
    assert sent == []
