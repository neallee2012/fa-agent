"""Pins NativeHitlService.submit's control changes per command: exact results, refusals and their order."""

from dataclasses import replace
from datetime import timedelta

import pytest

from conftest import A, TENANT
from shared_context.errors import Problem
from shared_context.native_hitl import consent_id, pending_id, question_id
from shared_context.native_hitl_wire import (
    ConnectorConsent, ConnectorConsentCreate, ConnectorConsentDecision, NativeHitlAction, NativeHitlAuthorize,
    NativeHitlCall, NativeHitlClose, NativeHitlEnd, NativeHitlPendingCreate, NativeHitlPendingQuery, NativeHitlReview,
    NativeHitlTurn,
)
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_hitl import START, change, decision, hitl, proposal  # noqa: F401
from test_native_hitl_questions import answer, ask

TURN = NativeHitlTurn(agent_id="native-a", conversation_id="cid-one", request_id="rid-one")
PURPOSE = "readback-only-not-execution-permission"


def submit(c, principal, command):
    return c.service.submit(principal, command).payload["result"]


def refused(c, principal, command, status, code, message):
    with pytest.raises(Problem) as caught:
        c.service.submit(principal, command)
    assert (caught.value.status, caught.value.body["error"]["code"], caught.value.body["error"]["message"]) == (
        status, code, message)


def base(command):
    return {"request_id": command.request_id, "kind": command.kind, "purpose": PURPOSE}


def end(c, request_id="end-one", turn=TURN):
    return submit(c, c.agent, NativeHitlEnd(request_id=request_id, turn=turn))


def expiring(c, command):
    """The intent is saved at START; its own apply reads the clock exactly at the wait's deadline."""
    times = iter((START, START + timedelta(seconds=c.service.wait_seconds[command.wait])))
    c.service.clock = lambda: next(times)


def pending(c, **kwargs):
    command = proposal(c, **kwargs)
    return command, submit(c, c.agent, command)


def routine(c, *, arguments=None, source="default", call_id="propose-one"):
    action = NativeHitlAction(
        call=NativeHitlCall(agent_id="native-a", conversation_id="cid-one", request_id="rid-one",
                            sdk_session_id="sdk-original", tool_call_id=call_id),
        category="routine", tool="propose_routine", target="conversation:cid-one",
        arguments={"local_time": "09:30", "time_zone": "UTC", "content": "Summarise the synthetic notes."}
        if arguments is None else arguments)
    return NativeHitlPendingCreate(
        request_id="pending-" + call_id, action=action, action_digest=action.sha256,
        review=NativeHitlReview(rules_revision=0, source=source, outcome="ask",
                               **({"reason": "review_unavailable"} if source == "unavailable" else {})))


def toolbox(c):
    action = NativeHitlAction(
        call=NativeHitlCall(agent_id="native-a", conversation_id="cid-one", request_id="rid-one",
                            sdk_session_id="sdk-original", tool_call_id="toolbox-one"),
        category="mcp", tool="search", target="synthetic-toolbox", arguments={"query": "synthetic"})
    return NativeHitlPendingCreate(
        request_id="pending-toolbox", action=action, action_digest=action.sha256,
        review=NativeHitlReview(rules_revision=0, source="toolbox", reason="toolbox_always", outcome="ask"))


def authorize(command, request_id="authorize-one"):
    return NativeHitlAuthorize(
        request_id=request_id, action=command.action, action_digest=command.action_digest,
        review=NativeHitlReview(rules_revision=0, source="default", outcome="allow"),
        expected_rules_etag=None, effect_token="effect-" + request_id)


def close(created, status, request_id="close-one", digest=None):
    return NativeHitlClose(request_id=request_id, pending_id=created["pending_id"],
                           pending_digest=digest or created["pending_digest"], status=status)


def consent(call_id="connect-one", request_id=None, toolbox_name="fa-connector-team-one-teams"):
    asked = ConnectorConsent(
        call=NativeHitlCall(agent_id="native-a", conversation_id="cid-one", request_id="rid-one",
                            sdk_session_id="sdk-original", tool_call_id=call_id),
        need="consent", connector_id="teams", toolbox_name=toolbox_name, server_label="teams_read",
        step="tools/list", consent_urls=("https://consent.example.test/login?state=x",))
    return ConnectorConsentCreate(request_id=request_id or "create-" + call_id, consent=asked,
                                  consent_digest=asked.sha256)


