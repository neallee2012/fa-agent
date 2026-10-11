"""Public tool pairing through actual projection/DTO/API, with offline SDK events."""

import asyncio
import json
from datetime import datetime
from functools import partial
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant_progress import public_event as project_event
from shared_context.behavior_assistant_hosted import AssistantSession
from shared_context.behavior_work_policy_api import install_work_policy_routes
from web_test_console.freeform import ConversationProjection
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn, ordinary
from test_work_policy_lifecycle import enabled

AT = "2026-09-25T10:00:00Z"
public_event = partial(project_event, allowed_tools=("read_context", "execute_operation", "read_user_turn", "read_skill"))


def start(call_id, name="read_context"):
    return {"type": "tool.execution_start", "timestamp": AT, "data": {
        "tool_call_id": call_id, "tool_name": name,
        "arguments": {"private": "PRIVATE_ARGUMENT_CANARY"},
    }}


def complete(call_id, success=True):
    return {"type": "tool.execution_complete", "timestamp": AT, "data": {
        "tool_call_id": call_id, "success": success,
        "error": {"message": "PRIVATE_ERROR_CANARY"},
    }}


def paired():
    tools = {}
    return (
        public_event(start("PRIVATE_CALL_A"), 1, tools),
        public_event(start("PRIVATE_CALL_B"), 2, tools),
        public_event(complete("PRIVATE_CALL_B", False), 3, tools),
        public_event(complete("PRIVATE_CALL_A"), 4, tools),
    )


def test_interleaved_same_name_tools_use_public_start_sequence_not_raw_ids():
    events = paired()
    assert [event.tool_start_seq for event in events] == [1, 2, 2, 1]
    assert [event.status for event in events] == ["started", "started", "failed", "succeeded"]
    snapshot = aw.PublicProgress(events=events)
    assert "PRIVATE_" not in snapshot.model_dump_json()


def test_missing_registry_never_assumes_a_tool_is_configured():
    tools = {}
    assert project_event(start("original"), 1, tools) is None
    assert project_event(complete("original"), 2, tools) is None


def test_only_actual_registered_tools_are_projected_without_reading_skill_body():
    tools = {}
    allowed = ("read_skill",)
    assert public_event(start("hidden", "read_context"), 1, tools, allowed_tools=allowed) is None
    opened = public_event(start("PRIVATE_SKILL_CALL", "read_skill"), 1, tools, allowed_tools=allowed)
    raw = complete("PRIVATE_SKILL_CALL")
    raw["data"]["result"] = {"content": "PRIVATE_SKILL_BODY"}
    closed = public_event(raw, 2, tools, allowed_tools=allowed)
    assert opened.tool_name == closed.tool_name == "read_skill"
    assert opened.tool_start_seq == closed.tool_start_seq == 1
    assert closed.source is None
    assert "PRIVATE_" not in aw.PublicProgress(events=(opened, closed)).model_dump_json()


def test_registered_memory_tool_progress_never_exposes_claim_or_source_body():
    tools = {}
    allowed = ("read_work_memory",)
    opened = project_event(start("PRIVATE_MEMORY_CALL", "read_work_memory"), 1, tools, allowed_tools=allowed)
    raw = complete("PRIVATE_MEMORY_CALL")
    raw["data"]["result"] = {"content": json.dumps({
        "text": "PRIVATE_REVIEWED_NOTE", "claim_id": "PRIVATE_CLAIM",
        "originals": [{"text": "PRIVATE_ORIGINAL"}],
    })}
    ended = project_event(raw, 2, tools, allowed_tools=allowed)
    assert opened.tool_name == ended.tool_name == "read_work_memory"
    assert opened.tool_start_seq == ended.tool_start_seq == 1 and ended.source is None
    assert "PRIVATE_" not in aw.PublicProgress(events=(opened, ended)).model_dump_json()


@pytest.mark.parametrize("call_id", [None, "", [], {}],
                         ids=["missing", "empty", "list", "object"])
