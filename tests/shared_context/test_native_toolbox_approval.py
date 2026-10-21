"""Official toolbox approval: a tool's own ``require_approval`` decides; the runtime pauses, asks, resumes or refuses.

Offline: real JWT/HITL API/CAS and NativeEffects, the toolbox as an httpx MockTransport, the model as a double
that must never be asked (no Auto Review for a toolbox tool).
"""

import asyncio
import json
from contextlib import AsyncExitStack
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from shared_context import native_toolbox
from shared_context.behavior_capabilities import CapabilityCapture
from shared_context.behavior_native_turn_wire import (
    NativeAgentCommand, NativeAgentOrigin, NativeAgentReceive, NativeReceipt, NativeSubmit, NativeTurnAddress,
    NativeTurnCapture,
)
from shared_context.m2_copilot import ToolFailure
from shared_context.native_capabilities import NativeCapabilityContext, bind_capabilities
from shared_context.native_hitl_runtime import NativeAutoReview, NativeHitlRuntime
from shared_context.native_hitl_wire import NativeHitlPendingQuery, NativeHitlReview
from shared_context.native_turns import NativeTurnControl
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_capabilities import selected_capture
from test_native_hitl import change, decision, error, hitl, post  # noqa: F401
from test_native_hitl_h2 import SID, EffectRuntime, api_client, model_sender, permission_request, verdict
from test_native_toolbox import ENDPOINT, GRANT, Toolbox, approving
from test_native_turn_wire import submit

ARGUMENTS = {"search_query": "quarterly numbers"}
# The shape a Teams send is expected to take (Work IQ create_entity); IDs and text are synthetic.
TEAMS_SEND = {"agentId": "agent-example", "parentUrl": "https://graph.example.test/v1.0/chats/19:fake-chat/messages",
              "jsonBody": json.dumps({"body": {"contentType": "text", "content": "Line " * 3000 + "end."}})}


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(native_toolbox, "_CALLS", native_toolbox._Calls())
    monkeypatch.setattr(native_toolbox, "_DECLINED", set())


def toolbox_capture():
    data = selected_capture().model_dump(mode="json")
    data["template"]["settings"]["capabilities"] = [GRANT.ref.model_dump(mode="json")]
    data["capabilities"] = CapabilityCapture(head_selection_id="OFFLINE-selection", grants=(GRANT,)).model_dump(
        mode="json")
    return NativeTurnCapture.model_validate_json(json.dumps(data))


def agent_origin(rid):
    """Another Agent's request: like a scheduled turn, nobody is watching this conversation."""
    sender = NativeTurnAddress(agent_id="native-b", conversation_id="sender-cid", request_id="sender-rid")
    return NativeAgentOrigin(kind="agent_request", message_kind="request", message_id=rid, sender=sender,
                             recipient_agent_id="native-a", root_id="root-one", root=sender, hop=1)


def setup(c, tmp_path, approval, *, hitl_on=True, rid="rid-one", toolbox=None, provider=None, from_agent=False):
    capture = toolbox_capture()
    if from_agent:
        source = agent_origin(rid)
        command = NativeAgentCommand(request_id=rid, content="Please search for the quarterly numbers.")
        submitted = NativeAgentReceive(command=command, capture=capture, source=source)
    else:
        source, command = None, submit(rid, text="Please search for the quarterly numbers.").command
        submitted = NativeSubmit(command=command, capture=capture)
    receipt = NativeReceipt(state_profile=submitted.state_profile,
        agent_id="native-a", hosted_session_id="hosted-one", conversation_id="cid-one", request_id=rid,
        command=command, capture=capture, submit_digest=submitted.sha256, source=source,
        capture_digest=capture.sha256, status="started", turn_number=1)
    runtime, sender = EffectRuntime(receipt), model_sender([verdict("deny")])
    context = NativeCapabilityContext(
        receipt=receipt, scope=SimpleNamespace(sdk_session_id=SID, agent_id="native-a", conversation_id="cid-one"),
        control=NativeTurnControl(), root=tmp_path / "native-turns", turn_root=tmp_path / "native-turns" / rid,
        runtime=runtime, model=sender, api_url="https://api.offline.invalid",
        api_token_provider=lambda: c.headers(c.agent)["Authorization"][7:], model_token_provider=lambda: "OFFLINE")
    client = api_client(c)
    gate = NativeHitlRuntime(context, client, NativeAutoReview(sender), clock=lambda: c.now, poll_seconds=0.005)
    if hitl_on:
        context.hitl = gate
    toolbox = Toolbox(approval=approval) if toolbox is None else toolbox

    async def bind():
        async with AsyncExitStack() as stack:
            bindings = await bind_capabilities(
                context, (provider or native_toolbox.toolbox_provider(transport=toolbox.transport),), stack)
        return {spec.name: spec for binding in bindings for spec in binding.tools}
    return SimpleNamespace(c=c, context=context, client=client, gate=gate, sender=sender, toolbox=toolbox, bind=bind)


