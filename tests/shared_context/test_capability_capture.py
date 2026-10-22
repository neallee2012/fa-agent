"""Capability refs are human-selected, captured per turn, only narrow, and rebuild from records alone."""

import json
from pathlib import Path

import pytest

from shared_context.behavior_agent_template import TemplateSettings
from shared_context.behavior_assistant_wire import InstructionTask
from shared_context.behavior_capabilities import (
    CapabilityCapture, CapabilityRef, available_grants, capture, check_captured, enabled_toolboxes,
    load_runtime_grants,
)
from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES, HISTORY_PROFILE
from shared_context.errors import Problem
from shared_context.tool_catalog import build_catalog, load_catalog, parse_entry_file
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import check_history, instruction_models, runtime_path, sdk_io
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key

FILES = "fa-user-text-files-v1"
SOURCE = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
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


def turn(h, control, request_id, conversation=None):
    accepted, _ = submit(h, request_id, "Compare the two sources.", conversation)
    assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
    return accepted, control.records[accepted["binding_id"]].profile.instruction


def test_no_capability_wire_template_and_profile_bytes_are_unchanged():
    settings = TemplateSettings(user_text_file_profile=FILES)
    assert "capabilities" not in settings.model_dump_json()
    assert TemplateSettings(user_text_file_profile=FILES, capabilities=()).model_dump_json() == settings.model_dump_json()
    task = InstructionTask(content="x", conversation_id="c", turn_number=1)
    assert "capabilities" not in task.model_dump_json()
    with pytest.raises(ValueError):
        TemplateSettings(capabilities=tuple(reversed(refs("WebSearch-MCP", "playwright"))))


def test_selected_capabilities_are_captured_and_can_only_narrow(assistant, monkeypatch):
    h, control = enabled(assistant, monkeypatch, tools=(*HISTORY_CAPABILITIES, *WEB_TOOLS))
    first_choice, _ = choose(h, "choose-web-and-browser", ("WebSearch-MCP", "playwright"))
    assert first_choice.status_code == 200, first_choice.text
    first_id = first_choice.json()["result"]["selection_id"]
    accepted, task = turn(h, control, "capability-first")
    assert task.capabilities.head_selection_id == first_id
    assert [g.entry_id for g in task.capabilities.grants] == ["WebSearch-MCP", "playwright"]
    web, browser = task.capabilities.grants
    assert web.kind == "mcp-api" and web.launch is None and web.tools == ("WebSearch-MCP-web", "WebSearch-MCP-browse")
    assert browser.kind == "mcp-hosted" and browser.launch.command == "/opt/fa-tools/bin/playwright-mcp"
    assert "x-apikey" not in task.model_dump_json() and "secret" not in task.model_dump_json()

    narrowed, _ = choose(h, "choose-web-and-desktop", ("WebSearch-MCP", "fa-desktop-v1"), previous=first_id)
    assert narrowed.status_code == 200, narrowed.text
    followed, follow = turn(h, control, "capability-follow", accepted["conversation_id"])
    assert [g.entry_id for g in follow.capabilities.grants] == ["WebSearch-MCP"]
    assert follow.capabilities.head_selection_id == narrowed.json()["result"]["selection_id"]
    assert control.records[followed["binding_id"]].profile_selection_id == first_id

    control.records.clear()
    control.refresh()
    check_history(h)
    h.runtime.capability_grants = {}
    control.records.clear()
    control.refresh()
    check_history(h)


def test_catalog_removal_narrows_the_next_turn_without_breaking_history(assistant, monkeypatch):
    h, control = enabled(assistant, monkeypatch, tools=(*HISTORY_CAPABILITIES, *WEB_TOOLS))
    choice, _ = choose(h, "choose-web", ("WebSearch-MCP",))
    assert choice.status_code == 200, choice.text
    accepted, first = turn(h, control, "catalog-first")
    assert [g.entry_id for g in first.capabilities.grants] == ["WebSearch-MCP"]
    h.runtime.capability_catalog = build_catalog([PLAYWRIGHT])
    h.runtime.capability_grants = available_grants(h.runtime.capability_catalog)
    sdk_io(monkeypatch, expected_tools=HISTORY_CAPABILITIES)
    _, follow = turn(h, control, "catalog-follow", accepted["conversation_id"])
    assert follow.capabilities.grants == ()
    check_history(h)


