"""H2 offline seams: real JWT/control CAS/NativeEffects, synthetic model, tools and SDK I/O."""

import asyncio
import base64
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import random
import struct
from types import SimpleNamespace
import zlib

import httpx
import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ServiceRequestError
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from conftest import A, TENANT
from shared_context import native_sdk
from shared_context.behavior_model_direct import DirectCompletions, DirectResponses
from shared_context.behavior_native_turn_wire import NativeReceipt, NativeSubmit, material_digest
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure, ToolOutput
from shared_context.native_capabilities import NativeCapabilityContext, guard_native_tool
from shared_context.native_hitl import NativeHitlService, hitl_key, validate_native_hitl_history
from shared_context.native_hitl_runtime import NativeAutoReview, NativeHitlClient, NativeHitlRuntime
from shared_context.native_hitl_wire import (
    NativeHitlAction, NativeHitlApprovalReference, NativeHitlAuthorize, NativeHitlCall,
    NativeHitlDecision, NativeHitlEnd, NativeHitlPendingPage, NativeHitlPendingQuery,
    NativeHitlReadback, NativeHitlReview, NativeHitlRule, NativeHitlRules, NativeHitlTurn,
)
from shared_context.native_turns import NativeTurnControl
from test_instruction_tool_feedback import run_callbacks
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import hitl, change, decision, error, fail_apply, inventory, post, proposal, read, create_pending
from test_native_capabilities import selected_capture
from test_native_turn_wire import submit
from test_native_turns import IDENTITY, local_env
from test_native_responses import responses, response_submit

MODEL_ROOT = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"
SID = "offline-instruction-feedback"


class EffectStore:
    """The StateStore ETag shape only, not a Hosted recovery claim."""

    def __init__(self):
        self.items, self.sequence = {}, 0
        self.lose_after_set = False
        self.fail_set = False

    async def create_item(self, key, value):
        if key in self.items:
            raise ResourceExistsError("OFFLINE existing effect")
        self.sequence += 1
        self.items[key] = SimpleNamespace(value=copy.deepcopy(value), etag=str(self.sequence))

    async def get_item(self, key):
        return copy.deepcopy(self.items.get(key))

    async def set_item(self, key, value, *, if_match):
        if self.fail_set:
            raise ServiceRequestError("OFFLINE no ACK")
        if self.items[key].etag != if_match:
            raise ResourceModifiedError("OFFLINE ETag")
        self.sequence += 1
        self.items[key] = SimpleNamespace(value=copy.deepcopy(value), etag=str(self.sequence))
        if self.lose_after_set:
            raise ServiceRequestError("OFFLINE lost ACK after write")


class EffectRuntime:
    def __init__(self, receipt):
        self.receipt, self.store = receipt, EffectStore()
        self.identity = SimpleNamespace(agent_id=receipt.agent_id)

    async def read(self, rid):
        assert rid == self.receipt.request_id
        return self.receipt

    @asynccontextmanager
    async def _store(self, name):
        assert name == "fa/v1/effects"
        yield self.store


def api_client(c):
    writes = []

    def api(request):
        writes.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        result = c.client.request(request.method, request.url.path, content=request.content, headers=request.headers)
        return httpx.Response(result.status_code, content=result.content, headers=result.headers)
    client = NativeHitlClient("https://api.offline.invalid", lambda: c.headers(c.agent)["Authorization"][7:],
                              transport=httpx.MockTransport(api))
    client.observed = writes
    return client


