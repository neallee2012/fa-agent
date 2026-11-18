"""The memory superset preserves published history/draft prompts and cannot grant its own scope."""

import hashlib
from types import SimpleNamespace

import pytest

from shared_context.behavior_history_skill_draft_prompt import HISTORY_SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.errors import Problem
from assistant_api_helpers import assistant_api
from assistant_api_helpers import assistant_api
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


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

