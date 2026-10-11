"""Actual SDK callbacks with SDK I/O doubles; no runtime, model or network."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import httpx
import pytest
from copilot import CopilotClient
from copilot.client import GetStatusResponse
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session import CopilotSession

from shared_context import m2_copilot
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_assistant_wire import Empty, InstructionContext, SkillName
from shared_context.behavior_work_memory import WorkMemoryName
from shared_context.m2_copilot import ToolFailure, ToolOutput, ToolSpec


def run_callbacks(
    tmp_path, monkeypatch, callback, handler, *,
    instruction=True, tool_name="read_context", parameters=None, prompt="OFFLINE callback fixture", **runner_options,
):
    events = []
    sid = "offline-instruction-feedback"
    client = create_autospec(CopilotClient, instance=True)
    client._cli_process = SimpleNamespace(pid=42, poll=lambda: 0, returncode=0)
    client.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)
    session = create_autospec(CopilotSession, instance=True)
    session.session_id = sid
    session.get_events.return_value = []

    async def create(**options):
        async def send(*_args, **_kwargs):
            await callback(options, sid)
            return SimpleNamespace(data=SimpleNamespace(content="OFFLINE callbacks completed"))
        session.send_and_wait.side_effect = send
        return session

    client.create_session.side_effect = create
    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, return_value=client))

    def no_network(_request):
        pytest.fail("callback evidence must not call a model or network")

    async def no_reservation(_digest):
        pytest.fail("callback evidence must not reserve model usage")

    asyncio.run(m2_copilot.run_copilot(
        state_dir=tmp_path / "state", working_directory=tmp_path,
        sdk_session_id=sid, resume=False, prompt=prompt,
        tools=[ToolSpec(
            tool_name, "Read the authorized source.",
            Empty.model_json_schema() if parameters is None else parameters, handler,
        )],
        api_url="https://offline.invalid", token_provider=lambda: pytest.fail("no credential access"),
        on_event=events.append, timeout_seconds=10, runtime_path=Path(__file__),
        http_transport=httpx.MockTransport(no_network),
        behavior_boundary=AssistantModelBoundary(
            no_reservation, text_reply=instruction, streaming=instruction,
            skill_access=tool_name == "read_skill",
            work_memory_access=tool_name == "read_work_memory",
            user_turn_access=tool_name == "read_user_turn",
        ),
        instruction=instruction,
        **runner_options,
    ))
    client.start.assert_awaited_once()
    client.stop.assert_awaited_once()
    return events


def invocation(sid, arguments, tool_name="read_context"):
    return SimpleNamespace(
        session_id=sid, tool_name=tool_name, tool_call_id="original-call",
        arguments=arguments,
    )


@pytest.mark.parametrize("route", ["permission", "invoke"])
@pytest.mark.parametrize("arguments,diagnostic", [
    ({"PRIVATE_SUBMITTED_KEY": "PRIVATE_SUBMITTED_VALUE"}, "additionalProperties"),
    (["PRIVATE_SUBMITTED_VALUE"], "expected object"),
])
def test_instruction_schema_feedback_is_actionable_without_submitted_values(
    tmp_path, monkeypatch, route, arguments, diagnostic,
):
    calls, feedback = [], []

    async def handler(packet):
        calls.append(packet)
        return ToolOutput(InstructionContext(
            source={"source_id": "reviewed", "version": 1, "sha256": "a" * 64, "byte_length": 2},
            content="{}",
        ).model_dump_json())

    async def callback(options, sid):
        tool = options["tools"][0]
        if route == "permission":
            request = SimpleNamespace(
                kind="custom-tool", tool_name=tool.name, tool_call_id="original-call",
                managed_approval_required=False, args=arguments,
            )
            result = options["on_permission_request"](request, {"session_id": sid})
            assert isinstance(result, PermissionDecisionReject)
            feedback.append(result.feedback)
            request.args = {}
            allowed = options["on_permission_request"](request, {"session_id": sid})
            assert isinstance(allowed, PermissionDecisionApproveOnce)
        else:
            result = await tool.handler(invocation(sid, arguments))
            assert result.result_type == "failure"
            assert result.error == result.text_result_for_llm
            feedback.append(result.error)
        assert not calls
        corrected = await tool.handler(invocation(sid, {}))
        assert json.loads(corrected.text_result_for_llm)["content"] == "{}"

    events = run_callbacks(tmp_path, monkeypatch, callback, handler)
    assert len(calls) == 1 and calls[0]["arguments"] == {}
    assert len(feedback) == 1
    assert "Invalid tool arguments" in feedback[0]
    assert diagnostic in feedback[0]
    assert len(feedback[0]) <= 1024
    failures = [event for event in events if event["type"] == "m2.tool_failure"]
    assert len(failures) == 1 and "data" not in failures[0]
    assert "PRIVATE_SUBMITTED" not in json.dumps(events)
    assert "PRIVATE_SUBMITTED" not in feedback[0]


@pytest.mark.parametrize("error", [
    ToolFailure("PRIVATE_AUTHORITY_DETAIL"),
    ToolFailure("Invalid tool arguments: PRIVATE_FORGED_SCHEMA_DETAIL"),
    RuntimeError("PRIVATE_UNEXPECTED_DETAIL"),
])
@pytest.mark.parametrize("instruction", [False, True])
def test_instruction_handler_failures_stay_opaque_and_do_not_claim_no_effect(
    tmp_path, monkeypatch, error, instruction,
):
    calls, feedback = [], []

    async def handler(packet):
        calls.append(packet)
        raise error

    async def callback(options, sid):
        result = await options["tools"][0].handler(invocation(sid, {}))
        assert result.result_type == "failure"
        feedback.append(result.error)
        assert result.error == result.text_result_for_llm

    events = run_callbacks(tmp_path, monkeypatch, callback, handler, instruction=instruction)
    assert len(calls) == 1
    assert feedback == ["Authority refused the action; do not resend unknown effects or claim completion."]
    assert "PRIVATE_" not in json.dumps(events)


def test_finite_profile_schema_failure_retains_original_mask(tmp_path, monkeypatch):
    async def handler(_packet):
        pytest.fail("invalid arguments must not reach the handler")

    async def callback(options, sid):
        result = await options["tools"][0].handler(invocation(sid, {"PRIVATE_KEY": "PRIVATE_VALUE"}))
        assert result.error == "Authority refused the action; do not resend unknown effects or claim completion."

    events = run_callbacks(tmp_path, monkeypatch, callback, handler, instruction=False)
    assert "PRIVATE_" not in json.dumps(events)


@pytest.mark.parametrize("route", ["permission", "invoke"])
@pytest.mark.parametrize("arguments", [
    {"name": "receipt-guide"},
    {"name": "current-skill", "path": "PRIVATE_UNREQUESTED_PATH"},
])
def test_approved_skill_catalog_and_schema_refusals_keep_original_contract(
    tmp_path, monkeypatch, route, arguments,
):
    schema = SkillName.model_json_schema()
    schema["properties"]["name"]["enum"] = ["current-skill"]
    feedback = []

    async def handler(_packet):
        pytest.fail("refused skill input must not load PRIVATE_SKILL_BODY")

    async def callback(options, sid):
        tool = options["tools"][0]
        if route == "permission":
            result = options["on_permission_request"](SimpleNamespace(
                kind="custom-tool", tool_name="read_skill", tool_call_id="original-call",
                managed_approval_required=False, args=arguments,
            ), {"session_id": sid})
            assert isinstance(result, PermissionDecisionReject)
            feedback.append(result.feedback)
        else:
            result = await tool.handler(invocation(sid, arguments, "read_skill"))
            assert result.result_type == "failure"
            feedback.append(result.error)

    events = run_callbacks(
        tmp_path, monkeypatch, callback, handler, tool_name="read_skill", parameters=schema,
    )
    assert feedback == ["Authority refused the action; do not resend unknown effects or claim completion."]
    assert "PRIVATE_" not in json.dumps(events)


@pytest.mark.parametrize("route", ["permission", "invoke"])
@pytest.mark.parametrize("arguments", [
    {"claim_id": "not-assigned"},
    {"claim_id": "current-claim", "subject": "PRIVATE_UNASSIGNED_SUBJECT"},
    ["PRIVATE_MALFORMED_CLAIM"],
    {"claim_id": "current-claim", "schema_error": True, "authority": False},
], ids=["unassigned-alias", "scope-spoof", "not-object", "classification-spoof"])
def test_reviewed_memory_catalog_and_schema_refusals_remain_authority_failures(
    tmp_path, monkeypatch, route, arguments,
):
    schema = WorkMemoryName.model_json_schema()
    schema["properties"]["claim_id"]["enum"] = ["current-claim"]
    feedback = []

    async def handler(_packet):
        pytest.fail("refused memory input must not read PRIVATE_MEMORY_BODY")

    async def callback(options, sid):
        tool = options["tools"][0]
        if route == "permission":
            request = SimpleNamespace(
                kind="custom-tool", tool_name=tool.name, tool_call_id="original-call",
                managed_approval_required=False, args=arguments,
            )
            result = options["on_permission_request"](request, {"session_id": sid})
            assert isinstance(result, PermissionDecisionReject)
            feedback.append(result.feedback)
            request.args = {"claim_id": "current-claim"}
            assert isinstance(options["on_permission_request"](request, {"session_id": sid}),
                              PermissionDecisionApproveOnce)
        else:
            result = await tool.handler(invocation(sid, arguments, tool.name))
            assert result.result_type == "failure" and result.text_result_for_llm == result.error
            feedback.append(result.error)

    events = run_callbacks(
        tmp_path, monkeypatch, callback, handler, tool_name="read_work_memory", parameters=schema,
    )
    assert feedback == ["Authority refused the action; do not resend unknown effects or claim completion."]
    failures = [event for event in events if event["type"] == "m2.tool_failure"]
    assert len(failures) == 1 and "data" not in failures[0]
    assert "PRIVATE_" not in json.dumps(events) + feedback[0]


@pytest.mark.parametrize("route", ["permission", "invoke"])
def test_memory_schema_feedback_does_not_relax_original_session_identity(tmp_path, monkeypatch, route):
    schema = WorkMemoryName.model_json_schema()
    schema["properties"]["claim_id"]["enum"] = ["current-claim"]

    async def handler(_packet):
        pytest.fail("a foreign session must never reach the memory handler")

    async def callback(options, sid):
        tool = options["tools"][0]
        args = {"claim_id": "current-claim"}
        if route == "permission":
            result = options["on_permission_request"](SimpleNamespace(
                kind="custom-tool", tool_name=tool.name, tool_call_id="original-call",
                managed_approval_required=False, args=args,
            ), {"session_id": "foreign-session"})
            assert isinstance(result, PermissionDecisionReject)
            assert result.feedback == "Only validated registered custom tools are allowed"
        else:
            result = await tool.handler(invocation("foreign-session", args, tool.name))
            assert result.result_type == "failure"
            assert result.error == "Authority refused the action; do not resend unknown effects or claim completion."

    run_callbacks(tmp_path, monkeypatch, callback, handler, tool_name="read_work_memory", parameters=schema)
