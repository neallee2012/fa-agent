"""D2 (Neal 2026-11-05): Auto Review's per-rule reasons are kept for the owner to read and never move the outcome."""

import asyncio
from datetime import datetime, timezone
import json
import logging

import httpx
import pydantic
import pytest

from shared_context.behavior_model_direct import DirectCompletions, DirectResponses
from shared_context.behavior_native_turn_wire import material_digest
from shared_context.behavior_delegation_wire import DELEGATION_PREPARE_BYTES
from shared_context.native_hitl_runtime import _REVIEW_STREAM_BYTES, NativeAutoReview
from shared_context.native_hitl_wire import (
    MAX_RULES, NATIVE_HITL_BODY_BYTES, NativeHitlAuthorize, NativeHitlPendingCreate, NativeHitlReview,
)
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import change, hitl, post, proposal, read
from test_native_hitl_h2 import MODEL_ROOT, pending_view, permit, setup_gate, verdict

assert hitl and jwk_client and signing_key  # fixtures, used by name


def two_rules(c):
    for number, (text, behavior) in enumerate((("寄信前先問我", "ask_first"), ("可以執行 echo", "allow_automatically"))):
        assert post(c, "/rules", change(c, f"d2-rule-{number}", text=text, behavior=behavior)).status_code == 200
    return tuple(rule.rule_id for rule in c.service.rules(c.owner).rules)


def test_a_review_without_reasons_keeps_its_old_bytes_and_digest():
    review = NativeHitlReview(rules_revision=3, matched_rule_ids=("rule-a",), source="rules", outcome="ask",
                              reason="ask_rule", reviewed_at=datetime(2026, 11, 5, 1, 2, 3, tzinfo=timezone.utc))
    # Bytes and digest taken from the same review built before D2 existed.
    assert review.model_dump_json() == ('{"rules_revision":3,"matched_rule_ids":["rule-a"],"source":"rules",'
                                        '"outcome":"ask","reason":"ask_rule","reviewed_at":"2026-11-05T01:02:03Z"}')
    assert material_digest(review) == "0bbf00569d5f482926a87f006addf5b77551ce601c5ba6345d3cd2155e444408"
    explained_review = review.model_copy(update={"rule_reasons": {"rule-a": "要先問"}})
    assert explained_review.model_dump_json().endswith(',"rule_reasons":{"rule-a":"要先問"}}')


@pytest.mark.parametrize("fields", [
    {"source": "toolbox", "outcome": "ask", "reason": "toolbox_always", "rule_reasons": {"rule-a": "x"}},
    {"source": "unavailable", "outcome": "ask", "reason": "review_unavailable", "rule_reasons": {"rule-a": "x"}},
    {"source": "rules", "outcome": "ask", "matched_rule_ids": ("rule-a",), "rule_reasons": {"rule-b": "x"}},
    {"source": "default", "outcome": "ask", "rule_reasons": {"rule-a": ""}},
    {"source": "default", "outcome": "ask", "rule_reasons": {"rule-a": "長" * 201}},
])
def test_the_wire_refuses_reasons_no_model_review_could_give(fields):
    with pytest.raises(pydantic.ValidationError):
        NativeHitlReview(rules_revision=1, **fields)


@pytest.mark.parametrize("reason", [None, "和寄信有關"])
def test_the_api_accepts_a_pending_review_with_and_without_reasons(hitl, reason):
    rule_id = two_rules(hitl)[0]
    original = proposal(hitl, matched=(rule_id,))
    reasons = {rule_id: reason} if reason else {}
    command = NativeHitlPendingCreate(
        request_id=original.request_id, action=original.action, action_digest=original.action_digest,
        review=original.review.model_copy(update={"rule_reasons": reasons}))
    response = post(hitl, "/pending", command, hitl.agent)
    assert response.status_code == 200, response.text
    stored = read(hitl, response.json()["result"]["pending_id"], hitl.owner).json()["pending"]["command"]["review"]
    assert stored.get("rule_reasons") == (reasons or None)