def test_invalid_call_ids_do_not_create_pairs_or_expose_values(call_id, caplog):
    tools = {}
    assert public_event(start(call_id), 1, tools) is None
    assert public_event(complete(call_id), 2, tools) is None
    assert not tools
    assert "INSTRUCTION_PUBLIC_TOOL_ID_UNAVAILABLE" in caplog.text
    assert "PRIVATE_ID_" not in caplog.text


def test_long_sdk_identifier_is_not_truncated_or_published():
    tools = {}
    call_id = "PRIVATE_LONG_ID_" * 40
    opened = public_event(start(call_id), 1, tools)
    closed = public_event(complete(call_id), 2, tools)
    assert opened.tool_start_seq == closed.tool_start_seq == 1
    assert "PRIVATE_" not in aw.PublicProgress(events=(opened, closed)).model_dump_json()


def test_repeated_or_reused_call_ids_never_get_a_new_fabricated_pair(caplog):
    tools = {}
    opened = public_event(start("PRIVATE_REUSED"), 1, tools)
    assert opened.tool_start_seq == 1
    closed = public_event(complete("PRIVATE_REUSED"), 2, tools)
    assert closed.tool_start_seq == 1
    assert public_event(complete("PRIVATE_REUSED"), 3, tools) is None
    assert public_event(start("PRIVATE_REUSED"), 3, tools) is None
    assert public_event(complete("PRIVATE_REUSED"), 4, tools) is None
    assert "INSTRUCTION_PUBLIC_TOOL_ID_REUSED" in caplog.text
    assert "PRIVATE_REUSED" not in caplog.text


def test_ambiguous_second_start_invalidates_pair_before_any_completion():
    tools = {}
    assert public_event(start("duplicate"), 1, tools) is not None
    assert public_event(start("duplicate", "execute_operation"), 2, tools) is None
    assert public_event(complete("duplicate"), 3, tools) is None
    assert public_event(complete("unknown"), 3, tools) is None


def test_unconfigured_start_cannot_rebind_an_existing_or_later_call():
    tools = {}
    assert public_event(start("duplicate"), 1, tools) is not None
    assert public_event(start("duplicate", "unconfigured"), 2, tools) is None
    assert public_event(complete("duplicate"), 3, tools) is None
    assert public_event(start("unknown-first", "unconfigured"), 2, tools) is None
    assert public_event(start("unknown-first"), 2, tools) is None
    assert public_event(complete("unknown-first"), 3, tools) is None


@pytest.mark.parametrize("success", [None, "false", 0, {}],
                         ids=["missing", "string", "number", "object"])
def test_untyped_completion_is_not_a_failed_result_and_does_not_consume_the_pair(success, caplog):
    tools = {}
    public_event(start("original"), 1, tools)
    assert public_event(complete("original", success), 2, tools) is None
    valid = public_event(complete("original", True), 2, tools)
    assert valid.status == "succeeded" and valid.tool_start_seq == 1
    assert "INSTRUCTION_PUBLIC_TOOL_RESULT_UNAVAILABLE" in caplog.text


def test_legacy_event_serialization_does_not_gain_a_null_correlation_field():
    legacy = {
        "seq": 1, "at": AT, "kind": "stage", "tool_name": None,
        "status": "started", "summary": "SDK turn started", "source": None,
    }
    assert aw.PublicProgressEvent.model_validate_json(json.dumps(legacy)).model_dump(mode="json") == legacy
    assert "tool_start_seq" not in aw.PublicProgressEvent.model_validate_json(json.dumps(legacy)).model_dump_json()


@pytest.mark.parametrize("kind,seq,reference,name", [
    ("stage", 2, 1, None),
    ("error", 2, 1, None),
    ("tool_started", 2, 1, "read_context"),
    ("tool_completed", 2, 2, "read_context"),
    ("tool_completed", 2, 3, "read_context"),
    ("tool_completed", 2, 0, "read_context"),
    ("tool_completed", 2, True, "read_context"),
    ("tool_completed", 2, 1, None),
    ("tool_started", 2**53, 2**53, "read_context"),
    ("tool_completed", 2**53, 1, "read_context"),
])
def test_invalid_correlation_metadata_is_rejected(kind, seq, reference, name):
    with pytest.raises(ValidationError):
        aw.PublicProgressEvent.model_validate_json(json.dumps({
            "seq": seq, "at": AT, "kind": kind, "tool_name": name,
            "tool_start_seq": reference,
        }))


