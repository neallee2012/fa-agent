"""從 test_capability_capture.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import json
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES, HISTORY_PROFILE
from shared_context.tool_catalog import build_catalog, parse_entry_file
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import sdk_io


FILES = "fa-user-text-files-v1"


WEBIQ = parse_entry_file("websearch.json", json.dumps({"mcpServers": {"WebSearch-MCP": {
    "type": "http", "url": "https://websearch.example.test/v3/mcp",
    "headers": {"x-apikey": "${secret:websearch-api-key}"}, "tools": ["web", "browse"]}}}).encode())


PLAYWRIGHT = parse_entry_file("playwright.json", json.dumps({"mcpServers": {"playwright": {
    "type": "local", "command": "/opt/fa-tools/bin/playwright-mcp", "args": ["--headless"],
    "tools": ["browser_navigate"]}}}).encode())


CATALOG = build_catalog([WEBIQ, PLAYWRIGHT])


GRANTS = available_grants(CATALOG, ("fa-desktop-v1",))


WEB_TOOLS = ("WebSearch-MCP-web", "WebSearch-MCP-browse")


def fake_services(monkeypatch):
    """Offline stand-ins for the API-side MCP client; no network is reached."""
    from shared_context import mcp_client
    from shared_context.behavior_capabilities import McpToolList, McpToolResult, McpToolSchema
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    calls = []

    async def list_tools(entry, lookup, **_):
        return McpToolList(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256, tools=tuple(
            McpToolSchema(name=name, description="Offline " + name, input_schema=schema,
                          input_schema_sha256="1" * 64) for name in entry.tools))

    async def call_tool(entry, lookup, tool, arguments, **_):
        calls.append((entry.entry_id, tool, arguments))
        return McpToolResult(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256, tool=tool, is_error=False,
                             text="offline result for " + json.dumps(arguments), truncated=False)
    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    monkeypatch.setattr(mcp_client, "call_tool", call_tool)
    return calls


def refs(*names):
    return tuple(sorted((GRANTS[name].ref for name in names), key=lambda item: item.entry_id))


def preinstalled(monkeypatch, *commands):
    """Offline stand-in for the Hosted image's /opt/fa-tools launchers; other paths are unchanged."""
    import os
    isfile, access = os.path.isfile, os.access
    monkeypatch.setattr(os.path, "isfile", lambda path: path in commands or isfile(path))
    monkeypatch.setattr(os, "access", lambda path, mode: path in commands or access(path, mode))


def enabled(assistant, monkeypatch, grants=GRANTS, catalog=CATALOG, tools=HISTORY_CAPABILITIES, image_tools=True):
    h, control, human = configured(assistant)
    h.runtime.capability_grants = grants
    h.runtime.capability_catalog = catalog
    fake_services(monkeypatch)
    if image_tools:
        preinstalled(monkeypatch, "/opt/fa-tools/bin/playwright-mcp")
    sdk_io(monkeypatch, expected_tools=tools)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": f"Reply {index}."} for index in range(8)])
    return h, control


def choose(h, request_id, names, previous=None, profile=HISTORY_PROFILE):
    return select(h, request_id=request_id, previous=previous,
                  value=template(profile, user_text_file_profile=FILES, capabilities=refs(*names)))
