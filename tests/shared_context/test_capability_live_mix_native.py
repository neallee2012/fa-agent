"""Pinned SDK 1.0.13 / runtime 1.0.83 turn with the live v44 capability mix: WebIQ + desktop + Playwright.

Scripted provider, in-process fake WebIQ through the real API-side MCP client, and an offline stdio stand-in
for Playwright with the same tool names; the desktop handler is replaced by a refusing stub (no X on this host).
No network, cloud or real model.
"""

import json
import logging
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest
from copilot import CopilotClient
from fastapi.testclient import TestClient

from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted
from shared_context import mcp_client
from shared_context.behavior_agent_template import TemplateSettings
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_capabilities import CapabilityRef, available_grants, enabled_toolboxes
from shared_context.behavior_desktop_tools import DESKTOP_TOOLS, tool_schema
from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES, HISTORY_PROFILE
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.m2_copilot import ToolFailure, ToolSpec
from shared_context.tool_catalog import StdioEntry, build_catalog, load_catalog
from test_behavior_assistant import assistant
from test_capability_calls import WEBIQ
from test_capability_proxy_native import KEY, served
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import check_history, instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key

SOURCE = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / "tool_catalog"
FAKE_PLAYWRIGHT = Path(__file__).with_name("fake_playwright_stdio_server.py")


def live_like_catalog():
    """The committed catalog as deployed (N5 toolbox entries off); only the Playwright command is swapped.

    The committed catalog has no mcp-api entry since WebIQ-MCP was retired (2026-10-22), so the fictional
    WebSearch-MCP fixture stands in for the API-brokered kind."""
    committed = enabled_toolboxes(load_catalog(SOURCE), ())
    committed = build_catalog([*committed.entries, WEBIQ])
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH") if name in os.environ}
    entries = []
    for entry in committed.entries:
        if entry.entry_id == "playwright":
            entry = entry.model_copy(update={"config": StdioEntry(
                type="stdio", command=sys.executable, args=(str(FAKE_PLAYWRIGHT),),
                env=tuple(sorted({**dict(entry.config.env), **env}.items())))})
        entries.append(entry)
    return build_catalog(entries)


def refusing_desktop(session, grant):
    async def refuse(packet):
        raise ToolFailure("Desktop refused the action; do not claim completion or resend it.",
                          public_code="DESKTOP_UNAVAILABLE")
    return (tuple(ToolSpec(tool.name, tool.description, tool_schema(tool.name), refuse) for tool in DESKTOP_TOOLS),
            {tool.name: tool.result for tool in DESKTOP_TOOLS})