def test_snapshot_rejects_wrong_visible_origin_but_allows_a_pruned_start():
    events = paired()
    assert aw.PublicProgress(events=events[2:]).events == events[2:]
    wrong = events[2].model_copy(update={"tool_name": "execute_operation"})
    with pytest.raises(ValidationError):
        aw.PublicProgress(events=(*events[:2], wrong))
    with pytest.raises(ValidationError):
        aw.PublicProgress(events=(events[0], events[3].model_copy(update={"tool_start_seq": 2}),
                                  events[2].model_copy(update={"tool_start_seq": 3})))
    with pytest.raises(ValidationError):
        aw.PublicProgress(events=(*events, events[3].model_copy(update={"seq": 5})))


def test_sdk_emitter_uses_the_actual_registered_tools_and_preserves_public_pairs(tmp_path, monkeypatch):
    from test_instruction_tool_feedback import run_callbacks

    async def unused(_packet):
        pytest.fail("projection must not execute a handler")

    async def callback(options, _sid):
        for event in (
            start("PRIVATE_CONFIGURED"),
            start("PRIVATE_NOT_CONFIGURED", "read_skill"),
            complete("PRIVATE_NOT_CONFIGURED"),
            complete("PRIVATE_CONFIGURED", False),
        ):
            options["on_event"](event)

    events = run_callbacks(tmp_path, monkeypatch, callback, unused)
    projected = [aw.PublicProgressEvent.model_validate_json(json.dumps(event["public_progress"]))
                 for event in events if "public_progress" in event]
    assert [(event.kind, event.seq, event.tool_start_seq, event.tool_name) for event in projected] == [
        ("tool_started", 1, 1, "read_context"), ("tool_completed", 2, 1, "read_context"),
    ]
    assert "PRIVATE_" not in json.dumps(events)


def test_hosted_snapshot_retains_correlation_when_old_start_is_pruned(tmp_path):
    snapshots = []
    session = AssistantSession(SimpleNamespace(
        progress=snapshots.append, assignment=SimpleNamespace(task=SimpleNamespace(instruction=None)),
    ), root=tmp_path, runtime_path=tmp_path / "not-executed")
    tools = {}

    async def run():
        session._public_event({"public_progress": public_event(start("PRIVATE_PRUNED"), 1, tools).model_dump(mode="json")})
        for seq in range(2, 50):
            event = aw.PublicProgressEvent(seq=seq, at=datetime.fromisoformat(AT.replace("Z", "+00:00")),
                                           kind="stage", status="started", summary="Safe public step. " * 12)
            session._public_event({"public_progress": event.model_dump(mode="json")})
        end = public_event(complete("PRIVATE_PRUNED"), 50, tools)
        session._public_event({"public_progress": end.model_dump(mode="json")})
        await session._progress_tail

    asyncio.run(run())
    final = snapshots[-1]
    assert len(final.model_dump_json().encode()) <= 8192
    assert final.events[0].seq > 1
    assert final.events[-1].seq == 50 and final.events[-1].tool_start_seq == 1
    assert "PRIVATE_" not in final.model_dump_json()


