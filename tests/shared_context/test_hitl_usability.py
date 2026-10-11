"""Approval usability at the original SDK callbacks, virtual files and review sender; no model traffic."""

import json

import pytest
from copilot.generated.rpc import PermissionDecisionReject

from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import ToolFailure
from shared_context.native_capabilities import guard_native_tool
from shared_context.native_hitl_wire import native_hitl_failure_feedback
from shared_context.native_operations import NativeOperationGuard
from shared_context.native_sdk import NativeToolOutputPage, tool_output_reader
from test_instruction_tool_feedback import run_callbacks
from test_native_hitl import change, hitl, post
from test_native_hitl_h2 import human_decide, pending_view, permission_request, setup_gate, verdict
from test_m3_auth import jwk_client, signing_key

SID = "offline-instruction-feedback"
OUTPUT_PATH = "/session-state/temp/copilot-tool-output-original.txt"
RULE_SCOPE = "A rule limited to shell or run_command applies only to shell command execution."


def session_files():
    files = SdkSessionFiles(SID)
    files.files.update({
        "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": SID}}) + "\n",
        "workspace.yaml": "id: " + SID + "\n",
    })
    return files


def test_session_output_reader_uses_original_files_without_review_or_effect(hitl, tmp_path):
    import asyncio

    files = session_files()
    case = setup_gate(hitl, tmp_path, (verdict("ask"),))
    tool = guard_native_tool(case.context, tool_output_reader(files, case.context.control))
    operations = NativeOperationGuard(case.context.effects, {})

    async def check():
        content = "complete original output\u539f\u6587\n" * 5000
        await files.write_file(OUTPUT_PATH, content)
        original = dict(files.files)
        result, offset = [], 0
        while True:
            packet = {**case.packet, "tool_name": tool.name,
                      "arguments": {"path": OUTPUT_PATH, "offset": offset, "limit": 16000}}
            assert await case.gate.permission(permission_request(packet), {"session_id": SID}) == (True, None)
            assert await operations.prepare(packet) == "read"
            page = NativeToolOutputPage.model_validate_json((await operations.invoke(packet, tool.handler)).text)
            assert len(page.text) <= 16000
            result.append(page.text)
            if page.next_offset is None:
                break
            assert page.next_offset > offset
            offset = page.next_offset
        assert "".join(result) == content
        directory = NativeToolOutputPage.model_validate_json((await tool.handler({
            **case.packet, "tool_name": tool.name, "arguments": {},
        })).text)
        assert directory.kind == "directory" and directory.text == "copilot-tool-output-original.txt"
        assert files.files == original and not files.failed
        assert not case.sender.observed and not case.client.observed and not case.runtime.store.items
        assert not operations.claims

    asyncio.run(check())


def test_unavailable_feedback_does_not_block_later_user_requests():
    text = native_hitl_failure_feedback("NATIVE_HITL_AUTHORITY_UNAVAILABLE")
    assert "這一次呼叫" in text and "之後的新請求" in text
    assert "不要重送" in text and "UNKNOWN" in text


@pytest.mark.parametrize("answer,outcome,reason", [
    (verdict("allow"), "allow", "reviewed"),
    (verdict("allow", uncertain=True), "ask", "review_uncertain"),
    (verdict("deny", takeover=True), "deny", "human_takeover"),
])
def test_review_distinguishes_shell_rule_scope_from_mcp_uncertainty(hitl, tmp_path, answer, outcome, reason):
    import asyncio

    assert post(hitl, "/rules", change(
        hitl, "shell-only", text="Ask before using shell (run_command).", behavior="ask_first")).status_code == 200
    case = setup_gate(hitl, tmp_path, (answer,))

    async def check():
        action = case.gate._action(case.packet).model_copy(update={
            "category": "mcp", "tool": "WebIQ-web", "target": "WebIQ/WebIQ-web",
            "arguments": {"query": "public documentation", "maxResults": 5},
        })
        result = await case.gate.reviewer(action, await case.client.rules(), {"initial": "Find public documentation."})
        assert (result.outcome, result.reason) == (outcome, reason)
        assert result.matched_rule_ids == ()
        assert RULE_SCOPE in case.sender.observed[0]["instructions"]

    asyncio.run(check())


def test_output_reader_rejects_other_paths_and_sessions_without_poisoning_checkpoint(hitl, tmp_path):
    import asyncio

    files = session_files()
    case = setup_gate(hitl, tmp_path)
    tool = tool_output_reader(files, case.context.control)
    paths = (
        "/session-state", "/session-state/events.jsonl", "/session-state/checkpoints/1.md",
        "/session-state/temp/../events.jsonl", "/session-state/temperature/file.txt",
        "/session-state/temp//output.txt", "/session-state/temp/\x00.txt",
        "/session-state/temp/./output.txt", "/files/output.txt", r"C:\private\file.txt",
    )

    async def check():
        for path in paths:
            with pytest.raises(ToolFailure) as rejected:
                await tool.handler({**case.packet, "arguments": {"path": path}})
            assert rejected.value.public_code == "SDK_TOOL_OUTPUT_SCOPE"
        with pytest.raises(ToolFailure):
            await tool.handler({**case.packet, "session_id": "another-session", "arguments": {"path": OUTPUT_PATH}})
        with pytest.raises(ToolFailure) as missing:
            await tool.handler({**case.packet, "arguments": {"path": OUTPUT_PATH}})
        assert missing.value.public_code == "SDK_TOOL_OUTPUT_MISSING"
        await files.write_file(OUTPUT_PATH, "available")
        with pytest.raises(ToolFailure) as wrong_range:
            await tool.handler({**case.packet, "arguments": {"path": OUTPUT_PATH, "offset": 100}})
        assert wrong_range.value.public_code == "SDK_TOOL_OUTPUT_RANGE"
        assert not files.failed
        assert files.snapshot().files["temp/copilot-tool-output-original.txt"] == "available"

    asyncio.run(check())


def test_shell_read_of_tool_output_still_waits_for_original_shell_rule(hitl, tmp_path, monkeypatch):
    import asyncio

    assert post(hitl, "/rules", change(hitl, "shell-rule", text="Ask before using shell (run_command).")).status_code == 200
    rule = hitl.service.rules(hitl.owner).rules[0]
    case = setup_gate(hitl, tmp_path, (verdict("allow", (rule.rule_id,)),))
    case.packet["arguments"] = {"command": "cat " + OUTPUT_PATH}
    observed = []

    async def callback(options, sid):
        wait = asyncio.create_task(options["on_permission_request"](
            permission_request(case.packet), {"session_id": sid}))
        card = await pending_view(case)
        observed.extend((not wait.done(), card.pending.command.review.reason == "ask_rule",
                         card.pending.command.action.arguments == case.packet["arguments"]))
        human_decide(case, card, "deny")
        observed.append(isinstance(await wait, PermissionDecisionReject))

    run_callbacks(
        tmp_path, monkeypatch, callback, lambda _: pytest.fail("the denied shell must not run"),
        tool_name="run_command", parameters={
            "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
            "additionalProperties": False,
        }, session_files=session_files(), native_hitl=case.gate,
    )
    assert observed == [True] * 4


def test_effect_unknown_feedback_does_not_authorize_a_replay():
    text = native_hitl_failure_feedback("NATIVE_HITL_EFFECT_UNKNOWN")
    assert "原操作效果未確認" in text and "不要重送" in text
    assert "之後的新請求" not in text
