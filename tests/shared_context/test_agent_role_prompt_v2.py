"""B1 SP series: fa-instruction-agent-role-v2, one native platform prompt for A, B and new Agents.

Offline assembly and wire contracts; the pinned-runtime turn lives in test_native_sdk_runtime.py.
"""

import hashlib
import json

import pytest

from shared_context.behavior_agent_role_prompt import (
    AGENT_ROLE_V2_PROFILE, ROLE_FIXED_PROMPT, ROLE_SECTION, ROLE_V2_CAPABILITIES, ROLE_V2_PROMPT, RolePromptCapture,
    assemble_role_v2_prompt,
    with_role_text,
)
from shared_context.behavior_instruction_prompt import assemble_instruction_prompt
from shared_context.behavior_native_turn_wire import NativeTurnCapture
from shared_context.behavior_skill_prompt import assemble_prompt
from test_native_turn_wire import capture
from test_behavior_assistant import assistant  # noqa: F401
from test_instruction_flow import instruction_models, runtime_path  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401

V3 = assemble_instruction_prompt("fa-instruction-v3", capabilities=()).system_message


def v3_section(title):
    return next("## " + part for part in V3.split("\n\n## ") if part.startswith(title + "\n"))


def role(text="ROLE-ALPHA keeps answers short."):
    return RolePromptCapture(version=1, sha256=hashlib.sha256(text.encode()).hexdigest(), text=text)


def test_the_v3_rules_are_kept_word_for_word_with_the_native_readers_and_no_unavailable_operation():
    prompt = assemble_prompt(AGENT_ROLE_V2_PROFILE)
    text = prompt.system_message
    assert text.startswith("FA instruction profile: fa-instruction-agent-role-v2.\n\n")
    for title in ("Identity and origin", "Reply shape", "Facts and evidence", "Questions", "Purchases",
                  "Code changes", "Autonomy", "Initiative", "Instruction scope", "Internal details"):
        assert v3_section(title) in text, title
    # The v3 attribution rule and the drafting rules stay, with 4afa's short counterexample appended.
    assert "dispatch or acceptance is not a completed check" in text
    assert v3_section("Drafting and sending") in text
    assert "\"we are actively working on it\"" in text and "ordinary apologies and thanks are fine" in text
    # Native-true wording: no operation that always fails, no blanket "not configured" list.
    assert "execute_operation" not in text
    assert "Environment diagnostics are not enabled" not in text
    assert "contact external recipients" not in text
    assert "No general transcript search" not in text and "browser, shell" not in text
    assert "Use only the tools actually offered in this turn" in text
    # SP-08, SP-24 and the HITL line.
    assert "## Where you work\n" in text and "never claim to have acted on the user's device" in text
    assert "do not guess or switch accounts" in text
    assert "only that decision approves the action" in text and "teammate messages" in text
    # The original-message readers of agent-role v1, unchanged.
    for name in ("read_user_turn", "read_user_file", "read_assistant_reply"):
        line = next(line for line in ROLE_FIXED_PROMPT.splitlines() if line.startswith(f"- {name}:"))
        assert line in text
    assert "## Original user messages\n" in text
    assert "## Team-approved agent role" not in text, "the role section appears only with role text"
    record = prompt.record
    assert record.profile == AGENT_ROLE_V2_PROFILE and record.declared_capabilities == ROLE_V2_CAPABILITIES
    assert record.prefix_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert record.prefix_utf8_bytes == len(text.encode()) < 12 * 1024
    with pytest.raises(ValueError):
        assemble_role_v2_prompt(capabilities=("read_context", "execute_operation"))


def test_role_text_is_optional_and_always_last():
    base = assemble_role_v2_prompt().system_message
    later = base + "\n\n## Native turn origin\nOrigin text."
    combined = with_role_text(later, role())
    assert combined.startswith(later) and combined.endswith("\n\nROLE-ALPHA keeps answers short.")
    assert "## Team-approved agent role\n" in combined[len(later):]
    with pytest.raises(ValueError):
        with_role_text(V3, role())

    def selected(profile, role_prompt):
        values = capture().model_dump(mode="json")
        values["template"]["role"]["profile"] = profile
        values["role_prompt"] = None if role_prompt is None else role_prompt.model_dump(mode="json")
        return NativeTurnCapture.model_validate_json(json.dumps(values))

    assert selected(AGENT_ROLE_V2_PROFILE, None).role_prompt is None
    assert selected(AGENT_ROLE_V2_PROFILE, role()).role_prompt == role()
    with pytest.raises(ValueError):
        selected("fa-instruction-agent-role-v1", None)
    with pytest.raises(ValueError):
        selected("fa-instruction-v3", role())


