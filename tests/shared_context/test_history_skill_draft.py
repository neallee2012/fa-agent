"""Human-selected history readers plus one unapproved user-procedure draft; local signed API and SDK-I/O doubles."""

import hashlib

import pytest

from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES
from shared_context.behavior_history_skill_draft_prompt import (
    HISTORY_SKILL_DRAFT_CAPABILITIES, HISTORY_SKILL_DRAFT_PROMPT,
)
from shared_context.behavior_profile_selection import READ_ONLY_PROFILES
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE, SKILL_DRAFT_PROFILE, SkillDraftToolArgs
from shared_context.behavior_skill_draft_prompt import SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_prompt import assemble_prompt
from conversation_profile_selection_helpers import configured
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from skill_draft_helpers import BODY, OLD_PROMPTS, PURPOSE

FILES = "fa-user-text-files-v1"


def draft(turn, body=BODY):
    return turn.client.save_skill_draft(SkillDraftToolArgs(purpose=PURPOSE, body=body))


def test_new_prompt_is_separate_and_every_older_profile_keeps_its_bytes_and_tools():
    raw = HISTORY_SKILL_DRAFT_PROMPT.encode("utf-8")
    assert len(raw) == 4917
    assert hashlib.sha256(raw).hexdigest() == "173f7608565cdcf38824f2d7f33396ff11b14d01a8dcb6b3e152241549af5b3e"
    assembled = assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE, capabilities=HISTORY_SKILL_DRAFT_CAPABILITIES)
    assert assembled.record.declared_capabilities == (*HISTORY_CAPABILITIES, "save_skill_draft")
    assert assembled.record.capability_catalog_version == "fa-instruction-tools-history-skill-draft-v1"
    for subset in (HISTORY_CAPABILITIES, SKILL_DRAFT_CAPABILITIES):
        with pytest.raises(ValueError):
            assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE, capabilities=subset)

    for profile, size, sha, capabilities in (*OLD_PROMPTS, (
            SKILL_DRAFT_PROFILE, 2638, "f47c84ffb238af98c9082d7e060f622052f70bc2bb796e8c845964c7fd508ad2",
            SKILL_DRAFT_CAPABILITIES)):
        old = assemble_prompt(profile)
        data = old.system_message.encode("utf-8")
        assert (len(data), hashlib.sha256(data).hexdigest()) == (size, sha)
        assert (() if old.record is None else old.record.declared_capabilities) == capabilities
        if profile is not None:
            with pytest.raises(ValueError):
                assemble_prompt(profile, capabilities=HISTORY_SKILL_DRAFT_CAPABILITIES)
    assert HISTORY_SKILL_DRAFT_PROFILE not in READ_ONLY_PROFILES and SKILL_DRAFT_PROFILE not in READ_ONLY_PROFILES
