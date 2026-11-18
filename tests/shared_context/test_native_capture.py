"""Pure capture and the original source readers; no native execution."""

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path

import pytest

from shared_context.behavior_agent_role_prompt import AGENT_ROLE_PROFILE
from shared_context.behavior_agent_role_prompt_versions import (
    AgentRolePromptHead, SaveAgentRolePrompt, head_key, save_role_prompt,
)
from shared_context.behavior_agent_template import (
    AgentTemplate, TemplateRole, TemplateSettings, export_template,
)
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_attachments import TextAttachment
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_wire import ExactSourceRef
from shared_context.behavior_text_files import UserTextFile
from shared_context.behavior_native_capture import project_capture, resolve_capture
from shared_context.behavior_skill_prompt import SKILL_PROFILE, assemble_prompt
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.source_materials import (
    AuthenticatedSourceContext, SourceInput, SourceReference, SourceWithdrawalInput,
)
from shared_context.behavior_native_turn_wire import NativeConversation
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.errors import Problem
from test_conversation_transfer_startup import synthetic_standing_v1
from test_m3_auth import jwk_client, signing_key
from test_native_turn_wire import capture
from instruction_flow_helpers import instruction_models


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


def test_selected_origin_setting_is_captured_only_by_new_work(capture_case):
    """Native twin of test_turn_origin_profile's selection case: the human's origin choice reaches only captures taken
    after it; an earlier capture keeps its bytes and never carries the field."""
    c = capture_case
    earlier = resolve_capture(c.control, c.authority, c.human,
                              InstructionSubmit(request_id="origin-before-selection", content="Keep original settings."))
    original, policy = earlier.model_dump_json(), c.control.policy.model_dump_json()
    assert "turn_origin_profile" not in original
    current = export_template(c.control, c.authority, c.human)
    template = AgentTemplate.model_validate({**current.model_dump(), "settings": TemplateSettings.model_validate({
        **current.settings.model_dump(), "turn_origin_profile": "fa-instruction-context-origin-v1"})})
    selected = select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id="select-origin-context", expected_selection_id=None, template=template))
    later = resolve_capture(c.control, c.authority, c.human,
                            InstructionSubmit(request_id="origin-after-selection", content="Use selected settings."))
    assert later.profile_selection_id == selected.payload["result"]["selection_id"]
    assert later.template.settings.turn_origin_profile == "fa-instruction-context-origin-v1"
    assert earlier.model_dump_json() == original and c.control.policy.model_dump_json() == policy


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


# Refusals the retired Table submit path also made. Each case is a native capture over the same input.

def _select(c, *, role=None, request_id="native-guard-profile", **settings):
    current = export_template(c.control, c.authority, c.human)
    changed = {"settings": TemplateSettings.model_validate({**current.settings.model_dump(), **settings})}
    if role is not None:
        record = assemble_prompt(role).record
        changed["role"] = TemplateRole(profile=role, prefix_sha256=record.prefix_sha256,
                                       capability_catalog_version=record.capability_catalog_version)
    template = AgentTemplate.model_validate({**current.model_dump(), **changed})
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id=request_id, expected_selection_id=None, template=template))


def _refused(c, command=None, **kwargs):
    """The Problem a native capture raises."""
    with pytest.raises(Problem) as refused:
        resolve_capture(c.control, c.authority, c.human,
                        command or InstructionSubmit(request_id="native-refused", content="Do not run."), **kwargs)
    return refused.value


def _role_agent(c):
    _select(c, role=AGENT_ROLE_PROFILE, user_text_file_profile="fa-user-text-files-v1")


def _save_role(c, request_id, text):
    return save_role_prompt(c.control, c.human, SaveAgentRolePrompt(request_id=request_id, text=text))


