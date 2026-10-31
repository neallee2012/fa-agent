"""P3a: deterministic capability prompt, merged session needs, gateway/boundary grants, archive record."""

import hashlib
import json

import pytest

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_assistant_gateway import AssistantProvider, MAX_REQUEST_IMAGES
from shared_context.behavior_assistant_wire import WireModel
from shared_context.behavior_capabilities import CapabilityCapture, available_grants
from shared_context.behavior_capability_prompt import (
    assemble_capabilities, capability_record, session_needs,
)
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_instruction_prompt import INSTRUCTION_CAPABILITIES
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.errors import Problem
from shared_context.m2_copilot import _retain_latest_inline_png
from shared_context.tool_catalog import build_catalog, parse_entry_file
from test_behavior_assistant import assistant
from test_capability_capture import WEB_TOOLS, choose, enabled, turn
from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES
from test_instruction_flow import instruction_models, runtime_path
from test_instruction_prompt import saved_turn, sdk_options
from test_m3_auth import jwk_client, signing_key

WEBIQ = parse_entry_file("websearch.json", json.dumps({"mcpServers": {"WebSearch-MCP": {
    "type": "http", "url": "https://websearch.example.test/v3/mcp",
    "headers": {"x-apikey": "${secret:websearch-api-key}"}, "tools": ["web", "browse"]}}}).encode())
PLAYWRIGHT = parse_entry_file("playwright.json", json.dumps({"mcpServers": {"playwright": {
    "type": "local", "command": "/opt/fa-tools/bin/playwright-mcp", "args": ["--headless", "--isolated"],
    "env": {"PLAYWRIGHT_BROWSERS_PATH": "0"}, "tools": ["browser_navigate", "browser_snapshot"]}}}).encode())
GRANTS = available_grants(build_catalog([WEBIQ, PLAYWRIGHT]), ("fa-desktop-v1",))
ALL = CapabilityCapture(head_selection_id="head", grants=tuple(GRANTS[name] for name in sorted(GRANTS)))
PNG = "data:image/png;base64," + "A" * 64
BASE_HISTORY = (3678, "3d1c1e75d3eb95e36090c169195264cacee24d957e3e0e356b76f25db0d5648f")


def image_message(count):
    return {"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG}} for _ in range(count)]}


def request(*messages, tools=()):
    return {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Go."}, *messages],
            "tools": [{"type": "function", "function": {"name": name, "parameters": {}}} for name in tools]}


def test_prefix_keeps_base_bytes_and_records_a_verifiable_catalog_version():
    base = assemble_prompt(HISTORY_PROFILE)
    assert (len(base.system_message.encode()), hashlib.sha256(base.system_message.encode()).hexdigest()) == BASE_HISTORY
    assert assemble_capabilities(HISTORY_PROFILE, None) is None
    assert assemble_capabilities(HISTORY_PROFILE, CapabilityCapture(head_selection_id=None, grants=())) is None
    combined = assemble_capabilities(HISTORY_PROFILE, ALL)
    assert combined.system_message.startswith(base.system_message)
    assert combined.system_message == assemble_capabilities(HISTORY_PROFILE, ALL).system_message
    record = combined.record
    raw = combined.system_message.encode()
    assert record.prefix_sha256 == hashlib.sha256(raw).hexdigest() and record.prefix_utf8_bytes == len(raw)
    assert record.base_prefix_sha256 == BASE_HISTORY[1]
    assert record.declared_capabilities == (*base.record.declared_capabilities, *combined.capability_tools)
    assert record.capability_catalog_version.startswith("fa-instruction-tools-history-v1+caps-")
    assert "x-apikey" not in combined.system_message and "/opt/fa-tools" not in combined.system_message
    assert capability_record(HISTORY_PROFILE, ALL) == record
    narrower = CapabilityCapture(head_selection_id="head", grants=(GRANTS["WebSearch-MCP"],))
    assert capability_record(HISTORY_PROFILE, narrower).capability_set_sha256 != record.capability_set_sha256
    with pytest.raises(ValueError):
        assemble_capabilities("fa-instruction-user-files-v1", ALL)


def test_session_needs_follow_each_grant_kind():
    session = session_needs(ALL)
    assert session_needs(None) is None
    assert session.custom_tools == ("WebSearch-MCP-web", "WebSearch-MCP-browse", "desktop_open_file", "desktop_observe",
                                    "desktop_act", "desktop_read_file")
    assert session.mcp_servers == {"playwright": {
        "type": "stdio", "command": "/opt/fa-tools/bin/playwright-mcp", "args": ["--headless", "--isolated"],
        "env": {"PLAYWRIGHT_BROWSERS_PATH": "0"}, "tools": ["browser_navigate", "browser_snapshot"]}}
    assert session.mcp_tools == ("playwright-browser_navigate", "playwright-browser_snapshot")
    assert session.permitted_mcp == {("playwright", "playwright-browser_navigate"),
                                     ("playwright", "playwright-browser_snapshot")}
    assert session.vision and session.image_tools == {"desktop_open_file", "desktop_observe", "desktop_act",
                                                      "desktop_read_file"}
    assert session.builtin_tools == () and session.custom_agents == ()
    web_only = session_needs(CapabilityCapture(head_selection_id=None, grants=(GRANTS["WebSearch-MCP"],)))
    assert not web_only.vision and not web_only.image_tools and not web_only.mcp_servers


def test_gateway_admits_capability_tools_and_images_only_from_the_grant():
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions", object())
    ok = dict(text_reply=True, model="gpt-4.1-mini")
    gateway.validate(request(), **ok)
    with pytest.raises(Problem) as denied:
        gateway.validate(request(tools=("WebSearch-MCP-web",)), **ok)
    assert denied.value.body["error"]["code"] == "ASSISTANT_TOOLS_DENIED"
    gateway.validate(request(tools=("WebSearch-MCP-web",)), capability_tools=frozenset({"WebSearch-MCP-web"}), **ok)
    with pytest.raises(Problem) as denied:
        gateway.validate(request(image_message(1)), capability_tools=frozenset({"WebSearch-MCP-web"}), **ok)
    assert denied.value.body["error"]["code"] == "ASSISTANT_IMAGES_DENIED"
    gateway.validate(request(image_message(MAX_REQUEST_IMAGES)), allow_images=True, **ok)
    for bad in (image_message(MAX_REQUEST_IMAGES + 1),
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}]}):
        with pytest.raises(Problem) as denied:
            gateway.validate(request(bad), allow_images=True, **ok)
        assert denied.value.body["error"]["code"] == "ASSISTANT_IMAGES_DENIED"
    with pytest.raises(Problem):
        gateway.validate(request(tools=("read_context",)), capability_tools=frozenset({"read_context"}), **ok)
    with pytest.raises(Problem):
        gateway.validate({**request(), "max_tokens": 1024, "model": "gpt-4.1-mini"},
                         capability_tools=frozenset({"WebSearch-MCP-web"}))


