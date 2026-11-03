"""Pins NativeHitlService's identity check, read-set, approval readback and effect authorization.

Every refusal is asserted by status, code and message, so another refusal reached first fails the test. Rows the
service itself never writes are edited in place: those checks are record integrity, not a path a caller can walk.
"""

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from conftest import A, B, TENANT
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_team_registry import IdentityRegistration, identity_key
from shared_context.errors import Problem
from shared_context.native_hitl import consent_id, hitl_key, pending_id, question_id
from shared_context.native_hitl_wire import (
    NativeHitlApprovalReference, NativeHitlAuthorize, NativeHitlEnd, NativeHitlReview, NativeHitlTurn,
)
from shared_context.records import Record
from shared_context.source_materials import digest
from test_m3_auth import HUMAN_OID, jwk_client, signing_key
from test_native_hitl import START, change, decision, hitl, proposal
from test_native_hitl_changes import authorize, click, close, consent, end, pending, routine, submit
from test_native_hitl_questions import answer, ask

OID = "12121212-1212-1212-1212-121212121212"
OTHER = "99999999-9999-9999-9999-999999999999"
TURN = NativeHitlTurn(agent_id="native-a", conversation_id="cid-one", request_id="rid-one")
ABSENT = "0" * 64
ALL = frozenset({"read", "submit", "approve"})
HUMAN = BehaviorPrincipal(TENANT, OID, A.team, "human", None, ALL)
AGENT = BehaviorPrincipal(TENANT, OID, A.team, "agent", "agent-d", ALL)
NOT_OWNER = (403, "NATIVE_HITL_OWNER_ONLY", "the current original Team owner is required")
NOT_DELEGATED = (403, "NATIVE_HITL_OWNER_ONLY", "only the delegated Team owner may record rules or decisions")
NOT_AGENT = (403, "NATIVE_HITL_AGENT_ONLY", "an active registered Agent of this Team is required")
NO_ACCESS = (403, "BEHAVIOR_ACCESS_DENIED", "principal cannot perform this action in the requested team")
NEEDS_HUMAN = (403, "NATIVE_HITL_PERMISSION_REQUIRED", "a current original human once decision is required")
REOPENED = (403, "NATIVE_HITL_PERMISSION_REQUIRED", "a terminal original wait cannot be reopened by another rule")
NOT_ALLOWED = (403, "NATIVE_HITL_PERMISSION_REQUIRED", "the original human did not allow this live call")
REVIEW_REFUSED = (403, "NATIVE_HITL_REVIEW_DENIED", "current review or human takeover refuses this action")
OTHER_INPUT = (409, "NATIVE_HITL_ACTION_CHANGED", "approval cannot authorize different original input")
STATE_DIFFERS, DECISION_DIFFERS, GRANT_DIFFERS = (
    "the state differs from the pending call", "the saved decision differs from the pending call",
    "the saved grant differs from the original human choice")


def denied(expected, call, *args, **kwargs):
    with pytest.raises(Problem) as caught:
        call(*args, **kwargs)
    error = caught.value.body["error"]
    assert (caught.value.status, error["code"], error["message"]) == expected


def key(*parts):
    return hitl_key(parts[0], TENANT, A.team, *parts[1:])


def row(c, name):
    return next(value for (_, _, stored), value in c.table.backing.rows.items()
                if stored == "behavior:v1:row:" + name)


def data(c, name):
    return json.loads(row(c, name)["Payload"])["data"]


def tamper(c, name, **fields):
    entity = row(c, name)
    payload = json.loads(entity["Payload"])
    payload["data"].update(fields)
    entity["Payload"] = json.dumps(payload).encode()


def registered(kind, **changes):
    values = dict(kind=kind, tenant_id=TENANT, oid=OID, team_id=A.team, agent_id="agent-d" if kind == "agent" else None)
    return IdentityRegistration(**{**values, **changes})


def check(c, monkeypatch, principal, value, source):
    """Run the identity check against one registration, from the transaction's rows or the live registry."""
    if source == "rows":
        record = None if value is None else Record({"data": value.model_dump(mode="json")}, "synthetic-etag")
        monkeypatch.setattr(c.verifier, "teams", SimpleNamespace(legacy_owner=c.teams.legacy_owner))
        c.service._authorize(principal, rows={identity_key(TENANT, principal.oid): record})
        return

    def read(oid):
        assert oid == principal.oid
        return value

    monkeypatch.setattr(c.verifier, "teams", SimpleNamespace(owner=read, lookup=read))
    c.service._authorize(principal)


