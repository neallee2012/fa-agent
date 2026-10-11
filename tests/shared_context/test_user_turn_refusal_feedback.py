"""Only reviewed read-user error codes may become fixed feedback, never authority."""

import json

import pytest

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_user_turn import UserTurnNumber
from shared_context.m2_copilot import ToolFailure
from test_instruction_tool_feedback import run_callbacks, invocation


HINTS = {
    "USER_TURN_SCOPE_DENIED": "completed past turn",
    "USER_TURN_NOT_AVAILABLE": "unavailable",
    "USER_TURN_SOURCE_CHANGED": "provenance changed",
    "USER_TURN_SOURCE_LIMIT": "supported read bound",
}
OPAQUE = "Authority refused the action; do not resend unknown effects or claim completion."


@pytest.mark.parametrize("code,hint", HINTS.items())
def test_reviewed_reader_feedback_uses_only_fixed_code_and_guidance(tmp_path, monkeypatch, code, hint):
    messages = []

    async def handler(_packet):
        raise ToolFailure("PRIVATE_HANDLER_MESSAGE", public_code=code)

    async def callback(options, sid):
        result = await options["tools"][0].handler(invocation(sid, {"turn_number": 1}, "read_user_turn"))
        assert result.result_type == "failure"
        assert result.error == result.text_result_for_llm
        messages.append(result.error)

    events = run_callbacks(tmp_path, monkeypatch, callback, handler,
                           tool_name="read_user_turn", parameters=UserTurnNumber.model_json_schema())
    assert len(messages) == 1 and code in messages[0] and hint in messages[0]
    assert messages[0].startswith("Authority refused")
    assert "unknown effects" in messages[0] and len(messages[0]) <= 1024
    assert "PRIVATE_" not in messages[0] and "PRIVATE_" not in json.dumps(events)
    assert code not in json.dumps(events)
    failures = [event for event in events if event["type"] == "m2.tool_failure"]
    assert len(failures) == 1 and "data" not in failures[0]


@pytest.mark.parametrize("code", [
    None, "PRIVATE_UNKNOWN_CODE", "USER_TURN_SCOPE_DENIED\nPRIVATE_SUFFIX",
    "USER_TURN_SOURCE_INVALID", "ASSISTANT_API_UNKNOWN", "BEHAVIOR_COMMIT_UNKNOWN",
])
def test_unknown_integrity_or_uncertain_reader_failure_stays_opaque(tmp_path, monkeypatch, code):
    feedback = []

    async def handler(_packet):
        raise ToolFailure("PRIVATE_DETAILS_WITH_USER_TURN_SCOPE_DENIED", public_code=code)

    async def callback(options, sid):
        result = await options["tools"][0].handler(invocation(sid, {"turn_number": 1}, "read_user_turn"))
        feedback.append(result.error)

    events = run_callbacks(tmp_path, monkeypatch, callback, handler,
                           tool_name="read_user_turn", parameters=UserTurnNumber.model_json_schema())
    assert feedback == [OPAQUE]
    assert "PRIVATE_" not in json.dumps(events)


@pytest.mark.parametrize("tool", ["execute_operation", "read_context", "read_skill", "read_work_memory"])
def test_reader_codes_cannot_reclassify_other_tools(tmp_path, monkeypatch, tool):
    feedback = []

    async def handler(_packet):
        raise ToolFailure("PRIVATE_WRONG_TOOL", public_code="USER_TURN_SCOPE_DENIED")

    async def callback(options, sid):
        result = await options["tools"][0].handler(invocation(sid, {}, tool))
        feedback.append(result.error)

    events = run_callbacks(tmp_path, monkeypatch, callback, handler, tool_name=tool)
    assert feedback == [OPAQUE] and "PRIVATE_" not in json.dumps(events)


@pytest.mark.parametrize("text_reply,user_turn_access", [(False, True), (True, False), (False, False)])
def test_reader_feedback_requires_the_current_instruction_reader_capability(text_reply, user_turn_access):
    boundary = AssistantModelBoundary(None, text_reply=text_reply, user_turn_access=user_turn_access)
    assert boundary.public_tool_failure("read_user_turn", "USER_TURN_SCOPE_DENIED") is None


@pytest.mark.parametrize("tool", ["execute_operation", "read_context", "read_skill", "read_work_memory"])
def test_enabled_reader_capability_does_not_override_the_tool_name_check(tool):
    boundary = AssistantModelBoundary(None, text_reply=True, user_turn_access=True)
    assert boundary.public_tool_failure(tool, "USER_TURN_SCOPE_DENIED") is None


def test_request_arguments_cannot_choose_public_failure_code(tmp_path, monkeypatch):
    feedback = []

    async def handler(_packet):
        pytest.fail("schema-spoofed request must not execute")

    async def callback(options, sid):
        result = await options["tools"][0].handler(invocation(
            sid, {"turn_number": 1, "public_code": "USER_TURN_SCOPE_DENIED"}, "read_user_turn",
        ))
        feedback.append(result.error)

    run_callbacks(tmp_path, monkeypatch, callback, handler,
                  tool_name="read_user_turn", parameters=UserTurnNumber.model_json_schema())
    assert len(feedback) == 1 and "additionalProperties" in feedback[0]
    assert "USER_TURN_SCOPE_DENIED" not in feedback[0]
