"""`public_event` decides what reaches the screen: full-event comparisons prove nothing else came along."""
import hashlib
import json
import logging
from datetime import datetime, timezone

import pytest

from shared_context.behavior_assistant_progress import MODEL_FAILURE_CODES, PEER_FAILURE_CODES, public_event
from shared_context.native_hitl_wire import NATIVE_HITL_FAILURE_GUIDANCE, native_hitl_failure_feedback

AT = "2026-01-01T00:00:00Z"
SEND = "send_agent_message"
PRIVATE = {
    "message": "PRIVATE_MESSAGE", "error": {"message": "PRIVATE_ERROR", "code": "PRIVATE_ERROR_CODE"},
    "result": {"content": "PRIVATE_RESULT", "detailedContent": "PRIVATE_DETAIL"},
    "arguments": {"secret": "PRIVATE_ARGUMENT", "path": "PRIVATE_PATH"},
    "reasoning_text": "PRIVATE_REASONING", "reasoning_opaque": "PRIVATE_OPAQUE",
    "reasoning_blocks": {"provider": "PRIVATE_PROVIDER", "blocks": "PRIVATE_BLOCKS"},
    "system_prompt": "PRIVATE_SYSTEM", "stack": "PRIVATE_STACK", "raw": "PRIVATE_RAW", "body": "PRIVATE_BODY",
    "session_id": "PRIVATE_SESSION", "request_id": "PRIVATE_REQUEST", "headers": {"authorization": "PRIVATE_AUTH"},
    "tool_start_seq": 99, "source": "PRIVATE_SOURCE", "tool_name_raw": "PRIVATE_TOOL",
    "summary": "PRIVATE_SUMMARY", "reasoning_summary": "PRIVATE_SUMMARY_FIELD", "status": "PRIVATE_STATUS",
}
OUTER = {"id": "PRIVATE_EVENT_ID", "parentId": "PRIVATE_PARENT", "ephemeral": "PRIVATE_EPHEMERAL",
         "summary": "PRIVATE_OUTER_SUMMARY"}


def event(kind, **data):
    return {**OUTER, "type": kind, "timestamp": AT, "data": {**PRIVATE, **data}}


def shown(value, seq=1, tools=None, **options):
    """The whole public payload, without the fields every event has; None when nothing is shown."""
    result = public_event(value, seq, {} if tools is None else tools, **options)
    if result is None:
        return None
    assert "PRIVATE" not in result.model_dump_json()
    dumped = result.model_dump(mode="json")
    assert dumped.pop("seq") == seq and dumped.pop("at") == "2026-01-01T00:00:00Z"
    return dumped


def expected(kind, status, summary, *, tool_name=None, source=None, **extra):
    return {"kind": kind, "status": status, "summary": summary, "tool_name": tool_name, "source": source, **extra}


def error(status, summary):
    return expected("error", status, summary)


def source():
    text = "OFFLINE source"
    return {"source_id": "progress-source", "version": 1, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "byte_length": len(text.encode())}


def native_context():
    return json.dumps({"schema_version": "fa-instruction-context-origin-v1", "content": "PRIVATE_NATIVE", "turn_origin": {
        "source_kind": "human", "actor_role": "original-requester", "agent_id": "agent", "conversation_id": "c",
        "request_id": "r", "turn_number": 1, "previous_turn_number": None, "capture_digest": "0" * 64,
        "source": None}})


@pytest.mark.parametrize("kind,summary,status", [
    ("session.start", "SDK session started", "started"),
    ("assistant.turn_start", "SDK turn started", "started"),
    ("session.idle", "SDK turn ended", "succeeded"),
])
@pytest.mark.parametrize("data", [{"data": PRIVATE}, {"data": None}, {"data": "PRIVATE_NOT_A_DICT"}, {}])
def test_lifecycle_stages_are_fixed_text_whatever_their_data(kind, summary, status, data):
    assert shown({"type": kind, "timestamp": AT, **data}) == expected("stage", status, summary)


