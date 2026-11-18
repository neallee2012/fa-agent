"""Approved origin settings are captured for new work, not read from mutable defaults."""

import json
from types import SimpleNamespace

import pytest

from shared_context import m2_copilot
from shared_context.behavior_agent_template import TemplateSettings
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_wire import InstructionTask
from assistant_api_helpers import assistant_api
from test_conversation_profile_selection import PATH, configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path, sdk_io
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


@pytest.mark.parametrize("identity", ["agent", "read-only"])
def test_origin_setting_does_not_relax_human_approval_requirements(assistant_api, identity):
    h, control, _ = configured(assistant_api)
    before = h.store._guard()
    response, _ = select(h, request_id="forbidden-origin-choice",
                         value=template(turn_origin_profile=ORIGIN_PROFILE), identity=identity)
    assert response.status_code == 403
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


@pytest.mark.parametrize("value", [True, "unknown-origin-version"])
def test_unknown_origin_setting_is_rejected_without_creating_a_selection(assistant_api, value):
    h, control, _ = configured(assistant_api)
    candidate = template().model_dump(mode="json")
    candidate["settings"]["turn_origin_profile"] = value
    before = h.store._guard()
    response = h.client.post(PATH + "/select", json={
        "request_id": "invalid-origin-choice", "expected_selection_id": None, "template": candidate,
    }, headers=h.headers())
    assert response.status_code == 422
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_template_inspection_shows_origin_setting_differences_without_activating_them(assistant_api):
    h, control, _ = configured(assistant_api)
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


def test_none_origin_setting_is_omitted_from_legacy_settings_and_work():
    assert "turn_origin_profile" not in TemplateSettings().model_dump()
    assert TemplateSettings(turn_origin_profile=None).model_dump_json() == TemplateSettings().model_dump_json()


def test_origin_setting_requires_an_explicit_instruction_prompt_profile(assistant_api):
    _, control, _ = configured(assistant_api)
    with pytest.raises(ValueError):
        AssistantProfile.model_validate_json(control.policy.profile.model_copy(update={
            "turn_origin_profile": ORIGIN_PROFILE, "prompt_profile": None,
        }).model_dump_json())
    with pytest.raises(ValueError):
        InstructionTask(content="Original text.", conversation_id="conversation", turn_number=1,
                        turn_origin_profile=ORIGIN_PROFILE)


def test_published_selector_parser_and_typed_receipts_preserve_captured_origin_settings(assistant_api):
    from shared_context.behavior_profile_selection import (
        ConversationProfileReceipt, SelectedConversationProfile, SelectConversationProfile,
    )
    from web_test_console.agent_template import intent_key, parse_selection, parse_template

    h, control, _ = configured(assistant_api)
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
    exported = h.client.get(PATH, headers=h.headers("read-only"))
    assert exported.status_code == 200
    assert parse_template(exported.content, code="TEMPLATE_RESPONSE_INVALID", status=502).model_dump_json() == parsed.model_dump_json()
    assert control.policy.model_dump_json() == original_policy and not h.endpoint.requests
