"""Real official local admission; only SDK/model I/O is synthetic.

Since 2026-10-23 a run carries no command CID and names its own conversation in ``routine.conversation_id``. An image
before that change cannot receive it: its ``NativeRoutineOrigin.check_command`` requires the command CID to equal the
origin's, so it refuses the run (422) and the API clock sends the same RID again next minute. That is why API and Hosted
change together, and why the change is one-way (the README's native-routine-fresh-conversation section).
"""

from datetime import timedelta

import pytest

from shared_context.behavior_native_turn_wire import (
    NativeAgentCommand, NativeHistory, NativeReceivedPage, NativeReceivedRead, NativeRoutineSubmit,
)
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


def fresh_run(cid, day):
    """A run since 2026-10-23: no command CID; its origin names the conversation the API opened for it."""
    origin = routine_origin()
    source = origin.model_copy(update={
        "agent_id": IDENTITY.agent_id, "conversation_id": cid,
        "occurrence": origin.occurrence.model_copy(update={
            "scheduled_for": origin.occurrence.scheduled_for + timedelta(days=day)})})
    return NativeRoutineSubmit(
        command=NativeAgentCommand(request_id=source.request_id, content="Read the original work."),
        capture=capture(), routine=source)


def test_each_run_opens_its_own_conversation_and_starts_without_the_previous_runs_history(harness, monkeypatch):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    runs = (fresh_run("run-cid-1", 0), fresh_run("run-cid-2", 1))
    for request in runs:
        assert harness.invoke(request).status_code == 202
        done = harness.wait(request.command.request_id, "completed")
        assert (done.conversation_id, done.turn_number, done.routine) == (
            request.routine.conversation_id, 1, request.routine)
    # Run 2's SDK turn started with no checkpoint: none of run 1's history reached its first model call.
    assert [call[:3] for call in harness.sdk.calls] == [
        (runs[0].command.request_id, "run-cid-1", None), (runs[1].command.request_id, "run-cid-2", None)]
    page = harness.call(harness.runtime.history, NativeHistory(conversation_id="run-cid-2"))
    assert [turn.request_id for turn in page.turns] == [runs[1].command.request_id]
    # A retried dispatch of the same run reads its original; no second conversation or SDK turn.
    again = harness.invoke(runs[1])
    assert again.json()["conversation_id"] == "run-cid-2" and len(harness.sdk.calls) == 2
    # The owner's workbench finds each run as its own conversation in the received listing.
    listed = NativeReceivedPage.model_validate_json(harness.invoke(NativeReceivedRead()).content)
    assert listed.items == ()
    assert sorted((item.request_id, item.conversation_id, item.routine_id, item.scheduled_for, item.status)
                  for item in listed.routine_runs) == sorted(
        (run.command.request_id, run.routine.conversation_id, "daily-summary", run.routine.occurrence.scheduled_for,
         "completed") for run in runs)
