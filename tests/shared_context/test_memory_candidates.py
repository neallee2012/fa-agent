"""MEM-01 suggestions stay private and inactive until the original turn is saved."""

import hashlib

import pytest

from shared_context.errors import Problem
from shared_context.behavior_capabilities import CapabilityGrant
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from test_behavior_assistant import assistant
from test_conversation_profile_selection import template
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1
from test_conversation_note_memory import memory_conversations
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import configured, open_turn, ordinary, settle


def test_candidate_capability_adds_only_its_tool_and_preserves_the_base_prefix():
    from shared_context.behavior_capabilities import CapabilityCapture, available_grants
    from shared_context.behavior_capability_prompt import assemble_capabilities, session_needs
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.memory_candidates import CANDIDATE_CAPABILITY

    grants = available_grants(None, ("fa-memory-candidate-v1",))
    grant = grants["fa-memory-candidate-v1"]
    assert grant.entry_sha256 == CANDIDATE_CAPABILITY.entry_sha256
    assert grant.tools == ("stage_memory_candidate",) and grant.vision is False
    capture = CapabilityCapture(head_selection_id="selected-candidate", grants=(grant,))
    base = assemble_prompt(HISTORY_PROFILE)
    prompt = assemble_capabilities(HISTORY_PROFILE, capture)
    assert prompt.system_message.startswith(base.system_message)
    assert prompt.record.base_prefix_sha256 == base.record.prefix_sha256
    assert "does not create an effective memory" in prompt.system_message
    assert "original answer is saved successfully" in prompt.system_message
    assert "Observe before acting" not in prompt.system_message
    needs = session_needs(capture)
    assert needs.custom_tools == ("stage_memory_candidate",)
    assert not needs.vision and not needs.image_tools and not needs.mcp_servers
    assert not needs.builtin_tools and not needs.custom_agents


def test_candidate_composes_with_delegate_without_giving_the_child_tools():
    from shared_context.behavior_capabilities import CapabilityCapture, available_grants
    from shared_context.behavior_capability_prompt import assemble_capabilities, session_needs

    grants = available_grants(None, ("fa-delegate-v1", "fa-memory-candidate-v1"))
    capture = CapabilityCapture(head_selection_id="candidate-and-delegate",
                                grants=tuple(grants[name] for name in sorted(grants)))
    needs = session_needs(capture)
    assert needs.custom_tools == ("stage_memory_candidate",)
    assert needs.builtin_tools == ("task",)
    assert len(needs.custom_agents) == 1 and needs.custom_agents[0]["tools"] == []
    assert {"task", "stage_memory_candidate"} <= set(
        assemble_capabilities(HISTORY_PROFILE, capture).record.declared_capabilities)


@pytest.fixture
def candidate_turn(assistant, request):
    from shared_context.memory_candidates import CANDIDATE_CAPABILITY

    h, control, human = configured(assistant)
    builtin = CANDIDATE_CAPABILITY
    grant = CapabilityGrant(entry_id=builtin.entry_id, entry_sha256=builtin.entry_sha256,
                            kind="fa-builtin", tools=builtin.tools, vision=False)
    h.runtime.capability_grants = {builtin.entry_id: grant}
    if getattr(request, "param", False):
        from shared_context.memory_bridge_settings import MemoryBridgeSettingsService, SelectMemoryBridgeSettings
        MemoryBridgeSettingsService(h.runtime).select(human, SelectMemoryBridgeSettings(
            schema_version="fa-memory-bridge-settings-command-v1", request_id="candidate-memory-slots",
            expected_settings_id=None, enabled=True))
    select_profile(control, h.assistant, human, SelectConversationProfile(
        request_id="select-memory-candidate", expected_selection_id=None,
        template=template(HISTORY_PROFILE, capabilities=(grant.ref,),
                          user_text_file_profile="fa-user-text-files-v1",
                          checkpoint_profile=control.policy.profile.checkpoint_profile,
                          sdk_start_profile=control.policy.profile.sdk_start_profile)))
    turn = open_turn(h, control, human, "memory-candidate-turn")
    try:
        yield turn
    finally:
        turn.client.close()