@pytest.mark.parametrize("profile", ["fa-instruction-agent-role-v2", "fa-instruction-agent-role-v3",
                                     "fa-instruction-agent-role-v4"])
def test_skillify_and_other_capabilities_compose_on_the_new_profile(profile):
    from shared_context.behavior_capabilities import (
        BUILTIN_CAPABILITIES, CAPABILITY_BASE_PROFILES, SKILLIFY_ENTRY_ID, base_supports, _grant,
    )
    from shared_context.behavior_capabilities import CapabilityCapture
    from shared_context.behavior_capability_prompt import assemble_capabilities

    AGENT_ROLE_V2_PROFILE = profile
    assert AGENT_ROLE_V2_PROFILE in CAPABILITY_BASE_PROFILES
    assert base_supports(SKILLIFY_ENTRY_ID, AGENT_ROLE_V2_PROFILE)
    assert not base_supports(SKILLIFY_ENTRY_ID, "fa-instruction-agent-role-v1")
    grants = (_grant(BUILTIN_CAPABILITIES[SKILLIFY_ENTRY_ID]),)
    combined = assemble_capabilities(AGENT_ROLE_V2_PROFILE, CapabilityCapture(head_selection_id=None, grants=grants))
    base = assemble_prompt(profile).system_message
    assert combined.system_message.startswith(base + "\n\n## Organization capabilities for this turn\n")
    assert "read_user_turn or read_assistant_reply" in combined.system_message
    assert "do not invent history readers" not in combined.system_message
    assert combined.record.declared_capabilities == (*ROLE_V2_CAPABILITIES, "save_skill_draft")
    # The native writer accepts the same selection (native_skill_draft.read_intent and the API source check).
    from shared_context.behavior_skill_draft import skill_draft_enabled
    capture = CapabilityCapture(head_selection_id=None, grants=grants)
    assert skill_draft_enabled(AGENT_ROLE_V2_PROFILE, capture)
    assert not skill_draft_enabled("fa-instruction-agent-role-v1", capture)


def test_v2_stays_selectable_for_existing_agents():
    from shared_context.behavior_profile_selection import SELECTABLE_PROFILES

    # New Agents use agent-role v4 since 2026-10-04 (test_agent_role_prompt_v4); v2 selections keep working.
    assert AGENT_ROLE_V2_PROFILE in SELECTABLE_PROFILES


@pytest.mark.parametrize("profile", ["fa-instruction-agent-role-v2", "fa-instruction-agent-role-v3",
                                     "fa-instruction-agent-role-v4"])
def test_the_job_bound_lane_runs_v2_without_execute_operation_and_with_optional_role_text(assistant, monkeypatch,
                                                                                            profile):
    """8765 sidecars without a native state profile (new + Agents, deploy windows) keep working on v2."""
    from shared_context.behavior_agent_role_prompt import _SHARED_ROLE_PROMPTS
    from test_agent_role_prompt import FILES, save
    from test_conversation_profile_selection import configured, select, template
    from test_instruction_prompt import sdk_options, submit

    h, control, human = configured(assistant)
    selected, _ = select(h, value=template(profile, user_text_file_profile=FILES))
    assert selected.status_code == 200, selected.text
    _, options = sdk_options(monkeypatch, expected_tools=ROLE_V2_CAPABILITIES)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "one"}, {"content": "two"}])
    first, _ = submit(h, "v2-plain", "Start.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    save(control, human, "v2-role", "You are the release notes Agent.")
    second, _ = submit(h, "v2-role-turn", "Continue.", first["conversation_id"])
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    prompt = _SHARED_ROLE_PROMPTS[profile]
    assert [value["system_message"]["content"] for value in options] == [
        prompt, prompt + ROLE_SECTION + "\n\nYou are the release notes Agent."]
