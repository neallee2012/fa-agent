"""Real candidate routes behind the Hosted factory, with no SDK process or live service."""

import asyncio
from types import SimpleNamespace

import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure
from shared_context.memory_candidate_capability import ENTRY_ID, memory_candidate_capability
from shared_context.memory_candidate_wire import CandidateReceipt
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_memory_candidates import candidate_turn


@pytest.mark.parametrize("lose_response", [False, True])
def test_factory_uses_the_original_candidate_once_and_types_the_result(
        candidate_turn, monkeypatch, lose_response):
    c = candidate_turn
    calls = []
    original = c.client._post

    def post(action, command, model, **options):
        calls.append(action)
        result = original(action, command, model, **options)
        if lose_response and action == "stage-memory-candidate":
            raise Problem(503, "ASSISTANT_API_UNKNOWN", "synthetic response loss after the original commit")
        return result

    monkeypatch.setattr(c.client, "_post", post)
    session = SimpleNamespace(client=c.client, _authority_lock=asyncio.Lock())
    grant = c.assignment.task.instruction.capabilities.grants[0]
    assert hosted.CAPABILITY_HANDLERS[ENTRY_ID] is memory_candidate_capability
    specs, models = memory_candidate_capability(session, grant)
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results=models)
    assert tuple(spec.name for spec in specs) == ("stage_memory_candidate",)
    packet = {"arguments": {"decision": "candidate", "quote": "original inventory", "reason": "durable_fact"}}

    async def run():
        if lose_response:
            with pytest.raises(ToolFailure) as unknown:
                await specs[0].handler(packet)
            assert unknown.value.public_code == "CAPABILITY_UNAVAILABLE"
            assert boundary.public_tool_failure(specs[0].name, unknown.value.public_code)
        first = await specs[0].handler(packet)
        second = await specs[0].handler({**packet, "tool_call_id": "duplicate-handler"})
        assert second == first
        receipt = CandidateReceipt.model_validate_json(boundary.tool_result(specs[0].name, first.text))
        assert receipt.binding_id == c.binding.binding_id and receipt.effective is False
        with pytest.raises(ToolFailure) as changed:
            await specs[0].handler({"arguments": {**packet["arguments"], "quote": "inventory"}})
        assert changed.value.public_code == "MEMORY_CANDIDATE_CONFLICT"

    asyncio.run(run())
    assert calls == ["stage-memory-candidate"] + (["memory-candidate-reconcile"] if lose_response else [])


def test_bad_arguments_never_reach_the_candidate_api(candidate_turn, monkeypatch):
    c = candidate_turn
    monkeypatch.setattr(c.client, "_post", lambda *_args, **_kwargs: pytest.fail("invalid arguments must not send"))
    session = SimpleNamespace(client=c.client, _authority_lock=asyncio.Lock())
    specs, _ = memory_candidate_capability(session, c.assignment.task.instruction.capabilities.grants[0])
    with pytest.raises(ToolFailure) as invalid:
        asyncio.run(specs[0].handler({"arguments": {
            "decision": "no_candidate", "reason": "sensitive", "quote": "not for retention",
        }}))
    assert invalid.value.public_code == "CAPABILITY_ARGUMENTS"