def stage(turn, command):
    from shared_context.memory_candidates import stage_candidate

    principal = turn.h.verifier(turn.h.client.build_request("GET", "/", headers=turn.h.headers("agent")))
    return stage_candidate(turn.h.assistant, turn.session, command, principal)


def test_candidate_becomes_reviewable_only_after_its_original_answer_is_saved(candidate_turn):
    from shared_context.memory_candidates import CandidateCommand, MemoryCandidates

    c = candidate_turn
    service = MemoryCandidates(c.h.runtime)
    assert service.read(c.binding.binding_id, c.human).state == "not_recorded"
    original = c.h.runtime.current_claim(c.binding.binding_id, c.binding.claim_ids[0], c.human)
    command = CandidateCommand(
        request_id="stage-memory-candidate", decision="candidate", quote="original inventory",
        reason="durable_fact")
    receipt = stage(c, command)
    assert receipt.state == "staged" and receipt.effective is False
    waiting = service.read(c.binding.binding_id, c.human)
    assert waiting.state == "awaiting_saved_turn" and waiting.text is None
    guard = c.h.store._guard()
    assert stage(c, command) == receipt
    assert c.h.store._guard() == guard

    settle(c, ordinary(c.assignment.sdk_session_id))
    ready = service.read(c.binding.binding_id, c.human)
    assert ready.state == "pending_review" and ready.effective is False
    assert ready.text == "original inventory"
    assert ready.source.source_sha256 == hashlib.sha256(
        c.assignment.task.instruction.content.encode("utf-8")).hexdigest()
    assert ready.source.speaker == "user"
    assert c.h.runtime.current_claim(c.binding.binding_id, c.binding.claim_ids[0], c.human) == original
    assert c.h.endpoint.requests == []


def test_no_candidate_is_a_saved_decision_not_a_fabricated_note(candidate_turn):
    from shared_context.memory_candidates import CandidateCommand, MemoryCandidates

    c = candidate_turn
    command = CandidateCommand(request_id="stage-memory-candidate", decision="no_candidate",
                               reason="no_durable_information")
    stage(c, command)
    settle(c, ordinary(c.assignment.sdk_session_id))
    value = MemoryCandidates(c.h.runtime).read(c.binding.binding_id, c.human)
    assert value.state == "skipped" and value.text is value.source is None
    assert value.reason == "no_durable_information" and value.effective is False


def test_changed_decision_and_unavailable_capability_do_not_write_again(candidate_turn):
    from shared_context.memory_candidates import CandidateCommand

    c = candidate_turn
    command = CandidateCommand(request_id="stage-memory-candidate", decision="candidate",
                               reason="durable_fact", quote="original inventory")
    stage(c, command)
    before = c.h.store._guard()
    with pytest.raises(Problem) as changed:
        stage(c, command.model_copy(update={"quote": "inventory"}))
    assert changed.value.body["error"]["code"] == "MEMORY_CANDIDATE_CONFLICT"
    c.h.runtime.capability_grants = {}
    with pytest.raises(Problem) as disabled:
        stage(c, command)
    assert disabled.value.body["error"]["code"] == "MEMORY_CANDIDATE_DISABLED"
    assert c.h.store._guard() == before


def test_invalid_candidate_does_not_prevent_saving_the_answer(candidate_turn):
    from shared_context.memory_candidates import CandidateCommand, MemoryCandidates

    c = candidate_turn
    before = c.h.store._guard()
    with pytest.raises(Problem) as missing:
        stage(c, CandidateCommand(request_id="stage-memory-candidate", decision="candidate",
                                  reason="durable_fact", quote="not present in the original user text"))
    assert missing.value.body["error"]["code"] == "MEMORY_CANDIDATE_SOURCE_INVALID"
    assert c.h.store._guard() == before
    settle(c, ordinary(c.assignment.sdk_session_id))
    assert MemoryCandidates(c.h.runtime).read(c.binding.binding_id, c.human).state == "not_recorded"
    assert c.control.read_reply(c.human, c.assignment.task.instruction.conversation_id, 1).text


