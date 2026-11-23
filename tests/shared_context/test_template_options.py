"""Template options list only currently effective Team skills and the available capability references."""

import hashlib

import pytest

from shared_context.behavior_agent_template import check_selectable_skills, template_options
from shared_context.errors import Problem
from assistant_api_helpers import assistant_api
from test_conversation_profile_selection import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skills_capability import FILES, GRANTS, SKILLS_REF


def test_options_offer_effective_skills_and_capabilities_that_selection_accepts(
        assistant_api, signing_key, jwk_client, monkeypatch):
    from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
    from test_skill_purpose_api import add_purpose_skill
    approved = add_purpose_skill(assistant_api, signing_key, jwk_client, name="approved-guide")
    add_purpose_skill(assistant_api, signing_key, jwk_client, name="waiting-guide", approve=False)
    h, control, human = configured(assistant_api)
    h.runtime.capability_grants = GRANTS
    monkeypatch.setenv("FA_CAPABILITY_SELECTION_MAX", "30")
    options = template_options(control, h.assistant, human)
    assert options.max_capabilities == 30
    assert [item.name for item in options.skills] == ["approved-guide"] and not options.more_skills
    assert options.skills[0].version_id == approved["version_id"]
    assert options.skills[0].metadata_sha256 == approved["metadata_sha256"]
    # Display only, beside the references: the owner picks by title, selection still sends the ID.
    assert options.skill_titles == {"approved-guide": "Not the approved purpose"}
    assert [(item.entry_id, item.entry_sha256, item.tools) for item in options.capabilities] == [
        (SKILLS_REF.entry_id, SKILLS_REF.entry_sha256, ("read_skill",))]
    assert HISTORY_SKILL_DRAFT_PROFILE in options.composable_profiles
    assert options.authorization is False and options.activated is False
    value = template(HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES,
                     capabilities=(SKILLS_REF,)).model_copy(update={"skills": options.skills})
    selected, _ = select(h, request_id="choose-from-options", value=value)
    assert selected.status_code == 200, selected.text



@pytest.mark.parametrize("limit,sent", [(None, None), ("12", None), ("13", 13), ("1", 1)])
def test_options_send_the_limit_only_when_it_differs_from_the_default(assistant_api, monkeypatch, limit, sent):
    """At 12 the reply has no ``max_capabilities``, as before the setting: a bridge older than it forbids the field
    (502 TEMPLATE_RESPONSE_INVALID), and the console reads a missing field as 12."""
    h, control, human = configured(assistant_api)
    if limit is not None:
        monkeypatch.setenv("FA_CAPABILITY_SELECTION_MAX", limit)
    reply = template_options(control, h.assistant, human).model_dump(mode="json")
    assert reply.get("max_capabilities") == sent and ("max_capabilities" in reply) is (sent is not None)

def test_options_need_current_standing_read_scope(assistant_api):
    h, control, human = configured(assistant_api)
    control.revoke(human, "revoke-template-options")
    with pytest.raises(Problem) as error:
        template_options(control, h.assistant, human)
    assert error.value.body["error"]["code"] == "WORK_POLICY_REVOKED"


def approved_native_skills(h, control, names, purpose_bytes=179):
    """Approved native skill drafts named ``names``, each with a ``purpose_bytes`` purpose."""
    from shared_context.m3_auth import HumanApprover
    from shared_context.m3_models import ApprovalInput, NativeArtifactSource, SkillPurpose, VersionInput
    service = h.assistant.artifacts
    actor = h.assistant.actors[control.policy.template.agent_oid]
    approver = HumanApprover(service.approval.approver_oid, actor.team)
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


@pytest.mark.parametrize("purpose_bytes", [179, 512])
def test_native_drafts_never_block_team_options_or_hide_old_skills(
    assistant_api, signing_key, jwk_client, purpose_bytes,
):
    from test_skill_purpose_api import add_purpose_skill

    add_purpose_skill(assistant_api, signing_key, jwk_client, name="z-original-guide")
    h, control, human = configured(assistant_api)
    h.runtime.capability_grants = GRANTS
    names = tuple(f"{number:064x}" for number in range(1, 5))
    approved_native_skills(h, control, names, purpose_bytes)
    options = template_options(control, h.assistant, human)
    assert tuple(item.name for item in options.skills) == (*names, "z-original-guide")
    assert all(options.skill_titles[name] == "x" * 63 + "…" for name in names)
    assert all(item.metadata_sha256 is not None for item in options.skills)
    assert not options.authorization and not options.activated and not options.more_skills
    if purpose_bytes == 179:
        from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
        value = template(HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES,
                         capabilities=(SKILLS_REF,)).model_copy(update={"skills": options.skills[:4]})
        selected, _ = select(h, request_id="choose-four-native-skills", value=value)
        assert selected.status_code == 200, selected.text


