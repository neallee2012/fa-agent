"""D2 (Neal 2026-11-05): Auto Review's per-rule reasons are kept for the owner to read and never move the outcome."""

from datetime import datetime, timezone

import pydantic
import pytest

from shared_context.behavior_native_turn_wire import material_digest
from shared_context.behavior_delegation_wire import DELEGATION_PREPARE_BYTES
from shared_context.native_hitl_wire import (
    MAX_RULES, NATIVE_HITL_BODY_BYTES, NativeHitlAuthorize, NativeHitlPendingCreate, NativeHitlReview,
)
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import change, hitl, post, proposal, read

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