def test_failed_turn_never_makes_a_staged_quote_reviewable(candidate_turn):
    from shared_context.memory_candidates import CandidateCommand, MemoryCandidates

    c = candidate_turn
    stage(c, CandidateCommand(request_id="stage-memory-candidate", decision="candidate",
                             reason="durable_fact", quote="original inventory"))
    archive = c.client.save_sdk_state(ordinary(c.assignment.sdk_session_id), c.stop)
    c.client.finish_failed(archive, c.stop, "sdk_failed")
    value = MemoryCandidates(c.h.runtime).read(c.binding.binding_id, c.human)
    assert value.state == "ineligible" and value.text is value.source is None
    assert value.effective is False


def test_candidate_ack_loss_only_reconciles_the_original_receipt(candidate_turn, monkeypatch):
    from shared_context.behavior_operations import control_key
    from shared_context.memory_candidates import CandidateCommand, candidate_key, reconcile_candidate

    c = candidate_turn
    command = CandidateCommand(request_id="stage-memory-candidate", decision="candidate",
                               reason="durable_fact", quote="original inventory")
    key = candidate_key(c.h.store._scope.authority_domain, c.binding.binding_id)
    request_id = control_key("memory-candidate-write", key)
    submit, attempts = c.h.store._submit, []

    def lose_response(operations, request):
        if request is not None and request.request_id == request_id:
            attempts.append(request.request_id)
            c.h.transport.after_apply = "lost"
        try:
            return submit(operations, request)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lose_response)
    with pytest.raises(Problem) as unknown:
        stage(c, command)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    before = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_: pytest.fail("lookup must not write"))
    principal = c.h.verifier(c.h.client.build_request("GET", "/", headers=c.h.headers("agent")))
    found = reconcile_candidate(c.h.assistant, c.session, command, principal)
    assert found.candidate_id == key and attempts == [request_id]
    assert stage(c, command) == found
    assert c.h.store._guard() == before


def test_human_candidate_lookup_uses_current_read_and_cannot_activate(candidate_turn):
    from shared_context.memory_candidate_api import install_memory_candidate_routes
    from shared_context.memory_candidates import CandidateCommand

    c = candidate_turn
    install_memory_candidate_routes(c.h.app, c.h.runtime, c.h.verifier)
    path = "/behavior/v1/bindings/" + c.binding.binding_id + "/memory-candidate"
    stage(c, CandidateCommand(request_id="stage-memory-candidate", decision="candidate",
                             reason="durable_fact", quote="original inventory"))
    assert c.h.client.get(path, headers=c.h.headers("read-only")).json()["text"] is None
    settle(c, ordinary(c.assignment.sdk_session_id))
    before = c.h.store._guard()
    response = c.h.client.get(path, headers=c.h.headers("read-only"))
    assert response.status_code == 200
    assert response.json()["state"] == "pending_review"
    assert response.json()["text"] == "original inventory"
    for identity in ("agent", "old-m3"):
        assert c.h.client.get(path, headers=c.h.headers(identity)).status_code == 403
    assert c.h.client.get(path + "?activate=true", headers=c.h.headers()).status_code == 422
    assert c.h.client.post(path, json={}, headers=c.h.headers()).status_code == 405
    assert c.h.store._guard() == before


def test_staged_candidate_record_survives_the_installed_startup_readers(synthetic_standing_v1, monkeypatch):
    from shared_context.memory_candidates import (
        CANDIDATE_CAPABILITY, CandidateCommand, MemoryCandidates, stage_candidate,
    )

    c = synthetic_standing_v1
    builtin = CANDIDATE_CAPABILITY
    grant = CapabilityGrant(entry_id=builtin.entry_id, entry_sha256=builtin.entry_sha256,
                            kind="fa-builtin", tools=builtin.tools, vision=False)
    c.h.runtime.capability_grants = {builtin.entry_id: grant}
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id="startup-candidate-selection", expected_selection_id=None,
        template=template(HISTORY_PROFILE, capabilities=(grant.ref,),
                          user_text_file_profile="fa-user-text-files-v1",
                          checkpoint_profile=c.control.policy.profile.checkpoint_profile,
                          sdk_start_profile=c.control.policy.profile.sdk_start_profile)))
    original = c.authority.save_text_reply
    staged = []

    def before_save(session, command, principal):
        staged.append(stage_candidate(c.authority, session, CandidateCommand(
            request_id="stage-memory-candidate", decision="candidate", reason="durable_fact",
            quote="startup-reader"), principal))
        return original(session, command, principal)

    monkeypatch.setattr(c.authority, "save_text_reply", before_save)
    turn = saved_turn(c, "startup-memory-candidate")
    inventory = read_startup(c)
    assert len(staged) == 1
    assert inventory.rows[staged[0].candidate_id].payload["data"]["kind"] == "memory_turn_candidate"
    assert MemoryCandidates(c.h.runtime).read(turn.binding.binding_id, c.human).text == "startup-reader"
    assert c.sessions.calls == []