def test_the_api_refuses_a_reason_for_a_rule_the_revision_does_not_have(hitl):
    rule_id = two_rules(hitl)[0]
    original = proposal(hitl, matched=(rule_id,))
    command = original.model_copy(update={"review": original.review.model_copy(
        update={"rule_reasons": {rule_id: "和寄信有關", "rule-gone": "已刪的規則"}})})
    response = post(hitl, "/pending", command, hitl.agent)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "NATIVE_HITL_RULES_CHANGED")


def test_the_largest_delegation_card_with_the_longest_reasons_fits_the_body_limit(hitl):
    for number in range(MAX_RULES):
        assert post(hitl, "/rules", change(hitl, f"d2-cap-{number}", text=f"規則 {number}")).status_code == 200
    ids = tuple(rule.rule_id for rule in hitl.service.rules(hitl.owner).rules)
    original = proposal(hitl, matched=ids[:1])
    action = original.action.model_copy(update={
        "category": "delegation", "tool": "task", "target": "fa-delegate-v1",
        "arguments": {"name": "large-child", "prompt": "q" * DELEGATION_PREPARE_BYTES}})
    # Sent as compact UTF-8, a control character still escapes to six bytes: the longest a reason can be.
    review = original.review.model_copy(update={"rule_reasons": dict.fromkeys(ids, "\x01" * 200)})
    command = NativeHitlPendingCreate(request_id=original.request_id, action=action, action_digest=action.sha256,
                                      review=review)
    size = len(command.model_dump_json().encode())
    assert DELEGATION_PREPARE_BYTES + 24 * 1024 < size <= NATIVE_HITL_BODY_BYTES  # the old margin was too small
    response = post(hitl, "/pending", command, hitl.agent)
    assert response.status_code == 200, response.text


def test_authorize_refuses_a_reason_for_a_rule_the_revision_does_not_have(hitl):
    two_rules(hitl)
    original = proposal(hitl)
    rules = hitl.service.rules(hitl.owner)
    command = NativeHitlAuthorize(
        request_id="authorize-d2", action=original.action, action_digest=original.action_digest,
        review=NativeHitlReview(rules_revision=rules.revision, source="default", outcome="allow",
                                rule_reasons={"rule-gone": "已刪的規則"}),
        expected_rules_etag=rules.etag, effect_token="effect-d2")
    response = post(hitl, "/authorize", command, hitl.agent)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "NATIVE_HITL_RULES_CHANGED")


def answer(outcome, matched, results):
    """``results`` as rule items, sent in each outcome's strict shape: an allow's reasons in its matched order, an
    ask or deny's keyed by rule ID."""
    if outcome == "allow":
        return verdict(outcome, [item["rule_id"] for item in results], results=[item["reason"] for item in results])
    return verdict(outcome, matched, results={item["rule_id"]: item["reason"] for item in results})


def explained(keys, reasons=None):
    reasons = [f"說明 {number}" for number in range(len(keys))] if reasons is None else reasons
    return [{"rule_id": key, "reason": reason} for key, reason in zip(keys, reasons)]


def reviewed(c, tmp_path, model_answer):
    case = setup_gate(c, tmp_path, (model_answer,))

    async def check():
        return await case.gate.reviewer(case.gate._action(case.packet), await case.client.rules(), "Original request")
    return case, asyncio.run(check())