def test_a_saved_webiq_mcp_selection_runs_without_it_once_the_committed_catalog_drops_it(assistant, monkeypatch):
    """2026-10-22: WebIQ-MCP (API secret broker) left the shipped catalog; WebIQ stays only as the toolbox entry.
    A template and a conversation saved while it was offered keep their records: their next turns run without it,
    like any entry gone from the catalog, and only saving it again is refused."""
    retired = parse_entry_file("webiq.json", json.dumps({"mcpServers": {"WebIQ-MCP": {
        "type": "http", "url": "https://api.microsoft.ai/v3/mcp",
        "headers": {"x-apikey": "${secret:webiq-api-key}"}, "tools": ["web", "browse"]}}}).encode())
    before = build_catalog([retired, PLAYWRIGHT])
    h, control = enabled(assistant, monkeypatch, grants=available_grants(before), catalog=before,
                         tools=(*HISTORY_CAPABILITIES, "WebIQ-MCP-web", "WebIQ-MCP-browse"))
    saved = (available_grants(before)["WebIQ-MCP"].ref,)
    choice, _ = select(h, request_id="choose-retired", value=template(
        HISTORY_PROFILE, user_text_file_profile=FILES, capabilities=saved))
    assert choice.status_code == 200, choice.text
    accepted, first = turn(h, control, "retired-first")
    assert [g.entry_id for g in first.capabilities.grants] == ["WebIQ-MCP"]

    committed = enabled_toolboxes(load_catalog(SOURCE / "tool_catalog"), ())
    assert committed.entry("WebIQ-MCP") is None
    h.runtime.capability_catalog = committed
    h.runtime.capability_grants = available_grants(committed)
    sdk_io(monkeypatch, expected_tools=HISTORY_CAPABILITIES)
    _, follow = turn(h, control, "retired-follow", accepted["conversation_id"])
    _, fresh = turn(h, control, "retired-new-conversation")
    assert follow.capabilities.grants == () and fresh.capabilities.grants == ()
    before_refusal = h.store._guard()
    again, _ = select(h, request_id="choose-retired-again", previous=choice.json()["result"]["selection_id"],
                      value=template(HISTORY_PROFILE, user_text_file_profile=FILES, capabilities=saved))
    assert again.status_code == 409 and again.json()["error"]["code"] == "CAPABILITY_SOURCES_CHANGED"
    assert h.store._guard() == before_refusal
    check_history(h)


def test_selection_refuses_unknown_changed_or_non_composable_entries(assistant, monkeypatch):
    h, control = enabled(assistant, monkeypatch)
    before = h.store._guard()
    stale = (CapabilityRef(entry_id="WebSearch-MCP", entry_sha256="0" * 64),)
    response, _ = select(h, request_id="stale", value=template(HISTORY_PROFILE, user_text_file_profile=FILES,
                                                                capabilities=stale))
    assert response.status_code == 409 and response.json()["error"]["code"] == "CAPABILITY_SOURCES_CHANGED"
    response, _ = select(h, request_id="non-composable",
                         value=template("fa-instruction-user-files-v1", user_text_file_profile=FILES,
                                        capabilities=refs("WebSearch-MCP")))
    assert response.status_code == 422
    assert h.store._guard() == before


@pytest.mark.parametrize("image_input", [None, True])
def test_vision_capabilities_need_an_image_model_at_admission(assistant, monkeypatch, image_input):
    h, control = enabled(assistant, monkeypatch)
    name = control.models.default
    control.models.models[name] = control.models.models[name].model_copy(update={"supports_image_input": image_input})
    choice, _ = choose(h, "choose-desktop", ("fa-desktop-v1",))
    assert choice.status_code == 200, choice.text
    before = h.store._guard()
    from shared_context.behavior_assistant_wire import InstructionSubmit
    command = InstructionSubmit(request_id="desktop-turn", content="Open my notes.")
    response = h.client.post("/work-policy/v1/submit", content=command.model_dump_json(), headers=h.headers())
    if image_input is True:
        assert response.status_code == 200, response.text
        task = control.records[response.json()["binding_id"]].profile.instruction
        assert task.capabilities.vision and task.capabilities.grants[0].tools[0] == "desktop_open_file"
    else:
        assert response.status_code == 422 and response.json()["error"]["code"] == "CAPABILITY_MODEL_UNSUPPORTED"
        assert h.store._guard() == before


