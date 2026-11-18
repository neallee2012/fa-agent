"""Capability refs are human-selected, captured per turn, only narrow, and rebuild from records alone."""

import json
from pathlib import Path

import pytest

from shared_context.behavior_agent_template import TemplateSettings
from shared_context.behavior_assistant_wire import InstructionTask
from shared_context.behavior_capabilities import (
    CapabilityCapture,
    CapabilityRef,
    available_grants,
    capture,
    check_captured,
    load_runtime_capabilities,
)
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.errors import Problem
from shared_context.tool_catalog import build_catalog, parse_entry_file
from assistant_api_helpers import assistant_api
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key
from direct_submit_helpers import submit_direct
from capability_capture_helpers import fake_services, enabled, preinstalled, GRANTS, CATALOG, WEBIQ, choose, refs, FILES

SOURCE = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"


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


def test_selection_refuses_unknown_changed_or_non_composable_entries(assistant_api, monkeypatch):
    h, control = enabled(assistant_api, monkeypatch)
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
def test_vision_capabilities_need_an_image_model_at_admission(assistant_api, monkeypatch, image_input):
    h, control = enabled(assistant_api, monkeypatch)
    name = control.models.default
    control.models.models[name] = control.models.models[name].model_copy(update={"supports_image_input": image_input})
    choice, _ = choose(h, "choose-desktop", ("fa-desktop-v1",))
    assert choice.status_code == 200, choice.text
    before = h.store._guard()
    if image_input is True:
        accepted, _ = submit_direct(h, "desktop-turn", "Open my notes.")
        task = control.records[accepted["binding_id"]].profile.instruction
        assert task.capabilities.vision and task.capabilities.grants[0].tools[0] == "desktop_open_file"
    else:
        with pytest.raises(Problem) as refused:
            submit_direct(h, "desktop-turn", "Open my notes.")
        assert refused.value.status == 422 and refused.value.body["error"]["code"] == "CAPABILITY_MODEL_UNSUPPORTED"
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
    assert dict(load_runtime_capabilities()[0]) == {}
    directory = tmp_path / "catalog"
    directory.mkdir()
    (directory / "websearch.json").write_text(json.dumps({"mcpServers": {"WebSearch-MCP": {
        "type": "http", "url": "https://websearch.example.test/v3/mcp",
        "headers": {"x-apikey": "${secret:websearch-api-key}"}, "tools": ["web", "browse"]}}}), encoding="utf-8")
    monkeypatch.setenv("FA_TOOL_CATALOG_DIR", str(directory))
    monkeypatch.setenv("FA_CAPABILITY_BUILTINS", "fa-desktop-v1")
    grants = load_runtime_capabilities()[0]
    assert sorted(grants) == ["WebSearch-MCP", "fa-desktop-v1"] and grants["WebSearch-MCP"] == GRANTS["WebSearch-MCP"]
    with pytest.raises(TypeError):
        grants["x"] = grants["WebSearch-MCP"]
    monkeypatch.setenv("FA_CAPABILITY_BUILTINS", "fa-unknown-v1")
    with pytest.raises(Problem) as error:
        load_runtime_capabilities()[0]
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