@pytest.mark.parametrize("principal", [
    object(), None, replace(HUMAN, tenant_id=OTHER, actions=frozenset()),
    SimpleNamespace(tenant_id=TENANT, team=A.team, kind="human", oid=HUMAN_OID, actor_id=None, actions=ALL,
                    require=lambda *_: None),
], ids=["not-a-principal", "none", "another-tenant", "looks-like-a-principal"])
def test_only_a_principal_of_this_tenant_is_looked_at(hitl, principal):
    denied((403, "NATIVE_HITL_SCOPE_DENIED", "the verified original Team identity is required"),
           hitl.service._authorize, principal)


@pytest.mark.parametrize("options,actions,allowed", [
    ({}, {"read"}, True),
    ({}, {"submit", "approve"}, False),
    ({"write": True}, {"submit"}, True),
    ({"write": True}, {"read", "approve"}, False),
    ({"write": True, "approve": True}, {"approve"}, True),
    ({"write": True, "approve": True}, {"read", "submit"}, False),
    ({"owner": True, "write": True}, {"approve"}, True),
    ({"owner": True, "write": True}, {"read", "submit"}, False),
    ({"owner": True, "write": True, "approve": False}, {"submit"}, True),
    ({"owner": True, "write": True, "approve": False}, {"read", "approve"}, False),
    ({"owner": True}, {"read"}, True),
])
def test_the_action_required_follows_the_write_and_the_approval(hitl, options, actions, allowed):
    principal = replace(hitl.owner, actions=frozenset(actions))
    if allowed:
        hitl.service._authorize(principal, **options)
    else:
        denied(NO_ACCESS, hitl.service._authorize, principal, **options)


def test_the_owner_flag_is_checked_after_the_action_and_refuses_an_agent_or_an_actor(hitl):
    c = hitl
    denied(NO_ACCESS, c.service._authorize, replace(c.agent, actions=frozenset()), owner=True)
    denied(NOT_DELEGATED, c.service._authorize, c.agent, owner=True)
    denied(NOT_DELEGATED, c.service._authorize, replace(c.owner, actor_id="an-actor"), owner=True)


@pytest.mark.parametrize("source", ["registry", "rows"])
def test_a_registered_owner_passes(hitl, monkeypatch, source):
    check(hitl, monkeypatch, HUMAN, registered("owner"), source)


@pytest.mark.parametrize("source", ["registry", "rows"])
@pytest.mark.parametrize("value", [
    None,
    registered("owner", active=False, deactivation_id="disable-owner"),
    registered("agent"),
    registered("owner", tenant_id=OTHER),
    registered("owner", oid=OTHER),
    registered("owner", team_id="another-team"),
], ids=["unregistered", "inactive", "an-agent", "another-tenant", "another-oid", "another-team"])
def test_only_the_current_owner_of_this_team_passes(hitl, monkeypatch, value, source):
    denied(NOT_OWNER, check, hitl, monkeypatch, HUMAN, value, source)


@pytest.mark.parametrize("source", ["registry", "rows"])
def test_a_human_acting_for_an_actor_is_not_the_owner(hitl, monkeypatch, source):
    denied(NOT_OWNER, check, hitl, monkeypatch, replace(HUMAN, actor_id="an-actor"), registered("owner"), source)


def test_the_legacy_owner_without_a_row_is_owner_only_of_the_legacy_team(hitl):
    c = hitl
    rows = {identity_key(TENANT, HUMAN_OID): None}
    c.service._authorize(c.owner, rows=rows)
    denied(NOT_OWNER, c.service._authorize, replace(c.owner, team="another-team"), rows=rows)


@pytest.mark.parametrize("changes", [{}, {"oid": OTHER}, {"team": "another-team"}, {"actor_id": "an-actor"}],
                         ids=["configured", "another-oid", "another-team", "an-actor"])
