"""Memory first-use records through the published synthetic standing startup chain.

This reuses #141's fixture, including its real predecessor inventory/quiescence
check. SDK state and exit facts are synthetic; no SDK, model, or Azure is run.
"""

import hashlib

from shared_context.behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE, HISTORY_SKILL_DRAFT_MEMORY_PROFILE
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.memory_bridge_settings import (
    MemoryBridgeSettingsService, MemoryRecallChoice, SelectMemoryBridgeSettings, memory_slot_ids,
)
from shared_context.memory_note_proposal_bridge import NoteBridgeCommand, NoteProposalBridge, ReviewNoteBridge
from shared_context.memory_note_proposals import NoteProposalCommand, NoteProposalService
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def test_memory_first_use_and_recall_records_remain_readable_at_startup(synthetic_standing_v1):
    c = synthetic_standing_v1
    runtime, control, human = c.h.runtime, c.control, c.human
    baseline = read_startup(c)
    role = assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE).record
    selected = AgentTemplate(
        role=TemplateRole(profile=role.profile, prefix_sha256=role.prefix_sha256,
                          capability_catalog_version=role.capability_catalog_version),
        settings=TemplateSettings(checkpoint_profile=control.policy.profile.checkpoint_profile,
                                  sdk_start_profile=control.policy.profile.sdk_start_profile,
                                  user_text_file_profile="fa-user-text-files-v1"),
    )
    select_profile(control, c.authority, human, SelectConversationProfile(
        request_id="memory-startup-profile", expected_selection_id=None, template=selected))
    settings = MemoryBridgeSettingsService(runtime)
    enabled = settings.select(human, SelectMemoryBridgeSettings(
        schema_version="fa-memory-bridge-settings-command-v1", request_id="memory-startup-enabled",
        expected_settings_id=None, enabled=True))
    setting_id = enabled.payload["result"]["settings_id"]
    configured = read_startup(c)
    assert configured.rows[setting_id].payload["data"]["kind"] == "new_conversation_memory_settings"
    assert sum(row.payload["data"].get("kind") == "new_conversation_memory_settings_head"
               for row in configured.rows.values()) == 1

    first = saved_turn(c, "memory-startup-origin")
    record = control.records[first.binding.binding_id]
    assert record.profile.memory_scope.settings_id == setting_id
    assert record.runtime_policy.human_text_enabled
    read_startup(c)

    raw = record.profile.instruction.content.encode("utf-8")
    text = b"startup-reader"
    begin = raw.index(text)
    proposal = NoteProposalCommand(
        schema_version="fa-note-proposal-command-v1", request_id="memory-startup-quote",
        conversation_id=first.assignment.task.instruction.conversation_id, turn_number=1, speaker="user",
        source_sha256=hashlib.sha256(raw).hexdigest(), start_byte=begin, end_byte=begin + len(text),
        share_team_id=first.binding.team_id, share_work_id=first.binding.work.work_id, share_confirmed=True,
    )
    proposals = NoteProposalService(runtime)
    created = proposals.create(first.binding.binding_id, proposal, human)
    proposed = read_startup(c)
    original_proposal = proposed.rows[created.payload["result"]["proposal_id"]]
    assert original_proposal.payload["data"]["text"] == text.decode("utf-8")
    assert original_proposal.payload["data"]["effective"] is False

    source_id, claim_id = memory_slot_ids(record.admission_id)
    bridge = NoteProposalBridge(runtime)
    mapping = NoteBridgeCommand(
        schema_version="fa-note-bridge-v1", request_id="memory-startup-bridge",
        source_id=source_id, claim_id=claim_id, claim_version=1, effective_from=c.h.time)
    bridge.prepare(first.binding.binding_id, proposal.request_id, mapping, human)
    read_startup(c)
    preview = bridge.preview(first.binding.binding_id, proposal.request_id, mapping, human)
    review = ReviewNoteBridge(mapping=mapping,
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        **{name: preview["review"][name] for name in ("candidate_sha256", "evidence_sha256", "policy_version")})
    bridge.review(first.binding.binding_id, proposal.request_id, review, human)
    read_startup(c)
    committed = bridge.commit(first.binding.binding_id, proposal.request_id, mapping, human)
    read_startup(c)
    reference = committed.payload["result"]["claim"]
    assert runtime.current_claim(first.binding.binding_id, claim_id, human).candidate.text == text.decode("utf-8")

    recalled = settings.select(human, SelectMemoryBridgeSettings(
        schema_version="fa-memory-bridge-settings-command-v1", request_id="memory-startup-recall",
        expected_settings_id=setting_id, enabled=True, recall=MemoryRecallChoice(
            origin_binding_id=first.binding.binding_id, claim_id=claim_id,
            expected_claim_version=reference["version"], expected_claim_sha256=reference["sha256"],
            use_in_new_conversations=True)))
    read_startup(c)
    second = saved_turn(c, "memory-startup-consumer")
    captured = control.records[second.binding.binding_id].profile
    assert captured.prompt_profile == HISTORY_SKILL_DRAFT_MEMORY_PROFILE
    assert captured.memory_scope.settings_id == recalled.payload["result"]["settings_id"]
    assert captured.memory_scope.recall.source.claim_sha256 == reference["sha256"]
    assert claim_id not in second.binding.claim_ids and source_id not in second.binding.source_ids
    after = read_startup(c)
    assert after.rows[created.payload["result"]["proposal_id"]] == original_proposal
    assert all(after.receipts[key] == value for key, value in baseline.receipts.items())
    assert c.sessions.calls == []
