"""B1 SP: fa-instruction-agent-role-v3 = agent-role v2 plus the two live-probe fixes (SP-14 drafts, SP-08 devices).

Offline text and wiring only; whether the real model follows it is checked by rerunning probes 01, 04 and 12 live.
"""

import hashlib
import json

import pytest

from shared_context.behavior_agent_role_prompt import (
    AGENT_ROLE_V2_PROFILE, AGENT_ROLE_V3_PROFILE, ROLE_V2_CAPABILITIES, ROLE_V2_PROMPT, ROLE_V3_PROMPT,
    RoleV2PromptAssemblyRecord, RoleV3PromptAssemblyRecord, RolePromptCapture, assemble_role_v3_prompt, with_role_text,
)
from shared_context.behavior_native_turn_wire import NativeTurnCapture
from shared_context.behavior_skill_prompt import assemble_prompt
from test_native_turn_wire import capture

V2_SHA256 = "a2ebe72807770127e06e2cd7f3b7b600cb9377912740cfdbeb8114b916076334"


def sections(text):
    head, *parts = text.split("\n\n## ")
    return head, {part.split("\n", 1)[0]: part for part in parts}


def test_v3_is_v2_word_for_word_except_the_draft_and_device_fixes():
    # The published v2 is unchanged; v3 is only a new version next to it.
    assert hashlib.sha256(ROLE_V2_PROMPT.encode()).hexdigest() == V2_SHA256
    v2_head, v2 = sections(ROLE_V2_PROMPT)
    v3_head, v3 = sections(ROLE_V3_PROMPT)
    assert (v2_head, v3_head) == ("FA instruction profile: fa-instruction-agent-role-v2.",
                                  "FA instruction profile: fa-instruction-agent-role-v3.")
    assert list(v3) == list(v2)
    changed = {title for title in v2 if v2[title] != v3[title]}
    assert changed == {"Where you work", "Drafting and sending"}
    # v3 only adds sentences: removing them gives back the v2 section exactly.
    added = {
        "Where you work": " Call this computer the assistant's computer",
        "Drafting and sending": " When asked to write and send something you cannot send",
    }
    for title, start in added.items():
        begin = v3[title].index(start)
        end = v3[title].index(".", v3[title].index("placeholders" if title == "Drafting and sending" else "computer.",
                                                    begin)) + 1
        assert v3[title][:begin] + v3[title][end:] == v2[title], title
    # SP-14 (probes 01, 04): write the draft now, say it was not sent and why, ask only for what is missing.
    drafting = v3["Drafting and sending"]
    assert "give the complete draft in the same reply and say it was not sent and why" in drafting
    assert "no sending tool is offered, or the send needs a human decision" in drafting
    assert "Do not hold the draft back to ask whether the user wants it" in drafting
    assert "such as the recipient" in drafting and "placeholders" in drafting
    # SP-08 (probe 12): the user's device is not the assistant's computer; offer, do not switch silently.
    where = v3["Where you work"]
    assert "Call this computer the assistant's computer, never \"my computer\" in any language" in where
    assert "say you cannot operate their device" in where
    assert "offer to do it on the assistant's computer or give steps they can follow, and act only after they choose" in where
    assert "never quietly do it on another computer" in where
    # No blanket ban on contacting external recipients: sending still follows the tools and approvals offered.
    assert "contact external recipients" not in ROLE_V3_PROMPT
    assert len(ROLE_V3_PROMPT.encode()) < 12 * 1024


def test_v3_has_its_own_fixed_record_and_the_same_readers_and_role_placement():
    prompt = assemble_prompt(AGENT_ROLE_V3_PROFILE)
    raw = prompt.system_message.encode()
    assert prompt.system_message == ROLE_V3_PROMPT == assemble_role_v3_prompt().system_message
    record = prompt.record
    assert isinstance(record, RoleV3PromptAssemblyRecord)
    assert (record.profile, record.prefix_sha256, record.prefix_utf8_bytes) == (
        AGENT_ROLE_V3_PROFILE, hashlib.sha256(raw).hexdigest(), len(raw))
    assert record.declared_capabilities == ROLE_V2_CAPABILITIES
    assert record.capability_catalog_version == assemble_prompt(AGENT_ROLE_V2_PROFILE).record.capability_catalog_version
    # A record names exactly one fixed text: neither version's digest validates as the other.
    with pytest.raises(ValueError):
        RoleV3PromptAssemblyRecord(prefix_sha256=V2_SHA256, prefix_utf8_bytes=len(ROLE_V2_PROMPT.encode()))
    with pytest.raises(ValueError):
        RoleV2PromptAssemblyRecord(prefix_sha256=record.prefix_sha256, prefix_utf8_bytes=record.prefix_utf8_bytes)
    with pytest.raises(ValueError):
        assemble_role_v3_prompt(capabilities=("read_context", "execute_operation"))
    text = "ROLE-ALPHA keeps answers short."
    role = RolePromptCapture(version=1, sha256=hashlib.sha256(text.encode()).hexdigest(), text=text)
    later = ROLE_V3_PROMPT + "\n\n## Native turn origin\nOrigin text."
    combined = with_role_text(later, role)
    assert combined.startswith(later) and combined.endswith("\n\n" + text)

    def selected(role_prompt):
        values = capture().model_dump(mode="json")
        values["template"]["role"] = {"profile": AGENT_ROLE_V3_PROFILE, "prefix_sha256": record.prefix_sha256,
                                      "capability_catalog_version": record.capability_catalog_version}
        values["role_prompt"] = None if role_prompt is None else role_prompt.model_dump(mode="json")
        return NativeTurnCapture.model_validate_json(json.dumps(values))

    assert selected(None).role_prompt is None and selected(role).role_prompt == role


def test_v3_stays_selectable_for_existing_agents():
    from shared_context.behavior_profile_selection import SELECTABLE_PROFILES

    # New Agents use agent-role v4 since 2026-10-04 (test_agent_role_prompt_v4); v2/v3 selections keep working.
    assert {AGENT_ROLE_V2_PROFILE, AGENT_ROLE_V3_PROFILE} <= SELECTABLE_PROFILES
