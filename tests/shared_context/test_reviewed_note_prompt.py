"""The memory superset preserves published history/draft prompts and cannot grant its own scope."""

import hashlib
from types import SimpleNamespace

import pytest

from shared_context.behavior_history_skill_draft_prompt import HISTORY_SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from assistant_api_helpers import assistant_api
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from direct_submit_helpers import submit_direct


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


def test_a_template_alone_cannot_select_the_memory_superset(assistant_api):
    h, _, _ = configured(assistant_api)
    guard = h.store._guard()
    response, _ = select(h, value=template(profile=PROFILE, user_text_file_profile="fa-user-text-files-v1"))
    assert response.status_code == 422
    assert h.store._guard() == guard


def test_draft_authority_requires_an_independent_memory_capture_after_base_selection(assistant_api):
    from shared_context.behavior_skill_draft_source import _profile_authorized

    h, control, _ = configured(assistant_api)
    response, _ = select(h, value=template(profile=HISTORY_SKILL_DRAFT_PROFILE,
                                         user_text_file_profile="fa-user-text-files-v1"))
    assert response.status_code == 200
    identifier = response.json()["result"]["selection_id"]
    submitted, _ = submit_direct(h, "original-history-draft-work", "Save a draft only when I explicitly ask.")
    control.refresh()
    registered = control.records[submitted["binding_id"]]
    assert registered.profile_selection_id == identifier
    guard = h.store._guard()
    assert _profile_authorized(control, registered, control.installed(), HISTORY_SKILL_DRAFT_PROFILE)
    with pytest.raises(Problem) as refused:
        _profile_authorized(control, registered, control.installed(), PROFILE)
    assert refused.value.body["error"]["code"] == "MEMORY_RECALL_DISABLED"
    assert h.store._guard() == guard


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