def test_each_rules_reason_is_stored_with_the_review_and_read_back_through_the_api(hitl, tmp_path):
    ids = two_rules(hitl)
    case = setup_gate(hitl, tmp_path, (answer("ask", ids[:1], explained(ids, ("和寄信有關", "不是 echo 指令"))),))

    async def check():
        waiting = asyncio.create_task(permit(case))
        view = await pending_view(case)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        return view
    view = asyncio.run(check())
    review = view.pending.command.review
    assert (review.outcome, review.reason, review.matched_rule_ids) == ("ask", "ask_rule", ids[:1])
    assert review.rule_reasons == {ids[0]: "和寄信有關", ids[1]: "不是 echo 指令"}
    body = read(hitl, view.approval.pending_id, hitl.owner).json()
    assert body["pending"]["command"]["review"]["rule_reasons"] == {ids[0]: "和寄信有關", ids[1]: "不是 echo 指令"}
    schema = case.sender.observed[0]["text"]["format"]["schema"]
    assert schema["required"] == ["decision"]
    allow, held = schema["properties"]["decision"]["anyOf"]
    # Every field that decides the outcome comes before any reason; an allow's reasons name no rule, so add none.
    decided = ["outcome", "matched_rule_ids", "uncertain", "takeover_required"]
    assert allow["required"] == list(allow["properties"]) == [*decided, "matched_reasons"]
    assert allow["properties"]["outcome"]["enum"] == ["allow"]
    assert allow["properties"]["matched_reasons"]["items"] == {"type": "string"}
    assert held["required"] == list(held["properties"]) == [*decided, "rule_results"]
    assert held["properties"]["outcome"]["enum"] == ["ask", "deny"]
    # An ask or deny cannot leave a rule out: each rule is its own required property, in revision order.
    every = held["properties"]["rule_results"]
    assert every["required"] == list(every["properties"]) == list(ids) and every["additionalProperties"] is False


@pytest.mark.parametrize("bad,problem", [
    pytest.param(["適用", "多一句"], "ids", id="one-reason-too-many"),
    pytest.param([], "ids", id="no-reason"),
    pytest.param(["  "], "blank", id="blank-reason"),
    pytest.param([chr(0xD83D)], "type", id="lone-surrogate"),
    pytest.param([1], "type", id="not-text"),
    pytest.param("not a list", "type", id="not-a-list"),
    pytest.param(None, "missing", id="null"),
])
def test_an_allow_with_a_bad_explanation_drops_only_the_reasons(hitl, tmp_path, caplog, bad, problem):
    ids = two_rules(hitl)
    caplog.set_level(logging.WARNING, "shared_context.native_hitl_runtime")
    model_answer = verdict("allow", ids[1:])
    model_answer["decision"]["matched_reasons"] = bad
    _, result = reviewed(hitl, tmp_path, model_answer)
    assert (result.outcome, result.reason, result.source, result.matched_rule_ids) == (
        "allow", "reviewed", "rules", ids[1:])
    assert result.rule_reasons == {}
    assert f"NATIVE_HITL_REVIEW_REASONS_DROPPED problem={problem}" in caplog.text
    assert "NATIVE_HITL_REVIEW_INVALID" not in caplog.text


@pytest.mark.parametrize("matched,reasons", [
    pytest.param((), ["沒有命中卻有說明"], id="no-match-one-reason"),
    pytest.param((1, 1), ["適用", "又一句"], id="repeated-id"),
])
def test_an_allow_whose_reasons_cannot_pair_with_its_matches_drops_them(hitl, tmp_path, caplog, matched, reasons):
    ids = two_rules(hitl)
    caplog.set_level(logging.WARNING, "shared_context.native_hitl_runtime")
    _, result = reviewed(hitl, tmp_path, verdict("allow", [ids[number] for number in matched], results=reasons))
    expected = (ids[1],) if matched else ()
    assert (result.outcome, result.reason, result.matched_rule_ids, result.rule_reasons) == (
        "allow", "reviewed", expected, {})
    assert "NATIVE_HITL_REVIEW_REASONS_DROPPED problem=ids" in caplog.text
    assert "NATIVE_HITL_REVIEW_INVALID" not in caplog.text