def click(created, choice="resume", request_id="click-one", digest=None):
    return ConnectorConsentDecision(request_id=request_id, consent_id=created["consent_id"],
                                    consent_digest=digest or created["consent_digest"], choice=choice)


# --- End and rule changes -------------------------------------------------------------------------------------------

def test_end_closes_a_new_or_existing_turn_index(hitl):
    c = hitl
    empty = NativeHitlTurn(agent_id="native-a", conversation_id="cid-one", request_id="rid-empty")
    command = NativeHitlEnd(request_id="end-empty", turn=empty)
    assert submit(c, c.agent, command) == {**base(command), "turn": empty.model_dump(mode="json"), "closed": True}
    assert c.service.pending(c.owner, NativeHitlPendingQuery(turn=empty)).closed is True
    pending(c)
    assert end(c)["closed"] is True
    assert c.service.pending(c.owner, NativeHitlPendingQuery(turn=TURN)).closed is True


def test_end_of_another_agents_turn_is_refused(hitl):
    c = hitl
    refused(c, c.other_agent, NativeHitlEnd(request_id="end-other", turn=TURN),
            403, "NATIVE_HITL_SCOPE_DENIED", "the call belongs to another Agent or Team")


def test_rule_changes_return_the_new_revision_and_stable_rule_id(hitl):
    c = hitl
    added = change(c, "rule-add")
    result = submit(c, c.owner, added)
    rule_id = result["rule_id"]
    assert result == {**base(added), "rules_revision": 1, "rule_id": rule_id}
    assert [rule.rule_id for rule in c.service.rules(c.owner).rules] == [rule_id]
    updated = change(c, "rule-update", "update", rule_id=rule_id, text="Ask before every external message.")
    assert submit(c, c.owner, updated) == {**base(updated), "rules_revision": 2, "rule_id": rule_id}
    refused(c, c.owner, change(c, "rule-missing", "delete", rule_id="rule-not-current"),
            409, "NATIVE_HITL_RULE_MISSING", "the selected stable rule ID is not current")
    stale = change(c, "rule-stale").model_copy(update={"expected_rules_revision": 1})
    refused(c, c.owner, stale, 409, "NATIVE_HITL_RULES_CHANGED",
            "read the current rules and reassess the original action")


# --- Pending approval cards -----------------------------------------------------------------------------------------

def test_pending_create_returns_the_card_reference(hitl):
    c = hitl
    command, result = pending(c)
    pid = pending_id(TENANT, A.team, command.action.call)
    view = c.service.read(c.owner, pid)
    assert result == {**base(command), "pending_id": pid, "pending_digest": view.pending.sha256, "status": "pending"}
    assert view.status == "pending" and view.pending.expires_at == START + timedelta(seconds=600)


def test_pending_create_after_the_turn_ended_is_refused(hitl):
    c = hitl
    end(c)
    refused(c, c.agent, proposal(c), 409, "NATIVE_HITL_TURN_CLOSED",
            "the original turn no longer has a live approval wait")


def test_pending_create_needs_its_reviewed_rules_in_the_current_revision(hitl):
    c = hitl
    refused(c, c.agent, proposal(c, matched=("rule-not-current",)), 409, "NATIVE_HITL_RULES_CHANGED",
            "reviewed rule IDs must exist in the current revision")
    rule_id = submit(c, c.owner, change(c, "rule-add"))["rule_id"]
    assert pending(c, matched=(rule_id,))[1]["status"] == "pending"


def test_a_routine_card_needs_the_default_review_and_a_valid_draft(hitl):
    c = hitl
    refused(c, c.agent, routine(c, source="unavailable"), 422, "NATIVE_HITL_INVALID_REQUEST",
            "a routine proposal always asks the owner directly")
    refused(c, c.agent, routine(c, arguments={"local_time": "25:99", "time_zone": "UTC", "content": "x"}),
            422, "NATIVE_HITL_INVALID_REQUEST", "the routine proposal draft is invalid")
    assert submit(c, c.agent, routine(c))["status"] == "pending"


