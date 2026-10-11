"""Only a verified pre-handler platform refusal can close an unstarted Responses RID."""

import json

import pytest
from pydantic import ValidationError

from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
from test_native_responses import response_submit, responses
from test_native_turns import local_env


def rejection(receipt):
    return NATIVE_REQUEST.validate_json(json.dumps({
        "operation": "response_not_started", "request_id": receipt.request_id,
        "conversation_id": receipt.conversation_id, "submit_digest": receipt.submit_digest,
        "reason": "protocol_unavailable",
    }))


def test_known_pre_handler_refusal_closes_only_the_original_unstarted_response(responses):
    request = response_submit()
    assert responses.invoke(request).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    refused = responses.invoke(rejection(accepted))
    assert refused.status_code == 200, refused.text
    original = responses.call(responses.runtime.read, "rid-1")
    assert original.status == "unknown" and original.failure_code == "NATIVE_TURN_NOT_STARTED"
    assert original.command == request.command and original.capture == request.capture
    assert original.response_id is None and original.turn_number is None
    assert responses.invoke(rejection(accepted)).json() == refused.json()
    late = responses.respond(accepted)
    assert responses.response_result(late.json()["id"])["error"]["code"] == "NATIVE_TURN_NOT_STARTED"
    assert not responses.sdk.calls


def test_rejection_does_not_close_a_queued_started_or_different_original_intent(responses):
    responses.sdk.block = True
    assert responses.invoke(response_submit("active")).status_code == 202
    active = responses.call(responses.runtime.read, "active")
    assert responses.respond(active).status_code == 200
    started = responses.wait("active", "started")
    assert responses.invoke(rejection(active)).json() == started.model_dump(mode="json")
    assert not responses.sdk.controls["active"].stop_requested()

    assert responses.invoke(response_submit("queued")).status_code == 202
    waiting = responses.call(responses.runtime.read, "queued")
    assert responses.respond(waiting).status_code == 200
    original = responses.queued("queued")
    refused = responses.invoke(rejection(waiting))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_TURN_QUEUED"
    changed = rejection(waiting).model_copy(update={"submit_digest": "0" * 64})
    assert responses.invoke(changed).json()["error"]["code"] == "NATIVE_RID_CONFLICT"
    changed = rejection(waiting).model_copy(update={"conversation_id": "another-cid"})
    assert responses.invoke(changed).json()["error"]["code"] == "NATIVE_STATE_SCOPE"
    assert responses.call(responses.runtime.read, "queued") == original
    assert [call[0] for call in responses.sdk.calls] == ["active"]


@pytest.mark.parametrize("reason", ["timeout", "transient_502", "unknown"])
def test_transport_uncertainty_is_not_an_accepted_rejection_reason(reason):
    with pytest.raises(ValidationError):
        NATIVE_REQUEST.validate_json(json.dumps({
            "operation": "response_not_started", "request_id": "rid", "conversation_id": "cid",
            "submit_digest": "a" * 64, "reason": reason,
        }))