@pytest.mark.parametrize("data", [{"data": PRIVATE}, {"data": "PRIVATE_NOT_A_DICT"}, {}])
def test_unresolved_session_error_is_a_fixed_line(data):
    assert shown({"type": "session.error", "timestamp": AT, **data}) == error(
        "unknown", "SDK reported an unresolved error")


@pytest.mark.parametrize("kind", ["m2.provider_failed", "session.error"])
@pytest.mark.parametrize("code", sorted(MODEL_FAILURE_CODES))
def test_fixed_model_codes_win_over_provider_refusals(kind, code):
    refused = {"upstream_error_code": "ASSISTANT_PROVIDER_HTTP_FAILURE", "provider_status": 429}
    assert shown(event(kind), model_failure_code=code, model_failure=refused) == error("failed", code)


@pytest.mark.parametrize("kind", ["m2.provider_failed", "session.error"])
def test_provider_refusal_shows_only_status_code_and_param(kind):
    refused = {"upstream_error_code": "ASSISTANT_PROVIDER_HTTP_FAILURE", "provider_status": 429,
               "provider_error_code": "rate_limit", "provider_error_param": "messages[0]",
               "provider_error_message": "PRIVATE_PROVIDER_MESSAGE", "raw_body": "PRIVATE_PROVIDER_BODY"}
    assert shown(event(kind), model_failure=refused) == error(
        "failed", "Model provider refused: status=429 code=rate_limit param=messages[0]")


@pytest.mark.parametrize("failure", [
    None,
    {},
    "PRIVATE_FAILURE",
    {"upstream_error_code": "ASSISTANT_MODEL_UNKNOWN", "provider_status": 429},
    {"upstream_error_code": "ASSISTANT_PROVIDER_HTTP_FAILURE", "provider_status": 500},
    {"upstream_error_code": "ASSISTANT_PROVIDER_HTTP_FAILURE", "provider_status": "429"},
])
@pytest.mark.parametrize("code", [None, "PRIVATE_CODE", ["PRIVATE_LIST"], 1])
def test_other_model_failures_show_nothing_of_their_own(failure, code):
    assert shown(event("m2.provider_failed"), model_failure_code=code, model_failure=failure) is None
    assert shown(event("session.error"), model_failure_code=code, model_failure=failure) == error(
        "unknown", "SDK reported an unresolved error")


@pytest.mark.parametrize("kind,result", [
    ("session.start", expected("stage", "started", "SDK session started")),
    ("assistant.message", None),
    ("m2.tool_failure", None),
    ("m2.permission", None),
    ("other", None),
])
def test_model_failure_codes_only_answer_failure_events(kind, result):
    refused = {"upstream_error_code": "ASSISTANT_PROVIDER_HTTP_FAILURE", "provider_status": 429}
    value = event(kind, tool_name="read_context", code="PRIVATE_CODE", content="")
    assert shown(value, allowed_tools=("read_context",), model_failure_code="ASSISTANT_MODEL_BUDGET",
                 model_failure=refused) == result


def test_queued_relay_shows_only_the_recipient_notice():
    assert shown(event("m2.relay_queued", tool_name=SEND, recipient="agent-b"), allowed_tools=(SEND,)) == expected(
        "stage", "started", "已排隊，agent-b 換版完成後處理；尚未確認送達，不會重送。")


def test_queued_relay_recipient_is_redacted_and_bounded():
    value = event("m2.relay_queued", tool_name=SEND, recipient="r" * 400 + " password=PRIVATE_VALUE")
    summary = shown(value, allowed_tools=(SEND,))["summary"]
    assert summary == "已排隊，" + "r" * 176 + " 換版完成後處理；尚未確認送達，不會重送。"