def test_a_new_role_version_applies_to_the_next_capture_and_a_saved_capture_keeps_its_own(capture_case):
    c = capture_case
    _role_agent(c)
    assert _save_role(c, "role-v1", "You are the release checklist Agent.").payload["result"]["version"] == 1
    first = resolve_capture(c.control, c.authority, c.human, InstructionSubmit(request_id="role-first", content="Start."))
    assert _save_role(c, "role-v2", "You are the release notes Agent.").payload["result"]["version"] == 2
    original = NativeConversation(agent_id=c.native.targets[0].agent_id, hosted_session_id=c.native.targets[0].session_id,
                                  conversation_id="native-cid", last_turn=1, last_request_id="role-first",
                                  capture=first, capture_digest=first.sha256)
    follow = resolve_capture(c.control, c.authority, c.human, InstructionSubmit(
        request_id="role-second", conversation_id="native-cid", content="Continue."), conversation=original)
    other = resolve_capture(c.control, c.authority, c.human, InstructionSubmit(request_id="role-other", content="New."))
    assert (first.role_prompt.version, follow.role_prompt.version, other.role_prompt.version) == (1, 2, 2)
    assert follow.role_prompt.text == other.role_prompt.text == "You are the release notes Agent."
    assert first.role_prompt.text == "You are the release checklist Agent."


@pytest.mark.parametrize("code", ["AGENT_ROLE_PROMPT_MISSING", "AGENT_ROLE_PROMPT_INVALID"])
def test_a_role_agent_without_a_usable_saved_role_is_refused(capture_case, code):
    c = capture_case
    _role_agent(c)
    if code == "AGENT_ROLE_PROMPT_INVALID":
        # A head that names a version nobody saved.
        key = head_key(c.control.policy.scope_ids)
        c.control.store.transact(
            ControlRequest(request_id="role-head-without-version", request_digest="b" * 64),
            lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="create", data=AgentRolePromptHead(
                version=1, sha256="a" * 64).model_dump(mode="json")),), result={}))
    refused = _refused(c)
    assert (refused.status, refused.body["error"]["code"]) == (409, code)
    if code == "AGENT_ROLE_PROMPT_MISSING":
        _save_role(c, "first-role", "Role.")
        assert resolve_capture(c.control, c.authority, c.human, InstructionSubmit(
            request_id="with-role", content="Hello.")).role_prompt.text == "Role."


FACTS = "FA_INSTRUCTION_MODEL_FACTS_FILE"


def _facts(c, content):
    """Server-declared model facts are read once per control, so a changed file needs a fresh read."""
    Path(os.environ[FACTS]).write_text(json.dumps(content), encoding="utf-8")
    c.control._models = None