def test_pending_create_never_restarts_a_saved_card_or_claim(hitl):
    c = hitl
    pending(c)
    refused(c, c.agent, proposal(c, request_id="pending-again"), 409, "NATIVE_HITL_CALL_EXISTS",
            "read the original pending call; never restart its wait")
    claimed = proposal(c, call_id="tool-claimed", request_id="pending-claimed")
    assert "authorization" in submit(c, c.agent, authorize(claimed))
    refused(c, c.agent, claimed, 409, "NATIVE_HITL_CALL_EXISTS",
            "read the original pending call; never restart its wait")


def test_pending_create_at_its_deadline_is_expired(hitl):
    c = hitl
    command = proposal(c)
    expiring(c, command)
    refused(c, c.agent, command, 410, "NATIVE_HITL_EXPIRED", "the original waiting window has expired")


# --- Questions -------------------------------------------------------------------------------------------------------

def test_question_create_returns_its_reference(hitl):
    c = hitl
    command = ask(c)
    qid = question_id(TENANT, A.team, command.question.call)
    result = submit(c, c.agent, command)
    digest = c.service.read_question(c.owner, qid).question.sha256
    assert result == {**base(command), "question_id": qid, "question_digest": digest, "status": "pending"}


def test_question_create_refusals(hitl):
    c = hitl
    submit(c, c.agent, ask(c))
    refused(c, c.agent, ask(c, request_id="question-again"), 409, "NATIVE_HITL_CALL_EXISTS",
            "read the original question; never restart its wait")
    late = ask(c, call_id="ask-user-late", request_id="question-late")
    expiring(c, late)
    refused(c, c.agent, late, 410, "NATIVE_HITL_EXPIRED", "the original waiting window has expired")
    c.service.clock = lambda: c.now
    end(c)
    refused(c, c.agent, ask(c, call_id="ask-user-closed", request_id="question-closed"), 409,
            "NATIVE_HITL_TURN_CLOSED", "the original turn no longer has a live question wait")


def test_an_answer_is_recorded_once_from_the_choices_or_a_permitted_own_reply(hitl):
    c = hitl
    created = submit(c, c.agent, ask(c))
    reply = answer(created)
    assert submit(c, c.owner, reply) == {**base(reply), "question_id": created["question_id"], "status": "answered"}
    refused(c, c.owner, answer(created, "red", request_id="answer-two"), 409, "NATIVE_HITL_STATE_CONFLICT",
            "the original question already has its one answer")
    own = submit(c, c.agent, ask(c, call_id="ask-user-own", request_id="question-own"))
    assert submit(c, c.owner, answer(own, "green", freeform=True, request_id="answer-own"))["status"] == "answered"


def test_an_answer_must_name_the_original_question(hitl):
    c = hitl
    created = submit(c, c.agent, ask(c))
    wrong = answer({**created, "question_digest": "0" * 64})
    refused(c, c.owner, wrong, 409, "NATIVE_HITL_QUESTION_CHANGED",
            "the answer must name the original complete question")


def test_an_answer_outside_the_choices_is_refused(hitl):
    c = hitl
    fixed = submit(c, c.agent, ask(c, freeform=False))
    refused(c, c.owner, answer(fixed, "green"), 422, "NATIVE_HITL_ANSWER_INVALID",
            "pick one of the original choices or a permitted own reply")
    refused(c, c.owner, answer(fixed, "blue", freeform=True, request_id="answer-freeform"), 422,
            "NATIVE_HITL_ANSWER_INVALID", "pick one of the original choices or a permitted own reply")
    assert submit(c, c.owner, answer(fixed, "red", request_id="answer-choice"))["status"] == "answered"


def test_an_answer_at_the_deadline_or_after_the_turn_ended_is_refused(hitl):
    c = hitl
    created = submit(c, c.agent, ask(c))
    other = submit(c, c.agent, ask(c, call_id="ask-user-two", request_id="question-two"))
    c.now = START + timedelta(seconds=600)
    refused(c, c.owner, answer(created), 410, "NATIVE_HITL_EXPIRED",
            "an expired question cannot receive a new answer")
    c.now = START
    end(c)
    refused(c, c.owner, answer(other, request_id="answer-closed"), 409, "NATIVE_HITL_TURN_CLOSED",
            "the original SDK wait has ended")