@pytest.mark.parametrize("bad,problem", [
    pytest.param(lambda ids: dict.fromkeys(ids, "  "), "blank", id="blank-reason"),
    pytest.param(lambda ids: dict.fromkeys(ids, chr(0xD83D)), "type", id="lone-surrogate"),
    pytest.param(lambda ids: dict.fromkeys(ids, 1), "type", id="not-text"),
    pytest.param(lambda ids: {**dict.fromkeys(ids[:-1], "x"), "invented": "x"}, "ids", id="unknown-id"),
    pytest.param(lambda ids: dict.fromkeys(ids[:-1], "x"), "ids", id="a-rule-missing"),
    pytest.param(lambda ids: "not a map", "type", id="not-a-map"),
    pytest.param(lambda ids: None, "missing", id="null"),
])
@pytest.mark.parametrize("outcome,matched,expected", [
    ("ask", 1, ("ask", "ask_rule")),       # the Ask-first rule
    ("deny", 2, ("deny", "reviewed")),     # the automatic-allow rule
])
def test_an_ask_or_deny_with_a_bad_explanation_drops_only_the_reasons(hitl, tmp_path, caplog, bad, problem,
                                                                      outcome, matched, expected):
    ids = two_rules(hitl)
    chosen = ids[matched - 1:matched]
    caplog.set_level(logging.WARNING, "shared_context.native_hitl_runtime")
    _, result = reviewed(hitl, tmp_path, verdict(outcome, chosen, results=bad(ids)))
    assert (result.outcome, result.reason, result.matched_rule_ids, result.rule_reasons) == (*expected, chosen, {})
    assert f"NATIVE_HITL_REVIEW_REASONS_DROPPED problem={problem}" in caplog.text
    assert "NATIVE_HITL_REVIEW_INVALID" not in caplog.text


@pytest.mark.parametrize("outcome,matched,keys,expected", [
    pytest.param("allow", 2, 2, ("allow", "reviewed"), id="allow-explains-its-match"),
    pytest.param("allow", 0, 0, ("allow", "reviewed"), id="allow-without-a-match-explains-nothing"),
    # The model's own allow sets the shape, even when the Ask-first rule it matched turns it into ask.
    pytest.param("allow", 1, 1, ("ask", "ask_rule"), id="allow-made-ask-by-its-ask-rule"),
    pytest.param("ask", 1, None, ("ask", "ask_rule"), id="ask-explains-every-rule"),
    pytest.param("deny", 2, None, ("deny", "reviewed"), id="deny-explains-every-rule"),
])
def test_the_reasons_the_outcome_asks_for_are_stored_in_revision_order(hitl, tmp_path, caplog, outcome, matched,
                                                                       keys, expected):
    ids = two_rules(hitl)
    chosen = ids[matched - 1:matched] if matched else ()
    keyed = ids if keys is None else ids[keys - 1:keys] if keys else ()
    caplog.set_level(logging.WARNING, "shared_context.native_hitl_runtime")
    _, result = reviewed(hitl, tmp_path, answer(outcome, chosen, explained(keyed[::-1])))
    assert (result.outcome, result.reason, result.matched_rule_ids) == (*expected, chosen)
    assert list(result.rule_reasons) == list(keyed)
    assert "NATIVE_HITL_REVIEW_REASONS_DROPPED" not in caplog.text


def test_an_allows_reasons_pair_with_its_matched_rules_by_position(hitl, tmp_path):
    ids = two_rules(hitl)
    _, result = reviewed(hitl, tmp_path, verdict("allow", ids[::-1], results=["B", "A"]))
    assert (result.outcome, result.reason, result.matched_rule_ids) == ("ask", "ask_rule", ids[::-1])
    assert result.rule_reasons == {ids[0]: "A", ids[1]: "B"} and list(result.rule_reasons) == list(ids)


def test_an_ask_without_the_list_reviews_as_before(hitl, tmp_path):
    ids = two_rules(hitl)
    _, result = reviewed(hitl, tmp_path, verdict("deny", ids[1:]))
    assert (result.outcome, result.reason, result.rule_reasons) == ("deny", "reviewed", {})


def test_a_long_reason_is_cut_to_200_characters(hitl, tmp_path):
    ids = two_rules(hitl)
    _, result = reviewed(hitl, tmp_path, answer("ask", ids[:1], explained(ids, ("長" * 250, " 不是 echo "))))
    assert result.rule_reasons == {ids[0]: "長" * 200, ids[1]: "不是 echo"}


def test_an_uncertain_review_stores_no_reasons(hitl, tmp_path):
    ids = two_rules(hitl)
    model_answer = answer("ask", ids[:1], explained(ids))
    model_answer["decision"]["uncertain"] = True
    _, result = reviewed(hitl, tmp_path, model_answer)
    assert (result.outcome, result.reason, result.source, result.matched_rule_ids, result.rule_reasons) == (
        "ask", "review_uncertain", "uncertain", (), {})


