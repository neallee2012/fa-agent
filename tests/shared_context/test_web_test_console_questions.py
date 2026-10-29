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
    assert result.json()["original_request_resent"] is False
    assert result.json()["purpose"] == "readback-only-not-execution-permission"
    confirmed = c.ui.console.journal.get(identifier, PHASE)
    assert confirmed["automatic_retry"] is False
    # Checking a confirmed answer again keeps its first confirmation as it was saved.
    c.c.h.time += timedelta(seconds=30)
    assert ui_post(c, route(c, "answer-reconcile"), {"request_id": identifier}).status_code == 200
    assert c.ui.console.journal.get(identifier, PHASE) == confirmed
    assert len(answer_posts(c)) == 1
    reconciled = [call for call in c.calls if call[1] == "/native-hitl/v1/answers/reconcile"]
    assert len(reconciled) == 2 and all(json.loads(call[2]) == original["intent"]["body"] for call in reconciled)


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


def page_reads(c, start):
    return [json.loads(raw)["after"] for method, path, raw in c.calls[start:]
            if (method, path) == ("POST", "/native-hitl/v1/turn-questions")]


def test_twenty_one_questions_on_one_turn_are_read_over_two_pages(hitl_console):
    c = hitl_console
    for number in range(21):
        draft, _ = question_call(c, call_id="ask-user-local-" + str(number))
    before = len(c.calls)
    attention = attention_of(c, draft)
    assert len(attention) == 21 and {entry["item"]["type"] for entry in attention} == {"question_required"}
    assert page_reads(c, before) == [0, 20]


@pytest.mark.parametrize("damage", ["stuck", "other_turn", "missing_api", "denied"])
def test_a_question_page_that_cannot_be_read_shows_its_code_and_a_denied_read_fails_the_page(hitl_console, damage):
    c = hitl_console
    draft, _ = question_call(c)
    reads = []

    def damage_page(request, response):
        if request.url.path != "/native-hitl/v1/turn-questions":
            return response
        reads.append(json.loads(request.content)["after"])
        # A second read only comes from a reader that does not stop at a page that never moves on.
        if damage == "missing_api" or len(reads) > 1:
            return httpx.Response(404, json={"detail": "OFFLINE unavailable"})
        if damage == "denied":
            return httpx.Response(403, json={"detail": "OFFLINE denied"})
        data = response.json()
        if damage == "stuck":
            data["next_after"] = 0
        else:
            data["turn"]["request_id"] = "another-request"
        return httpx.Response(200, json=data, headers=response.headers)

    c.mutate[0] = damage_page
    state = progress(c, draft["thread_id"])
    assert reads == [0]
    if damage == "denied":
        assert state.status_code == 409 and state.json()["error"]["code"] == "BACKEND_DENIED", state.text
    else:
        assert state.status_code == 200, state.text
        assert state.json()["composer"]["native_questions"] == {"commands": [], "error": {
            "code": "NATIVE_HITL_UNAVAILABLE" if damage == "missing_api" else "NATIVE_HITL_RECORD_INVALID"}}
    c.mutate[0] = lambda request, response: response
    assert progress(c, draft["thread_id"]).json()["composer"]["native_questions"] == {"commands": [], "error": None}


def test_a_question_read_covers_only_this_threads_open_turns_and_a_finished_turn_drops_it(hitl_console, monkeypatch):
    c = hitl_console
    draft, created = question_call(c)
    native = c.ui.console.native_instructions
    rid, thread, token = draft["request_id"], draft["thread_id"], c.ui.console.vault.get()

    def refresh(thread_id, **options):
        before = len(c.calls)
        native.questions.refresh(thread_id, token, **options)
        return page_reads(c, before)

    assert refresh("another-thread") == []
    assert refresh(thread, only="another-request") == []
    assert refresh(thread, only=rid) == [0]
    assert [entry["item"]["item_id"] for entry in native.questions.attention()] == [created["question_id"]]
    started = native.receipts[rid]
    for status in ("completed", "busy_refused"):
        monkeypatch.setitem(native.receipts, rid, started)
        assert refresh(thread) == [0] and len(native.questions.attention()) == 1
        monkeypatch.setitem(native.receipts, rid, started.model_copy(update={"status": status}))
        assert refresh(thread) == [] and native.questions.attention() == []
    monkeypatch.setitem(native.receipts, rid, started)
    assert refresh(thread) == [0] and len(native.questions.attention()) == 1
    # Signed out: the question already read stays out of the list until the owner signs in again.
    c.ui.console.vault.clear()
    assert native.questions.attention() == []


def test_a_page_that_says_the_turn_closed_lists_no_question_even_with_items_on_it(hitl_console):
    c = hitl_console
    draft, _ = question_call(c)
    assert len(attention_of(c, draft)) == 1

    def closed(request, response):
        if request.url.path != "/native-hitl/v1/turn-questions":
            return response
        data = response.json()
        assert len(data["attention"]) == 1
        data["closed"] = True
        return httpx.Response(200, json=data, headers=response.headers)

    c.mutate[0] = closed
    assert attention_of(c, draft) == []


@pytest.mark.parametrize("damage", ["missing_fields", "method"])
def test_a_damaged_saved_answer_is_shown_as_an_error_and_is_never_sent(hitl_console, damage):
    c = hitl_console
    draft, _ = question_call(c)
    journal = c.ui.console.journal
    if damage == "missing_fields":
        identifier = "damaged-answer"
        journal.begin(identifier, PHASE, {"created_at": "2026-01-01T00:00:00+00:00"})
    else:
        saved = ui_post(c, route(c, "answers"), body(attention_of(c, draft)[0]["item"]))
        assert saved.status_code == 200, saved.text
        identifier = saved.json()["request_id"]
        intent = {**journal.get(identifier, PHASE)["intent"], "method": "PUT"}
        with journal.connect() as db:
            db.execute("UPDATE intents SET intent=? WHERE task_id=? AND phase=?",
                       (json.dumps(intent), identifier, PHASE))
    state = progress(c, draft["thread_id"])
    assert state.status_code == 200, state.text
    assert state.json()["composer"]["native_questions"] == {
        "commands": [], "error": {"code": "NATIVE_HITL_ORIGINAL_INVALID"}}
    before = len(c.calls)
    refused = ui_post(c, route(c, "answer-reconcile"), {"request_id": identifier})
    assert refused.status_code == 502 and refused.json()["error"]["code"] == "NATIVE_HITL_ORIGINAL_INVALID"
    assert not any(method == "POST" for method, _, _ in c.calls[before:])


def test_a_reconcile_receipt_for_another_control_request_is_refused_and_keeps_the_unknown_answer(hitl_console):
    c = hitl_console
    draft, _ = question_call(c)
    item = attention_of(c, draft)[0]["item"]

    def lose(request, response):
        if request.url.path == "/native-hitl/v1/answers":
            raise httpx.ReadTimeout("OFFLINE lost answer response", request=request)
        if request.url.path == "/native-hitl/v1/answers/reconcile":
            data = response.json()
            data["control_request_id"] = "0" * 64
            return httpx.Response(200, json=data, headers=response.headers)
        return response

    c.mutate[0] = lose
    assert ui_post(c, route(c, "answers"), body(item)).status_code != 200
    identifier, original = c.ui.console.journal.entries(PHASE)[0]
    refused = ui_post(c, route(c, "answer-reconcile"), {"request_id": identifier})
    assert refused.status_code == 502 and refused.json()["error"]["code"] == "NATIVE_HITL_RECORD_INVALID"
    assert c.ui.console.journal.get(identifier, PHASE) == original and original["state"] == "UNKNOWN"