# --- Connect cards ---------------------------------------------------------------------------------------------------

def test_consent_create_returns_its_reference(hitl):
    c = hitl
    command = consent()
    cid = consent_id(TENANT, A.team, command.consent.call)
    result = submit(c, c.agent, command)
    digest = c.service.read_consent(c.owner, cid).consent.sha256
    assert result == {**base(command), "consent_id": cid, "consent_digest": digest, "status": "pending"}


def test_consent_create_refusals(hitl):
    c = hitl
    submit(c, c.agent, consent())
    refused(c, c.agent, consent(request_id="create-again"), 409, "NATIVE_HITL_CALL_EXISTS",
            "read the original Connect card; never restart its wait")
    late = consent("connect-late")
    expiring(c, late)
    refused(c, c.agent, late, 410, "NATIVE_HITL_EXPIRED", "the original waiting window has expired")
    c.service.clock = lambda: c.now
    end(c)
    refused(c, c.agent, consent("connect-closed"), 409, "NATIVE_HITL_TURN_CLOSED",
            "the original turn no longer has a live Connect wait")


def test_a_resume_click_is_recorded_once_and_does_not_block_a_later_card(hitl):
    c = hitl
    created = submit(c, c.agent, consent())
    command = click(created)
    assert submit(c, c.owner, command) == {**base(command), "consent_id": created["consent_id"], "status": "resume"}
    refused(c, c.owner, click(created, "decline", request_id="click-two"), 409, "NATIVE_HITL_STATE_CONFLICT",
            "the original Connect card already has its one click")
    assert submit(c, c.agent, consent("connect-two"))["status"] == "pending"


def test_a_decline_blocks_later_cards_for_that_toolbox_and_keeps_the_first_decline(hitl):
    c = hitl
    first = submit(c, c.agent, consent())
    second = submit(c, c.agent, consent("connect-two"))
    other = submit(c, c.agent, consent("connect-other", toolbox_name="fa-connector-team-one-mail"))
    assert submit(c, c.owner, click(first, "decline"))["status"] == "decline"
    refused(c, c.agent, consent("connect-three"), 409, "NATIVE_HITL_HUMAN_DENIED",
            "the owner declined this connector in this conversation")
    assert submit(c, c.owner, click(second, "decline", request_id="click-two"))["status"] == "decline"
    assert submit(c, c.owner, click(other, request_id="click-other"))["status"] == "resume"
    assert submit(c, c.agent, consent("connect-mail", toolbox_name="fa-connector-team-one-mail"))["status"] == "pending"


def test_a_click_must_name_the_original_live_card(hitl):
    c = hitl
    created = submit(c, c.agent, consent())
    other = submit(c, c.agent, consent("connect-two"))
    refused(c, c.owner, click(created, digest="0" * 64), 409, "NATIVE_HITL_ACTION_CHANGED",
            "the click must name the original complete Connect card")
    c.now = START + timedelta(seconds=600)
    refused(c, c.owner, click(created), 410, "NATIVE_HITL_EXPIRED", "an expired Connect card cannot resume")
    c.now = START
    end(c)
    refused(c, c.owner, click(other, request_id="click-closed"), 409, "NATIVE_HITL_TURN_CLOSED",
            "the original turn has ended")


# --- Owner decisions -------------------------------------------------------------------------------------------------

def test_allow_once_writes_a_grant_and_deny_does_not(hitl):
    c = hitl
    _, created = pending(c)
    command = decision(c, created)
    assert submit(c, c.owner, command) == {
        **base(command), "pending_id": created["pending_id"], "status": "allow", "rules_revision": 0}
    view = c.service.read(c.owner, created["pending_id"])
    assert view.status == "allow" and view.grant.rules_revision == 0 and view.decision == command
    _, denied = pending(c, call_id="tool-two", request_id="pending-two")
    no = decision(c, denied, request_id="decision-deny", choice="deny")
    assert submit(c, c.owner, no)["status"] == "deny"
    view = c.service.read(c.owner, denied["pending_id"])
    assert view.status == "deny" and view.grant is None and view.decision == no