class Result(WireModel):
    text: str


def test_boundary_types_capability_results_without_shadowing_base_tools():
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results={"WebSearch-MCP-web": Result})
    assert json.loads(boundary.tool_result("WebSearch-MCP-web", '{"text":"page"}')) == {"text": "page"}
    with pytest.raises(ValueError):
        boundary.tool_result("WebSearch-MCP-web", '{"text":"page","extra":1}')
    with pytest.raises(KeyError):
        AssistantModelBoundary(None, text_reply=True).tool_result("WebSearch-MCP-web", '{"text":"page"}')
    with pytest.raises(ValueError):
        AssistantModelBoundary(None, text_reply=True, capability_results={"read_context": Result}).tool_result(
            "read_context", '{"text":"x"}')
    with pytest.raises(ValueError):
        AssistantModelBoundary(None, capability_results={"WebSearch-MCP-web": Result})


def test_image_retention_keeps_the_old_single_image_default_and_three_for_grants():
    def body(count):
        return {"messages": [image_message(1) for _ in range(count)]}
    single = body(4)
    assert _retain_latest_inline_png(single) == 3
    assert "only the latest desktop screenshot" in single["messages"][0]["content"][0]["text"]
    assert single["messages"][3]["content"][0]["type"] == "image_url"
    three = body(5)
    assert _retain_latest_inline_png(three, 3) == 2
    assert [m["content"][0]["type"] for m in three["messages"]] == ["text", "text", "image_url", "image_url", "image_url"]


@pytest.mark.parametrize("profile,base_tools", [
    (HISTORY_PROFILE, HISTORY_CAPABILITIES), ("fa-instruction-v2", INSTRUCTION_CAPABILITIES),
])
def test_archive_saves_and_rebuilds_the_capability_record(assistant, monkeypatch, profile, base_tools):
    h, control = enabled(assistant, monkeypatch, grants=GRANTS, catalog=build_catalog([WEBIQ, PLAYWRIGHT]),
                         tools=(*base_tools, *WEB_TOOLS))
    calls, options = sdk_options(monkeypatch, expected_tools=(*base_tools, *WEB_TOOLS))
    choice, _ = choose(h, "choose-web", ("WebSearch-MCP",), profile=profile)
    assert choice.status_code == 200, choice.text
    accepted, task = turn(h, control, "capability-archive")
    saved = saved_turn(h, h.runtime.bindings[accepted["binding_id"]].job_id)
    assert saved.capability_assembly == capability_record(task.prompt_profile, task.capabilities)
    assert saved.prompt_assembly == assemble_prompt(task.prompt_profile).record
    assert len(calls) == 1
    assert options[0]["system_message"] == {
        "mode": "replace", "content": assemble_capabilities(profile, task.capabilities).system_message,
    }
    context = h.client.get(f"/work-policy/v1/conversations/{accepted['conversation_id']}/turns/1/context",
                           headers=h.headers())
    assert context.status_code == 200, context.text
    assert context.json()["capability_assembly"] == saved.capability_assembly.model_dump(mode="json")