@pytest.mark.parametrize("with_public_note", [False, True])
def test_signed_progress_and_thin_client_preserve_pairs_without_new_authority(assistant, with_public_note):
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    turn = open_turn(h, control, human, "public-tool-pairs")
    try:
        progress = aw.PublicProgress(events=paired())
        if with_public_note:
            note = public_event({"type": "assistant.message", "timestamp": AT, "data": {
                "content": "I will check the next source.", "phase": "commentary",
                "reasoning_text": "PRIVATE_REASONING",
            }}, 5, {})
            progress = aw.PublicProgress(events=(*progress.events, note))
        assert turn.client.progress(progress).state == "accepted"
        guard = h.store._guard()
        raw = control.conversation(human, turn.assignment.task.instruction.conversation_id)
        parsed = ConversationProjection.model_validate_json(json.dumps(raw))
        assert [e.tool_start_seq for e in parsed.turns[0].progress.events
                if e.kind.startswith("tool_")] == [1, 2, 2, 1]
        assert parsed.turns[0].progress == progress
        assert not parsed.can_follow_up
        assert not parsed.turns[0].reply_available
        assert parsed.turns[0].delivery_id is None
        assert h.store._guard() == guard
        assert h.endpoint.requests == []
        assert "PRIVATE_" not in json.dumps(raw)
        reference = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        ended = aw.PublicProgressEvent(
            seq=progress.events[-1].seq+1, at=datetime.fromisoformat(AT.replace("Z", "+00:00")), kind="stage",
            status="succeeded", summary="SDK turn ended",
        )
        final_progress = aw.PublicProgress(events=(*progress.events, ended))
        saved = turn.client.save_text_reply(
            "Original inventory response.", reference, turn.stop, progress=final_progress,
        )
        h.executor._complete(turn.start, saved.model_dump(mode="json"))
        status = h.runtime.controller.step(turn.binding.binding_id)
        assert status.state == "succeeded" and status.result_forwarded
        assert not status.obligation_fulfilled
        archived = ConversationProjection.model_validate_json(json.dumps(
            control.conversation(human, turn.assignment.task.instruction.conversation_id),
        ))
        assert archived.turns[0].progress == final_progress
        assert h.endpoint.requests == []
    finally:
        turn.client.close()


def test_capability_tool_calls_are_public_with_the_stored_name_format():
    tools, allowed = {}, {"WebIQ-MCP-web", "playwright-browser_navigate", "read_context"}
    opened = public_event(start("PRIVATE_WEB", "WebIQ-MCP-web"), 1, tools, allowed_tools=allowed)
    closed = public_event(complete("PRIVATE_WEB"), 2, tools, allowed_tools=allowed)
    browser = public_event(start("PRIVATE_BROWSER", "playwright-browser_navigate"), 3, tools, allowed_tools=allowed)
    assert (opened.tool_name, closed.tool_name, browser.tool_name) == (
        "WebIQ_MCP_web", "WebIQ_MCP_web", "playwright_browser_navigate")
    assert closed.tool_start_seq == 1 and closed.status == "succeeded" and closed.source is None
    snapshot = aw.PublicProgress(events=(opened, closed, browser))
    assert "PRIVATE_" not in snapshot.model_dump_json() and "-" not in snapshot.events[0].tool_name
    lookalike = public_event(start("PRIVATE_LOOKALIKE", "read-context"), 4, tools, allowed_tools={"read-context"})
    ended = public_event(complete("PRIVATE_LOOKALIKE"), 5, tools, allowed_tools={"read-context"})
    assert lookalike.tool_name == "read_context" and ended.source is None


@pytest.mark.parametrize("code", ["MODEL_CALL_LIMIT_REACHED", "AGENT_MESSAGE_RESULT_UNCONFIRMED"])
def test_fixed_failure_code_survives_signed_progress_and_thin_client(assistant, code):
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    turn = open_turn(h, control, human, "public-failure-code")
    try:
        if code == "MODEL_CALL_LIMIT_REACHED":
            event = project_event({"type": "m2.provider_failed", "timestamp": AT,
                "data": {"message": "PRIVATE_DETAIL"}}, 1, {}, model_failure_code=code)
        else:
            event = project_event({"type": "m2.tool_failure", "timestamp": AT, "data": {
                "tool_name": "send_agent_message", "code": code, "error": "PRIVATE_DETAIL"}},
                1, {}, allowed_tools=("send_agent_message",))
        progress = aw.PublicProgress(events=(event,))
        assert turn.client.progress(progress).state == "accepted"
        before = h.store._guard()
        assert turn.client.progress(progress).state == "accepted"
        parsed = ConversationProjection.model_validate_json(json.dumps(
            control.conversation(human, turn.assignment.task.instruction.conversation_id)))
        assert parsed.turns[0].progress.events[0].summary == code
        assert not parsed.turns[0].reply_available and not parsed.can_follow_up
        assert "PRIVATE_" not in parsed.model_dump_json()
        assert h.store._guard() == before and h.endpoint.requests == []
    finally:
        turn.client.close()