@pytest.mark.parametrize("value,allowed", [
    (event("m2.relay_queued", tool_name=SEND, recipient="agent-b"), ()),
    (event("m2.relay_queued", tool_name=SEND, recipient="agent-b"), ("read_context",)),
    (event("m2.relay_queued", tool_name="read_context", recipient="agent-b"), ("read_context", SEND)),
    (event("m2.relay_queued", tool_name=SEND, recipient=1), (SEND,)),
    (event("m2.relay_queued", tool_name=SEND), (SEND,)),
    ({"type": "m2.relay_queued", "timestamp": AT, "data": "PRIVATE"}, (SEND,)),
    ({"type": "m2.relay_queued", "timestamp": AT}, (SEND,)),
])
def test_queued_relay_needs_its_own_configured_tool_and_text_recipient(value, allowed):
    assert shown(value, allowed_tools=allowed) is None


@pytest.mark.parametrize("code", sorted(PEER_FAILURE_CODES))
def test_peer_failure_shows_only_its_fixed_code(code):
    status = "unknown" if code == "AGENT_MESSAGE_RESULT_UNCONFIRMED" else "failed"
    assert shown(event("m2.tool_failure", tool_name=SEND, code=code), allowed_tools=(SEND,)) == error(status, code)


@pytest.mark.parametrize("data,allowed", [
    ({"tool_name": SEND, "code": "AGENT_MESSAGE_ALLOWANCE"}, ()),
    ({"tool_name": SEND, "code": "AGENT_MESSAGE_ALLOWANCE"}, ("read_context",)),
    ({"tool_name": "read_context", "code": "AGENT_MESSAGE_ALLOWANCE"}, ("read_context", SEND)),
    ({"tool_name": SEND, "code": "PRIVATE_CODE"}, (SEND,)),
    ({"tool_name": SEND, "code": ["PRIVATE_CODE"]}, (SEND,)),
    ({"tool_name": SEND}, (SEND,)),
    ({"tool_name": "read_context", "code": "PRIVATE_CODE"}, ("read_context",)),
    ({"tool_name": "read_context", "hitl": "AGENT_MESSAGE_ALLOWANCE"}, ("read_context",)),
])
def test_other_tool_failures_show_nothing(data, allowed):
    assert shown(event("m2.tool_failure", **data), allowed_tools=allowed) is None


@pytest.mark.parametrize("kind", ["m2.tool_failure", "m2.permission"])
def test_tool_failure_kinds_ignore_data_that_is_not_a_dict(kind, caplog):
    assert shown({"type": kind, "timestamp": AT, "data": "PRIVATE"}, allowed_tools=(SEND,)) is None
    assert "INSTRUCTION_PUBLIC_EVENT_DATA_INVALID" not in caplog.text


@pytest.mark.parametrize("code", sorted(NATIVE_HITL_FAILURE_GUIDANCE))
@pytest.mark.parametrize("kind,field", [("m2.permission", "hitl"), ("m2.tool_failure", "code")])
@pytest.mark.parametrize("tool", ["playwright-browser_click", SEND])
def test_hitl_outcome_shows_its_fixed_guidance_only(kind, field, code, tool):
    status = "unknown" if code in {"NATIVE_HITL_EFFECT_UNKNOWN", "NATIVE_HITL_AUTHORITY_UNAVAILABLE"} else "failed"
    assert shown(event(kind, tool_name=tool, **{field: code}), allowed_tools=(tool,)) == error(
        status, native_hitl_failure_feedback(code))


