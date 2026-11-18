"""User-provided procedure skill drafts: local signed API and SDK-I/O doubles."""

import asyncio
import copy
import hashlib
import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import A
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_skill_draft import (
    SaveSkillDraftCommand,
    SkillDraftOutcome,
    SkillDraftReceipt,
    SkillDraftToolArgs,
)
from shared_context.behavior_skill_draft_prompt import (
    SKILL_DRAFT_CAPABILITIES, SKILL_DRAFT_PROMPT, assemble_skill_draft_prompt,
)
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import signing_key
from agent_runtime_routing_helpers import agent_headers
from test_agent_runtime import peer_runtime
from test_conversation_transfer_startup import synthetic_standing_v1
from test_native_agent_api import relay_case
from test_native_skill_draft_api import draft_case
from skill_draft_helpers import PURPOSE, BODY, OLD_PROMPTS
from test_m3_auth import jwk_client


def post_draft(turn, purpose=PURPOSE, body=BODY, **extra):
    return turn.h.client.post(
        f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/save-skill-draft",
        headers={**turn.h.headers("agent"), "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()},
        json={"request_id": "save-skill-draft", "purpose": purpose, "body": body, **extra},
    )


def test_prompt_profile_and_old_prefix_lock_values_and_gateway_boundary():
    raw = SKILL_DRAFT_PROMPT.encode("utf-8")
    assert len(raw) == 2638
    assert hashlib.sha256(raw).hexdigest() == "f47c84ffb238af98c9082d7e060f622052f70bc2bb796e8c845964c7fd508ad2"
    assert "Organize only the procedure written in this turn's user message" in SKILL_DRAFT_PROMPT
    assert assemble_skill_draft_prompt().record.declared_capabilities == SKILL_DRAFT_CAPABILITIES
    with pytest.raises(ValueError):
        assemble_skill_draft_prompt(capabilities=("read_context", "execute_operation", "save_result"))

    for profile, size, sha, capabilities in OLD_PROMPTS:
        assembled = assemble_prompt(profile)
        data = assembled.system_message.encode("utf-8")
        assert (len(data), hashlib.sha256(data).hexdigest()) == (size, sha)
        assert (() if assembled.record is None else assembled.record.declared_capabilities) == capabilities
        with pytest.raises(ValueError):
            assemble_prompt(profile, capabilities=(*capabilities, "save_skill_draft"))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions", object())
    body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Save."}],
            "tools": [{"type": "function", "function": {"name": "save_skill_draft", "parameters": {}}}]}
    with pytest.raises(Problem) as denied:
        gateway.validate(body, text_reply=True, model="gpt-4.1-mini")
    assert denied.value.body["error"]["code"] == "ASSISTANT_TOOLS_DENIED"
    gateway.validate(body, text_reply=True, model="gpt-4.1-mini", skill_draft_access=True)


@pytest.mark.parametrize("payload", [
    {"purpose": "bad\x01purpose", "body": BODY},
    {"purpose": "x" * 513, "body": BODY},
    {"purpose": PURPOSE, "body": " \r\n\t "},
    {"purpose": PURPOSE, "body": "A" * 8193},
    {"purpose": PURPOSE, "body": "hidden\x00body"},
    {"purpose": PURPOSE, "body": BODY, "extra": "field"},
])
def test_invalid_purpose_body_or_extra_field_is_refused_before_any_write(request, signing_key, payload):
    from shared_context.native_skill_draft_api import PREFIX, install_native_skill_draft_routes
    # Event loop first: the startup fixture forbids the socket pair a new loop opens.
    loop = asyncio.new_event_loop()
    try:
        case = request.getfixturevalue("draft_case")
        app = FastAPI()
        _install_error_handlers(app)
        install_native_skill_draft_routes(app, case.base.a, enabled=True)
        agent = agent_headers(case.base.c, signing_key, 0)
        scope = {"conversation_id": case.sender.conversation_id, "request_id": case.sender.request_id}
        service = case.base.a.assistant.authority.artifacts
        before = copy.deepcopy(service.repository.data)
        with patch.object(service, "create_native_skill", wraps=service.create_native_skill) as create,                 TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
            refused = client.post(PREFIX + "/save", json={**scope, "arguments": payload}, headers=agent)
            assert refused.status_code == 422 and refused.json()["error"]["code"] == "BEHAVIOR_INVALID_REQUEST"
            assert payload["body"] not in refused.text
            assert service.repository.data == before and create.call_count == 0
            saved = client.post(PREFIX + "/save", json={
                **scope, "arguments": case.arguments.model_dump(mode="json")}, headers=agent)
            assert saved.status_code == 200, saved.text
            assert create.call_count == 1
    finally:
        loop.close()


def test_tool_argument_and_receipt_models_are_strict():
    SkillDraftToolArgs(purpose=PURPOSE, body=BODY)
    SaveSkillDraftCommand(request_id="save-skill-draft", purpose=PURPOSE, body=BODY)
    outcome = SkillDraftOutcome(schema_version="fa-skill-draft-outcome-v1", receipt_id="r",
                                intent_sha256="f" * 64, stage="create_unknown")
    assert outcome.known_effects == () and outcome.unresolved_operations == ("r",)
    with pytest.raises(ValueError):
        SkillDraftToolArgs.model_validate({"purpose": PURPOSE, "body": BODY, "target": "x"})
    with pytest.raises(ValueError):
        SkillDraftReceipt.model_validate({"schema_version": "fa-skill-draft-receipt-v1", "approved": True})
