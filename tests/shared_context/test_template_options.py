"""Template options list only currently effective Team skills and the available capability references."""

import hashlib

import pytest

from shared_context.behavior_agent_template import template_options
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skills_capability import FILES, GRANTS, SKILLS_REF


def test_options_offer_effective_skills_and_capabilities_that_selection_accepts(assistant, signing_key, jwk_client):
    from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
    from test_skill_purpose_api import add_purpose_skill
    approved = add_purpose_skill(assistant, signing_key, jwk_client, name="approved-guide")
    add_purpose_skill(assistant, signing_key, jwk_client, name="waiting-guide", approve=False)
    h, control, human = configured(assistant)
    h.runtime.capability_grants = GRANTS
    options = template_options(control, h.assistant, human)
    assert [item.name for item in options.skills] == ["approved-guide"] and not options.more_skills
    assert options.skills[0].version_id == approved["version_id"]
    assert options.skills[0].metadata_sha256 == approved["metadata_sha256"]
    assert [(item.entry_id, item.entry_sha256, item.tools) for item in options.capabilities] == [
        (SKILLS_REF.entry_id, SKILLS_REF.entry_sha256, ("read_skill",))]
    assert HISTORY_SKILL_DRAFT_PROFILE in options.composable_profiles
    assert options.authorization is False and options.activated is False
    value = template(HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES,
                     capabilities=(SKILLS_REF,)).model_copy(update={"skills": options.skills})
    selected, _ = select(h, request_id="choose-from-options", value=value)
    assert selected.status_code == 200, selected.text


def test_options_need_current_standing_read_scope(assistant):
    h, control, human = configured(assistant)
    control.revoke(human, "revoke-template-options")
    with pytest.raises(Problem) as error:
        template_options(control, h.assistant, human)
    assert error.value.body["error"]["code"] == "WORK_POLICY_REVOKED"


@pytest.mark.parametrize("purpose_bytes", [179, 512])
def test_native_drafts_never_block_team_options_or_hide_old_skills(
    assistant, signing_key, jwk_client, purpose_bytes,
):
    from shared_context.m3_auth import HumanApprover
    from shared_context.m3_models import ApprovalInput, NativeArtifactSource, SkillPurpose, VersionInput
    from test_skill_purpose_api import add_purpose_skill

    add_purpose_skill(assistant, signing_key, jwk_client, name="z-original-guide")
    h, control, human = configured(assistant)
    h.runtime.capability_grants = GRANTS
    service = h.assistant.artifacts
    actor = h.assistant.actors[control.policy.template.agent_oid]
    approver = HumanApprover(service.approval.approver_oid, actor.team)
    names = tuple(f"{number:064x}" for number in range(1, 5))
    body = b"Follow the original human procedure."
    for number, name in enumerate(names, 1):
        source = NativeArtifactSource(
            tenant_id=service.context.auth.tenant_id, team_id=actor.team,
            agent_id="11111111-1111-1111-1111-111111111111", agent_oid=actor.oid,
            conversation_id=f"native-options-cid-{number}", request_id=f"native-options-rid-{number}",
            submit_sha256=f"{number:064x}")
        created = service.create_native_skill(
            actor, actor.team, name, title="Unapproved skill draft from user procedure", native_source=source)
        metadata = VersionInput(filename="skill-draft.md", media_type="text/markdown",
            sha256=hashlib.sha256(body).hexdigest(),
            skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description="x" * purpose_bytes))
        saved = service.upload(actor, actor.team, name, "a" * 32, created.etag, metadata, body, native_source=source)
        version = saved.payload["versions"][0]
        service.decide(approver, actor.team, name, saved.etag, ApprovalInput(
            decision_id=f"{number:032x}", version_id=version["version_id"], sha256=version["sha256"],
            metadata_sha256=version["metadata_sha256"], decision="approve"))
    options = template_options(control, h.assistant, human)
    assert tuple(item.name for item in options.skills) == (*names, "z-original-guide")
    assert all(item.metadata_sha256 is not None for item in options.skills)
    assert not options.authorization and not options.activated and not options.more_skills
    if purpose_bytes == 179:
        from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
        value = template(HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES,
                         capabilities=(SKILLS_REF,)).model_copy(update={"skills": options.skills[:4]})
        selected, _ = select(h, request_id="choose-four-native-skills", value=value)
        assert selected.status_code == 200, selected.text
