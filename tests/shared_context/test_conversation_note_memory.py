"""Real local admission and note bridge across conversations; no native/model execution."""

import hashlib
from types import SimpleNamespace

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE, HISTORY_SKILL_DRAFT_MEMORY_PROFILE
from shared_context.memory_bridge_settings import memory_slot_ids
from shared_context.memory_bridge_settings_api import install_memory_bridge_settings_routes
from shared_context.memory_note_proposal_api import install_note_proposal_routes
from test_behavior_api import METRICS
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_inventory, instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn, ordinary, settle
from test_memory_note_bridge import review_command


SETTINGS = "/operator/v1/memory-bridge-settings"


def captured_memory_read_keys(policy, capture, *, first_turn, records):
    """The rows this feature adds to an admission's read set, spelled out apart from admission."""
    from shared_context.behavior_operations import control_key
    from shared_context.behavior_profile_selection import selection_key
    from shared_context.behavior_runtime import runtime_key
    from shared_context.behavior_work_policy import work_policy_key
    from shared_context.errors import Problem
    from shared_context.memory_bridge_settings import settings_head_key
    from shared_context.memory_governance import ClaimReference
    from shared_context.source_materials import SourceReference, SourceScope

    keys = [settings_head_key(policy.authority_domain, policy.agent_scope)] if first_turn else []
    if capture is None:
        return set(keys)
    keys.append(capture.settings_id)
    if first_turn and capture.recall is not None:
        origin = next((record for record in records
                       if record.binding.binding_id == capture.recall.origin_binding_id), None)
        if origin is None:
            raise Problem(409, "MEMORY_SETTINGS_INVALID", "the original note work is missing from retained history")
        if origin.agent_scope != policy.agent_scope:
            raise Problem(409, "MEMORY_SETTINGS_INVALID", "the selected note history belongs to another agent scope")
        b, source = origin.binding, capture.recall.source
        keys.extend((
            runtime_key("policy", b.binding_id), control_key("owner", b.owner.owner_record_id),
            control_key("policy", b.owner.policy_id), control_key("budget", b.owner.root_budget_id),
            work_policy_key("policy", *policy.scope_ids), work_policy_key("revocation", *policy.scope_ids),
            work_policy_key("registration", origin.admission_id),
        ))
        if origin.profile_selection_id is not None:
            keys.append(selection_key(policy.authority_domain, origin.profile_selection_id, policy.agent_id))
        if origin.profile.memory_scope is not None:
            keys.append(origin.profile.memory_scope.settings_id)
        scope = SourceScope(tenant_id=b.tenant_id, team_id=b.team_id, visibility="team")
        reference = ClaimReference(scope=scope, subject=b.subject, claim_id=source.claim_id,
                                   version=source.claim_version, sha256=source.claim_sha256)
        keys.extend((control_key("memory-subject", scope.key, b.subject), reference.key,
                     control_key("source-human-review", scope.key, source.claim_sha256, source.review_policy_version),
                     control_key("source-human-revocation", scope.key, source.claim_sha256)))
        for item in source.sources:
            ref = SourceReference(scope=scope, **item.model_dump())
            keys.extend((ref.key, ref.head_key))
    return set(keys)


@pytest.fixture
def memory_conversations(assistant):
    h, control, human = configured(assistant)
    response, _ = select(h, value=template(
        profile=HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile="fa-user-text-files-v1"))
    assert response.status_code == 200
    install_memory_bridge_settings_routes(h.app, h.runtime, h.verifier)
    install_note_proposal_routes(h.app, h.runtime, h.verifier)
    return h, control, human


def enable_memory(h):
    command = {
        "schema_version": "fa-memory-bridge-settings-command-v1", "request_id": "enable-work-note",
        "expected_settings_id": None, "enabled": True,
    }
    response = h.client.post(SETTINGS, json=command, headers=h.headers())
    assert response.status_code == 200, response.text
    return response.json()["result"]["settings_id"]


def commit_note(turn, *, kind=None, expires_at=None):
    h, record = turn.h, turn.control.records[turn.binding.binding_id]
    source_id, claim_id = memory_slot_ids(record.admission_id)
    raw = record.profile.instruction.content.encode("utf-8")
    quote = b"original inventory"
    start = raw.index(quote)
    command = {
        "schema_version": "fa-note-proposal-command-v1", "request_id": "select-work-quote",
        "conversation_id": record.profile.instruction.conversation_id, "turn_number": 1, "speaker": "user",
        "source_sha256": hashlib.sha256(raw).hexdigest(), "start_byte": start, "end_byte": start + len(quote),
        "share_team_id": turn.binding.team_id, "share_work_id": turn.binding.work.work_id, "share_confirmed": True,
    }
    path = "/behavior/v1/bindings/" + turn.binding.binding_id + "/note-proposals"
    assert h.client.post(path, json=command, headers=h.headers()).status_code == 200
    bridge = path + "/select-work-quote/bridge"
    mapping = {
        "schema_version": "fa-note-bridge-v1", "request_id": "bridge-work-quote", "source_id": source_id,
        "claim_id": claim_id, "claim_version": 1, "supersedes": None, "effective_from": h.time.isoformat(),
    }
    if kind is not None:
        mapping["kind"] = kind
    if expires_at is not None:
        mapping["expires_at"] = expires_at.isoformat()
    prepared = h.client.post(bridge + "/prepare", json=mapping, headers=h.headers())
    assert prepared.status_code == 200, prepared.text
    preview = h.client.post(bridge + "/preview", json=mapping, headers=h.headers())
    assert preview.status_code == 200, preview.text
    reviewed = h.client.post(bridge + "/review", json=review_command(mapping, preview.json()), headers=h.headers())
    assert reviewed.status_code == 200, reviewed.text
    committed = h.client.post(bridge + "/commit", json=mapping, headers=h.headers())
    assert committed.status_code == 200, committed.text
    return committed.json()["result"]["claim"]