@pytest.mark.parametrize("field", ["context_window_tokens", "max_prompt_tokens", "max_output_tokens"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1000"])
def test_invalid_model_facts_are_refused(capture_case, field, value):
    c = capture_case
    _facts(c, {"gpt-4.1-mini": {field: value}})
    refused = _refused(c, InstructionSubmit(request_id="invalid-capacity-facts", content="Do not dispatch.",
                                            model="gpt-4.1-mini"))
    assert (refused.status, refused.body["error"]["code"]) == (503, "INSTRUCTION_MODEL_CONFIGURATION")


@pytest.mark.parametrize("values", [
    {"context_window_tokens": 1000, "max_prompt_tokens": 1001},
    {"context_window_tokens": 1000, "max_output_tokens": 1001},
    {"context_window_tokens": 2**31},
])
def test_contradictory_or_unrepresentable_capacity_is_refused(capture_case, values):
    c = capture_case
    _select(c, model_capacity_profile="fa-model-capacity-v1")
    _facts(c, {"gpt-4.1-mini": values})
    refused = _refused(c, InstructionSubmit(request_id="contradictory-capacity", content="Do not dispatch.",
                                            model="gpt-4.1-mini"))
    assert (refused.status, refused.body["error"]["code"]) == (503, "MODEL_CAPACITY_INVALID")


DEFAULT_PROMPT = "FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE"


def _installed(c, monkeypatch, policy):
    """The control reads its policy back from the store; a different installed policy changes both views."""
    monkeypatch.setattr(c.control, "policy", policy)
    monkeypatch.setattr(c.control, "installed", lambda rows=None: policy)


@pytest.mark.parametrize("profile", ["fa-instruction-v1", "fa-instruction-v2", "fa-instruction-v3", "fa-instruction-v4",
                                     SKILL_PROFILE])
@pytest.mark.parametrize("default", ["fa-instruction-v1", "fa-instruction-v2", "unknown"])
def test_default_does_not_replace_an_explicit_server_policy_profile(capture_case, monkeypatch, profile, default):
    c = capture_case
    explicit = AssistantProfile.model_validate(
        c.control.policy.profile.model_copy(update={"prompt_profile": profile}).model_dump())
    policy = c.control.policy.model_copy(update={"profile": explicit})
    _installed(c, monkeypatch, policy)
    monkeypatch.setenv(DEFAULT_PROMPT, default)
    original = policy.model_dump_json()
    result = resolve_capture(c.control, c.authority, c.human,
                             InstructionSubmit(request_id="explicit-policy", content="Retain the configured policy."))
    assert result.template.role.profile == profile
    assert c.control.policy.model_dump_json() == original


@pytest.mark.parametrize("value", ["", "unknown", " fa-instruction-v1", "fa-instruction-v3", "fa-instruction-v4",
                                  "fa-instruction-skills-v1", "PRIVATE_PROMPT_CONFIG_CANARY"])
def test_invalid_default_refuses_new_work(capture_case, monkeypatch, value):
    c = capture_case
    monkeypatch.setenv(DEFAULT_PROMPT, value)
    refused = _refused(c)
    assert (refused.status, refused.body["error"]["code"]) == (503, "INSTRUCTION_DEFAULT_PROMPT_CONFIGURATION")
    assert "PRIVATE_PROMPT_CONFIG_CANARY" not in json.dumps(refused.body)


def test_literal_files_and_attachments_need_their_explicit_profile(capture_case):
    c = capture_case
    attachment = TextAttachment(name="source.json", source=c.control.policy.profile.source)
    files = UserTextFile(name="data.csv", text="  USER_FILE_CANARY\r\n名稱,數值\n範例,42\n")
    plain = InstructionSubmit(request_id="native-plain", content="Original plain text.")
    assert "files" not in plain.model_dump(mode="json") and "attachments" not in plain.model_dump(mode="json")
    refused = _refused(c, plain.model_copy(update={"files": (files,)}))
    assert (refused.status, refused.body["error"]["code"]) == (403, "USER_TEXT_FILES_DISABLED")
    refused = _refused(c, plain.model_copy(update={"attachments": (attachment,)}))
    assert (refused.status, refused.body["error"]["code"]) == (403, "ATTACHMENTS_DISABLED")
    resolve_capture(c.control, c.authority, c.human, plain)


def test_no_attachments_does_not_consult_an_unused_private_source_scope(capture_case, monkeypatch):
    c = capture_case
    _installed(c, monkeypatch, c.control.policy.model_copy(update={"legacy_template_policy": (
        c.control.policy.legacy_template_policy.model_copy(update={"source_visibility": "private"}))}))
    _select_attachments(c)  # the attachment profile is selected; only this command carries no attachment
    monkeypatch.setattr(c.control.runtime.dep.sources, "read_graph",
                        lambda *_a, **_k: pytest.fail("no attachment sources to read"))
    result = resolve_capture(c.control, c.authority, c.human, InstructionSubmit(
        request_id="native-empty-private", content="Answer without using any source."))
    assert result.attached_text is None


def test_an_actual_private_attachment_is_denied_before_any_source_read(capture_case, monkeypatch):
    c = capture_case
    _installed(c, monkeypatch, c.control.policy.model_copy(update={"legacy_template_policy": (
        c.control.policy.legacy_template_policy.model_copy(update={"source_visibility": "private"}))}))
    _select_attachments(c)
    attachment = TextAttachment(name="source.json", source=c.control.policy.profile.source)
    monkeypatch.setattr(c.control.runtime.dep.sources, "read_graph",
                        lambda *_a, **_k: pytest.fail("a private attachment is not supported"))
    refused = _refused(c, InstructionSubmit(request_id="native-private", content="Read source.",
                                            attachments=(attachment,)))
    assert (refused.status, refused.body["error"]["code"]) == (403, "ATTACHMENT_SOURCE_DENIED")


def _source_context(c):
    binding, policy = c.control.policy.template, c.control.policy.legacy_template_policy
    return AuthenticatedSourceContext(binding.tenant_id, binding.team_id, c.human.source_actor_id,
                                      binding.ingress_id, policy.allowed_purposes)


def _add_source(c, version, *, source_id=None, expires_at=None, necessary=()):
    """A source version committed through the runtime's own source reader; by default of the original source."""
    sources, original = c.control.runtime.dep.sources, c.control.policy.profile.source
    plan = sources.plan(_source_context(c), SourceInput(
        source_id=source_id or original.source_id, version=version, visibility=c.control.policy.legacy_template_policy.source_visibility,
        purpose=c.control.policy.legacy_template_policy.purpose, schema_id="fa-artifact-metrics-v1",
        media_type="application/json", necessary=necessary, expires_at=expires_at), b'{"schema_version":"fa-artifact-metrics-v1","item_count":1,"completed_count":0}')
    sources.commit(_source_context(c), plan)
    exact = {name: getattr(plan.reference, name) for name in ("source_id", "version", "sha256", "byte_length")}
    return TextAttachment(name="reference.json", source=ExactSourceRef(**exact))


def test_an_expired_attachment_is_refused(capture_case):
    c = capture_case
    _select_attachments(c)
    sources = c.control.runtime.dep.sources
    now = sources._clock()
    attachment = _add_source(c, 2, expires_at=now + timedelta(seconds=1))
    resolve_capture(c.control, c.authority, c.human, InstructionSubmit(
        request_id="live-attachment", content="Read source.", attachments=(attachment,)))  # accepted until it expires
    sources._clock = lambda: now + timedelta(seconds=2)
    refused = _refused(c, InstructionSubmit(request_id="expired-attachment", content="Read source.",
                                            attachments=(attachment,)))
    assert (refused.status, refused.body["error"]["code"]) == (409, "SOURCE_EXPIRED")


def test_a_withdrawn_necessary_source_denies_the_attachment_without_any_write(capture_case):
    c = capture_case
    _select_attachments(c)
    dependency = _add_source(c, 1, source_id="dependency")
    reference = SourceReference(scope=_source_context(c).scope("team"), **dependency.source.model_dump())
    attachment = _add_source(c, 2, necessary=(reference,))
    resolve_capture(c.control, c.authority, c.human, InstructionSubmit(
        request_id="live-dependency", content="Use the derived source.", attachments=(attachment,)))  # accepted until withdrawn
    c.control.runtime.dep.sources.withdraw_source(c.human, _source_context(c), SourceWithdrawalInput(
        withdrawal_id="withdraw-dependency", reference=reference, expected_access_revision=1,
        reason="The original human withdrew this dependency."))
    refused = _refused(c, InstructionSubmit(request_id="withdrawn-dependency", content="Use the derived source.",
                                            attachments=(attachment,)))
    assert (refused.status, refused.body["error"]["code"]) == (403, "SOURCE_WITHDRAWN")


@pytest.mark.parametrize("image_input", [None, True])
def test_vision_capabilities_need_an_image_model_at_capture(image_input):
    original = capture()
    available = available_grants(None, ("fa-desktop-v1",))
    refs = (available["fa-desktop-v1"].ref,)
    template = AgentTemplate.model_validate({
        **original.template.model_dump(), "settings": TemplateSettings(capabilities=refs)})
    model = original.model.model_copy(update={"supports_image_input": image_input})

    def project():
        return project_capture(template, "selection", model, head_refs=refs, head_selection_id="selection",
                               available=available, role_prompt=original.role_prompt, count_tokens=lambda _: 0)

    if image_input is True:
        assert project().capabilities.vision
        return
    with pytest.raises(Problem) as refused:
        project()
    assert refused.value.status == 422 and refused.value.body["error"]["code"] == "CAPABILITY_MODEL_UNSUPPORTED"


@pytest.mark.parametrize("declared", ["no-capacity-profile", "no-input-limit"])
def test_a_skill_catalog_budget_needs_a_declared_input_capacity(declared):
    original = capture()
    settings = TemplateSettings(skill_catalog_budget_profile="fa-skill-catalog-budget-v1", **(
        {} if declared == "no-capacity-profile" else {"model_capacity_profile": "fa-model-capacity-v1"}))
    record = assemble_prompt(SKILL_PROFILE).record
    template = AgentTemplate.model_validate({**original.template.model_dump(), "settings": settings, "role": TemplateRole(
        profile=SKILL_PROFILE, prefix_sha256=record.prefix_sha256,
        capability_catalog_version=record.capability_catalog_version)})
    # Only an output limit is declared, so no input capacity exists.
    model = original.model.model_copy(update={"max_output_tokens": 2048})
    with pytest.raises(Problem) as refused:
        project_capture(template, "selection", model, available={}, role_prompt=original.role_prompt,
                        count_tokens=lambda _: 0)
    assert refused.value.status == 503 and refused.value.body["error"]["code"] == "SKILL_CATALOG_CAPACITY_UNKNOWN"


@pytest.mark.parametrize("claim", [{"kind": "agent"}, {"actor_id": "agent-b"}], ids=["kind-only", "actor-only"])
def test_an_agent_principal_cannot_stand_in_for_the_human_owner(capture_case, claim):
    c = capture_case
    spoof = replace(c.human, **claim)  # each half of the human check must refuse on its own
    with pytest.raises(Problem) as denied:
        resolve_capture(c.control, c.authority, spoof, InstructionSubmit(
            request_id="header-human-spoof", content="A header is not delegated human identity."))
    assert (denied.value.status, denied.value.body["error"]["code"]) == (403, "WORK_POLICY_HUMAN_REQUIRED")


# A human profile selection reaches only new native conversations (the retired submit path tested this per Job).

def _choose(c, value, request_id="choose-v1", previous=None):
    from shared_context.behavior_profile_selection import current_selection
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id=request_id, expected_selection_id=previous, template=value))
    return current_selection(c.control.store, c.control.policy).selection_id


