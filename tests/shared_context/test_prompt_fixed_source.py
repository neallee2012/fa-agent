"""Every fixed-prompt assembly record refuses a hash, size or omissions that differ from its source, with its own text."""

import hashlib

import pytest
from pydantic import ValidationError

from shared_context.behavior_agent_role_prompt import (
    assemble_role_prompt, assemble_role_v2_prompt, assemble_role_v3_prompt, assemble_role_v4_prompt,
)
from shared_context.behavior_assistant_reply_prompt import assemble_assistant_reply_prompt
from shared_context.behavior_history_prompt import assemble_history_prompt
from shared_context.behavior_history_skill_draft_prompt import assemble_history_skill_draft_prompt
from shared_context.behavior_reviewed_note_prompt import assemble_reviewed_note_prompt
from shared_context.behavior_skill_copy_prompt import assemble_sop_copy_prompt
from shared_context.behavior_skill_draft_prompt import assemble_skill_draft_prompt
from shared_context.behavior_skill_prompt import SKILL_PROFILE, assemble_prompt
from shared_context.behavior_skill_purpose_prompt import assemble_skill_purpose_prompt
from shared_context.behavior_skill_resources_prompt import assemble_skill_resources_prompt
from shared_context.behavior_user_file_prompt import assemble_user_file_prompt
from shared_context.behavior_user_turn_prompt import assemble_user_turn_prompt
from shared_context.behavior_work_memory_prompt import assemble_work_memory_prompt

CASES = [
    (assemble_role_prompt, "Agent-role assembly differs from its fixed source"),
    (assemble_role_v2_prompt, "Agent-role v2 assembly differs from its fixed source"),
    (assemble_role_v3_prompt, "Agent-role v2 assembly differs from its fixed source"),
    (assemble_role_v4_prompt, "Agent-role v2 assembly differs from its fixed source"),
    (assemble_assistant_reply_prompt, "Past-assistant-reply assembly differs from its fixed source"),
    (assemble_history_prompt, "History-reader assembly differs from its fixed source"),
    (assemble_history_skill_draft_prompt, "History skill-draft assembly differs from its fixed source"),
    (assemble_reviewed_note_prompt, "The reviewed-note superset differs from its fixed source"),
    (assemble_sop_copy_prompt, "SOP copy assembly differs from its original literal profile"),
    (assemble_skill_draft_prompt, "Skill draft assembly differs from its original literal profile"),
    (lambda: assemble_prompt(SKILL_PROFILE), "Skill prompt assembly differs from its versioned source"),
    (assemble_skill_purpose_prompt, "Skill purpose assembly differs from its fixed source"),
    (assemble_skill_resources_prompt, "Skill-resource prompt differs from its versioned source"),
    (assemble_user_file_prompt, "Original-user-file assembly differs from its fixed source"),
    (assemble_user_turn_prompt, "Original-user prompt assembly differs from its fixed source"),
    (assemble_work_memory_prompt, "Reviewed-work memory prompt differs from its fixed source"),
]
IDS = [str(index) for index in range(len(CASES))]


@pytest.mark.parametrize(("assemble", "refusal"), CASES, ids=IDS)
def test_the_original_record_is_accepted_unchanged(assemble, refusal):
    prompt = assemble()
    raw = prompt.system_message.encode("utf-8")
    dumped = prompt.record.model_dump()
    assert (dumped["prefix_sha256"], dumped["prefix_utf8_bytes"]) == (hashlib.sha256(raw).hexdigest(), len(raw))
    assert type(prompt.record).model_validate(dumped).model_dump() == dumped


@pytest.mark.parametrize("change", [
    {"prefix_sha256": "0" * 64}, {"prefix_utf8_bytes": 1}, {"omissions": ()},
], ids=["hash", "bytes", "omissions"])
@pytest.mark.parametrize(("assemble", "refusal"), CASES, ids=IDS)
def test_a_changed_record_is_refused_with_its_own_text(assemble, refusal, change):
    record = assemble().record
    with pytest.raises(ValidationError) as caught:
        type(record).model_validate({**record.model_dump(), **change})
    assert [error["msg"] for error in caught.value.errors()] == ["Value error, " + refusal]