@pytest.mark.parametrize("kind,data,allowed", [
    ("m2.permission", {"tool_name": "t", "hitl": "NATIVE_HITL_EXPIRED"}, ()),
    ("m2.permission", {"tool_name": "other", "hitl": "NATIVE_HITL_EXPIRED"}, ("t",)),
    ("m2.tool_failure", {"tool_name": "other", "code": "NATIVE_HITL_EXPIRED"}, ("t",)),
    ("m2.permission", {"tool_name": "t", "code": "NATIVE_HITL_EXPIRED"}, ("t",)),
    ("m2.tool_failure", {"tool_name": "t", "hitl": "NATIVE_HITL_EXPIRED"}, ("t",)),
    ("m2.permission", {"tool_name": "t", "hitl": "PRIVATE_CODE"}, ("t",)),
    ("m2.permission", {"tool_name": "t", "hitl": ["NATIVE_HITL_EXPIRED"]}, ("t",)),
    ("m2.permission", {"tool_name": "t"}, ("t",)),
    ("m2.permission", {"tool_name": SEND, "hitl": "AGENT_MESSAGE_ALLOWANCE"}, (SEND,)),
    ("m2.permission", {"tool_name": SEND, "code": "AGENT_MESSAGE_ALLOWANCE"}, (SEND,)),
])
def test_hitl_outcome_needs_the_configured_tool_and_its_own_field(kind, data, allowed):
    assert shown(event(kind, **data), allowed_tools=allowed) is None


@pytest.mark.parametrize("kind", [None, "", "user.message", "assistant.reasoning", "tool.execution_partial_result",
                                  "m2.other", "session.shutdown"])
def test_unlisted_events_show_nothing(kind):
    assert shown(event(kind, content="PRIVATE", tool_name="read_context"), allowed_tools=("read_context",)) is None


def test_unlisted_events_reach_neither_the_relay_nor_the_tool_pairs(caplog):
    caplog.set_level(logging.WARNING)
    pair = ("read_context", 1, "read_context")
    tools = {"call": pair}
    value = event("m2.other", tool_name=SEND, recipient="agent-b", tool_call_id="call", success=True)
    assert shown(value, 2, tools, allowed_tools=(SEND, "read_context")) is None
    assert shown(event("m2.other"), 2, tools) is None
    assert shown({"type": "m2.other", "timestamp": AT, "data": "PRIVATE"}, 2, tools) is None
    assert tools == {"call": pair} and caplog.text == ""


@pytest.mark.parametrize("value", [{"type": "session.start"}, {"type": "session.start", "timestamp": None},
                                   {"type": "session.start", "timestamp": 1}])
def test_events_without_a_text_timestamp_show_nothing(value):
    assert public_event(value, 1, {}) is None