def test_the_owner_may_decide_with_approve_but_without_submit(hitl):
    c = hitl
    _, created = pending(c)
    approver = replace(c.owner, actions=frozenset({"read", "approve"}))
    command = decision(c, created)
    assert submit(c, approver, command) == {
        **base(command), "pending_id": created["pending_id"], "status": "allow", "rules_revision": 0}


def test_allow_rule_adds_the_confirmed_rule_and_records_its_revision(hitl):
    c = hitl
    _, created = pending(c)
    command = decision(c, created, choice="allow_rule")
    result = submit(c, c.owner, command)
    rule_id = result["rule_id"]
    assert result == {
        **base(command), "rule_id": rule_id, "pending_id": created["pending_id"], "status": "allow",
        "rules_revision": 1}
    assert c.service.read(c.owner, created["pending_id"]).grant.rules_revision == 1
    assert [rule.rule_id for rule in c.service.rules(c.owner).rules] == [rule_id]


def test_a_decision_must_bind_the_original_open_card(hitl):
    c = hitl
    _, created = pending(c)
    refused(c, c.owner, decision(c, {**created, "pending_digest": "0" * 64}), 409, "NATIVE_HITL_ACTION_CHANGED",
            "the decision must bind the original complete pending action")
    submit(c, c.owner, decision(c, created, choice="deny"))
    refused(c, c.owner, decision(c, created, request_id="decision-two"), 409, "NATIVE_HITL_STATE_CONFLICT",
            "the original pending call already has a decision or observation")


def test_a_decision_at_the_deadline_after_the_turn_or_after_a_claim_is_refused(hitl):
    c = hitl
    _, late = pending(c)
    command, claimed = pending(c, call_id="tool-claimed", request_id="pending-claimed")
    _, closed = pending(c, call_id="tool-closed", request_id="pending-closed")
    assert "authorization" in submit(c, c.agent, authorize(command))
    refused(c, c.owner, decision(c, claimed, request_id="decision-claimed"), 409, "NATIVE_HITL_ALREADY_AUTHORIZED",
            "the original call already crossed its authorization point")
    c.now = START + timedelta(seconds=600)
    refused(c, c.owner, decision(c, late), 410, "NATIVE_HITL_EXPIRED",
            "an expired call cannot receive a new human decision")
    c.now = START
    end(c)
    refused(c, c.owner, decision(c, closed, request_id="decision-closed"), 409, "NATIVE_HITL_TURN_CLOSED",
            "the original SDK wait has ended")


def test_toolbox_and_routine_cards_are_never_approved_by_a_rule(hitl):
    c = hitl
    tool = submit(c, c.agent, toolbox(c))
    refused(c, c.owner, decision(c, tool, choice="allow_rule"), 422, "NATIVE_HITL_INVALID_REQUEST",
            "a toolbox tool is approved once per call, not by a rule")
    assert submit(c, c.owner, decision(c, tool, request_id="decision-tool"))["status"] == "allow"
    card = submit(c, c.agent, routine(c))
    refused(c, c.owner, decision(c, card, request_id="decision-routine", choice="allow_rule"), 422,
            "NATIVE_HITL_INVALID_REQUEST", "a routine proposal is approved once per card, not by a rule")
    assert submit(c, c.owner, decision(c, card, request_id="decision-routine-once"))["status"] == "allow"


def test_a_stale_decision_rules_version_is_refused(hitl):
    c = hitl
    _, created = pending(c)
    stale = decision(c, created)
    submit(c, c.owner, change(c, "rule-add"))
    refused(c, c.owner, stale, 409, "NATIVE_HITL_RULES_CHANGED", "read the current rules and reassess the original action")


# --- Agent closes ----------------------------------------------------------------------------------------------------