def model_sender(answers, *, responses=True, reasoning=False):
    requests = []
    answer_list = list(answers)

    def model(request):
        value = json.loads(request.content)
        requests.append(value)
        assert "tools" not in value and "tool_choice" not in value
        answer = answer_list.pop(0) if len(answer_list) > 1 else answer_list[0]
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": {"code": "OFFLINE-model-failure"}})
        text = answer if isinstance(answer, str) else json.dumps(answer)
        if responses:
            event = {"type": "response.completed", "response": {
                "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 10, "output_tokens": 3}}}
            if reasoning:
                event["response"]["output"].insert(0, {"type": "reasoning", "summary": []})
            raw = "data: " + json.dumps(event) + "\n\n"
        else:
            event = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
            raw = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())

    cls = DirectResponses if responses else DirectCompletions
    sender = cls(MODEL_ROOT + ("/responses" if responses else "/chat/completions"),
                 lambda: "OFFLINE model credential", "OFFLINE-model", transport=httpx.MockTransport(model))
    sender.observed = requests
    return sender


def verdict(outcome="ask", matched=(), *, uncertain=False, takeover=False):
    return {"outcome": outcome, "matched_rule_ids": list(matched), "uncertain": uncertain, "takeover_required": takeover}


def setup_gate(c, tmp_path, answers=(None,), *, entries=("fa-shell-v1",), responses=True):
    command = submit("rid-one", text="Please handle the synthetic work.").command
    if "fa-desktop-v1" in entries:
        from test_native_local_capabilities import with_grants
        capture = with_grants(*entries)
    else:
        capture = selected_capture(*entries)
    submitted = NativeSubmit(command=command, capture=capture)
    receipt = NativeReceipt(
        agent_id="native-a", hosted_session_id="hosted-one", conversation_id="cid-one", request_id="rid-one",
        command=command, capture=capture, submit_digest=submitted.sha256, capture_digest=capture.sha256,
        status="started", turn_number=1)
    runtime, control = EffectRuntime(receipt), NativeTurnControl()
    sender = model_sender([verdict() if item is None else item for item in answers], responses=responses)
    context = NativeCapabilityContext(
        receipt=receipt, scope=SimpleNamespace(sdk_session_id=SID, agent_id="native-a", conversation_id="cid-one"),
        control=control, root=tmp_path / "native-turns", turn_root=tmp_path / "native-turns" / "rid-one",
        runtime=runtime, model=sender, api_url="https://api.offline.invalid",
        api_token_provider=lambda: c.headers(c.agent)["Authorization"][7:], model_token_provider=lambda: "OFFLINE")
    client = api_client(c)
    gate = NativeHitlRuntime(context, client, NativeAutoReview(sender), clock=lambda: c.now, poll_seconds=0.005)
    context.hitl = gate
    packet = {"session_id": SID, "tool_call_id": "call-one", "tool_name": "run_command",
              "arguments": {"command": "echo synthetic"}}
    return SimpleNamespace(c=c, runtime=runtime, context=context, client=client, gate=gate, sender=sender, packet=packet)


def permission_request(packet, *, kind="custom-tool", server=None):
    return SimpleNamespace(kind=kind, tool_name=packet["tool_name"], tool_call_id=packet["tool_call_id"],
                           managed_approval_required=False, args=packet["arguments"], server_name=server)


async def permit(case, packet=None, **kwargs):
    packet = case.packet if packet is None else packet
    return await case.gate.permission(permission_request(packet, **kwargs), {"session_id": packet["session_id"]})


async def pending_view(case):
    for _ in range(100):
        page = await case.client.pending(NativeHitlPendingQuery(turn=case.gate.turn))
        if page.references:
            return await case.client.read(page.references[-1].pending_id)
        await asyncio.sleep(0.005)
    pytest.fail("HITL did not retain an original pending reference")


def human_decide(case, view, choice="allow_once", request_id="human-one"):
    original = {"pending_id": view.approval.pending_id, "pending_digest": view.approval.pending_digest}
    command = decision(case.c, original, request_id=request_id, choice=choice)
    response = post(case.c, "/decisions", command)
    assert response.status_code == 200, response.text
    return command


@pytest.mark.parametrize("responses", [True, False])
@pytest.mark.parametrize("answer,outcome,reason", [
    (verdict("allow"), "allow", "reviewed"), (verdict("deny"), "deny", "reviewed"),
    (verdict("ask"), "ask", "reviewed"), (503, "ask", "review_unavailable"),
    ('not JSON', "ask", "review_invalid"), (verdict("allow", ["invented"]), "ask", "review_invalid"),
    (verdict("allow", uncertain=True), "ask", "review_uncertain"),
    (verdict("allow", takeover=True, uncertain=True), "deny", "human_takeover"),
])
def test_independent_review_has_strict_output_no_tools_and_retains_usage(hitl, tmp_path, responses, answer, outcome, reason):
    case = setup_gate(hitl, tmp_path, (answer,), responses=responses)

    async def check():
        action = case.gate._action(case.packet)
        result = await case.gate.reviewer(action, await case.client.rules(), {"initial": "Original human request"})
        assert (result.outcome, result.reason) == (outcome, reason)
        assert len(case.sender.observed) == 1
        usage = case.sender.usage().usage[0]
        assert usage.requests == 1
        assert usage.measured_requests == (0 if answer == 503 else 1)
        assert result.takeover_required is (reason == "human_takeover")
    asyncio.run(check())


def test_review_uses_twenty_latest_rules_and_ask_wins(hitl, tmp_path):
    c = hitl
    for number in range(20):
        command = change(c, f"rule-{number}", behavior="ask_first" if number == 19 else "allow_automatically")
        assert post(c, "/rules", command).status_code == 200
    rules = c.service.rules(c.owner)
    case = setup_gate(c, tmp_path, (verdict("allow", (rules.rules[0].rule_id, rules.rules[-1].rule_id)),))

    async def check():
        result = await case.gate.reviewer(case.gate._action(case.packet), await case.client.rules(), "Original request")
        assert result.outcome == "ask" and result.reason == "ask_rule" and result.rules_revision == 20
        sent = json.loads(case.sender.observed[0]["input"][0]["content"])
        assert len(sent["rules"]) == 20 and sent["rules_revision"] == 20
        assert sent["action"]["arguments"] == case.packet["arguments"]
    asyncio.run(check())


@pytest.mark.parametrize("answer,outcome,reason", [
    # SP16-04 (7b3f): a matched Ask-first rule always stops for the human, even when the reviewer would deny.
    (lambda ids: verdict("deny", ids), "ask", "ask_rule"),
    # Takeover steps still go to the human VM viewer, never to a once card.
    (lambda ids: verdict("deny", ids, takeover=True), "deny", "human_takeover"),
    # Without a matched Ask-first rule, the reviewer's own deny stands.
    (lambda ids: verdict("deny", ids[1:]), "deny", "reviewed"),
])
def test_a_matched_ask_first_rule_turns_a_reviewer_deny_into_a_human_card(hitl, tmp_path, answer, outcome, reason):
    c = hitl
    for number, behavior in enumerate(("ask_first", "allow_automatically")):
        assert post(c, "/rules", change(c, f"sp16-rule-{number}", behavior=behavior)).status_code == 200
    ids = tuple(rule.rule_id for rule in c.service.rules(c.owner).rules)
    case = setup_gate(c, tmp_path, (answer(ids),))

    async def check():
        result = await case.gate.reviewer(case.gate._action(case.packet), await case.client.rules(),
                                          {"initial": "Original request"})
        assert (result.outcome, result.reason) == (outcome, reason)
        if outcome == "ask":
            # The gate keeps it waiting for a human instead of refusing it.
            waiting = asyncio.create_task(permit(case))
            view = await pending_view(case)
            assert not waiting.done() and view.pending.command.review.outcome == "ask"
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    asyncio.run(check())


def test_the_gate_also_asks_when_any_reviewer_denies_a_matched_ask_first_rule(hitl, tmp_path):
    """The gate's own check (any reviewer implementation) keeps the same rule; takeover still refuses."""
    c = hitl
    assert post(c, "/rules", change(c, "sp16-ask", behavior="ask_first")).status_code == 200
    rules = c.service.rules(c.owner)
    case = setup_gate(c, tmp_path)

    async def deny(action, current, human_request, *, takeover=False):
        return NativeHitlReview(rules_revision=current.revision, matched_rule_ids=(rules.rules[0].rule_id,),
                                source="rules", outcome="deny", takeover_required=takeover,
                                reason="human_takeover" if takeover else "reviewed")

    async def check():
        case.gate.reviewer = deny
        call = await case.gate._call(case.gate._action(case.packet))
        _, review = await case.gate._review(call)
        assert (review.outcome, review.reason) == ("ask", "ask_rule")

        async def takeover(*args):
            return await deny(*args, takeover=True)
        case.gate.reviewer = takeover
        other = await case.gate._call(case.gate._action({**case.packet, "tool_call_id": "call-two"}))
        _, review = await case.gate._review(other)
        assert (review.outcome, review.reason, review.takeover_required) == ("deny", "human_takeover", True)
    asyncio.run(check())


def test_original_await_pending_reader_and_provider_share_one_effect(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)
    ran = []

    async def tool(packet):
        ran.append(copy.deepcopy(packet))
        return ToolOutput("original result")

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        assert not waiting.done() and not ran
        # A new reader can discover it using only the original native address.
        reopened = api_client(hitl)
        page = await reopened.pending(NativeHitlPendingQuery(turn=case.gate.turn))
        assert page.attention[0].approval == view.approval
        chosen = human_decide(case, view)
        assert await waiting == (True, None)
        assert await permit(case) == (False, "NATIVE_HITL_ALREADY_AUTHORIZED")
        assert (await case.client.read(view.approval.pending_id)).status == "allow"
        first = await case.gate.invoke(case.packet, tool)
        assert first.text == "original result"
        with pytest.raises(ToolFailure):
            await case.gate.invoke(case.packet, tool)
        assert len(ran) == 1 and len(case.runtime.store.items) == 1
        saved = (await case.context.effects.read("call-one", case.gate.calls["call-one"].material))
        assert saved.status == "done" and saved.result["text"] == "original result"
        current = await case.client.read(view.approval.pending_id)
        assert current.decision == chosen and current.authorization.effect_token == saved.token
        assert current.status == "allow" and current.grant.uses == 1
        assert len(case.sender.observed) == 1
        await case.gate.close()
        final = await reopened.pending(NativeHitlPendingQuery(turn=case.gate.turn))
        assert final.closed and not final.attention
    asyncio.run(check())


@pytest.mark.parametrize("finish", ["deny", "expire", "cancel"])
def test_deny_expiry_and_cancel_never_execute_or_recreate_wait(hitl, tmp_path, finish):
    case = setup_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        if finish == "deny":
            human_decide(case, view, "deny")
        elif finish == "expire":
            hitl.now += timedelta(days=1, seconds=1)
        else:
            case.context.control.cancelled = True
        result = await waiting
        assert not result[0]
        with pytest.raises(ToolFailure):
            await case.gate.invoke(case.packet, lambda _: pytest.fail("denied effect executed"))
        state = await case.client.read(view.approval.pending_id)
        assert state.status == {"deny": "deny", "expire": "expired", "cancel": "interrupted"}[finish]
        assert state.authorization is None
        await case.gate.close()
    asyncio.run(check())


def test_owner_turn_card_waits_past_the_background_ttl_and_polls_slower_after_a_minute(hitl, tmp_path, monkeypatch):
    case = setup_gate(hitl, tmp_path)
    case.gate = NativeHitlRuntime(case.context, case.client, case.gate.reviewer, clock=lambda: hitl.now,
                                  poll_seconds=0.001, slow_poll_seconds=0.002)
    case.context.hitl = case.gate
    real_sleep, delays = asyncio.sleep, []

    async def sleep(delay, *args):
        if delay in (0.001, 0.002):
            delays.append(delay)
            hitl.now += timedelta(seconds=20)
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def check():
        waiting = asyncio.create_task(permit(case))
        while len(delays) < 40 and not waiting.done():
            await real_sleep(0)
        assert not waiting.done()
        view = await pending_view(case)
        assert view.pending.command.wait == "interactive" and view.status == "pending" and not view.expired
        assert delays[:5] == [0.001, 0.001, 0.001, 0.002, 0.002]
        human_decide(case, view)
        assert await waiting == (True, None)
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("choice", ["allow_once", "allow_rule"])
def test_rules_changed_after_once_reassess_but_never_ask_forever_or_mint_second_grant(hitl, tmp_path, choice):
    case = setup_gate(hitl, tmp_path, (verdict("ask"), verdict("ask"), verdict("ask")))

    async def check():
        waiting = asyncio.create_task(permit(case))
        original = await pending_view(case)
        human_decide(case, original, choice)
        assert (await waiting)[0]
        assert post(hitl, "/rules", change(hitl, "later-rule")).status_code == 200
        calls = []

        async def tool(_):
            calls.append("effect")
            return ToolOutput("done once")
        assert (await case.gate.invoke(case.packet, tool)).text == "done once"
        view = await case.client.read(original.approval.pending_id)
        assert view.pending == original.pending
        assert view.authorization.review.rules_revision == hitl.service.rules(hitl.owner).revision
        assert calls == ["effect"]
        assert len([row for row in inventory(hitl).rows.values()
                    if row.payload["data"].get("kind") == "native_hitl_once_grant"]) == 1
        assert len([body for _, path, body in case.client.observed if path.endswith("/pending")]) == 1
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("takeover", [False, True])
def test_latest_deny_or_takeover_wins_over_human_once(hitl, tmp_path, takeover):
    case = setup_gate(hitl, tmp_path, (verdict("ask"), verdict("deny", takeover=takeover)))

    async def check():
        waiting = asyncio.create_task(permit(case))
        original = await pending_view(case)
        command = human_decide(case, original)
        assert (await waiting)[0]
        assert post(hitl, "/rules", change(hitl, "changed-before-provider")).status_code == 200
        with pytest.raises(ToolFailure):
            await case.gate.invoke(case.packet, lambda _: pytest.fail("latest deny was bypassed"))
        view = await case.client.read(original.approval.pending_id)
        assert view.decision == command and view.status == "interrupted"
        assert view.authorization is None
        assert case.gate.calls["call-one"].claim.record.hitl.reviews[-1].outcome == "deny"
        await case.gate.close()
    asyncio.run(check())


def test_grant_cannot_change_digest_and_parallel_provider_calls_do_not_double_send(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path, (verdict("allow"),))
    calls = []

    async def tool(_):
        calls.append("sent")
        await asyncio.sleep(0.02)
        return ToolOutput("one result")

    async def check():
        assert (await permit(case))[0]
        different = {**case.packet, "arguments": {"command": "changed after approval"}}
        with pytest.raises(ToolFailure):
            await case.gate.invoke(different, tool)
        results = await asyncio.gather(case.gate.invoke(case.packet, tool), case.gate.invoke(case.packet, tool),
                                       return_exceptions=True)
        assert sum(isinstance(result, ToolOutput) for result in results) == 1
        assert sum(isinstance(result, ToolFailure) for result in results) == 1
        assert calls == ["sent"]
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("choice", ["allow_once", "allow_rule"])
def test_lost_owner_ack_is_only_read_back_and_grant_consumed_once(hitl, tmp_path, monkeypatch, choice):
    assert post(hitl, "/rules", change(hitl, "original-ask")).status_code == 200
    case = setup_gate(hitl, tmp_path)
    original_submit = hitl.table.submit_transaction
    count = 0

    def lost(*args, **kwargs):
        nonlocal count
        count += 1
        hitl.table.failure = "after" if count == 2 else None
        return original_submit(*args, **kwargs)

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        monkeypatch.setattr(hitl.table, "submit_transaction", lost)
        chosen = decision(hitl, {"pending_id": view.approval.pending_id, "pending_digest": view.approval.pending_digest},
                          choice=choice)
        error(post(hitl, "/decisions", chosen), 503)
        assert (await waiting)[0]
        async def tool(_):
            return ToolOutput("once")
        await case.gate.invoke(case.packet, tool)
        retained = await case.client.read(view.approval.pending_id)
        assert retained.decision == chosen and retained.grant.rules_revision == (2 if choice == "allow_rule" else 1)
        assert post(hitl, "/decisions/reconcile", chosen).status_code == 200
        assert len([row for row in inventory(hitl).rows.values()
                    if row.payload["data"].get("kind") == "native_hitl_authorization"]) == 1
        await case.gate.close()
    asyncio.run(check())


def test_restart_closes_old_pending_without_restoring_ram_or_a_second_model_request(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await NativeHitlRuntime.finish_turn(case.client, case.runtime, case.context.receipt)
        newer = NativeHitlRuntime(case.context, case.client, case.gate.reviewer, clock=lambda: hitl.now, poll_seconds=0.005)
        allowed = await newer.permission(permission_request(case.packet), {"session_id": SID})
        assert not allowed[0] and len(case.sender.observed) == 1
        retained = await case.client.read(view.approval.pending_id)
        assert retained.status == "interrupted" and retained.turn_closed
        error(post(hitl, "/decisions", decision(hitl, {
            "pending_id": view.approval.pending_id, "pending_digest": view.approval.pending_digest})), 409)
    asyncio.run(check())


def test_a_lost_end_apply_reaches_the_terminal_task_and_its_rerun_closes_the_card(hitl, tmp_path, monkeypatch):
    case = setup_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        fail_apply(hitl, monkeypatch, stage=2, after=False)  # /end's intent commits; its apply does not
        with pytest.raises(Problem) as lost:
            await NativeHitlRuntime.finish_turn(case.client, case.runtime, case.context.receipt)
        assert lost.value.status == 503
        assert not (await case.client.read(view.approval.pending_id)).turn_closed
        fail_apply(hitl, monkeypatch, stage=1, after=False)
        await case.gate.close()  # the in-turn close stays best effort; the terminal task is the one that retries
        await NativeHitlRuntime.finish_turn(case.client, case.runtime, case.context.receipt)
        retained = await case.client.read(view.approval.pending_id)
        assert retained.status == "interrupted" and retained.turn_closed
    asyncio.run(check())


def test_a_lost_handoff_end_reaches_the_terminal_task_and_its_rerun_closes_the_card(hitl, tmp_path, monkeypatch):
    case = setup_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        fail_apply(hitl, monkeypatch, stage=2, after=False)  # /end's intent commits; its apply does not
        with pytest.raises(Problem) as lost:
            await NativeHitlRuntime.interrupt_handed_off(case.client, case.context.receipt)
        assert lost.value.status == 503
        assert not (await case.client.read(view.approval.pending_id)).turn_closed
        await NativeHitlRuntime.interrupt_handed_off(case.client, case.context.receipt)
        retained = await case.client.read(view.approval.pending_id)
        assert retained.status == "interrupted" and retained.turn_closed
    asyncio.run(check())


def test_a_wait_survives_a_few_failed_first_reads_but_not_endless_ones(hitl, tmp_path):
    retries = 4  # FIRST_READ_RETRIES, written out so an older runtime fails on the behavior, not on an import
    case = setup_gate(hitl, tmp_path)
    calls = []

    def reader(failures, status=503):
        async def read(identifier):
            calls.append(identifier)
            if len(calls) <= failures:
                raise (Problem(412, "BEHAVIOR_CONFLICT", "OFFLINE owner write raced the read") if status == 412 else
                       Problem(status, "NATIVE_HITL_AUTHORITY_UNAVAILABLE", "OFFLINE API restart"))
            return "saved reply"
        return read

    async def outcome(read):
        calls.clear()
        try:
            return await case.gate._owner_reply(read, "qid", lambda view: view, None)
        except Problem as error:
            return error.status

    async def check():
        for status in (503, 412):
            assert await outcome(reader(retries, status)) == "saved reply" and len(calls) == retries + 1
        assert await outcome(reader(retries + 1)) == 503 and len(calls) == retries + 1
        assert await outcome(reader(1, 403)) == 403 and len(calls) == 1
    asyncio.run(check())


def test_unconfirmed_result_never_reexecutes_and_unknown_is_not_erased_by_expiry(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)
    ran = []

    async def tool(_):
        ran.append("effect")
        case.runtime.store.fail_set = True
        return ToolOutput("provider result whose save was not acknowledged")

    async def check():
        waiting = asyncio.create_task(permit(case))
        original = await pending_view(case)
        chosen = human_decide(case, original)
        assert (await waiting)[0]
        with pytest.raises(ToolFailure):
            await case.gate.invoke(case.packet, tool)
        case.runtime.store.fail_set = False
        assert not (await permit(case))[0]
        with pytest.raises(ToolFailure):
            await case.gate.invoke(case.packet, tool)
        hitl.now += timedelta(seconds=1000)
        await case.gate.close()
        view = await case.client.read(original.approval.pending_id)
        assert view.status == "effect_unknown" and view.decision == chosen and ran == ["effect"]
        assert case.gate.calls["call-one"].claim.record.status == "pending"
    asyncio.run(check())


def test_sdk_direct_mcp_claim_observes_completion_or_remains_unknown_without_second_permission(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)
    packet = {**case.packet, "tool_name": "browser_navigate", "arguments": {"url": "https://example.invalid"}}

    async def check():
        waiting = asyncio.create_task(permit(case, packet, kind="mcp", server="playwright"))
        view = await pending_view(case)
        human_decide(case, view)
        assert (await waiting)[0]
        assert not (await permit(case, packet, kind="mcp", server="playwright"))[0]
        assert len(case.runtime.store.items) == 1
        case.gate.observe({"type": "tool.execution_complete", "data": {"tool_call_id": "call-one", "success": True}})
        await case.gate.close()
        assert case.gate.calls["call-one"].claim.record.result == {
            "sdk_tool_completed": True, "purpose": "sdk-observation-not-external-exactly-once"}
        assert (await case.client.read(view.approval.pending_id)).status == "allow"
    asyncio.run(check())


def test_steering_text_and_questions_never_become_a_typed_human_decision(hitl, tmp_path):
    from copilot.session_events import UserMessageData, UserMessageDelivery
    from shared_context.copilot_steering import SdkSteeringChannel
    case = setup_gate(hitl, tmp_path)
    steering = SdkSteeringChannel(case.context.control.stop_requested)
    case.context.control.steering = steering
    case.gate.human_inputs = steering.human_inputs

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        async def sdk_send(content, *, mode):
            assert content == "同意，請繼續" and mode == "enqueue"
            return "observed-append"
        steering.open(SimpleNamespace(send=sdk_send))
        identifier = await steering.send("同意，請繼續")
        assert steering.human_inputs() == ()
        steering.observe(SimpleNamespace(data=UserMessageData(
            content="同意，請繼續", message_id=identifier, delivery=UserMessageDelivery.QUEUED)))
        assert steering.human_inputs() == ("同意，請繼續",)
        await asyncio.sleep(0.03)
        assert not waiting.done()
        assert (await case.client.read(view.approval.pending_id)).decision is None
        assert len(case.sender.observed) == 1
        human_decide(case, view, "deny")
        assert not (await waiting)[0]
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("reviewed", [False, True])
def test_close_ends_the_turn_on_the_reply_path_only_when_something_was_opened(hitl, tmp_path, reviewed):
    case = setup_gate(hitl, tmp_path)

    async def check():
        if reviewed:
            waiting = asyncio.create_task(permit(case))
            human_decide(case, await pending_view(case), "deny")
            assert not (await waiting)[0]
        before = len(case.client.observed)
        await case.gate.close()
        return [path for _, path, _ in case.client.observed[before:]]

    paths = asyncio.run(check())
    # With nothing opened, the terminal notification ends the turn later, off the reply's path.
    assert any(path.endswith("/end") for path in paths) is reviewed
    assert paths if reviewed else paths == []


def test_real_sdk_permission_callback_awaits_hitl_and_keeps_managed_and_schema_refusals(
        hitl, tmp_path, monkeypatch):
    case = setup_gate(hitl, tmp_path)
    async def callback(options, sid):
        request = permission_request(case.packet)
        request.managed_approval_required = True
        assert isinstance(options["on_permission_request"](request, {"session_id": sid}), PermissionDecisionReject)
        assert not case.sender.observed
        request.managed_approval_required = False
        request.args = {"unexpected": "field"}
        assert isinstance(options["on_permission_request"](request, {"session_id": sid}), PermissionDecisionReject)
        assert not case.sender.observed
        request.args = case.packet["arguments"]
        waiting = asyncio.create_task(options["on_permission_request"](request, {"session_id": sid}))
        original = await pending_view(case)
        assert not waiting.done()
        human_decide(case, original)
        assert isinstance(await waiting, PermissionDecisionApproveOnce)
        assert options["tools"][0].skip_permission is False
        await case.gate.close()
    async def no_tool(_):
        pytest.fail("permission-only SDK fixture must not execute a provider")
    run_callbacks(
        tmp_path, monkeypatch, callback, no_tool, tool_name="run_command",
        parameters={"type": "object", "properties": {"command": {"type": "string"}},
                    "required": ["command"], "additionalProperties": False}, native_hitl=case.gate)


def test_api_authorization_is_agent_only_single_claim_and_history_stays_typed(hitl):
    c = hitl
    pending_command, pending = create_pending(c)
    assert post(c, "/decisions", decision(c, pending)).status_code == 200
    ref = NativeHitlApprovalReference(pending_id=pending["pending_id"], pending_digest=pending["pending_digest"],
                                      call=pending_command.action.call)
    command = NativeHitlAuthorize(
        request_id="claim-one", action=pending_command.action, action_digest=pending_command.action_digest,
        review=pending_command.review, expected_rules_etag=None, effect_token="original-fresh-effect", approval=ref)
    error(post(c, "/authorize", command), 403)
    first = post(c, "/authorize", command, c.agent)
    assert first.status_code == 200, first.text
    before = len(c.table.submissions)
    assert post(c, "/authorize", command, c.agent).json() == first.json()
    assert len(c.table.submissions) == before
    error(post(c, "/authorize", command.model_copy(update={"request_id": "second-claim", "effect_token": "new-token"}),
               c.agent), 409)
    values = validate_native_hitl_history(inventory(c), c.config)
    assert any(type(value).__name__ == "_Authorization" for value in values.values())


def test_closed_rid_blocks_late_pending_and_human_click_without_queue_or_new_rid(hitl):
    c = hitl
    command = proposal(c)
    turn = NativeHitlTurn(**command.action.call.model_dump(include={"agent_id", "conversation_id", "request_id"}))
    end = NativeHitlEnd(request_id="end-original", turn=turn)
    assert post(c, "/end", end, c.agent).status_code == 200
    error(post(c, "/pending", command, c.agent), 409, "NATIVE_HITL_TURN_CLOSED")
    query = NativeHitlPendingQuery(turn=turn)
    result = post(c, "/turn-pending", query).json()
    assert result["closed"] and result["attention"] == [] and result["references"] == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("lane", ["foundry-native-v1", "foundry-responses-v1"])
def test_both_native_lanes_use_one_original_executor_and_off_keeps_no_review(
        hitl, responses, tmp_path, monkeypatch, lane, enabled):
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
    from shared_context.m2_copilot import ToolSpec
    from test_native_capabilities import OfflineResult

    hitl.now = datetime.now(timezone.utc)
    original_scope = hitl.config.agent_scope
    hitl.config.targets += (SimpleNamespace(agent_id=IDENTITY.agent_id),)
    hitl.config.agent_scope = lambda identifier: (
        SimpleNamespace(tenant_id=TENANT, team_id=A.team, identity=A) if identifier == IDENTITY.agent_id
        else original_scope(identifier))
    client, seen, effects, model_requests = api_client(hitl), [], [], []

    async def bind(context, grants):
        async def tool(packet):
            effects.append(packet["tool_call_id"])
            return ToolOutput(OfflineResult(text="original effect result").model_dump_json())
        return NativeCapabilityBinding(
            tools=(ToolSpec("run_command", "OFFLINE provider", {"type": "object"}, tool),),
            result_schemas={"run_command": OfflineResult})
    provider = NativeCapabilityProvider(accepts=lambda grant: grant.entry_id == "fa-shell-v1", bind=bind)

    def model(request):
        body = json.loads(request.content)
        assert "tools" not in body
        model_requests.append(body)
        event = {"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(verdict("allow"))}]}],
            "usage": {"input_tokens": 11, "output_tokens": 4}}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("data: " + json.dumps(event) + "\n\n").encode())

    async def sdk(**options):
        seen.append(options)
        assert options["steering"] is not None
        tool = next(spec for spec in options["tools"] if spec.name == "run_command")
        packet = {"session_id": options["sdk_session_id"], "tool_call_id": "lane-call",
                  "tool_name": tool.name, "arguments": {"command": "echo synthetic"}}
        if enabled:
            assert await options["native_hitl"].permission(permission_request(packet),
                                                           {"session_id": options["sdk_session_id"]}) == (True, None)
        else:
            assert "native_hitl" not in options
        assert json.loads((await tool.handler(packet)).text)["text"] == "original effect result"
        if enabled:
            with pytest.raises(ToolFailure):
                await tool.handler(packet)
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "OFFLINE H2 result",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "h2-native", runtime_path=tmp_path / "OFFLINE-NOT-EXECUTED",
        api_url="https://api.offline.invalid", model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE",
        model_transport=httpx.MockTransport(model), capability_providers=(provider,),
        hitl_client=client if enabled else None)
    executor.reader = responses.runtime
    responses.runtime.executor, responses.runtime.preflight = executor, executor.preflight
    original = response_submit() if lane == "foundry-responses-v1" else submit()
    data = original.model_dump(mode="json")
    data["capture"] = selected_capture("fa-shell-v1").model_dump(mode="json")
    command = NativeSubmit.model_validate_json(json.dumps(data))
    assert responses.invoke(command).status_code == 202
    if lane == "foundry-responses-v1":
        assert responses.respond(responses.call(responses.runtime.read, "rid-1")).status_code == 200
    completed = responses.wait("rid-1", "completed")
    assert completed.command == command.command
    assert effects == ["lane-call"] and len(seen) == 1
    assert responses.invoke(command).status_code == 202
    assert len(seen) == 1
    assert len(model_requests) == int(enabled)
    if enabled:
        assert completed.usage.usage[0].requests == completed.usage.usage[0].measured_requests == 1
        assert (completed.usage.usage[0].input_tokens, completed.usage.usage[0].output_tokens) == (11, 4)
    else:
        assert completed.usage.state == "none"


