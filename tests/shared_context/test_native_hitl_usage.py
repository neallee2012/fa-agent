"""Discriminating model/review counts through the original signed Agent+RID ledger; no live I/O."""

import asyncio
import json

import httpx
import pytest

from shared_context.behavior_model_direct import DirectCompletions
from shared_context.behavior_native_turn_wire import NativeReceipt
from shared_context.behavior_work_budget import BudgetEnforcementCommand, set_budget_enforcement
from shared_context.errors import Problem
from shared_context.native_hitl_runtime import NativeAutoReview
from shared_context.native_hitl_wire import NativeHitlAction, NativeHitlCall, NativeHitlRules
from shared_context.native_sdk import NativeUsageReporter
from shared_context.native_assembly import NativeAssembly
from shared_context.native_inference import NativeInferenceCollector
from test_agent_turn_usage_ledger import saved
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from test_native_hitl_h2 import MODEL_ROOT, verdict
from test_native_turn_api import PREFIX, native_api, submit
from test_turn_usage_ledger import ledger


@pytest.fixture
def usage_case(request):
    loop = asyncio.new_event_loop()
    case = request.getfixturevalue("native_api")
    yield case, loop
    loop.close()


@pytest.mark.parametrize("review_failure", [None, "unavailable", "stream_unknown"])
def test_review_usage_reaches_the_same_agent_rid_ledger_once_with_budget_disabled(
        usage_case, signing_key, review_failure):
    case, loop = usage_case
    c = case.c
    set_budget_enforcement(c.control, c.human, BudgetEnforcementCommand(
        request_id="offline-budget-mode", policy_sha256=c.control.policy.sha256, enforced=False))
    assert ledger(c).budget_enforced is False
    assert submit(case, rid="usage-with-review", model="gpt-4.1-mini").status_code == 202
    original = case.remote.records["usage-with-review"]
    sent = []

    def model(request):
        body = json.loads(request.content)
        review = body.get("response_format", {}).get("json_schema", {}).get("name") == "fa_auto_review"
        sent.append("review" if review else "execution")
        if review and review_failure == "unavailable":
            return httpx.Response(503, json={"error": {"code": "OFFLINE-unavailable"}})
        if review and review_failure == "stream_unknown":
            class Interrupted(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'data: {"choices":[{"delta":{"content":"{"},"finish_reason":null}]}\n\n'
                    raise httpx.ReadError("OFFLINE interrupted review stream")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Interrupted())
        counts = (7, 3) if review else (100, 10) if len(sent) == 1 else (200, 20)
        text = json.dumps(verdict("ask")) if review else "OFFLINE execution response"
        value = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": counts[0], "completion_tokens": counts[1]}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("data: " + json.dumps(value) + "\n\ndata: [DONE]\n\n").encode())

    observation = NativeInferenceCollector("offline-sdk")
    sender = DirectCompletions(MODEL_ROOT + "/chat/completions", lambda: "OFFLINE", original.capture.model.id,
                               transport=httpx.MockTransport(model), inference=observation)

    async def model_request(content):
        response = await sender.send(httpx.Request("POST", sender.url, json={
            "model": sender.model, "stream": True, "messages": [{"role": "user", "content": content}]}))
        await response.aread()
        await response.aclose()

    async def turn():
        await model_request("Synthetic instruction")
        action = NativeHitlAction(
            call=NativeHitlCall(agent_id=original.agent_id, conversation_id=original.conversation_id,
                               request_id=original.request_id, sdk_session_id="offline-sdk", tool_call_id="one-call"),
            category="shell", tool="run_command", target="synthetic workspace", arguments={"command": "echo synthetic"})
        reviewed = await NativeAutoReview(sender)(
            action, NativeHitlRules(team_id=c.control.policy.template.team_id, revision=0, etag=None),
            {"initial": "Synthetic instruction"})
        assert reviewed.outcome == "ask"
        assert reviewed.reason == ("reviewed" if review_failure is None else "review_unavailable")
        await model_request("Synthetic original tool result")
        return sender.usage()

    usage = loop.run_until_complete(turn())
    assert sent == ["execution", "review", "execution"]
    assert usage.usage[0].requests == 3
    assert usage.usage[0].measured_requests == (3 if review_failure is None else 2)
    assert (usage.usage[0].input_tokens, usage.usage[0].output_tokens) == (
        (307, 33) if review_failure is None else (300, 30))
    inference = observation.snapshot()
    assert [request.purpose for request in inference.requests] == ["unknown", "review", "unknown"]
    assert (inference.logical_prepared, inference.transport_started) == (3, 3)
    observation.event({"type": "assistant.usage", "id": "sdk-reported-only", "data": {
        "model": sender.model, "input_tokens": 1000, "output_tokens": 100}})
    assembly = NativeAssembly(state="recorded", inference=observation.snapshot())
    # The VM transport is synthetic here; real executor/NativeReceipt persistence is exercised by lane_wait.
    case.remote.completed(original.request_id)
    terminal = NativeReceipt.model_validate_json(
        case.remote.records[original.request_id].model_copy(update={"usage": usage, "assembly": assembly}).model_dump_json())
    case.remote.records[original.request_id] = terminal
    agent_token = token(signing_key, {
        "oid": c.control.policy.template.agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",))
    reports, fail = [], ["before"]

    def api(request):
        expected = "/agents/" + original.agent_id + "/work-policy/v1/turn-usage"
        assert request.url.path == expected and request.method == "POST"
        body = json.loads(request.content)
        assert body == {"request_id": original.request_id, "usage": usage.model_dump(mode="json")}
        reports.append(body)
        if fail[0] == "before":
            fail[0] = "after"
            return httpx.Response(503, json={"error": {"code": "OFFLINE-collector-unavailable"}})
        # The fixture owns one selected Agent composition; production's prefix selects that same composition.
        response = case.client.post("/work-policy/v1/turn-usage", content=request.content, headers=request.headers)
        assert response.status_code == 200, response.text
        if fail[0] == "after":
            fail[0] = None
            raise httpx.ReadTimeout("OFFLINE usage ACK lost", request=request)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    reporter = NativeUsageReporter("https://api.offline.invalid", original.agent_id, lambda: agent_token,
                                   transport=httpx.MockTransport(api))
    before = ledger(c)
    with pytest.raises(Problem) as unavailable:
        loop.run_until_complete(reporter(terminal))
    assert unavailable.value.body["error"]["code"] == "NATIVE_POST_TURN_UNKNOWN"
    assert ledger(c) == before and terminal.usage == usage and len(sent) == 3
    with pytest.raises(Problem) as unknown:
        loop.run_until_complete(reporter(terminal))
    assert unknown.value.body["error"]["code"] == "NATIVE_POST_TURN_UNKNOWN"
    recorded = ledger(c)
    assert recorded.budget_enforced is False
    assert recorded.model_metered_microusd == before.model_metered_microusd + (176 if review_failure is None else 168)
    assert recorded.model_unmetered_requests == before.model_unmetered_requests + int(review_failure is not None)
    assert saved(c, original.request_id).usage == usage
    readback = case.client.get(PREFIX + "/requests/" + original.request_id, headers=case.headers)
    assert readback.status_code == 200, readback.text
    assert readback.json()["usage"] == usage.model_dump(mode="json")
    assert readback.json()["assembly"]["inference"]["sdk_usage"][0]["tokens"]["input_tokens"] == 1000
    loop.run_until_complete(reporter(terminal))
    assert ledger(c) == recorded and len(reports) == 3 and len(sent) == 3
    assert all(report == reports[0] for report in reports)
    assert c.control.records == {}
