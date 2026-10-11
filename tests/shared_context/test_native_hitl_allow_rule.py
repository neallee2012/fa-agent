"""Original HITL call on rule revisions 1 -> 2; signed control API and synthetic model/effect I/O."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

import httpx
import pytest

from shared_context.behavior_model_direct import DirectCompletions
from shared_context.m2_copilot import ToolFailure, ToolOutput
from shared_context.native_hitl_runtime import NativeAutoReview, NativeHitlClient
from shared_context.native_hitl_wire import hitl_key
from shared_context.native_hitl_wire import NativeHitlClose, NativeHitlPendingQuery
from copilot.generated.rpc import PermissionDecisionReject
from test_instruction_tool_feedback import run_callbacks
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import change, hitl, post
from test_native_hitl_h2 import MODEL_ROOT, human_decide, pending_view, permit, setup_gate, verdict


@pytest.mark.parametrize("decision_at", [
    "between_polls", "rules_read_in_flight", "pending_read_in_flight",
    "rules_snapshot_in_flight", "pending_snapshot_in_flight",
])
def test_allow_rule_on_existing_ask_revision_executes_only_the_current_call(hitl, tmp_path, decision_at, record_property):
    assert post(hitl, "/rules", change(
        hitl, "ask-shell", text="Ask before changing synthetic text files.", behavior="ask_first")).status_code == 200
    case = setup_gate(hitl, tmp_path, responses=False)
    reviews, effects = [], []
    handoff = {}

    def control(request):
        snapshot = decision_at in {"rules_snapshot_in_flight", "pending_snapshot_in_flight"}
        suffix = "/rules" if decision_at.startswith("rules_") else "/pending/" + handoff.get("pid", "")
        if snapshot and handoff.get("armed") and request.method == "GET" and request.url.path.endswith(suffix):
            key = hitl_key("rules", hitl.owner.tenant_id, hitl.owner.team) if decision_at.startswith("rules_") else (
                hitl_key("state", hitl.owner.tenant_id, hitl.owner.team, handoff["pid"]))

            def after_get(row_key):
                if row_key != "behavior:v1:row:" + key:
                    hitl.table.after_get = after_get
                    return
                handoff["armed"] = False
                with ThreadPoolExecutor(max_workers=1) as owner:
                    handoff["decision"] = owner.submit(
                        human_decide, case, handoff["view"], "allow_rule", "allow-this-and-later").result()

            hitl.table.after_get = after_get
        response = hitl.client.request(request.method, request.url.path, content=request.content, headers=request.headers)
        if (not snapshot and decision_at != "between_polls" and handoff.get("armed")
                and request.method == "GET" and request.url.path.endswith(suffix)):
            handoff["armed"] = False
            handoff["decision"] = human_decide(case, handoff["view"], "allow_rule", request_id="allow-this-and-later")
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    case.client = NativeHitlClient("https://api.offline.invalid", lambda: hitl.headers(hitl.agent)["Authorization"][7:],
                                  transport=httpx.MockTransport(control))
    case.gate.client = case.client

    def model(request):
        body = json.loads(request.content)
        assert "tools" not in body
        payload = json.loads(body["messages"][1]["content"])
        reviews.append(payload)
        reply = verdict("allow", [rule["rule_id"] for rule in payload["rules"]])
        event = {"choices": [{"delta": {"content": json.dumps(reply)}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())

    sender = DirectCompletions(MODEL_ROOT + "/chat/completions", lambda: "OFFLINE", "OFFLINE-model",
                               transport=httpx.MockTransport(model))
    case.gate.reviewer = NativeAutoReview(sender)

    async def tool(packet):
        effects.append(packet["tool_call_id"])
        return ToolOutput("original result")

    async def check():
        waiting = asyncio.create_task(permit(case))
        original = await pending_view(case)
        assert original.pending.command.review.reason == "ask_rule"
        assert original.pending.command.review.rules_revision == 1
        assert not waiting.done() and effects == []
        if decision_at == "between_polls":
            chosen = human_decide(case, original, "allow_rule", request_id="allow-this-and-later")
        else:
            handoff.update(armed=True, pid=original.approval.pending_id, view=original)
        allowed = await asyncio.wait_for(waiting, 1)
        chosen = chosen if decision_at == "between_polls" else handoff["decision"]
        readback = await case.client.read(original.approval.pending_id)
        observation = {
            "permission": allowed, "state": readback.status, "authorization": readback.authorization,
            "decision": readback.decision.choice, "grant_uses": readback.grant.uses,
            "grant_revision": readback.grant.rules_revision, "effects": effects, "expired": readback.expired,
        }
        record_property("before_effect", json.dumps(observation))
        assert allowed == (True, None), observation
        result = await case.gate.invoke(case.packet, tool)
        rules = await case.client.rules()
        assert rules.revision == 2 and [rule.behavior for rule in rules.rules] == ["ask_first", "allow_automatically"]
        assert result.text == "original result"
        view = await case.client.read(original.approval.pending_id)
        assert view.pending == original.pending and view.decision == chosen
        assert view.status == "allow" and view.grant.uses == 1 and view.grant.rules_revision == 2
        assert view.authorization is not None and view.authorization.review.rules_revision == 2
        assert view.authorization.review.outcome == "ask"
        assert set(view.authorization.review.matched_rule_ids) == {rule.rule_id for rule in rules.rules}
        assert effects == ["call-one"]
        with pytest.raises(ToolFailure):
            await case.gate.invoke(case.packet, tool)
        assert effects == ["call-one"] and [entry["rules_revision"] for entry in reviews] == [1, 2]
        second_packet = {**case.packet, "tool_call_id": "call-two"}
        second_wait = asyncio.create_task(permit(case, second_packet))
        for _ in range(100):
            page = await case.client.pending(NativeHitlPendingQuery(turn=case.gate.turn, after=1))
            if page.references:
                break
            await asyncio.sleep(0.005)
        assert page.attention and not second_wait.done() and effects == ["call-one"]
        next_view = await case.client.read(page.references[0].pending_id)
        assert next_view.pending.command.review.outcome == "ask" and next_view.pending.command.review.rules_revision == 2
        assert set(next_view.pending.command.review.matched_rule_ids) == {rule.rule_id for rule in rules.rules}
        human_decide(case, next_view, "deny", request_id="deny-next-call")
        assert (await second_wait)[0] is False and effects == ["call-one"]
        assert (await case.client.read(original.approval.pending_id)).grant == view.grant
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("finish,code,phrase", [
    ("deny", "NATIVE_HITL_HUMAN_DENIED", "真人已拒絕"),
    ("interrupted", "NATIVE_HITL_INTERRUPTED", "系統中斷"),
    ("expired", "NATIVE_HITL_EXPIRED", "期限已到"),
    ("effect_unknown", "NATIVE_HITL_EFFECT_UNKNOWN", "效果未確認"),
])
def test_permission_feedback_distinguishes_owner_denial_from_system_end(
        hitl, tmp_path, monkeypatch, finish, code, phrase):
    case = setup_gate(hitl, tmp_path)
    feedback = []

    async def callback(options, sid):
        from test_native_hitl_h2 import permission_request
        waiting = asyncio.create_task(options["on_permission_request"](permission_request(case.packet), {"session_id": sid}))
        original = await pending_view(case)
        if finish == "deny":
            human_decide(case, original, "deny")
        elif finish == "expired":
            hitl.now += timedelta(days=1)
        else:
            if finish == "effect_unknown":
                human_decide(case, original, "allow_once")
            closed = NativeHitlClose(request_id="agent-observation", pending_id=original.approval.pending_id,
                                     pending_digest=original.approval.pending_digest, status=finish)
            assert post(hitl, "/observations", closed, hitl.agent).status_code == 200
        result = await waiting
        assert isinstance(result, PermissionDecisionReject)
        feedback.append(result.feedback)
        await case.gate.close()

    async def never(_):
        pytest.fail("a stopped original call must not execute")

    events = run_callbacks(tmp_path, monkeypatch, callback, never, tool_name="run_command",
                           parameters={"type": "object", "properties": {"command": {"type": "string"}},
                                       "required": ["command"], "additionalProperties": False},
                           native_hitl=case.gate)
    assert len(feedback) == 1
    assert code in feedback[0] and phrase in feedback[0]
    if finish != "deny":
        assert "不是人類否決" in feedback[0]
    assert "Native approval refused" not in feedback[0]
    permission = next(event for event in events if event["type"] == "m2.permission")
    assert permission["public_progress"]["summary"] == feedback[0]
    assert permission["public_progress"]["status"] == ("unknown" if finish == "effect_unknown" else "failed")


def test_stable_invalid_grant_still_stops_without_reobserving_it_as_a_conflict(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(permit(case))
        original = await pending_view(case)
        human_decide(case, original, "allow_rule")
        key = hitl_key("grant", hitl.owner.tenant_id, hitl.owner.team, original.approval.pending_id)
        entity = next(value for (_, _, row), value in hitl.table.backing.rows.items()
                      if row == "behavior:v1:row:" + key)
        payload = json.loads(entity["Payload"])
        payload["data"]["decision_id"] = "f" * 64
        entity["Payload"] = json.dumps(payload).encode()
        result = await asyncio.wait_for(waiting, 1)
        assert result == (False, "NATIVE_HITL_RECORD_INVALID")
        assert not any(path.endswith("/authorize") for _, path, _ in case.client.observed)
        with pytest.raises(ToolFailure):
            await case.gate.invoke(case.packet, lambda _: pytest.fail("invalid grant executed"))
        await case.gate.close()
    asyncio.run(check())


def test_read_conflict_cannot_extend_the_original_wait_deadline(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)
    reads = []

    async def check():
        waiting = asyncio.create_task(permit(case))
        original = await pending_view(case)
        transport = case.client.transport

        def conflict(request):
            if request.method == "GET" and request.url.path.endswith("/rules"):
                reads.append(request.url.path)
                hitl.now += timedelta(days=1)
                return httpx.Response(412, json={"error": {"code": "BEHAVIOR_CONFLICT"}})
            return transport.handle_request(request)

        case.client.transport = httpx.MockTransport(conflict)
        assert await asyncio.wait_for(waiting, 10) == (False, "NATIVE_HITL_EXPIRED")
        view = await case.client.read(original.approval.pending_id)
        assert view.status == "expired" and view.pending.expires_at == original.pending.expires_at
        assert view.authorization is None and view.decision is None and len(reads) == 1
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("status,code", [
    (412, "NATIVE_HITL_RECORD_INVALID"), (409, "BEHAVIOR_CONFLICT"), (503, "NATIVE_HITL_AUTHORITY_UNAVAILABLE"),
])
def test_other_read_failures_are_not_swallowed_as_an_owner_snapshot_race(hitl, tmp_path, status, code):
    case = setup_gate(hitl, tmp_path)
    reads = []

    async def check():
        waiting = asyncio.create_task(permit(case))
        original = await pending_view(case)
        transport = case.client.transport

        def refused(request):
            if request.method == "GET" and request.url.path.endswith("/rules"):
                reads.append(request.url.path)
                return httpx.Response(status, json={"error": {"code": code}})
            return transport.handle_request(request)

        case.client.transport = httpx.MockTransport(refused)
        assert await asyncio.wait_for(waiting, 1) == (False, code)
        final = await case.client.read(original.approval.pending_id)
        assert len(reads) == 1 and final.authorization is None and final.status == "interrupted"
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("code", [
    "NATIVE_HITL_HUMAN_DENIED", "NATIVE_HITL_INTERRUPTED", "NATIVE_HITL_EXPIRED", "NATIVE_HITL_EFFECT_UNKNOWN",
])
def test_fixed_tool_feedback_and_public_progress_never_publish_raw_error_material(code):
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.behavior_assistant_progress import public_event
    from shared_context.native_hitl_wire import native_hitl_failure_feedback
    from test_native_capabilities import OfflineResult

    boundary = AssistantModelBoundary(None, text_reply=True, capability_results={"run_command": OfflineResult})
    expected = native_hitl_failure_feedback(code)
    assert boundary.public_tool_failure("run_command", code) == expected
    event = {"type": "m2.tool_failure", "timestamp": "2026-10-03T00:00:00Z", "data": {
        "tool_name": "run_command", "tool_call_id": "original-call", "code": code,
        "error": "PRIVATE exception with token=DO_NOT_PUBLISH"}}
    result = public_event(event, 1, {}, allowed_tools=("run_command",))
    assert result.summary == expected and "PRIVATE" not in result.summary and "token=" not in result.summary
    assert "Authority refused" not in result.summary
    event["data"]["code"] = "PRIVATE exception"
    assert public_event(event, 1, {}, allowed_tools=("run_command",)) is None
