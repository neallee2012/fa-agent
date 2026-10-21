"""N6 shared wire uses the original capture, without importing a Hosted SDK."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeConversation, NativeReceipt
from shared_context.behavior_native_agent_wire import NativeRelayAcceptance
from test_native_turn_wire import agent_receive, submit


def test_responses_lane_is_explicit_and_keeps_the_original_submission():
    legacy = submit("rid-original", "cid-original")
    values = legacy.model_dump(mode="json")
    values["state_profile"] = "foundry-responses-v1"
    request = NATIVE_REQUEST.validate_json(json.dumps(values))
    assert request.state_profile == "foundry-responses-v1"
    assert request.command == legacy.command
    assert request.capture == legacy.capture
    assert request.sha256 != legacy.sha256

    receipt = NativeReceipt(
        state_profile=request.state_profile,
        agent_id="OFFLINE-agent", hosted_session_id="OFFLINE-session",
        request_id=request.command.request_id, conversation_id="cid-original",
        command=request.command, capture=request.capture,
        submit_digest=request.sha256, capture_digest=request.capture.sha256,
        status="accepted",
    )
    assert NativeReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    assert "response_id" not in receipt.model_dump(mode="json")
    assert "dispatch" not in receipt.model_dump(mode="json")
    assert "state_profile" not in legacy.model_dump(mode="json")
    assert NATIVE_REQUEST.validate_json(legacy.model_dump_json()) == legacy
    with pytest.raises(ValidationError):
        NativeReceipt.model_validate_json(
            receipt.model_copy(update={"state_profile": "foundry-native-v1"}).model_dump_json()
        )


def test_server_selected_new_cid_does_not_rewrite_the_original_command():
    legacy = submit("new-rid")
    values = legacy.model_dump(mode="json")
    values.update(state_profile="foundry-responses-v1", new_conversation_id="new-cid")
    request = NATIVE_REQUEST.validate_json(json.dumps(values))
    receipt = NativeReceipt(
        state_profile=request.state_profile, agent_id="OFFLINE-agent", hosted_session_id="OFFLINE-session",
        request_id="new-rid", conversation_id="new-cid", command=request.command, capture=request.capture,
        submit_digest=request.sha256, capture_digest=request.capture.sha256, status="accepted",
    )
    assert receipt.command == legacy.command
    assert receipt.command.conversation_id is None
    assert request.new_conversation_id == receipt.conversation_id
    assert NativeReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    for changes in (
        {"new_conversation_id": None},
        {"state_profile": "foundry-native-v1"},
        {"command": {**values["command"], "conversation_id": "existing-cid"}},
    ):
        with pytest.raises(ValidationError):
            NATIVE_REQUEST.validate_json(json.dumps({**values, **changes}))


def test_legacy_public_wire_bytes_keep_the_recorded_baseline_hashes():
    request = submit("rid-original", "cid-original")
    receipt = NativeReceipt(state_profile=request.state_profile,
        agent_id="OFFLINE-agent", hosted_session_id="OFFLINE-session",
        request_id="rid-original", conversation_id="cid-original",
        command=request.command, capture=request.capture,
        submit_digest=request.sha256, capture_digest=request.capture.sha256, status="accepted",
    )
    conversation = NativeConversation(
        agent_id="OFFLINE-agent", hosted_session_id="OFFLINE-session",
        conversation_id="cid-original", last_turn=0, last_request_id=None,
        capture=request.capture, capture_digest=request.capture.sha256,
    )
    # Recorded from 713b7d9b before adding any N6 fields.
    for value, expected in (
        (request, "2ee790c8e154b4ad80542ee09e424e6426bc6724aa9ee71b8442d44733640c71"),
        (agent_receive(), "d50ba91843b50466796bc760eb972b57da8a806a94c22df14d287c03278cd770"),
        (receipt, "cee0d404d84fc698999ba9590c83a5afb28a5ffec05784e015255e7512984180"),
        (conversation, "dedf5c300064dc184ed39969658a1f1f01ce0a5e02951407c2239ffaf0fe7f55"),
    ):
        assert hashlib.sha256(value.model_dump_json().encode("utf-8")).hexdigest() == expected


def test_two_step_dispatch_is_shared_with_relay_and_never_means_a_started_turn_can_resend():
    request = NATIVE_REQUEST.validate_json(json.dumps({
        **submit("rid-original", "cid-original").model_dump(mode="json"),
        "state_profile": "foundry-responses-v1",
    }))
    values = {
        "state_profile": request.state_profile,
        "agent_id": "OFFLINE-agent", "hosted_session_id": "OFFLINE-session",
        "request_id": "rid-original", "conversation_id": "cid-original",
        "command": request.command.model_dump(mode="json"),
        "capture": request.capture.model_dump(mode="json"),
        "submit_digest": request.sha256, "capture_digest": request.capture.sha256,
        "status": "accepted",
        "dispatch": {"state": "needs_response", "inflight_request_id": "rid-original"},
    }
    accepted = NativeReceipt.model_validate_json(json.dumps(values))
    relay = NativeRelayAcceptance.model_validate_json(json.dumps({
        "state_profile": request.state_profile, "message_id": "rid-original",
        "recipient_agent_id": "OFFLINE-agent", "conversation_id": "cid-original",
        "submit_digest": request.sha256, "status": "accepted", "dispatch": values["dispatch"],
    }))
    assert type(relay.dispatch) is type(accepted.dispatch)
    assert accepted.dispatch.state == "needs_response"
    started = NativeReceipt.model_validate_json(json.dumps({
        **values, "status": "started", "dispatch": None, "response_id": "resp_OFFICIAL_1",
    }))
    assert started.response_id == "resp_OFFICIAL_1"
    assert "dispatch" not in started.model_dump(mode="json")
    for changes in (
        {"status": "started", "response_id": "resp_OFFICIAL_1"},
        {"status": "started", "dispatch": None},
        {"response_id": "resp_OFFICIAL_1"},
        {"dispatch": {"state": "needs_response", "inflight_request_id": "another-rid"}},
        {"dispatch": {"state": "executed"}},
    ):
        with pytest.raises(ValidationError):
            NativeReceipt.model_validate_json(json.dumps({**values, **changes}))