def _native_conversation(c, captured, rid):
    return NativeConversation(agent_id=c.native.targets[0].agent_id, hosted_session_id=c.native.targets[0].session_id,
                              conversation_id="native-cid", last_turn=1, last_request_id=rid,
                              capture=captured, capture_digest=captured.sha256)


def _follow(control, c, conversation, rid):
    return resolve_capture(control, c.authority, c.human, InstructionSubmit(
        request_id=rid, conversation_id=conversation.conversation_id, content="Continue the original."),
        conversation=conversation)


def _rebuilt(c):
    """A new control over the same store: no memory of the earlier selection carries over."""
    from shared_context.behavior_work_policy import WorkPolicyControl
    return WorkPolicyControl(c.control.runtime, c.control.policy, c.control.cost_reader)


@pytest.mark.parametrize("profile", ["fa-instruction-v1", "fa-instruction-v2"])
def test_a_human_selection_reaches_only_new_native_conversations_and_survives_a_rebuilt_control(capture_case, profile):
    from conversation_profile_selection_helpers import template
    c = capture_case
    first = resolve_capture(c.control, c.authority, c.human,
                            InstructionSubmit(request_id="before-selection", content="Start with original settings."))
    original, policy = first.model_dump_json(), c.control.policy.model_dump_json()
    selection_id = _choose(c, template(profile))
    assert c.control.policy.model_dump_json() == policy
    control = _rebuilt(c)
    follow = _follow(control, c, _native_conversation(c, first, "before-selection"), "old-followup")
    assert follow.profile_selection_id is None and follow.template == first.template
    newest = resolve_capture(control, c.authority, c.human,
                             InstructionSubmit(request_id="after-selection", content="Start a new conversation."))
    assert newest.profile_selection_id == selection_id
    assert newest.template == template(profile) and newest.template.role.profile == profile
    assert first.model_dump_json() == original