@pytest.mark.parametrize("decision_kind", ["allow", "deny"])
def test_delegation_awaits_before_original_sdk_prepare_and_reuses_its_claim(hitl, tmp_path, decision_kind):
    from shared_context.native_delegation import NativeDelegationRuntime
    case = setup_gate(hitl, tmp_path, entries=("fa-delegate-v1",))
    delegate = NativeDelegationRuntime(case.context)
    arguments = {"name": "offline-child", "description": "Check a synthetic sample", "prompt": "Read this sample.",
                 "agent_type": "fa-delegate-v1", "mode": "sync"}
    delegate.observe({"type": "tool.execution_start", "data": {
        "tool_name": "task", "tool_call_id": "child-call", "arguments": arguments}})

    async def check():
        waiting = asyncio.create_task(delegate.pre_tool(
            {"toolName": "task", "toolArgs": arguments, "sessionId": SID}, {"session_id": SID}))
        view = await pending_view(case)
        assert not waiting.done() and delegate.tool_call_id is None
        human_decide(case, view, "allow_once" if decision_kind == "allow" else "deny")
        output = await waiting
        assert output["permissionDecision"] == decision_kind
        assert len(case.runtime.store.items) == 1
        if decision_kind == "allow":
            assert delegate.claims["child-call"].record.token == case.gate.calls["child-call"].claim.record.token
            assert delegate.claims["child-call"].record.hitl.authorization is not None
            repeated = await delegate.pre_tool(
                {"toolName": "task", "toolArgs": arguments, "sessionId": SID}, {"session_id": SID})
            assert repeated["permissionDecision"] == "deny"
        else:
            assert delegate.tool_call_id is None
            await delegate.require_complete()
        await case.gate.close()
        final = await case.client.read(view.approval.pending_id)
        assert final.status == ("effect_unknown" if decision_kind == "allow" else "deny")
    asyncio.run(check())


