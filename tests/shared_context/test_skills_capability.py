"""fa-skills-v1: approved-skill reads compose with the history profiles as a captured capability."""

import json

import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_agent_template import TemplateSkill
from shared_context.behavior_capabilities import SKILLS_ENTRY_ID, CapabilityCapture, CapabilityRef, available_grants
from shared_context.behavior_capability_calls import gateway_options
from shared_context.behavior_capability_prompt import assemble_capabilities, capability_record, session_needs
from shared_context.behavior_history_skill_draft_prompt import HISTORY_SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_instruction_prompt import saved_turn, submit
from test_m3_auth import jwk_client, signing_key

FILES = "fa-user-text-files-v1"
GRANTS = available_grants(None, (SKILLS_ENTRY_ID,))
SKILLS = GRANTS[SKILLS_ENTRY_ID]
SKILLS_REF = CapabilityRef(entry_id=SKILLS_ENTRY_ID, entry_sha256=SKILLS.entry_sha256)


def skill_template(version, *, capabilities=(SKILLS_REF,), skills=True):
    value = template(HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES, capabilities=capabilities)
    if not skills:
        return value
    return value.model_copy(update={"skills": (TemplateSkill(
        name="receipt-guide", version_id=version["version_id"], sha256=version["sha256"],
        metadata_sha256=version["metadata_sha256"]),)})


def test_skills_are_a_feature_capability_served_by_the_existing_read_skill_path():
    capture = CapabilityCapture(head_selection_id=None, grants=(SKILLS,))
    needs = session_needs(capture)
    assert needs.custom_tools == ("read_skill",) and needs.feature_tools == {"read_skill"} and not needs.vision
    combined = assemble_capabilities(HISTORY_SKILL_DRAFT_PROFILE, capture)
    assert combined.record.declared_capabilities == (*HISTORY_SKILL_DRAFT_CAPABILITIES, "read_skill")
    assert "read_skill: Read one exact currently approved skill" in combined.system_message
    session = type("S", (), {})()
    session.assignment = type("A", (), {})()
    session.assignment.task = type("T", (), {})()
    session.assignment.task.instruction = type("I", (), {"capabilities": capture})()
    assert gateway_options(session) == ({}, False)


def test_selected_skills_capability_reads_skills_with_history_tools_and_can_be_narrowed(
    assistant, monkeypatch, signing_key, jwk_client,
):
    from test_skill_purpose_api import PURPOSE, add_purpose_skill
    version = add_purpose_skill(assistant, signing_key, jwk_client, name="receipt-guide")
    h, control, _ = configured(assistant)
    h.runtime.capability_grants = GRANTS
    denied, _ = select(h, request_id="skills-without-capability", value=skill_template(version, capabilities=()))
    assert denied.status_code == 422 and denied.json()["error"]["code"] == "PROFILE_SELECTION_UNSUPPORTED"
    chosen, _ = select(h, request_id="choose-skills", value=skill_template(version))
    assert chosen.status_code == 200, chosen.text
    first_id = chosen.json()["result"]["selection_id"]

    reads, original = [], hosted.run_copilot

    async def read_then_reply(**options):
        names = [tool.name for tool in options["tools"]]
        if "read_skill" in names:
            tool = next(item for item in options["tools"] if item.name == "read_skill")
            # Names are artifact IDs; the approved purpose is what lets the model match a skill to the request.
            assert "receipt-guide" in tool.description and PURPOSE in tool.description
            assert "by its title and description" in tool.description
            assert "say it is not available" in tool.description
            reads.append(json.loads((await tool.handler({"arguments": {"name": "receipt-guide"}})).text))
        return await original(**options)
    monkeypatch.setattr(hosted, "run_copilot", read_then_reply)
    sdk_io(monkeypatch, expected_tools=(*HISTORY_SKILL_DRAFT_CAPABILITIES, "read_skill"))
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": f"Reply {index}."} for index in range(4)])
    accepted, _ = submit(h, "skills-first", "Use the approved receipt guide.")
    assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
    task = control.records[accepted["binding_id"]].profile.instruction
    assert [grant.entry_id for grant in task.capabilities.grants] == [SKILLS_ENTRY_ID]
    assert reads and reads[0]["skill"]["name"] == "receipt-guide" and reads[0]["skill"]["description"] == PURPOSE
    archive = saved_turn(h, h.runtime.bindings[accepted["binding_id"]].job_id)
    assert archive.skill_catalog is not None and [entry.name for entry in archive.skill_catalog.entries] == ["receipt-guide"]
    assert archive.skill_catalog.schema_version == "fa-approved-skill-catalog-v2"
    assert archive.capability_assembly == capability_record(task.prompt_profile, task.capabilities)

    narrowed, _ = select(h, request_id="choose-no-skills", previous=first_id,
                         value=skill_template(version, capabilities=(), skills=False))
    assert narrowed.status_code == 200, narrowed.text
    sdk_io(monkeypatch, expected_tools=HISTORY_SKILL_DRAFT_CAPABILITIES)
    followed, _ = submit(h, "skills-follow", "Continue without skills.", accepted["conversation_id"])
    assert h.runtime.controller.step(followed["binding_id"]).state == "succeeded"
    follow = control.records[followed["binding_id"]].profile.instruction
    assert follow.capabilities.grants == () and follow.skill_catalog_budget_profile is None
    assert saved_turn(h, h.runtime.bindings[followed["binding_id"]].job_id).skill_catalog is None
    assert len(reads) == 1