@pytest.mark.parametrize("delegate", [False, True])
def test_live_capability_mix_calls_every_kind(assistant, monkeypatch, caplog, delegate):
    h, control, _ = configured(assistant)
    catalog = live_like_catalog()
    grants = available_grants(catalog, ("fa-desktop-v1", "fa-delegate-v1") if delegate else ("fa-desktop-v1",))
    h.runtime.capability_grants, h.runtime.capability_catalog = grants, catalog
    name = control.models.default
    control.models.models[name] = control.models.models[name].model_copy(update={"supports_image_input": True})
    monkeypatch.setitem(hosted.CAPABILITY_HANDLERS, "fa-desktop-v1", refusing_desktop)
    monkeypatch.setenv("FA_CAPABILITY_SECRET_WEBSEARCH_API_KEY", KEY)
    service_calls = []
    monkeypatch.setattr(mcp_client, "list_tools", served(mcp_client.list_tools, service_calls))
    monkeypatch.setattr(mcp_client, "call_tool", served(mcp_client.call_tool, service_calls))
    refs = tuple(CapabilityRef(entry_id=grant.entry_id, entry_sha256=grant.entry_sha256)
                 for grant in sorted(grants.values(), key=lambda item: item.entry_id))
    chosen, _ = select(h, request_id="choose-live-mix", value=template(
        HISTORY_PROFILE, user_text_file_profile="fa-user-text-files-v1", capabilities=refs))
    assert chosen.status_code == 200, chosen.text

    options, requests, tool_messages = [], [], []
    create = CopilotClient.create_session

    async def created(instance, **kwargs):
        options.append(kwargs)
        return await create(instance, **kwargs)
    monkeypatch.setattr(CopilotClient, "create_session", created)
    script = [
        ("call-webiq", "WebSearch-MCP-web", {"query": "foundry agents"}),
        ("call-browser", "playwright-browser_navigate", {"url": "http://127.0.0.1:9/"}),
        ("call-desktop", "desktop_read_file", {"name": "notes.txt"}),
    ]

    def provider(request):
        body = json.loads(request.content)
        number = len(requests) + 1
        requests.append(sorted(item["function"]["name"] for item in body.get("tools", [])))
        tool_messages.append([m for m in body["messages"] if m["role"] == "tool"])
        if number <= len(script):
            call_id, name, arguments = script[number - 1]
            delta = {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                     "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        else:
            delta = {"content": "Done with the offline capability mix."}
        assert number <= len(script) + 1
        return sse(request, delta, number)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_LIVE_MIX_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    events = []
    original_event = hosted.AssistantSession._public_event

    def observe(self, event):
        events.append(event)
        return original_event(self, event)
    monkeypatch.setattr(hosted.AssistantSession, "_public_event", observe)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="live-mix-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory)
        with caplog.at_level(logging.WARNING):
            accepted, _ = submit(h, "native-live-mix-delegate" if delegate else "native-live-mix",
                                 "Search, browse and read my notes.")
            completed = h.runtime.controller.step(accepted["binding_id"])
        print("LIVE_MIX_STATE", completed.state, completed.result_forwarded)
        print("LIVE_MIX_TOOLS", requests[:1])
        print("LIVE_MIX_SERVICE_CALLS", service_calls)
        public = [e["public_progress"] for e in events if "public_progress" in e]
        print("LIVE_MIX_PUBLIC_TOOLS", [(p["kind"], p["tool_name"], p["status"]) for p in public if p["tool_name"]])
        print("LIVE_MIX_LOGS", [r.getMessage()[:160] for r in caplog.records
                                if r.getMessage().startswith(("FA_CAPABILITY_CALL", "FA_MCP_SERVER_STATUS",
                                                              "ASSISTANT_DEPENDENCY_FAILED"))])
        assert completed.state == "succeeded" and completed.result_forwarded
    expected = sorted({*HISTORY_CAPABILITIES, "WebSearch-MCP-web", "WebSearch-MCP-browse",
                       *(tool.name for tool in DESKTOP_TOOLS),
                       *("playwright-" + name for name in catalog.entry("playwright").tools),
                       *(("task",) if delegate else ())})
    assert requests[0] == expected
    assert service_calls == ["list_tools", "call_tool"]
    if delegate:
        assert options[0]["custom_agents"][0]["tools"] == []
        assert options[0]["custom_agents"][0]["name"] == "fa-delegate-v1"
        assert options[0]["excluded_tools"].to_list() == []
    assert "navigated:http://127.0.0.1:9/" in json.dumps(tool_messages[2])
    tool_events = [(p["kind"], p["tool_name"], p["status"]) for p in public if p["tool_name"]]
    assert tool_events == [
        ("tool_started", "WebSearch_MCP_web", "started"), ("tool_completed", "WebSearch_MCP_web", "succeeded"),
        ("tool_started", "playwright_browser_navigate", "started"),
        ("tool_completed", "playwright_browser_navigate", "succeeded"),
        ("tool_started", "desktop_read_file", "started"), ("tool_completed", "desktop_read_file", "failed")]
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("FA_CAPABILITY_CALL ") and '"outcome": "ok"' in m for m in messages)
    assert any(m.startswith("FA_MCP_SERVER_STATUS ") and '"server": "playwright"' in m for m in messages)
    assert not any(m.startswith("ASSISTANT_DEPENDENCY_FAILED") for m in messages) and KEY not in caplog.text
    check_history(h)
