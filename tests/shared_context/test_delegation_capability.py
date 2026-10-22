"""Captured delegation needs use the existing capability framework, not a new prompt profile."""

from types import SimpleNamespace
import hashlib
import json

import pytest

from shared_context.behavior_capabilities import CapabilityCapture, available_grants
from shared_context.behavior_capability_prompt import assemble_capabilities, session_needs
from shared_context.errors import Problem


def captured(*names):
    grants = available_grants(None, names)
    return CapabilityCapture(head_selection_id="human-selection",
                             grants=tuple(grants[name] for name in sorted(names)))


def test_delegation_declares_only_the_fixed_zero_tool_sdk_child():
    capture = captured("fa-delegate-v1")
    assert capture.grants[0].tools == ("task",)
    needs = session_needs(capture)
    assert needs.builtin_tools == ("task",)
    assert needs.custom_tools == needs.mcp_tools == ()
    assert needs.mcp_servers == {} and not needs.vision
    assert len(needs.custom_agents) == 1
    agent = needs.custom_agents[0]
    assert agent["name"] == "fa-delegate-v1" and agent["tools"] == []
    assert "model" not in agent


def test_delegation_keeps_the_parents_original_history_and_draft_tools():
    value = assemble_capabilities("fa-instruction-history-skill-draft-v1", captured("fa-delegate-v1"))
    assert "task" in value.record.declared_capabilities
    assert "execute_operation" in value.record.declared_capabilities
    assert "save_skill_draft" in value.record.declared_capabilities
    assert "read_user_turn" in value.record.declared_capabilities
    assert value.record.base_profile == "fa-instruction-history-skill-draft-v1"
    assert "synchronous" in value.system_message and "zero tools" in value.system_message


def test_delegation_composes_with_desktop_and_both_mcp_kinds_without_giving_child_tools():
    from shared_context.tool_catalog import build_catalog
    from test_capability_assembly import WEBIQ, PLAYWRIGHT
    grants = available_grants(build_catalog([WEBIQ, PLAYWRIGHT]), ("fa-delegate-v1", "fa-desktop-v1"))
    capture = CapabilityCapture(head_selection_id="head", grants=tuple(grants[name] for name in sorted(grants)))
    needs = session_needs(capture)
    assert needs.builtin_tools == ("task",)
    assert "desktop_act" in needs.custom_tools and "WebSearch-MCP-web" in needs.custom_tools
    assert "playwright-browser_navigate" in needs.mcp_tools and "playwright" in needs.mcp_servers
    assert needs.vision and "desktop_observe" in needs.image_tools
    assert needs.custom_agents[0]["tools"] == [] and needs.custom_agents[0]["mcp_servers"] == {}
    value = assemble_capabilities("fa-instruction-history-skill-draft-v1", capture)
    assert {"task", "save_skill_draft", "execute_operation", "desktop_act", "WebSearch-MCP-web",
            "playwright-browser_navigate"} <= set(value.record.declared_capabilities)


def test_no_grant_leaves_legacy_session_and_prompt_unchanged():
    assert session_needs(None) is None
    assert assemble_capabilities("fa-instruction-history-v1", None) is None


def test_reader_is_explicit_and_old_zero_tool_material_is_unchanged(monkeypatch):
    from shared_context.behavior_capabilities import load_runtime_grants
    from shared_context.behavior_delegation_wire import child_configuration

    monkeypatch.delenv("FA_TOOL_CATALOG_DIR", raising=False)
    monkeypatch.delenv("FA_CAPABILITY_TOOLBOX_ENTRIES", raising=False)
    monkeypatch.setenv("FA_CAPABILITY_BUILTINS", "fa-delegate-v1")
    grants = load_runtime_grants()
    assert tuple(grants) == ("fa-delegate-v1",)
    assert grants["fa-delegate-v1"].entry_sha256 == "f4adfc2afebb4799fa919817e1faef1ab69827e49b60afc0bd430dc42c91f711"
    raw = json.dumps(child_configuration(), sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(raw).hexdigest() == "ad9c5aa78a41e663b25d2435fafc01880893f7a2d8a626f2c1bd678dd55860eb"
    monkeypatch.setenv("FA_CAPABILITY_BUILTINS", "fa-delegate-v1,fa-delegate-reader-v1")
    assert set(load_runtime_grants()) == {"fa-delegate-v1", "fa-delegate-reader-v1"}
    capture = captured("fa-delegate-v1", "fa-delegate-reader-v1")
    needs = session_needs(capture)
    assert needs.custom_agents[0]["tools"] == ["read_user_file"]
    assert needs.custom_tools == () and needs.builtin_tools == ("task",)
    assert needs.custom_agents[0]["mcp_servers"] == {} and needs.custom_agents[0]["skills"] == []
    assert "read_user_file" in assemble_capabilities("fa-instruction-history-v1", capture).base_capabilities


@pytest.mark.parametrize("profile,names", [
    ("fa-instruction-history-v1", ("fa-delegate-reader-v1",)),
    ("fa-instruction-v2", ("fa-delegate-reader-v1", "fa-delegate-v1")),
])
def test_reader_selection_requires_delegation_and_the_parents_existing_reader(profile, names):
    from shared_context.behavior_capabilities import check_selectable

    grants = available_grants(None, names)
    with pytest.raises(Problem) as error:
        check_selectable(tuple(grants[name].ref for name in sorted(names)), profile, grants)
    assert error.value.body["error"]["code"] in {"CAPABILITY_SELECTION_INVALID", "CAPABILITY_BASE_UNSUPPORTED"}


@pytest.mark.parametrize("state_profile", ["foundry-native-v1", "foundry-responses-v1"])
def test_native_reader_selection_and_current_capture_can_remove_it(state_profile):
    from shared_context.behavior_capabilities import capture as capture_grants
    from shared_context.behavior_profile_selection import profile_values_from_template
    from test_conversation_profile_selection import template

    names = ("fa-delegate-reader-v1", "fa-delegate-v1")
    grants = available_grants(None, names)
    refs = tuple(grants[name].ref for name in sorted(names))
    value = template("fa-instruction-history-v1", capabilities=refs, state_profile=state_profile)
    assert profile_values_from_template(value)["capabilities"] == refs
    remaining = capture_grants(refs, (grants["fa-delegate-v1"].ref,), "new-head", grants)
    assert session_needs(remaining).custom_agents[0]["tools"] == []


def test_server_gateway_keeps_parent_tool_authority_when_delegation_is_selected():
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from shared_context.behavior_capability_calls import gateway_options
    session = SimpleNamespace(assignment=SimpleNamespace(
        task=SimpleNamespace(instruction=SimpleNamespace(capabilities=captured("fa-delegate-v1")))))
    options, _ = gateway_options(session)
    provider = AssistantProvider("https://synthetic.invalid/openai/v1/chat/completions", None)
    body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Read only."}],
            "tools": [{"type": "function", "function": {"name": "execute_operation", "parameters": {}}}]}
    try:
        provider.validate(body, text_reply=True, model="gpt-4.1-mini", **options)
        body["tools"][0]["function"]["name"] = "task"
        provider.validate(body, text_reply=True, model="gpt-4.1-mini", **options)
    finally:
        import asyncio
        asyncio.run(provider.aclose())