def test_new_conversation_reads_selected_committed_note_without_changing_older_conversations(memory_conversations):
    h, control, human = memory_conversations
    old = open_turn(h, control, human, "old-without-memory")
    settle(old, ordinary(old.assignment.sdk_session_id))
    old_record = control.records[old.binding.binding_id].model_dump_json()
    settings_id = enable_memory(h)
    first = open_turn(h, control, human, "new-note-origin")
    record = control.records[first.binding.binding_id]
    source_id, claim_id = memory_slot_ids(record.admission_id)
    assert source_id in first.binding.source_ids and claim_id in first.binding.claim_ids
    assert record.runtime_policy.human_text_enabled
    settle(first, ordinary(first.assignment.sdk_session_id))
    reference = commit_note(first)
    selected = h.client.post(SETTINGS, headers=h.headers(), json={
        "schema_version": "fa-memory-bridge-settings-command-v1", "request_id": "recall-work-note",
        "expected_settings_id": settings_id, "enabled": True, "recall": {
            "origin_binding_id": first.binding.binding_id, "claim_id": claim_id,
            "expected_claim_version": reference["version"], "expected_claim_sha256": reference["sha256"],
            "use_in_new_conversations": True,
        },
    })
    assert selected.status_code == 200, selected.text
    second = open_turn(h, control, human, "later-note-reader")
    later_old = None
    try:
        assert second.assignment.task.instruction.prompt_profile == HISTORY_SKILL_DRAFT_MEMORY_PROFILE
        assert claim_id not in second.binding.claim_ids and source_id not in second.binding.source_ids
        principal = h.verifier(h.client.build_request("GET", "/", headers=h.headers("agent")))
        h.assistant.reviewed_note_catalog(second.session, wire.Command(request_id="current-reviewed-note"), principal)
        catalog = second.client.reviewed_note_catalog()
        note = second.client.read_reviewed_note()
        assert note.source == catalog.source and note.text == "original inventory"
        assert note.originals[0].text == note.text
        control.records.clear()
        control.refresh()
        assert control.records[old.binding.binding_id].model_dump_json() == old_record
        assert second.client.read_reviewed_note() == note
        settle(second, ordinary(second.assignment.sdk_session_id))
        inventory = instruction_inventory(h)
        from shared_context.behavior_instruction import conversation_key
        from shared_context.behavior_operations import control_key
        from shared_context.behavior_profile_selection import head_key, selection_key
        from shared_context.behavior_runtime import runtime_key
        from shared_context.behavior_work_policy import work_policy_key
        for original in (control.records[first.binding.binding_id], control.records[second.binding.binding_id]):
            b = original.binding
            row_id = work_policy_key("registration", original.admission_id)
            actual = inventory.receipts[row_id].payload["request"]["read_set"]
            fixed = {control.policy_key, control.revocation_key, control.cost_key, control.registry_key, row_id,
                     control_key("owner", b.owner.owner_record_id), control_key("policy", b.owner.policy_id),
                     runtime_key("policy", b.binding_id), runtime_key("plan-index", b.binding_id),
                     head_key(control.policy.authority_domain),
                     selection_key(control.policy.authority_domain, original.profile_selection_id),
                     conversation_key(original.profile.instruction.conversation_id)}
            memory = captured_memory_read_keys(control.policy, original.profile.memory_scope,
                                                first_turn=True, records=tuple(control.records.values()))
            assert set(actual) == fixed | memory
        later_old = open_turn(h, control, human, "old-followup", old.assignment.task.instruction.conversation_id)
        assert later_old.assignment.task.instruction.prompt_profile == HISTORY_SKILL_DRAFT_PROFILE
        assert control.records[later_old.binding.binding_id].profile.memory_scope is None
        assert not control.records[later_old.binding.binding_id].runtime_policy.human_text_enabled
        assert h.endpoint.requests == []
    finally:
        old.client.close()
        first.client.close()
        second.client.close()
        if later_old is not None:
            later_old.client.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_agent_cannot_write_reserved_memory_source_while_original_metrics_behavior_is_unchanged(memory_conversations, enabled):
    h, control, human = memory_conversations
    if enabled:
        enable_memory(h)
    turn = open_turn(h, control, human, "reserved-memory-slot")
    try:
        record = control.records[turn.binding.binding_id]
        source_id, _ = memory_slot_ids(record.admission_id)
        path = "/behavior/v1/bindings/" + turn.binding.binding_id
        command = wire.SourcePrepare(request_id="agent-memory-write", version=1, original=METRICS)
        guard = h.store._guard()
        refused = h.client.post(path + "/sources/" + source_id + "/prepare",
                                content=command.model_dump_json(), headers=h.headers("agent"))
        assert refused.status_code == 403 and h.store._guard() == guard
        original_source = record.profile.source.source_id
        prepared = h.client.post(path + "/sources/" + original_source + "/prepare",
            content=wire.SourcePrepare(request_id="original-metrics-write", version=2, original=METRICS).model_dump_json(),
            headers=h.headers("agent"))
        assert prepared.status_code == 200, prepared.text
        committed = h.client.post(path + "/sources/" + original_source + "/commit",
            content=wire.Command(request_id="original-metrics-write").model_dump_json(), headers=h.headers("agent"))
        assert committed.status_code == 403
        fetched = h.client.get(path + "/sources/" + original_source + "/versions/1", headers=h.headers("agent"))
        assert fetched.status_code == 200 and fetched.content == METRICS
        assert h.endpoint.requests == []
    finally:
        turn.client.close()
