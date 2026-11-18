"""Versioned truthfulness guidance and original public seams, not model-quality evidence."""

import pytest

from shared_context.behavior_instruction_prompt import PromptAssemblyRecord, assemble_instruction_prompt
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.native_responses_wire import SENT_RELEASE
from conversation_profile_selection_helpers import configured
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


PROFILE = "fa-instruction-v3"
SOURCE_RULE = (
    "Attribute a result to an agent or tool only after that source actually returns the matching result for this task; "
    "dispatch or acceptance is not a completed check. If it fails, is pending or unknown, say so. "
    "Label any permitted calculation you do yourself as your own; it cannot satisfy a requested independent check."
)
DRAFT_RULE = (
    "When drafting for someone, preserve the supplied facts and time references. "
    "Do not invent causes, dates, commitments or completion states; omit unknown details or mark them as placeholders. "
    "Rewording must not create a new factual promise."
)


def test_v3_adds_only_two_truthfulness_rules_to_the_unchanged_v2_sections():
    original = assemble_instruction_prompt("fa-instruction-v2")
    updated = assemble_instruction_prompt(PROFILE)
    assert updated == assemble_prompt(PROFILE)
    assert original.record.prefix_utf8_bytes == 6593
    assert original.record.prefix_sha256 == "1a77e135cf02adaf3e789c3cc153207e1b191e5a30a262b0605efd89f4ab32bd"
    old_sections = original.system_message.split("\n\n")
    new_sections = updated.system_message.split("\n\n")
    assert new_sections[0] == "FA instruction profile: fa-instruction-v3."
    additions = {"## Facts and evidence": SOURCE_RULE, "## Drafting and sending": DRAFT_RULE}
    for before, after in zip(old_sections[1:], new_sections[1:], strict=True):
        addition = additions.get(before.split("\n", 1)[0])
        assert after == before + ("" if addition is None else " " + addition)
    assert updated.record.profile == PROFILE
    assert updated.record.prefix_utf8_bytes == 7149
    assert updated.record.prefix_sha256 == "eac73c98235499a0caa2784f7e038468a02176aa3312849552e634616c8950a0"
    assert updated.record.omissions == original.record.omissions
    assert updated.record.declared_capabilities == ("read_context", "execute_operation")


def test_v3_keeps_canonical_base_tools_and_exact_safe_record():
    full = assemble_prompt(PROFILE)
    reordered = assemble_prompt(PROFILE, capabilities=("execute_operation", "read_context"))
    read_only = assemble_prompt(PROFILE, capabilities=("read_context",))
    empty = assemble_prompt(PROFILE, capabilities=())
    assert full == reordered
    assert "- execute_operation:" not in read_only.system_message
    assert "- No tools configured." in empty.system_message
    assert len({value.record.prefix_sha256 for value in (full, read_only, empty)}) == 3
    for value in (full, read_only, empty):
        assert SOURCE_RULE in value.system_message and DRAFT_RULE in value.system_message
        assert value.record.prefix_tokens is None
        assert value.record.source == "server-profile-recomputed"
        assert (value.record.sdk_version, value.record.runtime_version) == SENT_RELEASE
        assert PromptAssemblyRecord.model_validate_json(value.record.model_dump_json()) == value.record
        assert value.system_message not in value.record.model_dump_json()
    assert "save_skill_draft" not in full.system_message
    for capabilities in (("save_skill_draft",), ("read_skill",), ("read_context", "read_context")):
        with pytest.raises(ValueError):
            assemble_prompt(PROFILE, capabilities=capabilities)
    with pytest.raises(ValueError):
        assemble_prompt("fa-instruction-v5")
