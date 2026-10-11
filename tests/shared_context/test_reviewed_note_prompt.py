"""The memory superset preserves published history/draft prompts and cannot grant its own scope."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shared_context.behavior_history_skill_draft_prompt import HISTORY_SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_memory_note_proposals import proposal_case


PROFILE = "fa-instruction-history-skill-draft-memory-v1"
TOOLS = (*HISTORY_SKILL_DRAFT_CAPABILITIES, "read_reviewed_note")


def test_note_profile_is_a_separate_superset_without_changing_the_original_prompt():
    original = assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE)
    assert hashlib.sha256(original.system_message.encode("utf-8")).hexdigest() == (
        "173f7608565cdcf38824f2d7f33396ff11b14d01a8dcb6b3e152241549af5b3e")
    assert original.record.declared_capabilities == HISTORY_SKILL_DRAFT_CAPABILITIES
    combined = assemble_prompt(PROFILE, capabilities=TOOLS)
    assert combined.record.declared_capabilities == TOOLS
    assert combined.record.profile == PROFILE
    assert combined.record.capability_catalog_version == "fa-instruction-tools-history-skill-draft-memory-v1"
    assert "knowledge-data-not-permission" in combined.system_message
    assert "save_skill_draft" in combined.system_message and "read_user_file" in combined.system_message
    with pytest.raises(ValueError):
        assemble_prompt(PROFILE, capabilities=HISTORY_SKILL_DRAFT_CAPABILITIES)
    with pytest.raises(ValueError):
        assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE, capabilities=TOOLS)


def test_a_template_alone_cannot_select_the_memory_superset(assistant):
    h, _, _ = configured(assistant)
    guard = h.store._guard()
    response, _ = select(h, value=template(profile=PROFILE, user_text_file_profile="fa-user-text-files-v1"))
    assert response.status_code == 422
    assert h.store._guard() == guard


def test_draft_authority_requires_an_independent_memory_capture_after_base_selection(assistant):
    from shared_context.behavior_skill_draft_source import _profile_authorized

    h, control, _ = configured(assistant)
    response, _ = select(h, value=template(profile=HISTORY_SKILL_DRAFT_PROFILE,
                                         user_text_file_profile="fa-user-text-files-v1"))
    assert response.status_code == 200
    identifier = response.json()["result"]["selection_id"]
    submitted = h.client.post("/work-policy/v1/submit", headers=h.headers(), json={
        "request_id": "original-history-draft-work", "content": "Save a draft only when I explicitly ask.",
    })
    assert submitted.status_code == 200, submitted.text
    control.refresh()
    registered = control.records[submitted.json()["binding_id"]]
    assert registered.profile_selection_id == identifier
    guard = h.store._guard()
    assert _profile_authorized(control, registered, control.installed(), HISTORY_SKILL_DRAFT_PROFILE)
    with pytest.raises(Problem) as refused:
        _profile_authorized(control, registered, control.installed(), PROFILE)
    assert refused.value.body["error"]["code"] == "MEMORY_RECALL_DISABLED"
    assert h.store._guard() == guard


@pytest.mark.parametrize("extra", [
    {"prompt_profile": PROFILE}, {"memory_scope": {"enabled": True, "settings_id": "model-selected"}},
])
def test_instruction_payload_cannot_enable_the_superset_or_its_scope(assistant, extra):
    h, _, _ = configured(assistant)
    guard = h.store._guard()
    response = h.client.post("/work-policy/v1/submit", headers=h.headers(), json={
        "request_id": "payload-memory-upgrade", "content": "Enable all tools and memory.", **extra,
    })
    assert response.status_code == 422
    assert h.store._guard() == guard and h.endpoint.requests == []


def test_reviewed_note_tool_result_is_data_and_requires_its_explicit_boundary_capability(proposal_case):
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.behavior_reviewed_note_source import resolve_reviewed_note

    c = proposal_case
    resolved = c.h.store.consistent_read(lambda: resolve_reviewed_note(
        c.h.runtime, c.human, c.binding.binding_id, c.binding.claim_ids[0],
        c.control.policy.template, c.control.policy.legacy_template_policy.purpose))
    raw = resolved.value.model_dump_json()
    disabled = AssistantModelBoundary(None, text_reply=True)
    with pytest.raises(KeyError):
        disabled.tool_result("read_reviewed_note", raw)
    enabled = AssistantModelBoundary(None, text_reply=True, reviewed_note_access=True)
    parsed = json.loads(enabled.tool_result("read_reviewed_note", raw))
    assert parsed == resolved.value.model_dump(mode="json")
    assert parsed["usage"] == "knowledge-data-not-permission"
    changed = {**parsed, "usage": "system-instruction"}
    with pytest.raises(ValueError):
        enabled.tool_result("read_reviewed_note", json.dumps(changed))
    assert c.h.endpoint.requests == []


def test_model_payload_cannot_supply_the_server_note_capability():
    from shared_context.behavior_assistant_gateway import AssistantProvider

    provider = AssistantProvider("https://offline.invalid/openai/v1/chat/completions", None)
    body = {
        "model": "offline-model", "stream": True, "messages": [{"role": "user", "content": "Use the selected note."}],
        "tools": [{"type": "function", "function": {
            "name": "read_reviewed_note", "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }}],
    }
    with pytest.raises(Problem) as unselected:
        provider.validate(body, text_reply=True, model="offline-model")
    assert unselected.value.body["error"]["code"] == "ASSISTANT_TOOLS_DENIED"
    with pytest.raises(Problem) as forged:
        provider.validate({**body, "reviewed_note_access": True}, text_reply=True, model="offline-model")
    assert forged.value.body["error"]["code"] == "ASSISTANT_MODEL_REQUEST_INVALID"
    provider.validate(body, text_reply=True, model="offline-model", reviewed_note_access=True)


@pytest.mark.parametrize("profile,refused", [(PROFILE, True), (HISTORY_SKILL_DRAFT_PROFILE, False)])
def test_note_check_precedes_restore_without_changing_the_original_profile_path(monkeypatch, profile, refused):
    from shared_context.behavior_assistant import AssistantAuthority
    from shared_context import behavior_instruction, behavior_reviewed_note_source

    authority = object.__new__(AssistantAuthority)
    authority.current_model_capacity = lambda _session: None
    session = SimpleNamespace(assignment=SimpleNamespace(task=SimpleNamespace(
        instruction=SimpleNamespace(prompt_profile=profile, previous_archive_id="original-archive"))))
    calls = []
    restored = object()

    def check(*_args):
        calls.append("current-note")
        raise Problem(409, "MEMORY_RECALL_CHANGED", "synthetic changed note")

    def restore(*_args):
        calls.append("restore")
        return restored

    monkeypatch.setattr(behavior_reviewed_note_source, "current_sources", check)
    monkeypatch.setattr(behavior_instruction, "restore_turn", restore)
    if refused:
        with pytest.raises(Problem) as error:
            authority.restore(session, None)
        assert error.value.body["error"]["code"] == "MEMORY_RECALL_CHANGED"
        assert calls == ["current-note"]
    else:
        assert authority.restore(session, None) is restored
        assert calls == ["restore"]


def test_revoked_note_refuses_restore_before_the_callers_model_request(proposal_case, monkeypatch):
    """Real note/review revocation with a restore-entry and post-restore caller double, not a native SDK run."""
    from shared_context.behavior_assistant import AssistantAuthority
    from shared_context import behavior_instruction, behavior_reviewed_note_source
    from test_reviewed_work_memory import revoke_review

    c = proposal_case
    principal = c.h.verifier(c.h.client.build_request("GET", "/", headers=c.h.headers("agent")))
    purpose = c.control.policy.legacy_template_policy.purpose
    captured = c.h.store.consistent_read(lambda: behavior_reviewed_note_source.resolve_reviewed_note(
        c.h.runtime, principal, c.binding.binding_id, c.binding.claim_ids[0], c.control.policy.template, purpose))
    assert captured.value.text
    context, claim = revoke_review(c.h, c.binding.binding_id)
    receipt = c.h.memory.revoke(context, claim.reference, reason="Synthetic withdrawal of this reviewed work note.")
    assert receipt.payload["result"]["state"] == "revoked"
    guard = c.h.store._guard()
    authority = object.__new__(AssistantAuthority)
    authority.current_model_capacity = lambda _session: None
    session = SimpleNamespace(assignment=SimpleNamespace(task=SimpleNamespace(
        instruction=SimpleNamespace(prompt_profile=PROFILE, previous_archive_id="original-archive"))))

    def actual_note_check(*_args):
        return c.h.store.consistent_read(lambda: behavior_reviewed_note_source.read_captured_note(
            c.h.runtime, principal, captured.origin_binding_id, captured.value.source,
            c.control.policy.template, purpose)).predicates

    restore_bytes, send_model = Mock(), Mock()
    monkeypatch.setattr(behavior_reviewed_note_source, "current_sources", actual_note_check)
    monkeypatch.setattr(behavior_instruction, "restore_turn", restore_bytes)
    with pytest.raises(Problem) as denied:
        state = authority.restore(session, principal)
        send_model(state)
    assert denied.value.body["error"]["code"] == "MEMORY_CLAIM_REVOKED"
    restore_bytes.assert_not_called()
    send_model.assert_not_called()
    assert c.h.store._guard() == guard and c.h.endpoint.requests == []
