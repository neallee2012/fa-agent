"""B1 PA-11 in the console: SDK ask_user questions join the attention list; one saved owner answer per question."""

from datetime import timedelta
import json

import httpx
import jwt
import pytest
from pydantic import TypeAdapter

from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_team_registry import resolve_agent_scope
from shared_context.native_hitl_wire import (
    NativeAttention, NativeHitlCall, NativeHitlEnd, NativeHitlQuestion, NativeHitlQuestionCreate, NativeHitlTurn,
    NativeQuestionRequiredItem,
)
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_turn_api import native_api  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_web_test_console_native import native_console, first, send, progress  # noqa: F401
from test_web_test_console_responses import responses_console, responses_api  # noqa: F401
from test_web_test_console_hitl import hitl_console, route, ui_post  # noqa: F401

PHASE = "native-question"


def question_call(c, *, call_id="ask-user-local-1", freeform=True):
    draft = first(c)
    if draft["request_id"] not in c.api.remote.records:
        assert send(c, draft).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    c.api.remote.records[draft["request_id"]] = original.model_copy(update={
        "status": "started", "turn_number": 1, "dispatch": None,
        "response_id": "resp-platform" if original.state_profile == "foundry-responses-v1" else None})
    scope = resolve_agent_scope(c.api.composition.runtime.store, c.api.composition.config, original.agent_id)
    agent = BehaviorPrincipal(str(scope.tenant_id), str(scope.identity.oid), scope.team_id, "agent",
                              scope.identity.actor, frozenset({"read", "execute", "consume", "submit"}))
    c.hitl.agent = agent
    question = NativeHitlQuestion(
        call=NativeHitlCall(agent_id=original.agent_id, conversation_id=original.conversation_id,
                           request_id=original.request_id, sdk_session_id="offline-sdk", tool_call_id=call_id),
        question="Send the <b>synthetic</b> report to whom?", choices=("Ann", "Bo"), allow_freeform=freeform)
    receipt = c.hitl.service.submit(agent, NativeHitlQuestionCreate(
        request_id="question-" + call_id, question=question, question_digest=question.sha256))
    return draft, receipt.payload["result"]


def attention_of(c, draft):
    state = progress(c, draft["thread_id"])
    assert state.status_code == 200, state.text
    return state.json()["composer"]["attention"]


def body(item, text="Ann", freeform=False):
    return {"question": item["question"], "answer": text, "was_freeform": freeform}


def answer_posts(c):
    return [call for call in c.calls if call[:2] == ("POST", "/native-hitl/v1/answers")]