def test_capture_rules_are_intersection_and_record_only_checks():
    everything = refs("WebSearch-MCP", "fa-desktop-v1", "playwright")
    head = refs("WebSearch-MCP", "playwright")
    captured = capture(refs("WebSearch-MCP", "fa-desktop-v1"), head, "head", GRANTS)
    assert [g.entry_id for g in captured.grants] == ["WebSearch-MCP"]
    assert capture((), everything, "head", GRANTS) is None
    assert check_captured(everything, captured, head) is captured
    with pytest.raises(Problem):
        check_captured(refs("playwright"), captured)
    with pytest.raises(Problem):
        check_captured(everything, captured, refs("playwright"))
    with pytest.raises(Problem):
        check_captured(everything, None)
    with pytest.raises(ValueError):
        CapabilityCapture(head_selection_id=None, grants=(GRANTS["playwright"], GRANTS["WebSearch-MCP"]))


def test_startup_loads_the_configured_catalog_and_builtins(tmp_path, monkeypatch):
    monkeypatch.delenv("FA_TOOL_CATALOG_DIR", raising=False)
    monkeypatch.delenv("FA_CAPABILITY_BUILTINS", raising=False)
    assert dict(load_runtime_grants()) == {}
    directory = tmp_path / "catalog"
    directory.mkdir()
    (directory / "websearch.json").write_text(json.dumps({"mcpServers": {"WebSearch-MCP": {
        "type": "http", "url": "https://websearch.example.test/v3/mcp",
        "headers": {"x-apikey": "${secret:websearch-api-key}"}, "tools": ["web", "browse"]}}}), encoding="utf-8")
    monkeypatch.setenv("FA_TOOL_CATALOG_DIR", str(directory))
    monkeypatch.setenv("FA_CAPABILITY_BUILTINS", "fa-desktop-v1")
    grants = load_runtime_grants()
    assert sorted(grants) == ["WebSearch-MCP", "fa-desktop-v1"] and grants["WebSearch-MCP"] == GRANTS["WebSearch-MCP"]
    with pytest.raises(TypeError):
        grants["x"] = grants["WebSearch-MCP"]
    monkeypatch.setenv("FA_CAPABILITY_BUILTINS", "fa-unknown-v1")
    with pytest.raises(Problem) as error:
        load_runtime_grants()
    assert error.value.body["error"]["code"] == "CAPABILITY_CONFIGURATION"


def test_colliding_tool_names_are_a_configuration_error_not_a_500():
    first = parse_entry_file("a.json", json.dumps({"mcpServers": {"a-b": {
        "type": "http", "url": "https://one.test/mcp", "headers": {"x-apikey": "${secret:one}"}, "tools": ["c"]}}}).encode())
    second = parse_entry_file("b.json", json.dumps({"mcpServers": {"a": {
        "type": "http", "url": "https://two.test/mcp", "headers": {"x-apikey": "${secret:two}"}, "tools": ["b-c"]}}}).encode())
    with pytest.raises(Problem) as error:
        available_grants(build_catalog([first, second]))
    assert error.value.body["error"]["code"] == "CAPABILITY_CONFIGURATION"
    grants = {**available_grants(build_catalog([first])), "a": available_grants(build_catalog([second]))["a"]}
    both = tuple(sorted((g.ref for g in grants.values()), key=lambda item: item.entry_id))
    with pytest.raises(Problem) as error:
        capture(both, both, "head", grants)
    assert error.value.body["error"]["code"] == "CAPABILITY_SOURCES_CHANGED"