def test_without_a_registry_only_the_configured_human_is_the_owner(hitl, monkeypatch, changes):
    c = hitl
    monkeypatch.setattr(c.verifier, "teams", None)
    if changes:
        denied(NOT_OWNER, c.service._authorize, replace(c.owner, **changes))
    else:
        c.service._authorize(c.owner)


@pytest.mark.parametrize("source", ["registry", "rows"])
def test_a_registered_agent_passes(hitl, monkeypatch, source):
    check(hitl, monkeypatch, AGENT, registered("agent"), source)


@pytest.mark.parametrize("source", ["registry", "rows"])
@pytest.mark.parametrize("value", [
    None,
    registered("agent", active=False, deactivation_id="retire-agent"),
    registered("owner"),
    registered("agent", tenant_id=OTHER),
    registered("agent", oid=OTHER),
    registered("agent", team_id="another-team"),
    registered("agent", agent_id="another-agent"),
], ids=["unregistered", "inactive", "an-owner", "another-tenant", "another-oid", "another-team", "another-agent"])
def test_only_an_active_registered_agent_of_this_team_passes(hitl, monkeypatch, value, source):
    denied(NOT_AGENT, check, hitl, monkeypatch, AGENT, value, source)


@pytest.mark.parametrize("source", ["registry", "rows"])
def test_an_owner_row_is_never_an_agent_even_without_an_actor(hitl, monkeypatch, source):
    denied(NOT_AGENT, check, hitl, monkeypatch, replace(AGENT, actor_id=None), registered("owner"), source)


def test_a_configured_actor_is_matched_before_the_registry_is_read(hitl, monkeypatch):
    c = hitl

    def unread(_oid):
        pytest.fail("a configured actor is not looked up")

    monkeypatch.setattr(c.verifier, "teams", SimpleNamespace(lookup=unread))
    c.service._authorize(c.agent)
    denied(NOT_AGENT, c.service._authorize, replace(c.agent, actor_id=B.actor))
    denied(NOT_AGENT, c.service._authorize, replace(c.agent, team="another-team"))


def test_without_a_registry_an_unconfigured_agent_is_refused(hitl, monkeypatch):
    monkeypatch.setattr(hitl.verifier, "teams", None)
    denied(NOT_AGENT, hitl.service._authorize, AGENT)


def test_any_other_kind_of_principal_is_refused(hitl):
    denied((403, "NATIVE_HITL_SCOPE_DENIED", "a verified Team identity is required"),
           hitl.service._authorize, replace(hitl.agent, kind="service"))


# --- The saved read-set: _original compares it with what is recomputed, so its order is part of the stored record ----

def reads(c, principal, command, *, legacy=False):
    return list(c.service._request(principal, command, legacy=legacy).read_set)


def start(principal, command):
    return [identity_key(TENANT, principal.oid), key("intent", principal.oid, command.request_id), key("rules")]


def card(pid):
    return [pid, key("state", pid), key("grant", pid)]


TURN_KEY = key("turn", "native-a", "cid-one", "rid-one")


def add_rule(c):
    submit(c, c.owner, change(c, "rule-one"))


def test_the_request_identity_and_digest_are_stable(hitl):
    c = hitl
    command = NativeHitlEnd(request_id="end-one", turn=TURN)
    request = c.service._request(c.agent, command)
    assert request.request_id == key("apply", key("intent", A.oid, "end-one"))
    assert request.request_digest == digest([TENANT, A.team, A.oid, command.model_dump(mode="json")])
    assert c.service._request(c.agent, command, legacy=True).request_digest == request.request_digest


def test_read_set_of_an_end(hitl):
    c = hitl
    command = NativeHitlEnd(request_id="end-one", turn=TURN)
    assert reads(c, c.agent, command) == [*start(c.agent, command), TURN_KEY]
    assert reads(c, c.agent, command, legacy=True) == start(c.agent, command)


def test_read_set_of_a_rule_change(hitl):
    c = hitl
    first = change(c, "rule-one")
    assert reads(c, c.owner, first) == reads(c, c.owner, first, legacy=True) == start(c.owner, first)
    submit(c, c.owner, first)
    second = change(c, "rule-two")
    expected = [*start(c.owner, second), key("rules-revision", "1")]
    assert reads(c, c.owner, second) == reads(c, c.owner, second, legacy=True) == expected