@pytest.mark.parametrize("hitl_console", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_a_question_joins_attention_and_one_saved_answer_is_sent_once(hitl_console, signing_key):
    c = hitl_console
    draft, created = question_call(c)
    attention = attention_of(c, draft)
    assert [entry["item"]["type"] for entry in attention] == ["question_required"]
    item = NativeQuestionRequiredItem.model_validate_json(json.dumps(attention[0]["item"]))
    assert isinstance(TypeAdapter(NativeAttention).validate_json(json.dumps(attention[0]["item"])), NativeQuestionRequiredItem)
    assert item.item_id == created["question_id"] and item.choices == ("Ann", "Bo") and not hasattr(item, "approval")
    # An ordinary owner sign-in, without Behavior.Approve, may answer.
    raw = c.api.headers["Authorization"][7:]
    claims = jwt.decode(raw, options={"verify_signature": False})
    claims["scp"] = "Behavior.Read"
    c.ui.console.vault.set(jwt.encode(claims, signing_key, algorithm="RS256", headers=jwt.get_unverified_header(raw)))
    observed = []

    def before_post(request):
        if request.url.path == "/native-hitl/v1/answers" and request.method == "POST":
            command = json.loads(request.content)
            entry = c.ui.console.journal.get(command["request_id"], PHASE)
            assert entry["state"] == "UNKNOWN" and entry["intent"]["body"] == command
            observed.append(command)

    c.before[0] = before_post
    saved = ui_post(c, route(c, "answers"), body(attention[0]["item"]))
    assert saved.status_code == 200, saved.text
    assert len(observed) == 1 and observed[0]["answer"] == "Ann" and observed[0]["was_freeform"] is False
    identifier = saved.json()["request_id"]
    assert c.ui.console.journal.get(identifier, PHASE)["state"] == "CONFIRMED"
    # A second click, even with another answer, or a reload only shows the saved original.
    again = ui_post(c, route(c, "answers"), body(attention[0]["item"], "Bo"))
    assert again.status_code == 200 and again.json()["commands"][0]["command"]["answer"] == "Ann"
    c.ui.client.__exit__(None, None, None)
    c.ui = c.open()
    assert ui_post(c, route(c, "answers"), body(attention[0]["item"], "Bo")).status_code == 200
    assert len(answer_posts(c)) == 1 and observed == observed[:1]
    assert attention_of(c, draft) == []
    assert not [call for call in c.calls if call[1] == "/native-hitl/v1/decisions"]
    assert c.hitl.service.rules(c.hitl.owner).revision == 0
    composer = progress(c, draft["thread_id"]).json()["composer"]
    assert composer["native_questions"]["commands"][0]["request_id"] == identifier
    # Neal 2026-10-07: the answered card stays as "助理問：…／你的回答：…" after a reload; the question text is kept
    # beside the saved original only for that, never sent.
    saved_answer = composer["native_questions"]["commands"][0]
    assert saved_answer["display"] == {"question": "Send the <b>synthetic</b> report to whom?"}
    assert saved_answer["created_at"] == c.ui.console.journal.get(identifier, PHASE)["intent"]["created_at"]
    assert all("display" not in json.loads(call[2]) for call in answer_posts(c))


def test_a_lost_answer_ack_is_only_reconciled_never_sent_again(hitl_console):
    c = hitl_console
    draft, _ = question_call(c)
    item = attention_of(c, draft)[0]["item"]

    def lose(request, response):
        if request.url.path == "/native-hitl/v1/answers":
            assert response.status_code == 200
            raise httpx.ReadTimeout("OFFLINE lost answer response", request=request)
        return response

    c.mutate[0] = lose
    assert ui_post(c, route(c, "answers"), body(item)).status_code != 200
    identifier, original = c.ui.console.journal.entries(PHASE)[0]
    assert original["state"] == "UNKNOWN"
    c.mutate[0] = lambda request, response: response
    c.ui.client.__exit__(None, None, None)
    c.ui = c.open()
    repeated = ui_post(c, route(c, "answers"), body(item, "Bo"))
    assert repeated.status_code == 200 and repeated.json()["commands"][0]["state"] == "UNKNOWN"
    result = ui_post(c, route(c, "answer-reconcile"), {"request_id": identifier})
    assert result.status_code == 200, result.text
    entry = result.json()["commands"][0]
    assert entry["state"] == "CONFIRMED" and entry["observation"]["result"]["status"] == "answered"
    assert len(answer_posts(c)) == 1
    reconciled = [call for call in c.calls if call[1] == "/native-hitl/v1/answers/reconcile"]
    assert len(reconciled) == 1 and json.loads(reconciled[0][2]) == original["intent"]["body"]


@pytest.mark.parametrize("boundary", ["not_a_choice", "no_freeform", "expired", "closed", "digest", "no_login"])
def test_invalid_or_unavailable_answers_never_post(hitl_console, boundary):
    c = hitl_console
    draft, _ = question_call(c, freeform=boundary != "no_freeform")
    item = attention_of(c, draft)[0]["item"]
    value = body(item)
    if boundary == "not_a_choice":
        value["answer"] = "Cy"
    elif boundary == "no_freeform":
        value.update(answer="Someone else", was_freeform=True)
    elif boundary == "expired":
        c.c.h.time += timedelta(seconds=601)
    elif boundary == "closed":
        call = item["question"]["call"]
        c.hitl.service.submit(c.hitl.agent, NativeHitlEnd(request_id="close-original", turn=NativeHitlTurn(**{
            key: call[key] for key in ("agent_id", "conversation_id", "request_id")})))
    elif boundary == "digest":
        value["question"]["question_digest"] = "f" * 64
    else:
        c.ui.console.vault.clear()
    before = len(c.calls)
    refused = ui_post(c, route(c, "answers"), value)
    assert refused.status_code in {400, 401, 403, 409, 410, 502}, refused.text
    assert not any(method == "POST" for method, _, _ in c.calls[before:])
    assert c.ui.console.journal.entries(PHASE) == []


def test_default_off_lists_no_questions_and_has_no_answer_route(hitl_console, monkeypatch):
    c = hitl_console
    draft, _ = question_call(c)
    c.ui.console.native_instructions.hitl.enabled = False
    before = len(c.calls)
    assert attention_of(c, draft) == []
    assert not any(call[1].startswith("/native-hitl/") for call in c.calls[before:])
    refused = ui_post(c, route(c, "answers"), {"question": {}, "answer": "Ann", "was_freeform": False})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_HITL_UNAVAILABLE"
