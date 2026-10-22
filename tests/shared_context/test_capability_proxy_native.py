"""One pinned SDK 1.0.13 / runtime 1.0.83 turn using a captured WebIQ-style capability.

The model is a scripted provider and the MCP service is an in-process fake served through the real
API-side MCP client; no network, cloud or real model is used.
"""

import importlib.metadata
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from copilot import CopilotClient
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse

from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted
from shared_context import mcp_client
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_capabilities import McpToolResult
from shared_context.behavior_capability_prompt import assemble_capabilities
from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_capability_capture import CATALOG, GRANTS, WEB_TOOLS, choose
from test_conversation_profile_selection import configured
from test_instruction_flow import check_history, instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key

KEY = "OfflineWebIqKey9Zx2Lp7Rt4Vw8Ky3Nb6"


def fake_webiq():
    # Served at the captured entry's URL path; the in-process transport never reaches the network.
    server = FastMCP("fake-webiq", stateless_http=True, json_response=True, streamable_http_path="/v3/mcp",
                     transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @server.tool(description="Search the web.")
    def web(query: str) -> str:
        return json.dumps({"webResults": [{"url": "https://example.test/fa", "content": "FA result for " + query}]})

    @server.tool(description="Read one URL.")
    def browse(url: str) -> str:
        return "page"
    app = server.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and dict(scope["headers"]).get(b"x-apikey") != KEY.encode():
            await PlainTextResponse("denied", status_code=401)(scope, receive, send)
            return
        await app(scope, receive, send)
    return server, httpx.ASGITransport(app=guarded)


def served(operation, calls):
    """Each API call runs its own fake server inside the event loop the real client uses."""
    async def wrapped(*args, **kwargs):
        server, transport = fake_webiq()
        try:
            async with server.session_manager.run():
                result = await operation(*args, transport=transport, **kwargs)
        except BaseExceptionGroup as group:
            # The fake server's own task group wraps what the client raised.
            problems = group.subgroup(lambda error: isinstance(error, Problem))
            if problems is None or len(problems.exceptions) != 1:
                raise
            raise problems.exceptions[0] from None
        calls.append(operation.__name__)
        return result
    return wrapped


def test_pinned_runtime_calls_a_captured_api_capability_through_the_fa_proxy(assistant, monkeypatch):
    h, control, _ = configured(assistant)
    h.runtime.capability_grants, h.runtime.capability_catalog = GRANTS, CATALOG
    monkeypatch.setenv("FA_CAPABILITY_SECRET_WEBSEARCH_API_KEY", KEY)
    service_calls = []
    monkeypatch.setattr(mcp_client, "list_tools", served(mcp_client.list_tools, service_calls))
    monkeypatch.setattr(mcp_client, "call_tool", served(mcp_client.call_tool, service_calls))
    chosen, _ = choose(h, "choose-webiq", ("WebSearch-MCP",))
    assert chosen.status_code == 200, chosen.text

    bridges, options, requests, results = [], [], [], []
    initialize, create = hosted.AssistantSession.__init__, CopilotClient.create_session

    def bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def created(instance, **kwargs):
        options.append(kwargs)
        return await create(instance, **kwargs)
    monkeypatch.setattr(hosted.AssistantSession, "__init__", bridge)
    monkeypatch.setattr(CopilotClient, "create_session", created)

    def provider(request):
        body = json.loads(request.content)
        number = len(requests) + 1
        tools = {item["function"]["name"] for item in body["tools"]}
        assert tools == {*HISTORY_CAPABILITIES, *WEB_TOOLS}
        system = [item["content"] for item in body["messages"] if item["role"] == "system"]
        requests.append({"number": number, "system": system})
        if number == 1:
            delta = {"tool_calls": [{"index": 0, "id": "call-webiq", "type": "function", "function": {
                "name": "WebSearch-MCP-web", "arguments": json.dumps({"query": "foundry agents"})}}]}
        else:
            tool = [item for item in body["messages"] if item["role"] == "tool"][-1]
            text = tool["content"] if isinstance(tool["content"], str) else "".join(
                part["text"] for part in tool["content"])
            results.append(McpToolResult.model_validate_json(text))
            delta = {"content": "Found it at https://example.test/fa."}
        assert number <= 2
        return sse(request, delta, number)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_CAPABILITY_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="capability-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory)
        accepted, _ = submit(h, "native-capability", "Search the web for foundry agents and cite it.")
        completed = h.runtime.controller.step(accepted["binding_id"])
        assert completed.state == "succeeded" and completed.result_forwarded
    assert len(bridges) == len(options) == 1 and len(requests) == 2 and len(results) == 1
    assert service_calls == ["list_tools", "call_tool"]
    result = results[0]
    assert not result.is_error and "FA result for foundry agents" in result.text and KEY not in result.text
    task = control.records[accepted["binding_id"]].profile.instruction
    assert [grant.entry_id for grant in task.capabilities.grants] == ["WebSearch-MCP"]
    combined = assemble_capabilities(task.prompt_profile, task.capabilities)
    assert combined.system_message in requests[0]["system"]
    assert sorted(options[0]["available_tools"].to_list()) == sorted(
        "custom:" + name for name in (*HISTORY_CAPABILITIES, *WEB_TOOLS))
    assert options[0]["mcp_servers"] == {}
    archive = saved_turn(h, h.runtime.bindings[accepted["binding_id"]].job_id)
    assert archive.capability_assembly == combined.record
    assert KEY not in json.dumps([r["system"] for r in requests])
    check_history(h)
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
