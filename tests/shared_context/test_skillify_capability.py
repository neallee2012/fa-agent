"""fa-skillify-v1: guidance to turn a procedure from this conversation into an unapproved skill draft."""


import pytest

from conftest import A
from shared_context.behavior_agent_role_prompt import AGENT_ROLE_PROFILE
from shared_context.behavior_capabilities import (
    BUILTIN_CAPABILITIES, CAPABILITY_BASE_PROFILES, DELEGATE_READER_ENTRY_ID, SKILLIFY_ENTRY_ID, SKILL_RESOURCES_ENTRY_ID,
    CapabilityCapture, CapabilityRef, available_grants, base_supports, check_selectable,
)
from shared_context.behavior_capability_calls import gateway_options
from shared_context.behavior_capability_prompt import assemble_capabilities, session_needs
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_history_skill_draft_prompt import HISTORY_SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_profile_selection import READ_ONLY_PROFILES
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.errors import Problem
from assistant_api_helpers import assistant_api
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key

FILES = "fa-user-text-files-v1"
V2_PROFILE = "fa-instruction-v2"
V3_PROFILE = "fa-instruction-v3"
V4_PROFILE = "fa-instruction-v4"
ROLE_V2_PROFILE = "fa-instruction-agent-role-v2"
GRANTS = available_grants(None, ("fa-skills-v1", SKILLIFY_ENTRY_ID))
SKILLIFY = GRANTS[SKILLIFY_ENTRY_ID]
SKILLIFY_REF = CapabilityRef(entry_id=SKILLIFY_ENTRY_ID, entry_sha256=SKILLIFY.entry_sha256)


@pytest.mark.parametrize("profile", [V2_PROFILE, V3_PROFILE, V4_PROFILE])
def test_human_selects_skillify_without_changing_permissions(assistant_api, profile):
    h, control, _ = configured(assistant_api)
    h.runtime.capability_grants = GRANTS
    original_policy = control.policy.model_dump_json()
    chosen, _ = select(h, request_id="v2-skillify-choice", value=template(
        profile, user_text_file_profile=FILES, capabilities=(SKILLIFY_REF,)))
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["result"]["permissions_changed"] is False
    assert chosen.json()["result"]["existing_conversations_changed"] is False
    assert control.policy.model_dump_json() == original_policy
    assert not control.records


@pytest.mark.parametrize("identity", ["agent", "read-only"])
@pytest.mark.parametrize("profile", [V2_PROFILE, V3_PROFILE])
def test_skillify_selection_still_requires_the_human_approver(assistant_api, identity, profile):
    h, control, _ = configured(assistant_api)
    h.runtime.capability_grants = GRANTS
    before = h.store._guard()
    response, _ = select(h, identity=identity, value=template(profile, capabilities=(SKILLIFY_REF,)))
    assert response.status_code == 403
    assert h.store._guard() == before and not control.records


@pytest.mark.parametrize("mismatch", ["missing-entry", "entry-version", "role-version"])
@pytest.mark.parametrize("profile", [V2_PROFILE, V3_PROFILE])
def test_skillify_selection_rejects_unavailable_or_changed_versions(assistant_api, mismatch, profile):
    h, control, _ = configured(assistant_api)
    h.runtime.capability_grants = {} if mismatch == "missing-entry" else GRANTS
    ref = SKILLIFY_REF.model_copy(update={"entry_sha256": "f" * 64}) if mismatch == "entry-version" else SKILLIFY_REF
    value = template(profile, capabilities=(ref,))
    if mismatch == "role-version":
        value = value.model_copy(update={"role": value.role.model_copy(update={"prefix_sha256": "f" * 64})})
    before = h.store._guard()
    response, _ = select(h, value=value)
    expected = (422, "PROFILE_SELECTION_UNSUPPORTED") if mismatch == "role-version" else (409, "CAPABILITY_SOURCES_CHANGED")
    assert (response.status_code, response.json()["error"]["code"]) == expected
    assert h.store._guard() == before and not control.records


def test_skillify_adds_guidance_to_the_base_draft_tool_without_a_second_tool():
    assert SKILLIFY.entry_sha256 == "ef54c887b4e95fa3ab7b84537bceca701d6aa54f25dac2313c833c10a69ca8b5"
    capture = CapabilityCapture(head_selection_id=None, grants=(SKILLIFY,))
    base = assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE)
    combined = assemble_capabilities(HISTORY_SKILL_DRAFT_PROFILE, capture)
    assert combined.system_message.startswith(base.system_message)
    assert "This replaces the save_skill_draft limit above" in combined.system_message
    assert "Do not turn mandatory safety" in combined.system_message
    assert combined.record.declared_capabilities == HISTORY_SKILL_DRAFT_CAPABILITIES
    needs = session_needs(capture)
    assert needs.custom_tools == ("save_skill_draft",) and needs.feature_tools == {"save_skill_draft"}
    session = type("S", (), {})()
    session.assignment = type("A", (), {})()
    session.assignment.task = type("T", (), {})()
    session.assignment.task.instruction = type("I", (), {"capabilities": capture})()
    assert gateway_options(session) == ({}, False)
    with pytest.raises(ValueError):
        assemble_capabilities(HISTORY_PROFILE, capture)


@pytest.mark.parametrize("profile", sorted(READ_ONLY_PROFILES - {
    V2_PROFILE, V3_PROFILE, V4_PROFILE, "fa-instruction-agent-role-v2", "fa-instruction-agent-role-v3",
    "fa-instruction-agent-role-v4"}))
def test_skillify_does_not_enable_a_writer_on_other_read_only_profiles(profile):
    with pytest.raises(Problem) as error:
        check_selectable((SKILLIFY_REF,), profile, GRANTS)
    assert error.value.status == 422 and error.value.body["error"]["code"] == "CAPABILITY_BASE_UNSUPPORTED"
    assert check_selectable((SKILLIFY_REF,), HISTORY_SKILL_DRAFT_PROFILE, GRANTS) == (SKILLIFY_REF,)


@pytest.mark.parametrize("profile", sorted(CAPABILITY_BASE_PROFILES))
def test_base_supports_is_exactly_the_selection_rule(profile):
    """With required companions selected, the onboarding profile gate matches selection."""
    grants = available_grants(None, tuple(BUILTIN_CAPABILITIES))
    for entry_id, grant in grants.items():
        ref = CapabilityRef(entry_id=entry_id, entry_sha256=grant.entry_sha256)
        refs = (ref,)
        companions = {DELEGATE_READER_ENTRY_ID: "fa-delegate-v1", SKILL_RESOURCES_ENTRY_ID: "fa-skills-v1"}
        if entry_id in companions:
            with pytest.raises(Problem) as missing_delegate:
                check_selectable(refs, profile, grants)
            assert missing_delegate.value.body["error"]["code"] == "CAPABILITY_SELECTION_INVALID"
            refs = tuple(sorted((ref, grants[companions[entry_id]].ref), key=lambda item: item.entry_id))
        try:
            check_selectable(refs, profile, grants)
            accepted = True
        except Problem as error:
            assert error.body["error"]["code"] == "CAPABILITY_BASE_UNSUPPORTED"
            accepted = False
        assert base_supports(entry_id, profile) is accepted, (profile, entry_id)
    assert base_supports(SKILLIFY_ENTRY_ID, AGENT_ROLE_PROFILE) is False
