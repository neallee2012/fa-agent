"""SDK execution-side counterexample: no FA pre-hook/parent-readonly fence participates."""

import asyncio
import importlib.metadata
import json

import pytest
from copilot import CopilotClient, CopilotRequestHandler, RuntimeConnection, ToolSet
from copilot.client import ModelCapabilities, ModelInfo, ModelLimits, ModelSupports
from copilot.generated.rpc import PermissionDecisionApproveOnce
from copilot.session import ModelCapabilitiesOverride, ModelLimitsOverride, ModelSupportsOverride
from copilot.tools import Tool, ToolResult

from shared_context.behavior_delegation_wire import child_configuration
from sdk_checkpoint_local import sse
from test_m2_copilot import runtime_path
from test_skill_isolation_native import child_environment


def test_zero_tool_child_cannot_execute_the_parents_effect_handler(tmp_path, runtime_path):
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    async def run():
        model, endpoint = "gpt-4.1-mini", "https://scripted.invalid/openai/v1/chat/completions"
        calls, effects, permissions, child_attempts = [], [], [], []

        class Scripted(CopilotRequestHandler):
            async def send_request(self, request, ctx):
                assert str(request.url) == endpoint and request.method == "POST"
                body = json.loads(request.content)
                child = ctx.parent_agent_id is not None
                previous = sum(kind == child for kind in calls)
                calls.append(child)
                assert len(calls) <= 5
                names = {tool["function"]["name"] for tool in body.get("tools", [])}
                assert names == (set() if child else {"task", "parent_effect"})
                if child:
                    delta = (tool_call("parent_effect", {"tag": "child"}, "child-attempt")
                             if previous == 0 else {"content": "Child finished after refused tool."})
                elif previous == 0:
                    delta = tool_call("task", {"name": "one-child", "description": "Synthetic scope check",
                        "prompt": "Use only the supplied synthetic material.", "agent_type": "fa-delegate-v1",
                        "mode": "sync"}, "parent-task")
                elif previous == 1:
                    delta = tool_call("parent_effect", {"tag": "parent"}, "parent-proof")
                else:
                    delta = {"content": "Parent completed the in-memory counterexample."}
                return sse(request, delta, len(calls))

            async def open_websocket(self, ctx):
                pytest.fail("No network or WebSocket is allowed")

        async def effect(invocation):
            effects.append((invocation.tool_call_id, invocation.arguments["tag"]))
            return ToolResult(text_result_for_llm="One synthetic in-memory count; no external effect.")

        def permission(request, context):
            permissions.append(request.tool_call_id)
            return PermissionDecisionApproveOnce()

        def observed(event):
            if event.type.value == "tool.execution_complete" and event.data.tool_call_id == "child-attempt":
                child_attempts.append(event.data.success)

        env = child_environment(tmp_path)
        client = CopilotClient(connection=RuntimeConnection.for_stdio(path=str(runtime_path)), env=env,
            working_directory=str(tmp_path / "work"), base_directory=str(tmp_path / "data"),
            use_logged_in_user=False, mode="empty", log_level="error", request_handler=Scripted(),
            on_list_models=lambda: [ModelInfo(model, model, ModelCapabilities(ModelSupports(vision=False),
                ModelLimits(max_prompt_tokens=32768, max_context_window_tokens=65536)))])
        owned = None
        try:
            await client.start()
            owned = client._cli_process
            assert (await client.get_status()).version == "1.0.83"
            session = await client.create_session(model=model, streaming=True,
                tools=[Tool(name="parent_effect", description="Only increments a synthetic in-memory counter.",
                    parameters={"type": "object", "properties": {"tag": {"type": "string"}},
                                "required": ["tag"], "additionalProperties": False},
                    handler=effect, skip_permission=False)],
                available_tools=ToolSet().add_builtin("task").add_custom("parent_effect"),
                excluded_tools=ToolSet().add_mcp("*"), on_permission_request=permission, on_event=observed,
                custom_agents=[{**child_configuration(), "model": model}],
                provider={"type": "openai", "wire_api": "completions", "transport": "http",
                    "base_url": endpoint.removesuffix("/chat/completions"), "model_id": model, "wire_model": model,
                    "bearer_token_provider": lambda _: "SYNTHETIC_NO_CREDENTIAL"},
                model_capabilities=ModelCapabilitiesOverride(supports=ModelSupportsOverride(vision=False),
                    limits=ModelLimitsOverride(max_prompt_tokens=32768, max_output_tokens=4096,
                                              max_context_window_tokens=65536)),
                system_message={"mode": "replace", "content": "Synthetic tool-scope counterexample."},
                working_directory=str(tmp_path / "work"), config_directory=str(tmp_path / "config"),
                mcp_servers={}, skill_directories=[], plugin_directories=[], instruction_directories=[],
                enable_config_discovery=False, enable_managed_settings=False)
            result = await session.send_and_wait("Delegate once; the parent then runs its counter.", timeout=45)
            assert result.data.content == "Parent completed the in-memory counterexample."
            assert calls == [False, True, True, False, False]
            assert child_attempts == [False]
            assert effects == [("parent-proof", "parent")]
            assert permissions == ["parent-proof"]
        finally:
            await asyncio.wait_for(client.stop(), timeout=25)
            assert owned is not None and owned.poll() is not None
    asyncio.run(run())


def tool_call(name, arguments, identity):
    return {"tool_calls": [{"index": 0, "id": identity, "type": "function",
                           "function": {"name": name, "arguments": json.dumps(arguments)}}]}
