"""Native routine attribution preserves original identity without becoming a human instruction."""

from datetime import datetime, timezone
import hashlib
import json

import pytest
from pydantic import ValidationError

from shared_context.behavior_routine_wire import RoutineOccurrence
from test_native_turn_wire import capture


def routine_origin():
    from shared_context.native_routine_wire import NativeRoutineOrigin

    return NativeRoutineOrigin(
        tenant_id="tenant", team_id="team", owner_oid="original-owner",
        agent_id="original-agent", conversation_id="original-cid",
        occurrence=RoutineOccurrence(
            routine_id="daily-summary", approved_version="approved-v1", time_zone="Asia/Taipei",
            scheduled_for=datetime(2026, 10, 4, 1, tzinfo=timezone.utc),
            definition_sha256="a" * 64, notification_policy="original-conversation"),
        content_sha256=hashlib.sha256(b"Read the original work.").hexdigest(),
        trigger_data={"purpose": "daily-summary"},
    )


def test_native_routine_round_trip_is_original_scheduled_data_not_a_human_or_approval():
    from shared_context.behavior_native_turn_wire import (
        NATIVE_REQUEST, NativeAgentCommand, NativeReceipt, NativeRoutineSubmit, NativeSubmit,
    )
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.native_sdk_profiles import native_context

    origin = routine_origin()
    command = NativeAgentCommand(
        request_id=origin.request_id, conversation_id=origin.conversation_id, content="Read the original work.")
    request = NativeRoutineSubmit(command=command, capture=capture(), routine=origin)
    assert NATIVE_REQUEST.validate_json(request.model_dump_json()) == request
    receipt = NativeReceipt(
        agent_id=origin.agent_id, hosted_session_id="hosted-session", request_id=command.request_id,
        conversation_id=origin.conversation_id, submit_digest=request.sha256,
        capture_digest=request.capture.sha256, command=command, capture=request.capture, routine=origin,
        status="started", turn_number=2)
    restored = NativeReceipt.model_validate_json(receipt.model_dump_json())
    context = native_context(restored, None)
    assert context.turn_origin.source_kind == "routine"
    assert context.turn_origin.actor_role == "routine-producer"
    assert context.turn_origin.routine == origin
    assert context.turn_origin.authorization is False and origin.authorization is False
    assert restored.human_origin is False
    assert command.authorize_own_result is False
    human = NativeSubmit(
        command=InstructionSubmit(request_id="human-rid", content="A human question"), capture=capture())
    assert "routine" not in human.model_dump(mode="json")
    with pytest.raises(ValidationError):
        NativeSubmit.model_validate_json(json.dumps({
            **human.model_dump(mode="json"), "routine": origin.model_dump(mode="json"),
        }))