def test_a_responses_selection_captures_only_new_native_conversations(capture_case):
    from conversation_profile_selection_helpers import template
    c = capture_case
    first = resolve_capture(c.control, c.authority, c.human,
                            InstructionSubmit(request_id="completions-first", content="Keep this original conversation."))
    original = first.model_dump_json()
    assert first.template.settings.model_provider is None
    selection_id = _choose(c, template(model_provider="openai-responses"))
    control = _rebuilt(c)
    follow = _follow(control, c, _native_conversation(c, first, "completions-first"), "completions-follow")
    assert follow.template.settings.model_provider is None and follow.template == first.template
    newest = resolve_capture(control, c.authority, c.human, InstructionSubmit(
        request_id="responses-first", content="Start a new conversation with the selected provider."))
    assert newest.template.settings.model_provider == "openai-responses"
    assert newest.profile_selection_id == selection_id
    assert first.model_dump_json() == original


def test_a_later_human_choice_keeps_the_original_selection_and_its_native_conversation(capture_case):
    from conversation_profile_selection_helpers import template
    from shared_context.behavior_profile_selection import read_selection
    c = capture_case
    selection_id = _choose(c, template())
    first = resolve_capture(c.control, c.authority, c.human,
                            InstructionSubmit(request_id="selected-first", content="Use the selected profile."))
    assert first.profile_selection_id == selection_id
    original = read_selection(c.control.store, c.control.policy, selection_id)
    changed = _choose(c, template(text_attachment_profile="fa-source-text-attachments-v1"),
                      request_id="choose-attachment-profile", previous=selection_id)
    follow = _follow(c.control, c, _native_conversation(c, first, "selected-first"), "selected-followup")
    assert follow.profile_selection_id == selection_id and follow.template.settings.text_attachment_profile is None
    newest = resolve_capture(c.control, c.authority, c.human,
                             InstructionSubmit(request_id="selected-new-conversation", content="Use the current profile."))
    assert newest.profile_selection_id == changed != selection_id
    assert newest.template.settings.text_attachment_profile == "fa-source-text-attachments-v1"
    assert read_selection(c.control.store, c.control.policy, selection_id) == original