def test_authorization_lost_ack_reconciles_same_effect_token_without_second_send(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path, (verdict("allow"),))
    sent = []

    def api(request):
        sent.append(request.url.path)
        response = hitl.client.request(request.method, request.url.path, content=request.content, headers=request.headers)
        if request.url.path.endswith("/authorize") and response.status_code == 200:
            return httpx.Response(503, json={"error": {"code": "BEHAVIOR_COMMIT_UNKNOWN"}})
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)
    case.client.transport = httpx.MockTransport(api)
    ran = []

    async def tool(_):
        ran.append("once")
        return ToolOutput("saved")
    async def check():
        assert (await permit(case))[0]
        assert (await case.gate.invoke(case.packet, tool)).text == "saved"
        assert sent.count("/native-hitl/v1/authorize") == 1
        assert sent.count("/native-hitl/v1/authorize/reconcile") == 1
        assert ran == ["once"]
        await case.gate.close()
    asyncio.run(check())


def test_rule_change_at_authorization_cas_reassesses_the_same_claim(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path, (verdict("allow"), verdict("allow")))
    calls = []
    original = case.client.write
    changed = False

    async def writing(path, command):
        nonlocal changed
        if path == "/authorize" and not changed:
            changed = True
            assert post(hitl, "/rules", change(hitl, "interleaved-policy", behavior="allow_automatically")).status_code == 200
        return await original(path, command)
    case.client.write = writing
    async def tool(_):
        calls.append("sent")
        return ToolOutput("one")
    async def check():
        assert (await permit(case))[0]
        token = case.gate.calls["call-one"].claim.record.token
        await case.gate.invoke(case.packet, tool)
        retained = case.gate.calls["call-one"].claim.record
        assert retained.token == token and len(case.runtime.store.items) == 1
        assert [review.rules_revision for review in retained.hitl.reviews] == [0, 1]
        assert retained.hitl.authorization.review.rules_revision == 1 and calls == ["sent"]
        await case.gate.close()
    asyncio.run(check())


