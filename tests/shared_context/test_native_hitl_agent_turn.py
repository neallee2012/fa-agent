"""Neal 2026-11-19: a same-Team Agent's handoff turn is reviewed by the rules alone; a schedule still asks."""

import asyncio
import json

import pytest

from shared_context.behavior_native_turn_wire import NativeAgentOrigin, NativeTurnAddress
from shared_context.native_hitl_runtime import NativeAutoReview, NativeHitlRuntime
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import change, hitl, post
from test_native_hitl_h2 import human_decide, model_sender, pending_view, permit, setup_gate, verdict
from test_native_routine_hitl import routine_gate


def agent_gate(hitl, tmp_path, outcome="allow", *, takeover=False):
    """Rules already added; the offline model answers ``outcome`` naming every current rule."""
    case = setup_gate(hitl, tmp_path)
    ids = [rule.rule_id for rule in asyncio.run(case.client.rules()).rules]
    case.sender = model_sender([verdict(outcome, ids, takeover=takeover)])
    address = NativeTurnAddress(agent_id="native-b", conversation_id="cid-b", request_id="rid-b")
    origin = NativeAgentOrigin(kind="agent_request", message_kind="request", message_id="msg-one", sender=address,
                               recipient_agent_id="native-a", root_id="root-one", root=address, hop=1)
    case.context.receipt = case.runtime.receipt = case.context.receipt.model_copy(update={"source": origin})
    case.gate = NativeHitlRuntime(case.context, case.client, NativeAutoReview(case.sender),
                                  clock=lambda: hitl.now, poll_seconds=0.005)
    case.context.hitl = case.gate
    return case


def add_rule(hitl, request_id, text, behavior):
    assert post(hitl, "/rules", change(hitl, request_id, text=text, behavior=behavior)).status_code == 200


def last_review(case):
    return case.gate.calls["call-one"].claim.record.hitl.reviews[-1]


def test_allow_rule_lets_an_agent_turn_run_without_a_card(hitl, tmp_path):
    add_rule(hitl, "allow-click", "Allow synthetic echo commands.", "allow_automatically")
    case = agent_gate(hitl, tmp_path)

    async def check():
        assert await asyncio.wait_for(permit(case), 10) == (True, None)
        payload = json.loads(case.sender.observed[0]["input"][0]["content"])
        assert payload["verified_human_request"] is None and payload["request_origin"] == "team_agent"
        assert last_review(case).source == "rules" and last_review(case).outcome == "allow"
        await case.gate.close()
    asyncio.run(check())


def test_agent_turn_without_a_matching_rule_asks(hitl, tmp_path):
    case = agent_gate(hitl, tmp_path, "ask")

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        assert view.pending.command.review.outcome == "ask" and len(case.sender.observed) == 1
        human_decide(case, view, "deny")
        assert (await waiting)[0] is False
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("matched", ["none", "ask_first_only"])
def test_model_allow_without_a_matched_allow_rule_is_turned_into_ask(hitl, tmp_path, matched):
    if matched == "ask_first_only":
        add_rule(hitl, "ask-echo", "Ask before echo commands.", "ask_first")
    case = agent_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        review = view.pending.command.review
        assert review.outcome == "ask" and not waiting.done()
        assert review.reason == ("reviewed" if matched == "none" else "ask_rule")
        human_decide(case, view, "deny")
        await waiting
        await case.gate.close()
    asyncio.run(check())


def test_ask_first_wins_over_allow_and_takeover_still_denies(hitl, tmp_path):
    add_rule(hitl, "allow-echo", "Allow echo commands.", "allow_automatically")
    add_rule(hitl, "ask-echo", "Ask before echo commands.", "ask_first")
    case = agent_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        assert view.pending.command.review.reason == "ask_rule"
        human_decide(case, view, "deny")
        await waiting
        await case.gate.close()
    asyncio.run(check())
    taken = agent_gate(hitl, tmp_path / "t", "deny", takeover=True)

    async def deny():
        assert await asyncio.wait_for(permit(taken), 10) == (False, "NATIVE_HITL_TAKEOVER")
        await taken.gate.close()
    asyncio.run(deny())


def test_scheduled_turn_without_an_append_still_never_calls_the_model(hitl, tmp_path):
    case = routine_gate(hitl, tmp_path)
    case.gate.human_inputs = lambda: ()

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        assert view.pending.command.review.reason == "human_request_unavailable"
        assert case.sender.observed == []
        human_decide(case, view, "deny")
        await waiting
        await case.gate.close()
    asyncio.run(check())
