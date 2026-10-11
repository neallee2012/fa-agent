"""A routine's original text is not human approval; observed owner appends remain human input."""

import asyncio
from datetime import timedelta
import hashlib
import json

from shared_context.behavior_native_turn_wire import NativeAgentCommand, NativeReceipt, NativeRoutineSubmit
from shared_context.native_hitl_runtime import NativeAutoReview, NativeHitlRuntime
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import hitl
from test_native_hitl_h2 import human_decide, pending_view, permit, setup_gate
from test_native_routine_wire import routine_origin


def routine_gate(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)
    original = case.context.receipt
    source = routine_origin().model_copy(update={
        "agent_id": original.agent_id, "conversation_id": original.conversation_id,
        "content_sha256": hashlib.sha256(original.command.content.encode()).hexdigest()})
    command = NativeAgentCommand(
        request_id=source.request_id, conversation_id=source.conversation_id, content=original.command.content)
    submitted = NativeRoutineSubmit(command=command, capture=original.capture, routine=source)
    receipt = NativeReceipt(
        agent_id=original.agent_id, hosted_session_id=original.hosted_session_id, request_id=source.request_id,
        conversation_id=source.conversation_id, command=command, capture=original.capture, routine=source,
        submit_digest=submitted.sha256, capture_digest=original.capture_digest, status="started", turn_number=1)
    case.runtime.receipt = case.context.receipt = receipt
    case.gate = NativeHitlRuntime(
        case.context, case.client, NativeAutoReview(case.sender), clock=lambda: hitl.now, poll_seconds=0.005,
        human_inputs=lambda: ("Owner append: only read the synthetic input.",))
    case.context.hitl = case.gate
    return case


def test_routine_review_sees_only_the_observed_owner_append_and_still_waits_for_a_decision(hitl, tmp_path):
    case = routine_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        assert len(case.sender.observed) == 1
        payload = json.loads(case.sender.observed[0]["input"][0]["content"])
        assert payload["verified_human_request"] == {
            "initial": None, "appended": ["Owner append: only read the synthetic input."]}
        assert not waiting.done() and view.decision is None
        human_decide(case, view, "deny")
        assert not (await waiting)[0]
        await case.gate.close()

    asyncio.run(check())


def test_a_scheduled_turn_card_keeps_the_background_expiry_and_never_runs_after_it(hitl, tmp_path):
    case = routine_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        assert view.pending.command.wait == "background"
        assert view.pending.expires_at == view.pending.created_at + timedelta(seconds=600)
        hitl.now += timedelta(seconds=601)
        assert await waiting == (False, "NATIVE_HITL_EXPIRED")
        assert (await case.client.read(view.approval.pending_id)).status == "expired"
        await case.gate.close()

    asyncio.run(check())
