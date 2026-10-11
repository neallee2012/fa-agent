"""A busy owner's append waits for the original work (SDK `enqueue`), on the pinned SDK and real runtime."""

import asyncio
import json

from shared_context.behavior_agent_message_wire import AgentMessageRecipient
from shared_context.behavior_native_agent_wire import NativePeerDirectory
from shared_context.behavior_native_turn_wire import NativeReceipt, NativeSubmit
from shared_context.behavior_wire import WireModel
from shared_context.m2_copilot import ToolOutput, ToolSpec
from test_m2_copilot import runtime_path  # noqa: F401
from test_native_agent_tools import collaboration_turn

APPEND = "另外，不用工具直接回答：台北 101 地上有幾層樓？"


class RouteResult(WireModel):
    text: str


def test_an_append_during_the_tool_loop_waits_until_the_original_work_finishes(runtime_path, tmp_path):
    """Stage A 2026-10-04: an append arrived while the browser tools ran; the model answered only the append and the
    original route, time and screenshot were never done. The append must not interrupt the unfinished work."""
    from shared_context import native_sdk
    from shared_context.behavior_native_turn_wire import NativeTurnCapture
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.native_agent_tools import NativeAgentTools
    from shared_context.native_turns import NativeTurnControl
    from test_m2_copilot import ScriptedEndpoint, tool_call
    from test_native_sdk import MODEL_ROOT

    original, recipient = collaboration_turn()
    values = original.capture.model_dump(mode="json")
    values["template"]["settings"]["model_provider"] = None
    captured = NativeTurnCapture.model_validate_json(json.dumps(values))
    sender = NativeReceipt.model_validate({
        **original.model_dump(), "capture": captured, "capture_digest": captured.sha256,
        "submit_digest": NativeSubmit(command=original.command, capture=captured).sha256})
    endpoint = ScriptedEndpoint([
        tool_call("lookup_route", {"label": "ASUS to Costco"}, "route-call"),
        {"content": "華碩立功大樓走路到 Costco 約 2 分鐘，路線圖已附上。"},
        {"content": "台北 101 地上有 101 層樓。"},
    ])
    endpoint.expected_url = MODEL_ROOT + "/chat/completions"
    endpoint.expected_model = sender.capture.model.id
    endpoint.unbounded = True

    class Directory:
        async def directory(self, _conversation_id=None):
            return NativePeerDirectory(agents=(
                AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="fa-stage-b"),))

    class RouteTools(NativeAgentTools):
        async def prepare(self):
            async def lookup(_packet):
                # The owner appends while the original tool is still running.
                assert await self.control.steering.send(APPEND)
                return ToolOutput(RouteResult(text="walk 2 minutes, 110 m").model_dump_json())

            self.result_schemas["lookup_route"] = RouteResult
            return [*await super().prepare(), ToolSpec("lookup_route", "Look up a walking route.", {
                "type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"],
                "additionalProperties": False}, lookup)]

    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=runtime_path, api_url="https://api.invalid", model_url=MODEL_ROOT,
        token_provider=endpoint.token, model_transport=endpoint.transport,
        agent_tools=lambda receipt, control: RouteTools(Directory(), receipt, control))
    scope = NativeCheckpointScope(
        agent_id=sender.agent_id, conversation_id=sender.conversation_id, sdk_session_id="enqueued-append-session")

    async def one_turn():
        try:
            return await asyncio.wait_for(executor(sender, scope, None, NativeTurnControl()), timeout=90)
        finally:
            await executor.aclose()  # ADR-0005: stop the kept SDK on this loop, as VM shutdown does

    result = asyncio.run(one_turn())
    bodies = [json.dumps(body, ensure_ascii=False) for body in endpoint.requests]
    assert len(bodies) == 3
    # The original work finishes from its tool result without the append; the append is the next round.
    assert APPEND not in bodies[1] and "walk 2 minutes" in bodies[1]
    assert APPEND in bodies[2]
    assert result.text == "華碩立功大樓走路到 Costco 約 2 分鐘，路線圖已附上。\n\n台北 101 地上有 101 層樓。"