def test_a_waiting_card_closes_as_interrupted_or_at_its_deadline_as_expired(hitl):
    c = hitl
    _, created = pending(c)
    command = close(created, "interrupted")
    assert submit(c, c.agent, command) == {**base(command), "pending_id": created["pending_id"], "status": "interrupted"}
    assert c.service.read(c.owner, created["pending_id"]).status == "interrupted"
    _, waiting = pending(c, call_id="tool-two", request_id="pending-two")
    refused(c, c.agent, close(waiting, "expired", "close-early"), 409, "NATIVE_HITL_STATE_CONFLICT",
            "this observation cannot replace the original decision or outcome")
    c.now = START + timedelta(seconds=600)
    assert submit(c, c.agent, close(waiting, "expired", "close-late"))["status"] == "expired"


def test_effect_unknown_closes_only_an_allowed_or_claimed_card(hitl):
    c = hitl
    _, waiting = pending(c)
    refused(c, c.agent, close(waiting, "effect_unknown"), 409, "NATIVE_HITL_STATE_CONFLICT",
            "this observation cannot replace the original decision or outcome")
    command, claimed = pending(c, call_id="tool-claimed", request_id="pending-claimed")
    submit(c, c.agent, authorize(command))
    assert submit(c, c.agent, close(claimed, "effect_unknown", "close-claimed"))["status"] == "effect_unknown"
    _, allowed = pending(c, call_id="tool-allowed", request_id="pending-allowed")
    allow = decision(c, allowed, request_id="decision-allowed")
    submit(c, c.owner, allow)
    refused(c, c.agent, close(allowed, "expired", "close-allowed-early"), 409, "NATIVE_HITL_STATE_CONFLICT",
            "this observation cannot replace the original decision or outcome")
    assert submit(c, c.agent, close(allowed, "effect_unknown", "close-allowed"))["status"] == "effect_unknown"
    view = c.service.read(c.owner, allowed["pending_id"])
    assert view.status == "effect_unknown" and view.decision == allow


def test_an_allowed_card_closes_as_interrupted_and_a_denied_one_never_closes(hitl):
    c = hitl
    _, allowed = pending(c)
    submit(c, c.owner, decision(c, allowed))
    assert submit(c, c.agent, close(allowed, "interrupted"))["status"] == "interrupted"
    _, denied = pending(c, call_id="tool-two", request_id="pending-two")
    submit(c, c.owner, decision(c, denied, request_id="decision-deny", choice="deny"))
    for status in ("interrupted", "effect_unknown"):
        refused(c, c.agent, close(denied, status, "close-denied-" + status), 409, "NATIVE_HITL_STATE_CONFLICT",
                "this observation cannot replace the original decision or outcome")
    c.now = START + timedelta(seconds=600)
    refused(c, c.agent, close(denied, "expired", "close-denied-expired"), 409, "NATIVE_HITL_STATE_CONFLICT",
            "this observation cannot replace the original decision or outcome")


def test_a_close_must_bind_the_original_card(hitl):
    c = hitl
    _, created = pending(c)
    refused(c, c.agent, close(created, "interrupted", digest="0" * 64), 409, "NATIVE_HITL_ACTION_CHANGED",
            "the decision must bind the original complete pending action")


def test_a_closed_card_never_closes_again(hitl):
    c = hitl
    for index, first in enumerate(("interrupted", "expired", "effect_unknown")):
        command, created = pending(c, call_id="tool-" + first, request_id="pending-" + first)
        if first == "effect_unknown":
            submit(c, c.agent, authorize(command, "authorize-" + first))
        c.now = START + timedelta(seconds=600) if first == "expired" else START
        submit(c, c.agent, close(created, first, "close-" + first))
        c.now = START + timedelta(seconds=600)
        for status in ("expired", "interrupted", "effect_unknown"):
            refused(c, c.agent, close(created, status, "again-%d-%s" % (index, status)), 409,
                    "NATIVE_HITL_STATE_CONFLICT", "this observation cannot replace the original decision or outcome")
        c.now = START


def test_an_allowed_card_closes_as_expired_at_its_deadline(hitl):
    c = hitl
    _, allowed = pending(c)
    submit(c, c.owner, decision(c, allowed))
    c.now = START + timedelta(seconds=600)
    assert submit(c, c.agent, close(allowed, "expired"))["status"] == "expired"