def test_pending_projection_is_paged_per_rid_and_other_agents_or_teams_cannot_write_it(hitl):
    c = hitl
    for number in range(21):
        create_pending(c, request_id=f"pending-{number}", call_id=f"call-{number}")
    query = NativeHitlPendingQuery(turn=NativeHitlTurn(agent_id="native-a", conversation_id="cid-one", request_id="rid-one"))
    first = post(c, "/turn-pending", query)
    assert first.status_code == 200, first.text
    page = NativeHitlPendingPage.model_validate_json(first.content)
    assert len(page.references) == len(page.attention) == 20 and page.next_after == 20
    second = NativeHitlPendingPage.model_validate_json(
        post(c, "/turn-pending", query.model_copy(update={"after": page.next_after})).content)
    assert len(second.references) == 1 and second.next_after is None
    error(post(c, "/turn-pending", query, c.other_agent), 403)
    error(post(c, "/turn-pending", query, c.owner_two), 403)
    error(post(c, "/end", NativeHitlEnd(request_id="not-my-rid", turn=query.turn), c.other_agent), 403)


def test_sdk_mcp_missing_completion_is_unknown_not_reusable_permission(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)
    packet = {**case.packet, "tool_name": "browser_click", "arguments": {"element": "synthetic button", "ref": "e1"}}
    async def check():
        waiting = asyncio.create_task(permit(case, packet, kind="mcp", server="playwright"))
        view = await pending_view(case)
        human_decide(case, view)
        assert (await waiting)[0]
        # No SDK completion was observed: do not invent an external result.
        await case.gate.close()
        assert (await case.client.read(view.approval.pending_id)).status == "effect_unknown"
        assert not (await permit(case, packet, kind="mcp", server="playwright"))[0]
        assert case.gate.calls["call-one"].claim.record.status == "pending"
    asyncio.run(check())