def packet(call_id, arguments=ARGUMENTS):
    return {"session_id": SID, "tool_call_id": call_id, "tool_name": "WebIQ-web", "arguments": dict(arguments)}


async def permit(case, call_id, arguments=ARGUMENTS):
    value = packet(call_id, arguments)
    return await case.gate.permission(permission_request(value), {"session_id": SID})


async def wait_pending(case, count):
    for _ in range(200):
        page = await case.client.pending(NativeHitlPendingQuery(turn=case.gate.turn))
        if len(page.references) >= count:
            return await case.client.read(page.references[count - 1].pending_id)
        await asyncio.sleep(0.005)
    pytest.fail("the toolbox call did not wait for the owner")


def decide(case, view, choice, request_id):
    original = {"pending_id": view.approval.pending_id, "pending_digest": view.approval.pending_digest}
    return post(case.c, "/decisions", decision(case.c, original, request_id=request_id, choice=choice))


def test_an_always_toolbox_tool_is_never_called_before_the_owner_approves_that_exact_call(hitl, tmp_path):
    case = setup(hitl, tmp_path, "always")

    async def check():
        web = (await case.bind())["WebIQ-web"]
        # Without the SDK permission callback, the guarded handler refuses before any toolbox request.
        with pytest.raises(ToolFailure) as unpermitted:
            await web.handler(packet("call-direct"))
        assert unpermitted.value.public_code == "NATIVE_HITL_PERMISSION_REQUIRED"

        waiting = asyncio.create_task(permit(case, "call-one"))
        view = await wait_pending(case, 1)
        assert not waiting.done() and case.toolbox.calls == []
        action, review = view.pending.command.action, view.pending.command.review
        # The card material: the tool name and its exact arguments, from the toolbox setting, not a rule review.
        assert (action.tool, action.arguments, action.category) == ("WebIQ-web", ARGUMENTS, "mcp")
        assert (review.source, review.outcome, review.reason, review.matched_rule_ids) == (
            "toolbox", "ask", "toolbox_always", ())
        # Every call is approved again: an approval can't become a standing Team rule.
        error(decide(case, view, "allow_rule", "owner-rule"), 422, "NATIVE_HITL_INVALID_REQUEST")
        assert decide(case, view, "allow_once", "owner-once").status_code == 200
        assert await waiting == (True, None)
        output = await web.handler(packet("call-one"))
        assert json.loads(output.text)["text"] == "found\ncited"
        assert case.toolbox.calls == [{"name": "WebIQ___web", "arguments": ARGUMENTS}]

        # The next call with the same arguments waits again; a denial never reaches the toolbox.
        second = asyncio.create_task(permit(case, "call-two"))
        again = await wait_pending(case, 2)
        assert again.approval.pending_id != view.approval.pending_id and not second.done()
        assert decide(case, again, "deny", "owner-deny").status_code == 200
        assert await second == (False, "NATIVE_HITL_HUMAN_DENIED")
        with pytest.raises(ToolFailure):
            await web.handler(packet("call-two"))
        assert len(case.toolbox.calls) == 1
        assert case.sender.observed == []
        await case.gate.close()
    asyncio.run(check())