@pytest.mark.parametrize("build", [lambda command: command, authorize], ids=["pending-create", "authorize"])
def test_read_set_of_a_call_that_names_its_action(hitl, build):
    c = hitl
    command = build(proposal(c))
    pid = pending_id(TENANT, A.team, command.action.call)
    assert reads(c, c.agent, command) == [*start(c.agent, command), *card(pid), key("authorization", pid), TURN_KEY]
    assert reads(c, c.agent, command, legacy=True) == [*start(c.agent, command), *card(pid)]


def test_read_set_of_a_proposal_reviewed_under_a_rule_revision(hitl):
    c = hitl
    add_rule(c)
    command = proposal(c)
    pid = pending_id(TENANT, A.team, command.action.call)
    assert reads(c, c.agent, command, legacy=True) == [
        *start(c.agent, command), key("rules-revision", "1"), *card(pid)]


def test_read_set_of_a_decision_or_close_on_an_absent_card(hitl):
    c = hitl
    created = {"pending_id": ABSENT, "pending_digest": ABSENT}
    for principal, command, revision in (
            (c.owner, decision(c, created), ()),
            (c.agent, close(created, "interrupted"), ())):
        assert reads(c, principal, command) == [
            *start(principal, command), *revision, *card(ABSENT), key("authorization", ABSENT)]
        assert reads(c, principal, command, legacy=True) == [*start(principal, command), *revision, *card(ABSENT)]


def test_read_set_of_a_decision_or_close_on_a_saved_card(hitl):
    c = hitl
    add_rule(c)
    _, created = pending(c)
    pid = created["pending_id"]
    command = decision(c, created)
    assert reads(c, c.owner, command) == [
        *start(c.owner, command), key("rules-revision", "1"), *card(pid), key("authorization", pid), TURN_KEY]
    assert reads(c, c.owner, command, legacy=True) == [
        *start(c.owner, command), key("rules-revision", "1"), *card(pid)]
    ending = close(created, "interrupted")
    assert reads(c, c.agent, ending) == [*start(c.agent, ending), *card(pid), key("authorization", pid), TURN_KEY]
    assert reads(c, c.agent, ending, legacy=True) == [*start(c.agent, ending), *card(pid)]


def test_read_set_of_a_question_and_its_answer(hitl):
    c = hitl
    command = ask(c)
    qid = question_id(TENANT, A.team, command.question.call)
    expected = [*start(c.agent, command), qid, key("answer", qid), TURN_KEY]
    assert reads(c, c.agent, command) == reads(c, c.agent, command, legacy=True) == expected
    absent = answer({"question_id": ABSENT, "question_digest": ABSENT})
    assert reads(c, c.owner, absent) == reads(c, c.owner, absent, legacy=True) == [
        *start(c.owner, absent), ABSENT, key("answer", ABSENT)]
    reply = answer(submit(c, c.agent, command))
    assert reads(c, c.owner, reply) == reads(c, c.owner, reply, legacy=True) == [
        *start(c.owner, reply), qid, key("answer", qid), TURN_KEY]


def test_read_set_of_a_connect_card_and_its_click(hitl):
    c = hitl
    command = consent()
    cid = consent_id(TENANT, A.team, command.consent.call)
    saved = [cid, key("consent-decision", cid), TURN_KEY]
    declined = key("consent-declined", "native-a", "cid-one", command.consent.toolbox_name)
    assert reads(c, c.agent, command) == [*start(c.agent, command), *saved, declined]
    assert reads(c, c.agent, command, legacy=True) == [*start(c.agent, command), *saved]
    absent = click({"consent_id": ABSENT, "consent_digest": ABSENT})
    expected = [*start(c.owner, absent), ABSENT, key("consent-decision", ABSENT)]
    assert reads(c, c.owner, absent) == reads(c, c.owner, absent, legacy=True) == expected
    pressed = click(submit(c, c.agent, command))
    assert reads(c, c.owner, pressed) == [*start(c.owner, pressed), *saved, declined]
    assert reads(c, c.owner, pressed, legacy=True) == [*start(c.owner, pressed), *saved]


@pytest.mark.parametrize("pid", ["not-an-original-id", 7, None])
def test_a_read_needs_one_original_id(hitl, pid):
    denied((422, "NATIVE_HITL_INVALID_REQUEST", "one original approval ID is required"),
           hitl.service.read, hitl.owner, pid)


