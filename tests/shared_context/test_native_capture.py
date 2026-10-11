"""Pure capture and the original source readers; no native execution."""

from dataclasses import replace

import pytest

from shared_context.behavior_agent_template import AgentTemplate, TemplateSettings, export_template
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_attachments import TextAttachment
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_native_capture import project_capture, resolve_capture
from shared_context.behavior_native_turn_wire import NativeConversation
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.errors import Problem
from test_conversation_transfer_startup import synthetic_standing_v1
from test_m3_auth import jwk_client, signing_key
from test_native_turn_wire import capture
from test_instruction_flow import instruction_models


@pytest.fixture
def capture_case(synthetic_standing_v1):
    return synthetic_standing_v1


@pytest.mark.parametrize("retained", [0, 1, 2])
def test_pure_capture_keeps_full_template_and_only_current_grants(retained):
    original = capture()
    available = available_grants(None, ("fa-memory-candidate-v1", "fa-shell-v1"))
    refs = tuple(available[key].ref for key in sorted(available))
    template = AgentTemplate.model_validate({
        **original.template.model_dump(), "settings": TemplateSettings(capabilities=refs),
    })
    before = template.model_dump_json()
    result = project_capture(
        template, "original-selection", original.model, head_refs=refs[:retained],
        head_selection_id="current-selection", available=available, role_prompt=original.role_prompt,
        count_tokens=lambda _: pytest.fail("unselected token budgeting must not run"),
    )
    assert result.template == template and result.template.model_dump_json() == before
    assert tuple(grant.ref for grant in result.capabilities.grants) == refs[:retained]
    assert result.profile_selection_id == "original-selection"
    assert result.template.settings.model_provider is None


def test_selected_native_start_profile_gets_per_turn_server_build_without_rewriting_template(monkeypatch):
    original = capture()
    settings = TemplateSettings.model_validate({
        **original.template.settings.model_dump(), "sdk_start_profile": "fa-sdk-start-gate-v1",
    })
    template = AgentTemplate.model_validate({**original.template.model_dump(), "settings": settings})
    before = template.model_dump_json()
    monkeypatch.setattr("shared_context.behavior_native_capture.code_sha256", lambda: "a" * 64)
    first = project_capture(template, "selection", original.model, available={},
                            role_prompt=original.role_prompt, count_tokens=lambda _: 0)
    monkeypatch.setattr("shared_context.behavior_native_capture.code_sha256", lambda: "b" * 64)
    second = project_capture(template, "selection", original.model, available={},
                             role_prompt=original.role_prompt, count_tokens=lambda _: 0)
    assert first.sdk_start_code_sha256 == "a" * 64
    assert second.sdk_start_code_sha256 == "b" * 64
    assert first.sha256 != second.sha256
    assert first.template == second.template == template and template.model_dump_json() == before


def test_resolver_does_not_derive_or_write_a_job(capture_case, monkeypatch):
    c = capture_case
    command = InstructionSubmit(request_id="native-capture", content="Only the supplied material.")
    before, bindings = c.control.store._guard(), dict(c.h.runtime.bindings)
    monkeypatch.setattr("shared_context.behavior_work_policy.derive_work",
                        lambda *_a, **_k: pytest.fail("native capture must not create an execution identity"))
    monkeypatch.setattr(c.control.store, "_submit",
                        lambda *_a, **_k: pytest.fail("native capture must not write Table"))
    result = resolve_capture(c.control, c.authority, c.human, command)
    assert result.model == c.control.models.select(None)
    assert result.template.role.profile == "fa-instruction-v2"
    assert c.control.store._guard() == before and dict(c.h.runtime.bindings) == bindings
    assert c.control.records == {}


def test_new_rid_in_native_cid_keeps_original_default(capture_case, monkeypatch):
    c = capture_case
    first = resolve_capture(c.control, c.authority, c.human,
                            InstructionSubmit(request_id="native-first", content="First"))
    original = NativeConversation(agent_id=c.native.targets[0].agent_id,
                                  hosted_session_id=c.native.targets[0].session_id, conversation_id="native-cid",
                                  last_turn=1, last_request_id="native-first",
                                  capture=first, capture_digest=first.sha256)
    monkeypatch.setenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", "fa-instruction-v1")
    next_capture = resolve_capture(
        c.control, c.authority, c.human,
        InstructionSubmit(request_id="native-next", conversation_id=original.conversation_id, content="Next"),
        conversation=original,
    )
    assert next_capture.template == first.template
    assert next_capture.template.role.profile == "fa-instruction-v2"


def _select_attachments(c):
    current = export_template(c.control, c.authority, c.human)
    settings = TemplateSettings.model_validate({
        **current.settings.model_dump(), "text_attachment_profile": "fa-source-text-attachments-v1",
    })
    template = AgentTemplate.model_validate({**current.model_dump(), "settings": settings})
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id="native-source-profile", expected_selection_id=None, template=template,
    ))


def test_native_attachment_uses_real_original_graph_without_session(capture_case, monkeypatch):
    c = capture_case
    _select_attachments(c)
    attachment = TextAttachment(name="source.json", source=c.control.policy.profile.source)
    before = c.control.store._guard()
    monkeypatch.setattr(c.control.store, "_submit",
                        lambda *_a, **_k: pytest.fail("source capture must not register a work"))
    result = resolve_capture(c.control, c.authority, c.human, InstructionSubmit(
        request_id="native-source", content="Read the supplied source.", attachments=(attachment,),
    ))
    assert result.attached_text.entries[0].attachment == attachment
    assert result.attached_text.entries[0].text
    assert c.control.store._guard() == before and c.control.records == {}


@pytest.mark.parametrize("change", [
    {"source_id": "outside-original-source-scope"}, {"sha256": "f" * 64},
])
def test_native_attachment_reference_never_grants_access(capture_case, change):
    c = capture_case
    _select_attachments(c)
    reference = c.control.policy.profile.source.model_copy(update=change)
    before = c.control.store._guard()
    with pytest.raises(Problem) as denied:
        resolve_capture(c.control, c.authority, c.human, InstructionSubmit(
            request_id="native-invalid-source", content="Read source.",
            attachments=(TextAttachment(name="source.json", source=reference),),
        ))
    assert denied.value.status in (403, 409)
    assert c.control.store._guard() == before and c.control.records == {}


def test_owner_and_revocation_apply_before_native_capture(capture_case):
    c = capture_case
    command = InstructionSubmit(request_id="native-refused", content="Do not run.")
    with pytest.raises(Problem) as denied:
        resolve_capture(c.control, c.authority, replace(c.human, team="another-team"), command)
    assert denied.value.status == 403
    c.control.revoke(c.human, "native-revoke")
    with pytest.raises(Problem) as denied:
        resolve_capture(c.control, c.authority, c.human, command)
    assert denied.value.body["error"]["code"] == "WORK_POLICY_REVOKED"