def test_a_never_toolbox_tool_runs_without_a_card_auto_review_or_a_team_rule(hitl, tmp_path):
    response = post(hitl, "/rules", change(hitl, "ask-web", text="Ask before any web search."))
    assert response.status_code == 200, response.text
    case = setup(hitl, tmp_path, "never")

    async def check():
        web = (await case.bind())["WebIQ-web"]
        assert await permit(case, "call-one") == (True, None)
        await web.handler(packet("call-one"))
        assert len(case.toolbox.calls) == 1 and case.sender.observed == []
        page = await case.client.pending(NativeHitlPendingQuery(turn=case.gate.turn))
        assert page.references == ()
        reviews = case.gate.calls["call-one"].claim.record.hitl.reviews
        assert [(item.source, item.outcome, item.reason) for item in reviews] == [("toolbox", "allow", "toolbox_never")]
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("approval,hitl_on", [("always", False), ("sometimes", True)])
def test_an_always_tool_without_hitl_or_an_unknown_setting_is_not_offered(hitl, tmp_path, approval, hitl_on, caplog):
    case = setup(hitl, tmp_path, approval, hitl_on=hitl_on)
    with pytest.raises(Exception) as refused:
        asyncio.run(case.bind())
    assert refused.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert ("reason=approval_required" if approval == "always" else "reason=approval_unknown") in caplog.text
    assert case.toolbox.calls == []


def test_a_toolbox_setting_only_asks_for_always_or_allows_for_never():
    assert NativeHitlReview(rules_revision=0, source="toolbox", outcome="ask", reason="toolbox_always").outcome == "ask"
    for outcome, reason in (("allow", "toolbox_always"), ("ask", "toolbox_never"), ("deny", "toolbox_always"),
                            ("allow", None), ("allow", "reviewed")):
        with pytest.raises(ValueError):
            NativeHitlReview(rules_revision=0, source="toolbox", outcome=outcome, reason=reason)
    with pytest.raises(ValueError):
        NativeHitlReview(rules_revision=0, source="default", outcome="allow", reason="toolbox_never")


def test_a_default_promoted_to_always_asks_for_approval_from_the_next_turn(hitl, tmp_path):
    toolbox = Toolbox()
    toolbox.versions = {"1": toolbox.tools, "2": approving(toolbox.tools, "always")}
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    first = setup(hitl, tmp_path, "never", toolbox=toolbox, provider=provider)

    async def check():
        await first.bind()
        assert first.gate.toolbox_approvals["WebIQ-web"] == "never"
        toolbox.default = "2"
        second = setup(hitl, tmp_path, "never", rid="rid-two", toolbox=toolbox, provider=provider)
        web = (await second.bind())["WebIQ-web"]
        assert second.gate.toolbox_approvals["WebIQ-web"] == "always"
        waiting = asyncio.create_task(second.gate.permission(permission_request(packet("call-two")), {"session_id": SID}))
        view = await wait_pending(second, 1)
        assert not waiting.done() and toolbox.calls == []
        assert view.pending.command.review.reason == "toolbox_always"
        assert decide(second, view, "allow_once", "owner-two").status_code == 200
        assert await waiting == (True, None)
        await web.handler(packet("call-two"))
        assert toolbox.requests[-1][0].url.path.endswith("/toolboxes/fa-org-tools/versions/2/mcp")
        assert len(toolbox.calls) == 1
        await second.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("hitl_on", [True, False])
