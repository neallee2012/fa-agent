"""Approved origin settings are captured for new work, not read from mutable defaults."""

import json
from types import SimpleNamespace

import pytest

from shared_context import m2_copilot
from shared_context.behavior_agent_template import TemplateSettings
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_wire import InstructionTask
from test_behavior_assistant import assistant
from test_conversation_profile_selection import PATH, configured, select, template
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key


ORIGIN_PROFILE = "fa-instruction-context-origin-v1"


def sdk_context_reads(monkeypatch):
    calls = sdk_io(monkeypatch)
    original_factory = m2_copilot.CopilotClient.side_effect
    results = []

    def factory(**connection):
        sdk = original_factory(**connection)
        original_open = sdk.create_session.side_effect

        async def opened(*args, **options):
            session = await original_open(*args, **options)
            original_send = session.send.side_effect

            async def send(prompt, **send_options):
                call_id = "context-read-" + str(len(results))
                permission = options["on_permission_request"](SimpleNamespace(
                    kind="custom-tool", tool_name="read_context", tool_call_id=call_id,
                    args={}, managed_approval_required=False,
                ), {"session_id": session.session_id})
                assert type(permission).__name__ == "PermissionDecisionApproveOnce"
                tool = next(value for value in options["tools"] if value.name == "read_context")
                result = await tool.handler(SimpleNamespace(
                    session_id=session.session_id, tool_name="read_context", tool_call_id=call_id, arguments={},
                ))
                assert result.result_type == "success", result.error
                results.append(json.loads(result.text_result_for_llm))
                return await original_send(prompt, **send_options)

            session.send.side_effect = send
            session.send_and_wait.side_effect = send
            return session

        sdk.create_session.side_effect = opened
        sdk.resume_session.side_effect = opened
        return sdk

    m2_copilot.CopilotClient.side_effect = factory
    return calls, results


def test_human_selected_origin_setting_is_captured_only_by_new_work(assistant):
    h, control, _ = configured(assistant)
    first, _ = submit(h, "origin-before-selection", "Keep original settings.")
    old_record = control.records[first["binding_id"]].model_dump_json()
    old_policy = control.policy.model_dump_json()
    assert "turn_origin_profile" not in old_record
    value = template().model_dump(mode="json")
    value["settings"]["turn_origin_profile"] = ORIGIN_PROFILE
    selected = h.client.post(PATH + "/select", json={
        "request_id": "select-origin-context", "expected_selection_id": None, "template": value,
    }, headers=h.headers())
    assert selected.status_code == 200, selected.text
    selection_id = selected.json()["result"]["selection_id"]

    second, _ = submit(h, "origin-after-selection", "Use selected settings.")
    captured = control.records[second["binding_id"]]
    assert captured.profile_selection_id == selection_id
    assert captured.profile.turn_origin_profile == ORIGIN_PROFILE
    assert captured.profile.instruction.turn_origin_profile == ORIGIN_PROFILE
    assert control.records[first["binding_id"]].model_dump_json() == old_record
    assert control.policy.model_dump_json() == old_policy
    exported = h.client.get(PATH, headers=h.headers("read-only"))
    assert exported.status_code == 200
    assert exported.json()["settings"]["turn_origin_profile"] == ORIGIN_PROFILE
    assert exported.json()["authorization"] is False
    assert not h.endpoint.requests

    retained = captured.model_dump_json()
    control.records.clear()
    control.refresh()
    assert control.records[second["binding_id"]].model_dump_json() == retained
    assert control.records[first["binding_id"]].model_dump_json() == old_record
    assert json.loads(retained)["profile_selection_id"] == selection_id