def test_a_read_checks_the_reader_before_the_record(hitl):
    denied((403, "NATIVE_HITL_SCOPE_DENIED", "the verified original Team identity is required"),
           hitl.service.read, object(), ABSENT)
    denied((404, "NATIVE_HITL_NOT_FOUND", "the original approval record is unavailable"),
           hitl.service.read, hitl.owner, ABSENT)


def decided(c):
    _, created = pending(c)
    pid = created["pending_id"]
    submit(c, c.owner, decision(c, created))
    decision_id = data(c, key("state", pid))["decision_id"]
    return pid, decision_id


def invalid(c, pid, message):
    denied((409, "NATIVE_HITL_RECORD_INVALID", message), c.service.read, c.owner, pid)


@pytest.mark.parametrize("fields", [
    {"tenant_id": "another-tenant"}, {"team_id": "another-team"}, {"pending_id": ABSENT},
], ids=["tenant", "team", "pending"])
def test_a_state_of_another_call_is_refused(hitl, fields):
    c = hitl
    _, created = pending(c)
    tamper(c, key("state", created["pending_id"]), **fields)
    invalid(c, created["pending_id"], STATE_DIFFERS)


@pytest.mark.parametrize("fields", [
    {"tenant_id": "another-tenant"}, {"team_id": "another-team"},
    {"command": {"pending_id": ABSENT}}, {"command": {"pending_digest": ABSENT}},
], ids=["tenant", "team", "pending", "digest"])
def test_a_decision_of_another_call_is_refused(hitl, fields):
    c = hitl
    pid, decision_id = decided(c)
    if "command" in fields:
        fields = {"command": {**data(c, decision_id)["command"], **fields["command"]}}
    tamper(c, decision_id, **fields)
    # The grant must still match, so only the decision's own comparison can refuse.
    invalid(c, pid, DECISION_DIFFERS)


@pytest.mark.parametrize("fields", [
    {"grant_id": ABSENT}, {"action_digest": ABSENT}, {"decision_id": ABSENT}, {"tenant_id": "another-tenant"},
    {"team_id": "another-team"}, {"expires_at": (START + timedelta(hours=1)).isoformat()},
    {"approval": {"pending_digest": ABSENT}},
], ids=["grant", "action", "decision", "tenant", "team", "expires", "approval"])
def test_a_grant_of_another_choice_is_refused(hitl, fields):
    c = hitl
    pid, _ = decided(c)
    if "approval" in fields:
        fields = {"approval": {**data(c, key("grant", pid))["approval"], **fields["approval"]}}
    tamper(c, key("grant", pid), **fields)
    invalid(c, pid, GRANT_DIFFERS)


def test_a_grant_beside_a_denied_decision_is_refused(hitl):
    c = hitl
    pid, decision_id = decided(c)
    tamper(c, decision_id, command={**data(c, decision_id)["command"], "choice": "deny"})
    invalid(c, pid, GRANT_DIFFERS)


def test_the_last_comparison_names_the_conflict_when_several_differ(hitl):
    c = hitl
    pid, decision_id = decided(c)
    tamper(c, key("state", pid), team_id="another-team")
    invalid(c, pid, STATE_DIFFERS)
    tamper(c, decision_id, team_id="another-team")
    invalid(c, pid, DECISION_DIFFERS)
    tamper(c, key("grant", pid), team_id="another-team")
    invalid(c, pid, GRANT_DIFFERS)


def test_a_decision_that_cannot_be_read_is_not_found(hitl):
    c = hitl
    pid, _ = decided(c)
    tamper(c, key("state", pid), decision_id=ABSENT)
    denied((404, "NATIVE_HITL_NOT_FOUND", "the original approval record is unavailable"), c.service.read, c.owner, pid)


def test_an_open_card_reads_back_its_attention_and_the_current_rules(hitl):
    c = hitl
    command, created = pending(c)
    add_rule(c)
    c.now = START + timedelta(seconds=30)
    view = c.service.read(c.owner, created["pending_id"])
    assert (view.status, view.decision, view.grant, view.authorization) == ("pending", None, None, None)
    assert (view.expired, view.turn_closed, view.observed_at) == (False, False, c.now)
    assert view.current_rules_revision == 1 and view.pending.command == command
    assert view.approval.pending_id == created["pending_id"] and view.approval.pending_digest == created["pending_digest"]
    item = view.attention
    assert (item.item_id, item.approval, item.created_at, item.expires_at) == (
        created["pending_id"], view.approval, START, START + timedelta(seconds=600))


