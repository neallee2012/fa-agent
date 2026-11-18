"""fa-skills-v1: approved-skill reads compose with the history profiles as a captured capability."""


import pytest

from shared_context.behavior_capabilities import SKILLS_ENTRY_ID, CapabilityCapture, CapabilityRef, available_grants
from shared_context.behavior_capability_calls import gateway_options
from shared_context.behavior_capability_prompt import assemble_capabilities, session_needs
from shared_context.behavior_history_skill_draft_prompt import HISTORY_SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
from instruction_flow_helpers import instruction_models, runtime_path

FILES = "fa-user-text-files-v1"
GRANTS = available_grants(None, (SKILLS_ENTRY_ID,))
SKILLS = GRANTS[SKILLS_ENTRY_ID]
SKILLS_REF = CapabilityRef(entry_id=SKILLS_ENTRY_ID, entry_sha256=SKILLS.entry_sha256)


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