def test_cancelled_callback_cannot_reenter_its_ram_wait_even_without_turn_cancel(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)
    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert not case.context.control.stop_requested()
        assert not (await permit(case))[0]
        assert case.gate.calls["call-one"].refused
        assert (await case.client.read(view.approval.pending_id)).status == "interrupted"
        assert len(case.sender.observed) == 1
        await case.gate.close()
    asyncio.run(check())


def test_closed_turn_does_not_turn_a_saved_unknown_child_observation_into_success(hitl, tmp_path):
    from shared_context.behavior_delegation_wire import DelegationObservation
    from shared_context.native_delegation import NativeDelegationRuntime
    case = setup_gate(hitl, tmp_path, entries=("fa-delegate-v1",))
    delegate = NativeDelegationRuntime(case.context)
    arguments = {"name": "offline-child", "description": "Check a sample", "prompt": "Synthetic input",
                 "agent_type": "fa-delegate-v1", "mode": "sync"}
    delegate.observe({"type": "tool.execution_start", "data": {
        "tool_name": "task", "tool_call_id": "child-call", "arguments": arguments}})
    async def check():
        pending = asyncio.create_task(delegate.pre_tool(
            {"toolName": "task", "toolArgs": arguments, "sessionId": SID}, {"session_id": SID}))
        view = await pending_view(case)
        human_decide(case, view)
        assert (await pending)["permissionDecision"] == "allow"
        reply = await delegate._operation("observe", DelegationObservation(
            event_id="child-unknown", tool_call_id="child-call", kind="unknown", failure_code="SDK_CHILD_OUTCOME_UNKNOWN"))
        assert reply.status == "unknown" and delegate.claims["child-call"].record.status == "done"
        await case.gate.close()
        assert (await case.client.read(view.approval.pending_id)).status == "effect_unknown"
    asyncio.run(check())