def test_a_decided_card_needs_no_attention(hitl):
    c = hitl
    pid, _ = decided(c)
    view = c.service.read(c.owner, pid)
    assert view.status == "allow" and view.attention is None and view.decision is not None and view.grant is not None


def test_a_card_at_its_deadline_needs_no_attention(hitl):
    c = hitl
    _, created = pending(c)
    c.now = START + timedelta(seconds=599)
    assert c.service.read(c.owner, created["pending_id"]).attention is not None
    c.now = START + timedelta(seconds=600)
    view = c.service.read(c.owner, created["pending_id"])
    assert view.expired is True and view.attention is None and view.status == "pending"


def test_a_card_of_an_ended_turn_needs_no_attention(hitl):
    c = hitl
    _, created = pending(c)
    end(c)
    view = c.service.read(c.owner, created["pending_id"])
    assert view.turn_closed is True and view.attention is None and view.status == "pending"


def test_a_claimed_card_needs_no_attention_and_reads_back_its_authorization(hitl):
    c = hitl
    command, created = pending(c)
    claimed = submit(c, c.agent, authorize(command))["authorization"]
    view = c.service.read(c.owner, created["pending_id"])
    assert view.status == "pending" and view.attention is None
    assert view.authorization.model_dump(mode="json") == claimed


def claim(c, command, *, request_id="authorize-one", approval=None, **review):
    rules = c.service.rules(c.agent)
    fields = {"source": "default", "outcome": "allow", **review}
    return NativeHitlAuthorize(
        request_id=request_id, action=command.action, action_digest=command.action_digest,
        review=NativeHitlReview(rules_revision=rules.revision, **fields), expected_rules_etag=rules.etag,
        effect_token="effect-" + request_id, approval=approval)


def reference(command, created, **changes):
    return NativeHitlApprovalReference(**{
        "pending_id": created["pending_id"], "pending_digest": created["pending_digest"],
        "call": command.action.call, **changes})


def allowed(c):
    command, created = pending(c)
    submit(c, c.owner, decision(c, created))
    return command, created, claim(c, command, approval=reference(command, created))


def test_an_automatic_allow_claims_without_a_grant_for_the_service_window(hitl):
    c = hitl
    command = proposal(c)
    c.now = START + timedelta(seconds=5)
    request = claim(c, command)
    result = submit(c, c.agent, request)["authorization"]
    pid = pending_id(TENANT, A.team, command.action.call)
    assert result["authorization_id"] == key("authorization", pid)
    assert (result["grant_id"], result.get("approval")) == (None, None)
    assert result["expires_at"] == (c.now + timedelta(seconds=c.service.ttl_seconds)).isoformat().replace("+00:00", "Z")
    assert (result["action_digest"], result["effect_token"]) == (command.action_digest, request.effect_token)


def test_an_allowed_card_claims_under_its_grant_and_its_deadline(hitl):
    c = hitl
    command, created, request = allowed(c)
    result = submit(c, c.agent, request)["authorization"]
    assert result["grant_id"] == key("grant", created["pending_id"])
    assert result["approval"] == request.approval.model_dump(mode="json")
    assert result["expires_at"] == data(c, key("grant", created["pending_id"]))["expires_at"]


def test_the_claim_needs_consume_before_anything_else(hitl):
    c = hitl
    request = claim(c, proposal(c))
    denied(NO_ACCESS, c.service.submit, replace(c.agent, actions=frozenset({"read", "submit"})), request)


def test_only_the_original_agent_claims(hitl):
    c = hitl
    request = claim(c, proposal(c))
    denied((403, "NATIVE_HITL_SCOPE_DENIED", "the call belongs to another Agent or Team"),
           c.service.submit, c.other_agent, request)
    denied((403, "NATIVE_HITL_AGENT_ONLY", "only the original Agent may propose or close its call"),
           c.service.submit, replace(c.owner, actions=c.owner.actions | {"consume"}), request)