def _approved_skill(c, version_id, body):
    """Upload and approve one version of the Team skill "receipt-guide" through the M3 artifact service."""
    import hashlib
    from conftest import InMemoryRepository
    from shared_context.behavior_agent_template import TemplateSkill
    from shared_context.m3_auth import HumanApprover
    from shared_context.m3_models import ApprovalInput, CreateArtifact, SkillPurpose, VersionInput
    service = c.authority.artifacts
    actor = c.authority.actors[c.control.policy.template.agent_oid]
    if not isinstance(service.repository, InMemoryRepository):
        service.repository = InMemoryRepository()
        current = service.create(actor, actor.team, "receipt-guide", CreateArtifact(
            kind="skill", title="Receipt guide", source_work="legacy-source"))
    else:
        current = service.get(actor, actor.team, "receipt-guide")
    uploaded = service.upload(actor, actor.team, "receipt-guide", version_id, current.etag, VersionInput(
        filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(body).hexdigest(),
        skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description="Check a receipt.")), body)
    version = uploaded.payload["versions"][-1]
    service.decide(HumanApprover(service.approval.approver_oid, actor.team), actor.team, "receipt-guide", uploaded.etag,
                   ApprovalInput(decision_id=version_id, version_id=version_id, sha256=version["sha256"],
                                 metadata_sha256=version["metadata_sha256"], decision="approve"))
    return TemplateSkill(name="receipt-guide", version_id=version_id, sha256=version["sha256"],
                         metadata_sha256=version["metadata_sha256"])