def test_new_auto_rule_cannot_rebind_an_existing_pending_call_to_different_arguments(hitl):
    command, _ = create_pending(hitl)
    action = command.action.model_copy(update={"arguments": {"command": "different"}})
    authorize = NativeHitlAuthorize(
        request_id="changed-action", action=action, action_digest=action.sha256,
        review=NativeHitlReview(rules_revision=0, source="default", outcome="allow"),
        expected_rules_etag=None, effect_token="fresh-but-wrong-input")
    error(post(hitl, "/authorize", authorize, hitl.agent), 409, "NATIVE_HITL_ACTION_CHANGED")


def test_h1_original_read_set_and_bytes_reconcile_without_index_backfill(hitl):
    """Synthetic H1 record shape, not a migration or recovery of a real SDK waiter."""
    from shared_context.behavior_operations import _write, control_key
    from shared_context.behavior_store import ControlChanges, ControlRequest
    from shared_context.native_hitl import _Intent, _State
    from shared_context.native_hitl_wire import NativeHitlPending, pending_id

    c = hitl
    command = proposal(c)
    request = c.service._request(c.agent, command, legacy=True)
    key = c.service._intent_key(c.agent, command)
    intent = _Intent(tenant_id=TENANT, team_id=A.team, intent_id=key, principal_oid=A.oid,
                     principal_kind="agent", command=command, created_at=c.now,
                     expires_at=c.now + timedelta(seconds=600), request=request)
    c.store.transact(ControlRequest(request_id=key, request_digest=request.request_digest, read_set=request.read_set),
                     lambda _: ControlChanges(writes=(_write(key, intent, "create"),),
                                              result={"intent_id": key}))
    pid = pending_id(TENANT, A.team, command.action.call)
    assert pid == control_key("native-hitl-pending", TENANT, A.team, material_digest(command.action.call))
    pending = NativeHitlPending(tenant_id=TENANT, team_id=A.team, pending_id=pid, agent_oid=A.oid,
                                command=command, created_at=c.now, expires_at=intent.expires_at)
    original = c.store.transact(request, lambda _: ControlChanges(writes=(
        _write(pid, pending, "create"),
        _write(hitl_key("state", TENANT, A.team, pid), _State(
            tenant_id=TENANT, team_id=A.team, pending_id=pid, status="pending"), "create")),
        result={"request_id": command.request_id, "kind": command.kind,
                "purpose": "readback-only-not-execution-permission",
                "pending_id": pid, "pending_digest": pending.sha256, "status": "pending"}))
    before, count = copy.deepcopy(c.table.backing.rows), len(c.table.submissions)
    assert c.service.reconcile(c.agent, command) == original
    assert c.service.submit(c.agent, command) == original
    assert c.service.read(c.owner, pid).pending == pending
    assert c.table.backing.rows == before and len(c.table.submissions) == count


@pytest.mark.parametrize("outcome,takeover", [("allow", False), ("deny", True)])
def test_p2_standard_reasoning_item_preserves_the_structured_review(hitl, tmp_path, outcome, takeover):
    case = setup_gate(hitl, tmp_path)
    sender = model_sender((verdict(outcome, takeover=takeover),), reasoning=True)
    async def check():
        answer = await NativeAutoReview(sender)(case.gate._action(case.packet), await case.client.rules(), "Original request")
        assert answer.outcome == outcome and answer.takeover_required == takeover
        assert answer.reason != "review_invalid"
    asyncio.run(check())


LOGIN_PAGE = "data:text/html;base64," + base64.b64encode(
    "<form><label>帳號 <input type=text></label><label>密碼 <input type=password></label>"
    "<button>登入</button></form>".encode()).decode()
TAKEOVER_SCOPE = (
    "set takeover_required=true and outcome=deny for an action that enters, fills or submits them",
    "typing or filling\na password/credential, 2FA code or CAPTCHA field, clicking a login/submit/pay button",
    "a URL that itself carries\ncredentials (user:pass@ or a token/password in the query)",
    "Opening, reading or capturing a page that merely shows\na login form is read-only and is not takeover",
)


@pytest.mark.parametrize("tool,arguments,answer,outcome", [
    # V4-02 (3c95): opening a page that only shows a login form is read-only.
    ("browser_navigate", {"url": LOGIN_PAGE}, verdict("allow"), "allow"),
    ("browser_type", {"ref": "e3", "element": "密碼 password field", "text": "synthetic"},
     verdict("deny", takeover=True), "deny"),
    ("browser_navigate", {"url": "https://user:secret@example.invalid/"}, verdict("deny", takeover=True), "deny"),
])
def test_review_takeover_covers_entering_credentials_not_opening_a_login_page(hitl, tmp_path, tool, arguments,
                                                                               answer, outcome):
    """The reviewer is told takeover means entering/submitting credentials; reading a login page is not takeover."""
    case = setup_gate(hitl, tmp_path)
    sender = model_sender((answer,))
    packet = {**case.packet, "tool_name": tool, "arguments": arguments}

    async def check():
        action = case.gate._action(packet, server="playwright")
        result = await NativeAutoReview(sender)(action, await case.client.rules(), {"initial": "Read the page."})
        assert (result.outcome, result.takeover_required) == (outcome, outcome == "deny")
        request = sender.observed[0]
        assert all(clause in request["instructions"] for clause in TAKEOVER_SCOPE)
        # The reviewer sees the exact tool and arguments it must judge, URL included.
        payload = json.loads(request["input"][0]["content"])
        assert payload["action"]["tool"] == tool and payload["action"]["arguments"] == arguments
        assert payload["action"]["target"] == "playwright/" + tool
    asyncio.run(check())


