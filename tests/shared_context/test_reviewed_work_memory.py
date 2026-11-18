"""Signed reviewed-claim producers and original instruction read authority; no Memory API."""

import json
from types import MappingProxyType
from unittest.mock import patch

import pytest

from shared_context.behavior_assistant import AssistantProfile
from shared_context.errors import Problem
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client
from reviewed_work_memory_helpers import reviewed_note, NOTE, configured_memory, PROFILE
from instruction_flow_helpers import runtime_path
from test_m3_auth import signing_key


def request(turn, action="read-work-memory", **fields):
    return turn.h.client.post(
        f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/{action}",
        headers={**turn.h.headers("agent"), "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()},
        json={"request_id": "current-work-memory" if action == "work-memory-catalog" else "read-work-memory", **fields},
    )


@pytest.mark.parametrize("field,value", [
    ("schema_version", "fa-work-memory-prompt-assembly-v99"), ("profile", "fa-instruction-v1"),
    ("declared_capabilities", ["read_work_memory", "memory_publish"]), ("prefix_tokens", 0),
    ("prefix_sha256", "0" * 64), ("prefix_utf8_bytes", 1), ("source", "model-attested"),
    ("system_message", "PRIVATE_PROMPT"),
])
def test_memory_prompt_record_is_exact_versioned_metadata_not_user_authority(field, value):
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.behavior_work_memory_prompt import WorkMemoryPromptAssemblyRecord
    prompt = assemble_prompt(PROFILE)
    assert prompt.record.prefix_sha256 == "1add3188623d9503311053721a13d931fa47c229e83b991f36b0abc7c97586f3"
    assert prompt.record.prefix_utf8_bytes == 2719 and NOTE not in prompt.system_message
    candidate = {**prompt.record.model_dump(mode="json"), field: value}
    with pytest.raises(ValueError):
        WorkMemoryPromptAssemblyRecord.model_validate_json(json.dumps(candidate))