def streamed(text, *, responses):
    """One SSE event per character with the padding real events carry: more events than tokens, the pessimistic case."""
    if responses:
        events = [{"type": "response.output_text.delta", "item_id": "msg_" + "0" * 48, "output_index": 0,
                   "content_index": 0, "delta": char, "sequence_number": number, "logprobs": [],
                   "obfuscation": "0" * 16} for number, char in enumerate(text)]
        events.append({"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}]}})
        tail = ""
    else:
        filters = {name: {"filtered": False, "severity": "safe"} for name in ("hate", "self_harm", "sexual", "violence")}
        events = [{"id": "chatcmpl-" + "0" * 29, "object": "chat.completion.chunk", "created": 1, "model": "offline",
                   "choices": [{"index": 0, "delta": {"content": char}, "finish_reason": None,
                                "content_filter_results": filters}]} for char in text]
        events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        tail = "data: [DONE]\n\n"
    return ("".join(f"data: {json.dumps(event)}\n\n" for event in events) + tail).encode()


def stream_review(c, tmp_path, content, *, responses):
    """Review with a model whose whole streamed answer is ``content``; returns the result and the request it was sent."""
    observed = []

    def model(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
    case = setup_gate(c, tmp_path, responses=responses)
    sender = (DirectResponses if responses else DirectCompletions)(
        MODEL_ROOT + ("/responses" if responses else "/chat/completions"), lambda: "OFFLINE model credential",
        "OFFLINE-model", transport=httpx.MockTransport(model))

    async def check():
        return await NativeAutoReview(sender)(case.gate._action(case.packet), await case.client.rules(), "Original")
    return asyncio.run(check()), observed[0]


@pytest.mark.parametrize("responses", [True, False])
def test_twenty_longest_reasons_streamed_one_event_per_character_are_kept(hitl, tmp_path, responses):
    for number in range(MAX_RULES):
        assert post(hitl, "/rules", change(hitl, f"d2-stream-{number}", text=f"規則 {number}")).status_code == 200
    ids = [rule.rule_id for rule in hitl.service.rules(hitl.owner).rules]
    # An ask that matched every rule, each with the longest reason (escaped to \uXXXX in every event).
    text = json.dumps(answer("ask", ids, explained(ids, ["長" * 200] * len(ids))), ensure_ascii=False)
    result, _ = stream_review(hitl, tmp_path, streamed(text, responses=responses), responses=responses)
    assert (result.outcome, result.reason, len(result.rule_reasons)) == ("ask", "ask_rule", MAX_RULES)


@pytest.mark.parametrize("responses", [True, False])
def test_a_review_streamed_past_its_bound_asks(hitl, tmp_path, caplog, responses):
    two_rules(hitl)
    caplog.set_level(logging.WARNING, "shared_context.native_hitl_runtime")
    padding = f"data: {json.dumps({'padding': 'x' * _REVIEW_STREAM_BYTES})}\n\n".encode()
    result, _ = stream_review(hitl, tmp_path, padding, responses=responses)
    assert (result.outcome, result.reason) == ("ask", "review_invalid")
    assert "NATIVE_HITL_REVIEW_INVALID reason=too_large" in caplog.text


@pytest.mark.parametrize("responses", [True, False])
def test_a_truncated_review_asks_and_its_cap_grows_with_the_rules(hitl, tmp_path, caplog, responses):
    two_rules(hitl)
    def model(request):
        event = ({"type": "response.incomplete", "response": {
            "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}} if responses else
                 {"choices": [{"index": 0, "delta": {"content": "{"}, "finish_reason": "length"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(event)}\n\n".encode())
    caplog.set_level(logging.WARNING, "shared_context.native_hitl_runtime")
    result, sent = stream_review(hitl, tmp_path, model(None).content, responses=responses)
    assert (result.outcome, result.reason) == ("ask", "review_invalid")
    assert sent["max_output_tokens" if responses else "max_completion_tokens"] == 16000 + 260 * 2
    assert "NATIVE_HITL_REVIEW_INVALID reason=no_completed_output" in caplog.text