def large_png():
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))
    rng = random.Random(7)
    raw = b"".join(b"\0" + rng.randbytes(512 * 3) for _ in range(512))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 512, 512, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_p2_legal_desktop_png_survives_one_effect_and_reads_back_exactly(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path, (verdict("allow"),), entries=("fa-desktop-v1",))
    case.packet.update(tool_name="desktop_act",
                       arguments={"observation_id": 1, "action": {"type": "click", "x": 10, "y": 20}})
    data = large_png()
    encoded = base64.b64encode(data).decode("ascii")
    assert 750_000 < len(data) < 1024 * 1024 and len(encoded) > 1_000_000
    ran = []
    async def screenshot(_):
        ran.append("desktop effect")
        return ToolOutput("original desktop result", png_base64=encoded)
    async def check():
        assert (await permit(case))[0]
        # The reviewed H2 shape cannot fit this valid original PNG in one item.
        from shared_context.native_turns import _json
        original = case.gate.calls["call-one"].claim.record
        with pytest.raises(Problem) as old_shape:
            _json(original.model_copy(update={"status": "done", "result": {"text": "original", "png_base64": encoded}}))
        assert old_shape.value.body["error"]["code"] == "NATIVE_REQUEST_TOO_LARGE"
        output = await case.gate.invoke(case.packet, screenshot)
        assert output.png_base64 == encoded and output.text == "original desktop result"
        assert await case.context.effects.read_image("call-one", case.gate.calls["call-one"].material) == encoded
        assert ran == ["desktop effect"]
        with pytest.raises(ToolFailure):
            await case.gate.invoke(case.packet, screenshot)
        assert ran == ["desktop effect"]
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("size", [30 * 1024, "maximum"])
@pytest.mark.parametrize("outcome", ["allow", "ask"])
def test_p2_original_delegation_capacity_keeps_complete_control_material(hitl, tmp_path, size, outcome):
    from shared_context.behavior_delegation_wire import DelegationPrepare, DelegationTask, DELEGATION_PREPARE_BYTES
    from shared_context.native_delegation import NativeDelegationRuntime
    case = setup_gate(hitl, tmp_path, (verdict(outcome),), entries=("fa-delegate-v1",))
    delegate = NativeDelegationRuntime(case.context)
    arguments = {"name": "large-child", "description": "Read complete original material", "prompt": "q",
                 "agent_type": "fa-delegate-v1", "mode": "sync"}
    minimum = DelegationPrepare(tool_call_id="large-call", arguments=DelegationTask.model_validate(arguments))
    length = DELEGATION_PREPARE_BYTES - len(minimum.model_dump_json().encode()) + 1 if size == "maximum" else size
    arguments["prompt"] = "q" * length
    command = DelegationPrepare(tool_call_id="large-call", arguments=DelegationTask.model_validate(arguments))
    assert 24 * 1024 < len(command.model_dump_json().encode()) <= DELEGATION_PREPARE_BYTES
    if size == "maximum":
        assert len(command.model_dump_json().encode()) == DELEGATION_PREPARE_BYTES
    delegate.observe({"type": "tool.execution_start", "data": {
        "tool_name": "task", "tool_call_id": "large-call", "arguments": arguments}})
    async def check():
        waiting = asyncio.create_task(delegate.pre_tool(
            {"toolName": "task", "toolArgs": arguments, "sessionId": SID}, {"session_id": SID}))
        if outcome == "ask":
            view = await pending_view(case)
            assert view.pending.command.action.arguments == arguments
            # Reopening the original RID must not need the large body in its projection.
            page = await case.client.pending(NativeHitlPendingQuery(turn=case.gate.turn))
            assert page.references == (view.approval,) and len(page.model_dump_json().encode()) < 4096
            human_decide(case, view)
        result = await waiting
        assert result["permissionDecision"] == "allow"
        assert delegate.claims["large-call"].record.hitl.action.arguments == arguments
        validate_native_hitl_history(inventory(hitl), hitl.config)
        await case.gate.close()
    asyncio.run(check())


def test_p2_segmented_control_lost_ack_reconciles_complete_original_without_any_rewrite(hitl, monkeypatch):
    from shared_context.native_hitl_material import HitlPackedRecord
    from test_native_hitl import fail_apply
    c = hitl
    original = proposal(c)
    action = original.action.model_copy(update={
        "category": "delegation", "tool": "task", "target": "fa-delegate-v1",
        "arguments": {"prompt": "原始資料" * 15000}})
    command = original.model_copy(update={"action": action, "action_digest": action.sha256})
    fail_apply(c, monkeypatch, stage=2, after=True)
    error(post(c, "/pending", command, c.agent), 503)
    before, count = copy.deepcopy(c.table.backing.rows), len(c.table.submissions)
    recovered = post(c, "/pending/reconcile", command, c.agent)
    assert recovered.status_code == 200, recovered.text
    pid = recovered.json()["result"]["pending_id"]
    view = read(c, pid)
    assert view.status_code == 200 and NativeHitlReadback.model_validate_json(view.content).pending.command == command
    packed = HitlPackedRecord.model_validate_json(json.dumps(c.store.read_record(pid).payload["data"]))
    assert len(packed.parts) > 1
    assert c.table.backing.rows == before and len(c.table.submissions) == count
    values = validate_native_hitl_history(inventory(c), c.config)
    assert values[pid].command == command


def test_p2_image_readback_refuses_changed_or_missing_original_part(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path, (verdict("allow"),), entries=("fa-desktop-v1",))
    case.packet.update(tool_name="desktop_act",
                       arguments={"observation_id": 1, "action": {"type": "click", "x": 10, "y": 20}})
    encoded = base64.b64encode(large_png()).decode("ascii")
    async def screenshot(_):
        return ToolOutput("original", png_base64=encoded)
    async def check():
        assert (await permit(case))[0]
        await case.gate.invoke(case.packet, screenshot)
        part = next(key for key in case.runtime.store.items if key.startswith("fx-img:"))
        original = case.runtime.store.items[part]
        case.runtime.store.items[part] = SimpleNamespace(
            etag=original.etag, value={**original.value, "token": "not-the-original-effect"})
        with pytest.raises(Problem) as changed:
            await case.context.effects.read_image("call-one", case.gate.calls["call-one"].material)
        assert changed.value.body["error"]["code"] == "NATIVE_EFFECT_CONFLICT"
        del case.runtime.store.items[part]
        with pytest.raises(Problem):
            await case.context.effects.read_image("call-one", case.gate.calls["call-one"].material)
        await case.gate.close()
    asyncio.run(check())