def test_cli_candidate_read_is_one_get_not_a_proposal_or_a_claim(candidate_turn, monkeypatch, capsys):
    from conftest import TENANT
    from shared_context import behavior_memory_cli as cli
    from shared_context.memory_candidate_api import install_memory_candidate_routes
    from shared_context.memory_candidates import CandidateCommand
    from test_m3_auth import CLIENT_ID
    from test_memory_note_proposal_cli import environment

    c = candidate_turn
    install_memory_candidate_routes(c.h.app, c.h.runtime, c.h.verifier)
    stage(c, CandidateCommand(request_id="stage-memory-candidate", decision="candidate",
                             reason="durable_fact", quote="original inventory"))
    settle(c, ordinary(c.assignment.sdk_session_id))
    credential, requests = environment(c, monkeypatch)
    before = c.h.store._guard()
    assert cli.main([
        "--api", "https://poc.example.westus3.azurecontainerapps.io",
        "--tenant", TENANT, "--audience", "api://22222222-2222-2222-2222-222222222222",
        "--client-id", CLIENT_ID, "candidate", c.binding.binding_id,
    ]) == 0
    assert requests == [("GET", "/behavior/v1/bindings/" + c.binding.binding_id + "/memory-candidate")]
    assert c.h.store._guard() == before
    output = capsys.readouterr()
    assert '"pending_review"' in output.out and '"effective": false' in output.out
    assert credential.get_token.return_value.token not in output.out + output.err


@pytest.mark.parametrize("candidate_turn", [True], indirect=True)
def test_human_can_share_the_saved_candidate_through_the_existing_review_bridge(candidate_turn):
    from shared_context.memory_bridge_settings import memory_slot_ids
    from shared_context.memory_candidates import CandidateCommand, MemoryCandidates
    from shared_context.memory_note_proposal_bridge import NoteBridgeCommand, NoteProposalBridge, ReviewNoteBridge
    from shared_context.memory_note_proposals import NoteProposalCommand, NoteProposalService

    c = candidate_turn
    stage(c, CandidateCommand(request_id="stage-memory-candidate", decision="candidate",
                             reason="durable_fact", quote="original inventory"))
    settle(c, ordinary(c.assignment.sdk_session_id))
    value = MemoryCandidates(c.h.runtime).read(c.binding.binding_id, c.human)
    proposal = NoteProposalCommand(
        schema_version="fa-note-proposal-command-v1", request_id="human-accept-candidate",
        conversation_id=value.conversation_id, turn_number=value.turn_number,
        speaker=value.source.speaker, source_sha256=value.source.source_sha256,
        start_byte=value.source.start_byte, end_byte=value.source.end_byte,
        share_team_id=value.team_id, share_work_id=value.work_id, share_confirmed=True,
    )
    pending = NoteProposalService(c.h.runtime).create(c.binding.binding_id, proposal, c.human)
    assert pending.payload["result"]["effective"] is False
    source_id, claim_id = memory_slot_ids(c.control.records[c.binding.binding_id].admission_id)
    bridge = NoteProposalBridge(c.h.runtime)
    mapping = NoteBridgeCommand(schema_version="fa-note-bridge-v1", request_id="review-candidate",
                                source_id=source_id, claim_id=claim_id, claim_version=1, effective_from=c.h.time)
    bridge.prepare(c.binding.binding_id, proposal.request_id, mapping, c.human)
    preview = bridge.preview(c.binding.binding_id, proposal.request_id, mapping, c.human)
    with pytest.raises(Problem):
        bridge.commit(c.binding.binding_id, proposal.request_id, mapping, c.human)
    review = ReviewNoteBridge(mapping=mapping,
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        **{name: preview["review"][name] for name in ("candidate_sha256", "evidence_sha256", "policy_version")})
    bridge.review(c.binding.binding_id, proposal.request_id, review, c.human)
    bridge.commit(c.binding.binding_id, proposal.request_id, mapping, c.human)
    assert c.h.runtime.current_claim(c.binding.binding_id, claim_id, c.human).candidate.text == value.text
    assert MemoryCandidates(c.h.runtime).read(c.binding.binding_id, c.human).effective is False