def _skill_template(skill):
    from conversation_profile_selection_helpers import template
    return template("fa-instruction-skill-purpose-v1").model_copy(update={"skills": (skill,)})


def test_an_approved_skill_selection_reaches_the_new_native_capture_without_moving_the_policy(capture_case):
    c = capture_case
    skill = _approved_skill(c, "1" * 32, b"Approved destination guide: check the receipt before reporting.\n")
    policy = c.control.policy.model_dump_json()
    selection_id = _choose(c, _skill_template(skill))
    captured = resolve_capture(c.control, c.authority, c.human, InstructionSubmit(
        request_id="selected-skill-conversation", content="Use the approved guide if needed."))
    assert captured.template.skills == (skill,) and captured.profile_selection_id == selection_id
    assert captured.template.role.profile == "fa-instruction-skill-purpose-v1"
    assert c.control.policy.model_dump_json() == policy


def test_a_deactivated_skill_refuses_its_native_conversation_until_a_new_approved_version_is_selected(capture_case):
    """A native conversation keeps its captured skill version: deactivation fences it, and only a newly approved
    version that the human selects reaches a new conversation (the retired lane swapped versions in place)."""
    from shared_context.m3_auth import HumanApprover
    from shared_context.m3_models import SkillDeactivationInput
    c = capture_case
    original = _approved_skill(c, "1" * 32, b"Original guide.\n")
    selection_id = _choose(c, _skill_template(original))
    first = resolve_capture(c.control, c.authority, c.human,
                            InstructionSubmit(request_id="skill-first", content="Read the approved guide."))
    conversation = _native_conversation(c, first, "skill-first")
    service = c.authority.artifacts
    actor = c.authority.actors[c.control.policy.template.agent_oid]
    service.deactivate_skill(HumanApprover(service.approval.approver_oid, actor.team), actor.team, "receipt-guide",
        service.get(actor, actor.team, "receipt-guide").etag, SkillDeactivationInput(
            schema_version="fa-skill-deactivation-v1", decision_id="d" * 32, version_id=original.version_id,
            sha256=original.sha256, metadata_sha256=original.metadata_sha256))
    for refused in (_refused(c, InstructionSubmit(request_id="skill-follow", conversation_id="native-cid",
                                                  content="Continue."), conversation=conversation),
                    _refused(c, InstructionSubmit(request_id="skill-new", content="Start again."))):
        assert (refused.status, refused.body["error"]["code"]) == (409, "PROFILE_SELECTION_SOURCES_CHANGED")
    current = _approved_skill(c, "2" * 32, b"Newly approved guide.\n")
    refused = _refused(c, InstructionSubmit(request_id="skill-follow-after-approval", conversation_id="native-cid",
                                            content="Continue."), conversation=conversation)
    assert (refused.status, refused.body["error"]["code"]) == (409, "PROFILE_SELECTION_SOURCES_CHANGED")
    _choose(c, _skill_template(current), request_id="choose-current-skill", previous=selection_id)
    newest = resolve_capture(c.control, c.authority, c.human,
                             InstructionSubmit(request_id="skill-current", content="Read the current guide."))
    assert newest.template.skills == (current,) and first.template.skills == (original,)
