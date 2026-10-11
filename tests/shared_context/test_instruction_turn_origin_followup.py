"""Follow-up attribution uses saved first-turn records, not a native resume claim."""

import httpx
import pytest

from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_instruction_prompt import configured, submit
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn


@pytest.mark.parametrize("hide_previous", [False, True])
def test_followup_origin_retains_the_saved_first_turn_and_validates_previous_metadata(
        assistant, monkeypatch, hide_previous):
    h, control, human = configured(assistant)
    calls = sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Original first-turn answer."}])
    first, first_command = submit(h, "origin-first-complete", "Keep the original conversation.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    first_record = control.records[first["binding_id"]].model_dump_json()
    first_reply = control.read_reply(human, first["conversation_id"], 1)
    reply_bytes = first_reply.model_dump_json()
    assert len(calls) == len(h.endpoint.requests) == 1

    second = open_turn(h, control, human, "origin-followup-request", first["conversation_id"])
    instruction = second.assignment.task.instruction
    assert instruction.turn_number == 2 and instruction.previous_archive_id is not None
    assert second.assignment.sdk_session_id == first_reply.sdk_session_id
    observed_requests = []
    request = h.client.request

    def response_with_hidden_previous(method, url, **options):
        observed_requests.append(str(url))
        response = request(method, url, **options)
        assert response.status_code == 200
        value = response.json()
        assert value["turn_origin"]["previous_turn_number"] == 1
        value["turn_origin"]["previous_turn_number"] = None
        return httpx.Response(200, json=value)

    try:
        if hide_previous:
            monkeypatch.setattr(h.client, "request", response_with_hidden_previous)
            with pytest.raises(Problem) as failed:
                second.client.context(include_origin=True)
            assert failed.value.status == 403
            assert len(observed_requests) == 1 and observed_requests[0].endswith("/context-origin")
        else:
            value = second.client.context(include_origin=True)
            assert value.turn_origin.source_kind == "human"
            assert value.turn_origin.request_id == "origin-followup-request"
            assert value.turn_origin.request_id != first_command.request_id
            assert value.turn_origin.conversation_id == first["conversation_id"]
            assert value.turn_origin.turn_number == 2 and value.turn_origin.previous_turn_number == 1
            assert value.turn_origin.reply_target == "original-conversation"
            assert value.turn_origin.authorization is False
        assert control.records[first["binding_id"]].model_dump_json() == first_record
        assert control.read_reply(human, first["conversation_id"], 1).model_dump_json() == reply_bytes
        assert len(calls) == len(h.endpoint.requests) == 1
    finally:
        second.client.close()