def test_an_unread_default_version_fails_closed_and_never_runs_without_approval(hitl, tmp_path, hitl_on, caplog):
    toolbox = Toolbox()
    toolbox.get_reply = lambda request: httpx.Response(503)
    case = setup(hitl, tmp_path, "never", toolbox=toolbox, hitl_on=hitl_on)

    async def check():
        if not hitl_on:
            with pytest.raises(Exception) as refused:
                await case.bind()
            assert refused.value.body["error"]["reason"] == "default_version_unread"
            assert "reason=default_version_unread" in caplog.text and toolbox.calls == []
            return
        web = (await case.bind())["WebIQ-web"]
        # The listed setting says never, but the version serving this turn is unknown: every call asks first.
        assert case.gate.toolbox_approvals["WebIQ-web"] == "always"
        waiting = asyncio.create_task(permit(case, "call-unread"))
        view = await wait_pending(case, 1)
        assert not waiting.done() and toolbox.calls == []
        assert view.pending.command.review.reason == "toolbox_always"
        assert decide(case, view, "allow_once", "owner-unread").status_code == 200
        assert await waiting == (True, None)
        await web.handler(packet("call-unread"))
        assert len(toolbox.calls) == 1
        assert toolbox.requests[-1][0].url.path.endswith("/toolboxes/fa-org-tools/mcp")
        await case.gate.close()
    asyncio.run(check())


def timed_out(body):
    raise httpx.ReadTimeout("no reply in time")


def dropped(body):
    raise httpx.RemoteProtocolError("peer closed the connection")


@pytest.mark.parametrize("reply", [
    timed_out, dropped, lambda body: httpx.Response(502), lambda body: httpx.Response(200, content=b"{not json"),
], ids=["timeout", "dropped", "server_error", "malformed"])
def test_a_write_whose_outcome_is_unknown_is_sent_once_and_reported_uncertain_not_resent(hitl, tmp_path, reply):
    case = setup(hitl, tmp_path, "always")

    async def check():
        web = (await case.bind())["WebIQ-web"]
        waiting = asyncio.create_task(permit(case, "call-send", TEAMS_SEND))
        view = await wait_pending(case, 1)
        # The card carries the target path and the whole message body as the Agent sent them.
        assert view.pending.command.action.arguments == TEAMS_SEND
        assert decide(case, view, "allow_once", "owner-send").status_code == 200
        assert await waiting == (True, None)
        case.toolbox.reply = reply
        with pytest.raises(ToolFailure) as failed:
            await web.handler(packet("call-send", TEAMS_SEND))
        assert failed.value.public_code == "NATIVE_TOOLBOX_OUTCOME_UNKNOWN"
        assert failed.value.public_message.startswith("The result of this call is unknown")
        # The same call again is refused before the toolbox; nothing is sent a second time.
        with pytest.raises(ToolFailure) as again:
            await web.handler(packet("call-send", TEAMS_SEND))
        assert again.value.public_code == "NATIVE_HITL_ALREADY_AUTHORIZED"
        assert case.toolbox.calls == [{"name": "WebIQ___web", "arguments": TEAMS_SEND}]
        assert (await case.client.read(view.approval.pending_id)).status == "effect_unknown"
        await case.gate.close()
    asyncio.run(check())


def test_a_write_in_a_turn_nobody_watches_waits_the_background_ttl_and_never_runs_after_it(hitl, tmp_path):
    case = setup(hitl, tmp_path, "always", from_agent=True)

    async def check():
        web = (await case.bind())["WebIQ-web"]
        waiting = asyncio.create_task(permit(case, "call-late", TEAMS_SEND))
        view = await wait_pending(case, 1)
        assert view.pending.command.wait == "background"
        assert view.pending.expires_at - view.pending.created_at == timedelta(seconds=600)
        hitl.now += timedelta(seconds=601)
        assert await waiting == (False, "NATIVE_HITL_EXPIRED")
        assert (await case.client.read(view.approval.pending_id)).status == "expired"
        error(decide(case, view, "allow_once", "owner-late"), 409, "NATIVE_HITL_STATE_CONFLICT")
        with pytest.raises(ToolFailure):
            await web.handler(packet("call-late", TEAMS_SEND))
        assert case.toolbox.calls == []
        await case.gate.close()
    asyncio.run(check())