def test_a_stale_review_revision_is_refused_first(hitl):
    c = hitl
    request = claim(c, proposal(c), outcome="deny")
    add_rule(c)
    denied((409, "NATIVE_HITL_RULES_CHANGED", "read the current rules and reassess the original action"),
           c.service.submit, c.agent, request)


def test_a_claim_after_the_turn_ended_is_refused_before_its_review(hitl):
    c = hitl
    command = proposal(c)
    end(c)
    denied((409, "NATIVE_HITL_TURN_CLOSED", "the original turn is closed"),
           c.service.submit, c.agent, claim(c, command, outcome="deny"))


@pytest.mark.parametrize("review", [
    {"outcome": "deny"}, {"outcome": "deny", "reason": "human_takeover", "takeover_required": True},
], ids=["deny", "takeover"])
def test_a_refusing_review_is_refused(hitl, review):
    c = hitl
    denied(REVIEW_REFUSED, c.service.submit, c.agent, claim(c, proposal(c), **review))


def test_a_review_of_rules_no_longer_current_is_refused(hitl):
    c = hitl
    denied(REVIEW_REFUSED, c.service.submit, c.agent,
           claim(c, proposal(c), source="rules", matched_rule_ids=("rule-not-in-this-revision",)))


def test_a_refusing_review_is_refused_before_a_saved_claim_is_read(hitl):
    c = hitl
    command = proposal(c)
    submit(c, c.agent, claim(c, command))
    denied(REVIEW_REFUSED, c.service.submit, c.agent, claim(c, command, request_id="again", outcome="deny"))


def test_a_second_claim_is_refused_before_the_call_is_compared(hitl):
    c = hitl
    command = proposal(c)
    submit(c, c.agent, claim(c, command))
    denied((409, "NATIVE_HITL_ALREADY_AUTHORIZED", "read the original authorization; never acquire another claim"),
           c.service.submit, c.agent, claim(c, command, request_id="again"))


def test_an_ask_review_needs_a_human(hitl):
    c = hitl
    denied(NEEDS_HUMAN, c.service.submit, c.agent, claim(c, proposal(c), outcome="ask"))


def test_a_routine_proposal_always_needs_a_human(hitl):
    c = hitl
    denied(NEEDS_HUMAN, c.service.submit, c.agent, claim(c, routine(c)))


def test_a_matched_ask_first_rule_needs_a_human(hitl):
    c = hitl
    add_rule(c)
    rule = c.service.rules(c.agent).rules[0]
    denied(NEEDS_HUMAN, c.service.submit, c.agent,
           claim(c, proposal(c), source="rules", matched_rule_ids=(rule.rule_id,)))


def test_a_matched_automatic_rule_needs_no_human(hitl):
    c = hitl
    submit(c, c.owner, change(c, "rule-one", behavior="allow_automatically"))
    rule = c.service.rules(c.agent).rules[0]
    result = submit(c, c.agent, claim(c, proposal(c), source="rules", matched_rule_ids=(rule.rule_id,)))
    assert result["authorization"]["grant_id"] is None


def test_an_allowed_card_cannot_be_claimed_without_its_approval(hitl):
    c = hitl
    command, _, _ = allowed(c)
    denied(NEEDS_HUMAN, c.service.submit, c.agent, claim(c, command, request_id="without-approval"))


def test_a_waiting_card_can_be_claimed_by_a_later_automatic_allow(hitl):
    c = hitl
    command, created = pending(c)
    assert submit(c, c.agent, claim(c, command))["authorization"]["grant_id"] is None
    assert c.service.read(c.owner, created["pending_id"]).status == "pending"


def test_a_saved_card_keeps_its_original_input(hitl):
    c = hitl
    command, _ = pending(c)
    action = command.action.model_copy(update={"arguments": {"command": "different"}})
    different = command.model_copy(update={"action": action, "action_digest": action.sha256})
    denied((409, "NATIVE_HITL_ACTION_CHANGED", "the original pending call cannot authorize different input"),
           c.service.submit, c.agent, claim(c, different))


@pytest.mark.parametrize("ending", ["deny", "interrupted", "expired"])
def test_a_terminal_wait_cannot_be_reopened(hitl, ending):
    c = hitl
    command, created = pending(c)
    if ending == "deny":
        submit(c, c.owner, decision(c, created, choice="deny"))
    elif ending == "interrupted":
        submit(c, c.agent, close(created, "interrupted"))
    else:
        c.now = START + timedelta(seconds=600)
        submit(c, c.agent, close(created, "expired"))
    denied(REOPENED, c.service.submit, c.agent, claim(c, command))