def test_assistant_candidate_routes_require_the_original_agent_ticket(candidate_turn):
    from shared_context.memory_candidates import CandidateCommand

    c = candidate_turn
    prefix = ("/behavior/v1/bindings/" + c.binding.binding_id
              + "/assistant/sessions/" + c.assignment.session_id)
    command = CandidateCommand(request_id="stage-memory-candidate", decision="candidate",
                               reason="durable_fact", quote="original inventory")
    headers = {**c.h.headers("agent"),
               "x-behavior-assistant-ticket": c.client.ticket.get_secret_value()}
    before = c.h.store._guard()
    for invalid in (c.h.headers("agent"), {
            **c.h.headers(), "x-behavior-assistant-ticket": c.client.ticket.get_secret_value()}):
        assert c.h.client.post(prefix + "/stage-memory-candidate", json=command.model_dump(),
                               headers=invalid).status_code == 403
    assert c.h.store._guard() == before
    response = c.h.client.post(prefix + "/stage-memory-candidate", json=command.model_dump(), headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "staged" and response.json()["effective"] is False
    before = c.h.store._guard()
    same = c.h.client.post(prefix + "/memory-candidate-reconcile", json=command.model_dump(), headers=headers)
    assert same.status_code == 200 and same.json() == response.json()
    assert c.h.store._guard() == before


def test_candidate_capability_composes_with_independently_captured_note_recall(memory_conversations):
    from shared_context.behavior_capabilities import available_grants
    from shared_context.behavior_capability_prompt import assemble_capabilities
    from shared_context.behavior_profile_selection import current_selection
    from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE, HISTORY_SKILL_DRAFT_MEMORY_PROFILE
    from shared_context.memory_candidates import CandidateCommand
    from test_conversation_profile_selection import select
    from test_reviewed_note_callback import selected_note

    h, control, human = memory_conversations
    selected_note(h, control, human)
    previous = current_selection(h.store, control.policy).selection_id
    h.runtime.capability_grants = available_grants(None, ("fa-memory-candidate-v1",))
    grant = h.runtime.capability_grants["fa-memory-candidate-v1"]
    chosen, _ = select(h, request_id="candidate-with-recall", previous=previous, value=template(
        HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile="fa-user-text-files-v1", capabilities=(grant.ref,)))
    assert chosen.status_code == 200, chosen.text
    turn = open_turn(h, control, human, "candidate-recall-turn")
    try:
        task = turn.assignment.task.instruction
        assert task.prompt_profile == HISTORY_SKILL_DRAFT_MEMORY_PROFILE
        combined = assemble_capabilities(task.prompt_profile, task.capabilities)
        assert {"stage_memory_candidate", "read_reviewed_note", "save_skill_draft"} <= set(
            combined.record.declared_capabilities)
        assert turn.client.read_reviewed_note().text == "original inventory"
        stage(turn, CandidateCommand(request_id="stage-memory-candidate", decision="candidate",
                                     quote="original inventory", reason="durable_fact"))
        assert turn.client.read_reviewed_note().text == "original inventory"
        before = h.store._guard()
        denied, _ = select(h, request_id="cannot-select-derived-note-profile",
            previous=chosen.json()["result"]["selection_id"], value=template(
                HISTORY_SKILL_DRAFT_MEMORY_PROFILE, user_text_file_profile="fa-user-text-files-v1",
                capabilities=(grant.ref,)))
        assert denied.status_code == 422 and h.store._guard() == before
    finally:
        turn.client.close()