def test_options_list_every_effective_skill_and_selection_keeps_to_the_skill_limit(assistant_api, monkeypatch):
    """Beyond 24 effective skills the options still list each one. The API's FA_SKILL_SELECTION_MAX (default 4) is
    checked only when a selection is written; up to MAX_SKILLS (32) is accepted once it is raised, 33 never."""
    from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
    h, control, human = configured(assistant_api)
    h.runtime.capability_grants = GRANTS
    names = tuple(f"{number:064x}" for number in range(1, 34))
    approved_native_skills(h, control, names)
    options = template_options(control, h.assistant, human)
    assert tuple(item.name for item in options.skills) == names and not options.more_skills
    assert "max_skills" not in options.model_dump(mode="json")
    value = template(HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES, capabilities=(SKILLS_REF,))
    five = value.model_copy(update={"skills": options.skills[:5]})
    with pytest.raises(Problem) as refused:
        check_selectable_skills(five)
    assert refused.value.status == 422 and refused.value.body["error"] | {"message": None} == {
        "code": "SKILL_SELECTION_LIMIT", "message": None, "limit": 4}
    assert select(h, request_id="choose-five-skills", value=five)[0].status_code == 422
    monkeypatch.setenv("FA_SKILL_SELECTION_MAX", "32")
    assert template_options(control, h.assistant, human).max_skills == 32
    with pytest.raises(ValueError):  # the wire bound, the same in every process
        select(h, request_id="choose-33-skills", value=value.model_copy(update={"skills": options.skills}))
    selected, _ = select(h, request_id="choose-32-skills", value=value.model_copy(update={"skills": options.skills[:32]}))
    assert selected.status_code == 200, selected.text


def test_a_bad_skill_limit_stops_api_startup(monkeypatch):
    from shared_context.behavior_capabilities import load_runtime_capabilities
    monkeypatch.delenv("FA_TOOL_CATALOG_DIR", raising=False)
    monkeypatch.delenv("FA_CAPABILITY_BUILTINS", raising=False)
    for bad in ("0", "33", "04", "4 ", ""):
        monkeypatch.setenv("FA_SKILL_SELECTION_MAX", bad)
        with pytest.raises(Problem) as stopped:
            load_runtime_capabilities()
        assert stopped.value.status == 503 and stopped.value.body["error"]["code"] == "SKILL_CONFIGURATION", bad


def test_a_template_older_images_cannot_read_waits_for_the_raised_skill_limit(monkeypatch):
    """Every image before the 32-skill bound reads templates up to 8192 bytes, so at the default skill limit a new
    selection keeps to that; stored ones are read against MAX_TEMPLATE_BYTES (16 KiB) alone."""
    from shared_context.behavior_capabilities import CapabilityRef
    big = template(capabilities=tuple(CapabilityRef(entry_id=f"entry-{number:03d}", entry_sha256=f"{number:064x}")
                                      for number in range(80)))
    assert 8192 < len(big.model_dump_json().encode()) <= 16 * 1024
    with pytest.raises(Problem) as refused:
        check_selectable_skills(big)
    assert refused.value.status == 422 and refused.value.body["error"]["code"] == "PROFILE_SELECTION_UNSUPPORTED"
    monkeypatch.setenv("FA_SKILL_SELECTION_MAX", "5")
    check_selectable_skills(big)


def test_four_skill_references_keep_their_bytes():
    """Raising the wire bound changes no byte of a selection, catalog or assembly within the old one (base 7ae81052)."""
    from shared_context.behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings, TemplateSkill
    from shared_context.m3_skills import PurposeSkillCatalog, PurposeSkillEntry
    from shared_context.native_assembly import AssemblySkill, NativeAssembly
    h = lambda text, n=64: hashlib.sha256(text.encode()).hexdigest()[:n]
    skills = sorted((TemplateSkill(name=h(f"n{i}"), version_id=h(f"v{i}", 32), sha256=h(f"s{i}"),
                                   metadata_sha256=h(f"m{i}")) for i in range(4)), key=lambda item: item.name)
    value = AgentTemplate(role=TemplateRole(profile="fa-instruction-history-skill-draft-v1", prefix_sha256=h("p"),
                                            capability_catalog_version="cat1"),
                          settings=TemplateSettings(), skills=tuple(skills))
    catalog = PurposeSkillCatalog(schema_version="fa-approved-skill-catalog-v2", entries=tuple(sorted((PurposeSkillEntry(
        name=h(f"n{i}"), title=f"T{i}", version_id=h(f"v{i}", 32), sha256=h(f"s{i}"), byte_length=10, team="t1",
        native_submit_sha256=h(f"x{i}"), description="d", metadata_sha256=h(f"m{i}")) for i in range(4)),
        key=lambda item: item.name)))
    assembly = NativeAssembly(state="recorded", skills=tuple(AssemblySkill(selected=item) for item in skills))
    assert [hashlib.sha256(item.model_dump_json().encode()).hexdigest() for item in (value, catalog, assembly)] == [
        "e77a835304c76a48cb7c90d14b45e2ec8d397a28cc9d4ede91577d6e6e5eb139",
        "87958c512c0f0722a855cb061bd45662f5db40f26f3c199e1ea4fa6e522e4a8c",
        "9474b634d4f4957a79f6dd99f8685dcb6039faf94f68047dafd348cd02c891bd"]