@pytest.mark.parametrize("decide", [False, True], ids=["waiting", "allowed"])
def test_a_claim_just_before_the_cards_deadline_passes(hitl, decide):
    c = hitl
    command, created = pending(c)
    if decide:
        submit(c, c.owner, decision(c, created))
    c.now = START + timedelta(seconds=599)
    assert "authorization" in submit(c, c.agent, claim(
        c, command, approval=reference(command, created) if decide else None))


@pytest.mark.parametrize("decide", [False, True], ids=["waiting", "allowed"])
def test_a_claim_at_the_cards_deadline_is_refused(hitl, decide):
    c = hitl
    command, created = pending(c)
    if decide:
        submit(c, c.owner, decision(c, created))
    c.now = START + timedelta(seconds=600)
    denied(REOPENED, c.service.submit, c.agent, claim(
        c, command, approval=reference(command, created) if decide else None))


def test_an_approval_of_a_card_never_saved_is_not_found(hitl):
    c = hitl
    command = proposal(c)
    approval = reference(command, {"pending_id": ABSENT, "pending_digest": ABSENT})
    denied((404, "NATIVE_HITL_NOT_FOUND", "the original approval record is unavailable"),
           c.service.submit, c.agent, claim(c, command, approval=approval))


@pytest.mark.parametrize("changes", [{"pending_digest": ABSENT}, {"pending_id": ABSENT}], ids=["digest", "pending"])
def test_an_approval_must_name_the_original_card(hitl, changes):
    c = hitl
    command, created, _ = allowed(c)
    denied(OTHER_INPUT, c.service.submit, c.agent,
           claim(c, command, request_id="wrong-card", approval=reference(command, created, **changes)))


def test_an_approval_of_a_card_nobody_allowed_is_refused(hitl):
    c = hitl
    command, created = pending(c)
    denied(NOT_ALLOWED, c.service.submit, c.agent, claim(c, command, approval=reference(command, created)))


def test_a_missing_grant_is_not_found(hitl):
    c = hitl
    command, created, request = allowed(c)
    grant = key("grant", created["pending_id"])
    del c.table.backing.rows[next(stored for stored in c.table.backing.rows if stored[2] == "behavior:v1:row:" + grant)]
    denied((404, "NATIVE_HITL_NOT_FOUND", "the original approval record is unavailable"),
           c.service.submit, c.agent, request)


def test_a_grant_of_another_action_is_refused(hitl):
    c = hitl
    _, created, request = allowed(c)
    tamper(c, key("grant", created["pending_id"]), action_digest=ABSENT)
    denied((409, "NATIVE_HITL_RECORD_INVALID", "the once grant names another action"),
           c.service.submit, c.agent, request)


def test_a_grant_of_another_approval_is_refused(hitl):
    c = hitl
    _, created, request = allowed(c)
    grant = key("grant", created["pending_id"])
    tamper(c, grant, approval={**data(c, grant)["approval"], "pending_digest": ABSENT})
    denied((409, "NATIVE_HITL_RECORD_INVALID", "the once grant names another action"),
           c.service.submit, c.agent, request)


def test_a_grant_is_refused_at_its_own_deadline(hitl):
    c = hitl
    _, created, request = allowed(c)
    deadline = START + timedelta(seconds=60)
    tamper(c, key("grant", created["pending_id"]), expires_at=deadline.isoformat())
    c.now = deadline - timedelta(seconds=1)
    denied_later = request.model_copy(update={"request_id": "at-the-grant-deadline", "effect_token": "effect-later"})
    assert submit(c, c.agent, request)["authorization"]["expires_at"].startswith("2026-10-02T09:01:00")
    pid = created["pending_id"]
    del c.table.backing.rows[next(stored for stored in c.table.backing.rows
                                  if stored[2] == "behavior:v1:row:" + key("authorization", pid))]
    c.now = deadline
    denied((410, "NATIVE_HITL_EXPIRED", "the original once decision has expired"),
           c.service.submit, c.agent, denied_later)