def test_captured_origin_settings_route_real_sdk_callbacks_without_switching_old_conversations(assistant, monkeypatch):
    monkeypatch.delenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", raising=False)
    h, control, _ = configured(assistant)
    calls, results = sdk_context_reads(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter({"content": text} for text in (
        "Original settings.", "Original conversation retained.", "Origin enabled.",
        "Origin conversation retained.", "New default settings.",
    ))
    request = h.client.request
    routes = []
    commands = {}

    def observe(method, url, **options):
        path = str(url)
        if "/assistant/sessions/" in path and path.endswith(("/context", "/context-origin")):
            routes.append(path.rsplit("/", 1)[-1])
        return request(method, url, **options)

    monkeypatch.setattr(h.client, "request", observe)

    def run(identifier, conversation=None):
        admitted, command = submit(h, identifier, "Read the allowed context.", conversation)
        commands[identifier] = command
        assert h.runtime.controller.step(admitted["binding_id"]).state == "succeeded"
        return admitted

    original = run("origin-routing-old")
    old_record = control.records[original["binding_id"]].model_dump_json()
    enabled, _ = select(h, request_id="origin-routing-enable",
                        value=template(turn_origin_profile=ORIGIN_PROFILE))
    assert enabled.status_code == 200, enabled.text
    enabled_id = enabled.json()["result"]["selection_id"]
    monkeypatch.setenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", "fa-instruction-v1")
    control.records.clear()
    control.refresh()
    old_followup = run("origin-routing-old-followup", original["conversation_id"])
    chosen = run("origin-routing-new")
    chosen_record = control.records[chosen["binding_id"]].model_dump_json()

    disabled, _ = select(h, request_id="origin-routing-disable", previous=enabled_id, value=template())
    assert disabled.status_code == 200, disabled.text
    control.records.clear()
    control.refresh()
    chosen_followup = run("origin-routing-new-followup", chosen["conversation_id"])
    default = run("origin-routing-default")

    assert routes == ["context", "context", "context-origin", "context-origin", "context"]
    assert ["turn_origin" in value for value in results] == [False, False, True, True, False]
    assert results[2]["turn_origin"]["request_id"] == "origin-routing-new"
    assert results[3]["turn_origin"]["turn_number"] == 2
    assert results[3]["turn_origin"]["previous_turn_number"] == 1
    assert results[2]["turn_origin"]["authorization"] is results[3]["turn_origin"]["authorization"] is False
    assert [value["resume"] for value in calls] == [False, True, False, True, False]
    assert len(h.endpoint.requests) == len(calls) == len(results) == 5
    assert control.records[original["binding_id"]].model_dump_json() == old_record
    assert control.records[chosen["binding_id"]].model_dump_json() == chosen_record
    for item, expected in (
        (old_followup, None), (chosen_followup, ORIGIN_PROFILE), (default, None),
    ):
        assert control.records[item["binding_id"]].profile.instruction.turn_origin_profile == expected
    assert control.records[old_followup["binding_id"]].profile.prompt_profile == "fa-instruction-v2"
    before = h.store._guard()
    replay = h.client.post("/work-policy/v1/submit-reconcile",
                          content=commands["origin-routing-new"].model_dump_json(), headers=h.headers("read-only"))
    assert replay.status_code == 200, replay.text
    assert replay.json()["binding_id"] == chosen["binding_id"]
    assert h.store._guard() == before


@pytest.mark.parametrize("identity", ["agent", "read-only"])
def test_origin_setting_does_not_relax_human_approval_requirements(assistant, identity):
    h, control, _ = configured(assistant)
    before = h.store._guard()
    response, _ = select(h, request_id="forbidden-origin-choice",
                         value=template(turn_origin_profile=ORIGIN_PROFILE), identity=identity)
    assert response.status_code == 403
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


@pytest.mark.parametrize("value", [True, "unknown-origin-version"])
def test_unknown_origin_setting_is_rejected_without_creating_a_selection(assistant, value):
    h, control, _ = configured(assistant)
    candidate = template().model_dump(mode="json")
    candidate["settings"]["turn_origin_profile"] = value
    before = h.store._guard()
    response = h.client.post(PATH + "/select", json={
        "request_id": "invalid-origin-choice", "expected_selection_id": None, "template": candidate,
    }, headers=h.headers())
    assert response.status_code == 422
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_per_message_origin_override_cannot_replace_a_selected_profile(assistant):
    h, control, _ = configured(assistant)
    before = h.store._guard()
    response = h.client.post("/work-policy/v1/submit", json={
        "request_id": "origin-unapproved-override", "content": "Use the context.",
        "authorize_own_result": True, "turn_origin_profile": ORIGIN_PROFILE,
    }, headers=h.headers())
    assert response.status_code == 422
    assert h.store._guard() == before and not control.records


def test_template_inspection_shows_origin_setting_differences_without_activating_them(assistant):
    h, control, _ = configured(assistant)
    selected, _ = select(h, request_id="baseline-profile")
    assert selected.status_code == 200
    original_policy = control.policy.model_dump_json()
    candidate = template(turn_origin_profile=ORIGIN_PROFILE)
    before = h.store._guard()
    inspection = h.client.post(PATH + "/inspect", content=candidate.model_dump_json(),
                               headers=h.headers("read-only"))
    assert inspection.status_code == 200
    assert inspection.json()["settings_match"] is False
    assert inspection.json()["references_match"] is False
    assert inspection.json()["authorization"] is inspection.json()["activated"] is False
    assert h.store._guard() == before and control.policy.model_dump_json() == original_policy
    assert "turn_origin_profile" not in h.client.get(PATH, headers=h.headers("read-only")).json()["settings"]


def test_none_origin_setting_is_omitted_from_legacy_settings_and_work(assistant):
    assert "turn_origin_profile" not in TemplateSettings().model_dump()
    assert TemplateSettings(turn_origin_profile=None).model_dump_json() == TemplateSettings().model_dump_json()
    h, control, _ = configured(assistant)
    admitted, _ = submit(h, "origin-legacy-wire", "Keep legacy settings.")
    record = control.records[admitted["binding_id"]]
    assert "turn_origin_profile" not in record.model_dump_json()
    assert "turn_origin_profile" not in h.runtime.job_input(record.binding).decode("utf-8")


def test_origin_setting_requires_an_explicit_instruction_prompt_profile(assistant):
    _, control, _ = configured(assistant)
    with pytest.raises(ValueError):
        AssistantProfile.model_validate_json(control.policy.profile.model_copy(update={
            "turn_origin_profile": ORIGIN_PROFILE, "prompt_profile": None,
        }).model_dump_json())
    with pytest.raises(ValueError):
        InstructionTask(content="Original text.", conversation_id="conversation", turn_number=1,
                        turn_origin_profile=ORIGIN_PROFILE)


def test_published_selector_parser_and_typed_receipts_preserve_captured_origin_settings(assistant):
    from shared_context.behavior_profile_selection import (
        ConversationProfileReceipt, SelectedConversationProfile, SelectConversationProfile,
    )
    from web_test_console.agent_template import intent_key, parse_selection, parse_template

    h, control, _ = configured(assistant)
    original_policy = control.policy.model_dump_json()
    candidate = template(turn_origin_profile=ORIGIN_PROFILE)
    parsed = parse_template(candidate.model_dump_json().encode("utf-8"), code="TEMPLATE_INVALID", status=400)
    assert parsed.model_dump_json() == candidate.model_dump_json()
    command = SelectConversationProfile(request_id="origin-ui-interop", expected_selection_id=None, template=parsed)
    captured = parse_selection(command.model_dump_json().encode("utf-8"))
    assert captured.model_dump_json() == command.model_dump_json()
    plain = command.model_copy(update={"template": template()})
    assert intent_key(captured) != intent_key(plain)
    selected = h.client.post(PATH + "/select", content=captured.model_dump_json(), headers=h.headers())
    assert selected.status_code == 200, selected.text
    receipt = ConversationProfileReceipt.model_validate_json(selected.content)
    current = h.client.get(PATH + "/selection", headers=h.headers("read-only"))
    assert current.status_code == 200
    observed = SelectedConversationProfile.model_validate_json(current.content)
    assert observed.selection_id == receipt.result.selection_id
    assert observed.template.settings.turn_origin_profile == ORIGIN_PROFILE
    assert observed.permissions_changed is observed.existing_conversations_changed is False
    admitted, _ = submit(h, "origin-ui-new-work", "Use the approved origin settings.")
    record = control.records[admitted["binding_id"]]
    assert record.profile_selection_id == observed.selection_id
    assert record.profile.instruction.turn_origin_profile == ORIGIN_PROFILE
    exported = h.client.get(PATH, headers=h.headers("read-only"))
    assert exported.status_code == 200
    assert parse_template(exported.content, code="TEMPLATE_RESPONSE_INVALID", status=502).model_dump_json() == parsed.model_dump_json()
    assert control.policy.model_dump_json() == original_policy and not h.endpoint.requests