def test_timestamp_offsets_are_parsed():
    assert public_event({"type": "session.start", "timestamp": "2026-01-01T00:00:00Z"}, 1, {}).at == datetime(
        2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        public_event({"type": "session.start", "timestamp": "not a time"}, 1, {})


def test_an_unhashable_type_is_left_to_the_callers_type_error_guard():
    with pytest.raises(TypeError):
        public_event({"type": [], "timestamp": AT}, 1, {})


@pytest.mark.parametrize("kind", ["assistant.message", "tool.execution_start", "tool.execution_complete"])
@pytest.mark.parametrize("data", ["PRIVATE", None, 1, []])
def test_activity_events_need_dict_data(kind, data, caplog):
    assert public_event({"type": kind, "timestamp": AT, "data": data}, 1, {}) is None
    assert "INSTRUCTION_PUBLIC_EVENT_DATA_INVALID" in caplog.text


def test_missing_data_counts_as_empty_not_as_invalid(caplog):
    assert public_event({"type": "assistant.message", "timestamp": AT}, 1, {}) is None
    assert "INSTRUCTION_PUBLIC_EVENT_DATA_INVALID" not in caplog.text
    assert public_event({"type": "tool.execution_start", "timestamp": AT}, 1, {}) is None
    assert "INSTRUCTION_PUBLIC_EVENT_DATA_INVALID" not in caplog.text
    assert "INSTRUCTION_PUBLIC_TOOL_ID_UNAVAILABLE" in caplog.text


NOTE = {"content": "Reading the source.", "phase": "commentary", "tool_requests": None}
REASONED = {"reasoning_blocks": {"provider": "openai-responses", "blocks": [
    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Short reason."}]}]}}


def test_assistant_note_and_reasoning_summary_are_the_only_message_fields_shown():
    responses = {"model_provider": "openai-responses", "reasoning_summary": True}
    assert shown(event("assistant.message", **NOTE, **REASONED), **responses) == expected(
        "stage", "started", "Assistant note: Reading the source.", reasoning_summary="Short reason.")
    assert shown(event("assistant.message", **{**NOTE, "content": ""}, **REASONED), **responses) == expected(
        "stage", "started", "OpenAI reasoning summary", reasoning_summary="Short reason.")


@pytest.mark.parametrize("options", [
    {},
    {"model_provider": "openai-responses"},
    {"reasoning_summary": True},
    {"model_provider": "openai", "reasoning_summary": True},
])
def test_reasoning_summary_needs_the_responses_provider_and_the_turn_option(options):
    assert shown(event("assistant.message", **NOTE, **REASONED), **options) == expected(
        "stage", "started", "Assistant note: Reading the source.")
    assert shown(event("assistant.message", **{**NOTE, "content": ""}, **REASONED), **options) is None


@pytest.mark.parametrize("agent,session,visible", [
    (None, None, True), (None, "sdk", True), ("sdk", "sdk", True), ("other", "sdk", False), ("other", None, False),
])
def test_messages_from_another_agent_are_not_shown(agent, session, visible):
    value = event("assistant.message", **NOTE) | {"agent_id": agent}
    assert (shown(value, sdk_session_id=session) is not None) is visible


def start(name="read_context", call_id="call", **data):
    return event("tool.execution_start", tool_call_id=call_id, tool_name=name, **data)


def complete(call_id="call", success=True, **data):
    return event("tool.execution_complete", tool_call_id=call_id, success=success, **data)


def test_tool_start_and_completion_show_only_the_public_name_and_status(caplog):
    caplog.set_level(logging.WARNING)
    tools, allowed = {}, ("WebIQ-MCP-web",)
    assert shown(start("WebIQ-MCP-web", arguments={"query": "docs", "secret": "PRIVATE_ARG"}), 3, tools,
                 allowed_tools=allowed) == expected(
        "tool_started", "started", "Search query: docs", tool_name="WebIQ_MCP_web", tool_start_seq=3)
    assert tools == {"call": ("WebIQ_MCP_web", 3, "WebIQ-MCP-web")}
    assert shown(complete(), 5, tools, allowed_tools=allowed) == expected(
        "tool_completed", "succeeded", "Tool returned a result", tool_name="WebIQ_MCP_web", tool_start_seq=3)
    assert tools == {"call": None}
    assert caplog.text == ""


def test_failed_completion_is_a_fixed_line():
    tools = {}
    shown(start(), 1, tools, allowed_tools=("read_context",))
    assert shown(complete(success=False), 2, tools, allowed_tools=("read_context",)) == expected(
        "tool_completed", "failed", "Tool did not return a confirmed result", tool_name="read_context",
        tool_start_seq=1)
    assert tools == {"call": None}


def test_tool_without_a_public_detail_keeps_the_fixed_summary():
    tools = {}
    assert shown(start(arguments={"query": "PRIVATE_QUERY"}), 1, tools, allowed_tools=("read_context",)) == expected(
        "tool_started", "started", "Configured tool call started", tool_name="read_context", tool_start_seq=1)


@pytest.mark.parametrize("name", ["other", None, 1, ["read_context"]])
def test_unconfigured_start_tombstones_its_id(name):
    tools = {}
    assert shown(start(name), 1, tools, allowed_tools=("read_context",)) is None
    assert tools == {"call": None}


def test_unhashable_tool_name_is_unconfigured_when_the_catalog_is_a_set():
    tools = {}
    assert shown(start(["read_context"]), 1, tools, allowed_tools=frozenset({"read_context"})) is None
    assert tools == {"call": None}


def test_reused_start_id_tombstones_the_pair_and_logs(caplog):
    tools = {}
    shown(start(), 1, tools, allowed_tools=("read_context",))
    assert shown(start(), 2, tools, allowed_tools=("read_context",)) is None
    assert tools == {"call": None}
    assert "INSTRUCTION_PUBLIC_TOOL_ID_REUSED" in caplog.text
    assert shown(start(), 3, tools, allowed_tools=("read_context",)) is None
    assert shown(complete(), 4, tools, allowed_tools=("read_context",)) is None


@pytest.mark.parametrize("build", [start, complete])
@pytest.mark.parametrize("call_id", [None, "", 1, ["call"]])
def test_activity_without_a_text_call_id_is_dropped_and_logged(build, call_id, caplog):
    tools = {}
    assert shown(build(call_id=call_id), 1, tools, allowed_tools=("read_context",)) is None
    assert tools == {} and "INSTRUCTION_PUBLIC_TOOL_ID_UNAVAILABLE" in caplog.text


def test_completion_for_an_unseen_id_changes_nothing(caplog):
    caplog.set_level(logging.WARNING)
    tools = {}
    assert shown(complete(), 1, tools) is None
    assert tools == {} and caplog.text == ""


@pytest.mark.parametrize("success", [None, 1, "true", 0])
def test_untyped_success_leaves_the_pair_open(success, caplog):
    tools = {"call": ("read_context", 1, "read_context")}
    assert shown(complete(success=success), 2, tools) is None
    assert tools == {"call": ("read_context", 1, "read_context")}
    assert "INSTRUCTION_PUBLIC_TOOL_RESULT_UNAVAILABLE" in caplog.text


def read_context_result(result, *, success=True, raw="read_context"):
    tools = {"call": ("read_context", 1, raw)}
    data = {**PRIVATE, "tool_call_id": "call", "success": success}
    data["result"] = result
    return public_event({"type": "tool.execution_complete", "timestamp": AT, "data": data}, 2, tools)


def test_read_context_publishes_only_its_source_reference():
    content = json.dumps({"source": source(), "content": "PRIVATE_CONTEXT"})
    result = read_context_result({"content": content})
    assert result.model_dump(mode="json")["source"] == source()
    assert "PRIVATE" not in result.model_dump_json()


@pytest.mark.parametrize("raw", ["read_context", "read_skill"])
def test_a_source_claimed_by_the_event_itself_is_not_published(raw):
    tools = {"call": ("read_context", 1, raw)}
    value = {"type": "tool.execution_complete", "timestamp": AT, "data": {
        "tool_call_id": "call", "success": True, "source": source(), "result": {"content": "not a context"}}}
    assert public_event(value, 2, tools).source is None


def test_only_a_successful_read_context_publishes_a_source():
    content = json.dumps({"source": source(), "content": "PRIVATE_CONTEXT"})
    assert read_context_result({"content": content}, success=False).source is None
    assert read_context_result({"content": content}, raw="read_skill").source is None


@pytest.mark.parametrize("result", [None, {}, [], "PRIVATE", {"content": 1}, {"content": None}])
def test_read_context_without_text_content_has_no_source(result, caplog):
    assert read_context_result(result).source is None
    assert "INSTRUCTION_PUBLIC_SOURCE_UNAVAILABLE" not in caplog.text


def test_native_context_has_no_source_and_no_warning(caplog):
    assert read_context_result({"content": native_context()}).source is None
    assert "INSTRUCTION_PUBLIC_SOURCE_UNAVAILABLE" not in caplog.text


@pytest.mark.parametrize("content", ["PRIVATE_NOT_JSON", "{}", '{"source":"PRIVATE"}'])
def test_unreadable_context_keeps_the_completion_without_a_source(content, caplog):
    result = read_context_result({"content": content})
    assert result.source is None and result.status == "succeeded"
    assert "PRIVATE" not in result.model_dump_json()
    assert "INSTRUCTION_PUBLIC_SOURCE_UNAVAILABLE" in caplog.text
