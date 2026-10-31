"""H1: signed local HTTP -> real control CAS/reconcile, fake Table only; no model or Hosted I/O."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import A, B, C, TENANT, InMemoryRepository
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier, BehaviorPrincipal
from shared_context.behavior_store import BehaviorStore
from shared_context.behavior_team_registry import TeamRegistry
from shared_context.errors import Problem
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.native_hitl import (
    NativeHitlService, _Intent, _RulesSnapshot, hitl_key, pending_id,
)
from shared_context.native_hitl_api import ENABLED_ENV, PREFIX, TTL_ENV, install_native_hitl_routes
from shared_context.native_hitl_wire import INTERACTIVE_TTL_ENV, hitl_settings
from shared_context.native_hitl_wire import (
    NativeAttentionItem, NativeHitlAction, NativeHitlCall, NativeHitlClose, NativeHitlDecision, NativeHitlEnd,
    NativeHitlTurn, NativeHitlPendingCreate, NativeHitlReadback, NativeHitlReview, NativeHitlRuleChange, NativeHitlRuleInput,
)
from shared_context.records import Record
from shared_context.team_data_relay import TeamDataRelay, install_team_data_routes
from test_behavior_store import FakeTable, SCOPE
from test_behavior_server import workspace
from test_m3_auth import CLIENT_ID, HUMAN_OID, UNKNOWN_OID, jwk_client, signing_key, token

SECOND_OWNER = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
START = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def hitl(auth, jwk_client, signing_key):
    table = FakeTable()
    store = BehaviorStore(table, SCOPE)
    store.initialize()
    teams = TeamRegistry(store, tenant_id=TENANT, operator_oid=HUMAN_OID, legacy_team_id=A.team,
                         legacy_owner_oid=HUMAN_OID, agent_oids=tuple(auth.actors))
    owner = BehaviorPrincipal(TENANT, HUMAN_OID, A.team, "human", None, frozenset({"read", "submit", "approve"}))
    teams.register_owner(owner, "second-owner", C.team, SECOND_OWNER)
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client, teams=teams)
    # The same config.agent_scope interface used by resolve_agent_scope; JWT and Team registry are real.
    scopes = {name: SimpleNamespace(tenant_id=TENANT, team_id=actor.team, identity=actor)
              for name, actor in (("native-a", A), ("native-b", B), ("native-c", C))}
    config = SimpleNamespace(
        identity=SimpleNamespace(tenant_id=TENANT, team_id=A.team, human_oid=HUMAN_OID),
        targets=tuple(SimpleNamespace(agent_id=name) for name in scopes),
        agent_scope=lambda agent_id: scopes[agent_id],
    )
    case = SimpleNamespace(table=table, store=store, verifier=verifier, teams=teams,
                           owner=owner, now=START, config=config)
    case.composition = SimpleNamespace(
        runtime=SimpleNamespace(store=store, clock=lambda: case.now), verifier=verifier, config=config)
    case.service = NativeHitlService(store, verifier, clock=lambda: case.now, resolve_agent=config.agent_scope)
    case.agent = BehaviorPrincipal(TENANT, A.oid, A.team, "agent", A.actor,
                                  frozenset({"read", "submit", "execute", "consume"}))
    case.other_agent = replace(case.agent, oid=B.oid, actor_id=B.actor)
    case.other_team = replace(case.agent, oid=C.oid, actor_id=C.actor, team=C.team)
    case.owner_two = replace(owner, oid=SECOND_OWNER, team=C.team)

    def headers(principal=None, *, read_only=False, claims=None):
        principal = owner if principal is None else principal
        values = ({"oid": principal.oid, "scp": "Behavior.Read" + ("" if read_only else " Behavior.Approve")}
                  if principal.kind == "human" else
                  {"oid": principal.oid, "roles": ["Behavior.Agent"], "idtyp": "app"})
        values.update(claims or {})
        return {"Authorization": "Bearer " + token(
            signing_key, values, remove=("scp",) if principal.kind == "agent" else ())}

    app = FastAPI()
    _install_error_handlers(app)
    install_native_hitl_routes(app, case.composition, environ={ENABLED_ENV: "1"})
    case.data = TeamDataRelay(InMemoryRepository(), InMemoryArtifactStore(TENANT), TENANT)
    install_team_data_routes(app, case.data, verifier)
    case.headers, case.app = headers, app
    with TestClient(app) as client:
        case.client = client
        yield case


def change(c, request_id, operation="add", *, rule_id=None, text="Ask before sending an external message.",
           behavior="ask_first", principal=None):
    principal = c.owner if principal is None else principal
    current = c.service.rules(principal)
    return NativeHitlRuleChange(
        request_id=request_id, operation=operation, rule_id=rule_id,
        rule=None if operation == "delete" else NativeHitlRuleInput(text=text, behavior=behavior),
        expected_rules_revision=current.revision, expected_rules_etag=current.etag)


def proposal(c, *, rid="rid-one", call_id="tool-one", request_id="pending-one", agent_id="native-a",
             principal=None, matched=()):
    principal = c.agent if principal is None else principal
    current = c.service.rules(principal)
    action = NativeHitlAction(
        call=NativeHitlCall(agent_id=agent_id, conversation_id="cid-one", request_id=rid,
                           sdk_session_id="sdk-original", tool_call_id=call_id),
        category="shell", tool="shell", target="synthetic-workspace",
        arguments={"command": "echo synthetic", "cwd": "synthetic-workspace"})
    return NativeHitlPendingCreate(
        request_id=request_id, action=action, action_digest=action.sha256,
        review=NativeHitlReview(rules_revision=current.revision, matched_rule_ids=matched,
                               source="rules" if matched else "default", outcome="ask"))


def post(c, path, command, principal=None, **kwargs):
    return c.client.post(PREFIX + path, json=command.model_dump(mode="json"),
                         headers=c.headers(principal, **kwargs))


def create_pending(c, **kwargs):
    command = proposal(c, **kwargs)
    response = post(c, "/pending", command, c.agent)
    assert response.status_code == 200, response.text
    return command, response.json()["result"]


def decision(c, pending, *, request_id="decision-one", choice="allow_once", principal=None):
    rules = c.service.rules(c.owner if principal is None else principal)
    return NativeHitlDecision(
        request_id=request_id, pending_id=pending["pending_id"], pending_digest=pending["pending_digest"],
        choice=choice, expected_rules_revision=rules.revision, expected_rules_etag=rules.etag,
        rule=NativeHitlRuleInput(text="Allow echo in the synthetic workspace.", behavior="allow_automatically")
        if choice == "allow_rule" else None)


def read(c, pid, principal=None):
    return c.client.get(PREFIX + "/pending/" + pid, headers=c.headers(principal))


def error(response, status, code=None):
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json()["error"]["code"] == code, response.text


def inventory(c):
    rows = {}
    for (_, _, key), entity in c.table.backing.rows.items():
        if key.startswith("behavior:v1:row:"):
            rows[key.removeprefix("behavior:v1:row:")] = Record(
                json.loads(entity["Payload"]), entity.metadata["etag"])
    return SimpleNamespace(rows=rows)


def fail_apply(c, monkeypatch, *, stage=2, after=True):
    count = 0

    def submit(*args, **kwargs):
        nonlocal count
        count += 1
        c.table.failure = ("after" if after else "before") if count == stage else None
        return FakeTable.submit_transaction(c.table, *args, **kwargs)
    monkeypatch.setattr(c.table, "submit_transaction", submit)


def test_default_off_adds_no_routes_state_reads_or_writes(hitl):
    c = hitl
    for environ in ({}, {ENABLED_ENV: "0"}):
        app = FastAPI()
        # Disabled composition does not even need a store, model, SDK or execution queue.
        install_native_hitl_routes(app, object(), environ=environ)
        assert not hasattr(app.state, "native_hitl")
        with TestClient(app) as client:
            assert client.get(PREFIX + "/rules").status_code == 404
            assert client.post(PREFIX + "/decisions", json={}).status_code == 404
    before = len(c.table.submissions)
    current = c.client.get(PREFIX + "/rules", headers=c.headers())
    assert current.json() == {"schema_version": "fa-native-hitl-rules-v1", "team_id": A.team,
                              "revision": 0, "etag": None, "rules": []}
    assert len(c.table.submissions) == before


@pytest.mark.parametrize("environ", [
    {ENABLED_ENV: "true"}, {ENABLED_ENV: "1", TTL_ENV: "0"}, {ENABLED_ENV: "1", TTL_ENV: "-1"},
    {ENABLED_ENV: "1", TTL_ENV: "1.0"}, {ENABLED_ENV: "1", TTL_ENV: "2147483648"},
])
def test_invalid_server_settings_fail_explicitly(hitl, environ):
    with pytest.raises(ValueError):
        install_native_hitl_routes(FastAPI(), hitl.composition, environ=environ)


def test_interactive_wait_bound_is_its_own_setting():
    assert hitl_settings({ENABLED_ENV: "1"}) == (True, 600, 86400)
    assert hitl_settings({ENABLED_ENV: "1", TTL_ENV: "300", INTERACTIVE_TTL_ENV: "7200"}) == (True, 300, 7200)
    with pytest.raises(ValueError):
        hitl_settings({ENABLED_ENV: "1", INTERACTIVE_TTL_ENV: "0"})


def test_a_record_without_wait_is_background_and_keeps_its_original_bytes(hitl):
    c = hitl
    old = proposal(c).model_dump(mode="json")
    assert "wait" not in old
    response = c.client.post(PREFIX + "/pending", json=old, headers=c.headers(c.agent))
    assert response.status_code == 200, response.text
    view = read(c, response.json()["result"]["pending_id"]).json()
    assert view["pending"]["command"] == old
    assert view["pending"]["expires_at"] == "2026-10-02T09:10:00Z"
    interactive = proposal(c, rid="rid-owner", request_id="pending-owner").model_copy(update={"wait": "interactive"})
    response = post(c, "/pending", interactive, c.agent)
    stored = read(c, response.json()["result"]["pending_id"]).json()
    assert stored["pending"]["command"]["wait"] == "interactive"
    assert stored["pending"]["expires_at"] == "2026-10-03T09:00:00Z"


def test_signed_owner_rules_create_update_delete_keep_ids_revisions_and_history(hitl):
    c = hitl
    add = change(c, "rule-add")
    added = post(c, "/rules", add)
    assert added.status_code == 200, added.text
    original = c.service.rules(c.owner)
    rid = original.rules[0].rule_id
    assert original.revision == 1 and original.etag is not None
    before = inventory(c).rows
    update = change(c, "rule-update", "update", rule_id=rid, text="Allow echo.", behavior="allow_automatically")
    assert post(c, "/rules", update).status_code == 200
    edited = c.service.rules(c.owner)
    assert edited.revision == 2 and edited.etag != original.etag
    assert edited.rules[0].rule_id == rid and edited.rules[0].behavior == "allow_automatically"
    delete = change(c, "rule-delete", "delete", rule_id=rid)
    assert post(c, "/rules", delete).status_code == 200
    assert c.service.rules(c.owner).revision == 3 and c.service.rules(c.owner).rules == ()
    after = inventory(c).rows
    original_snapshot = c.service._snapshot_key(c.owner, 1)
    assert after[original_snapshot] == before[original_snapshot]
    assert set(before) <= set(after)
    # Read-back uses the old command, not the new revision/ETag.
    writes = len(c.table.submissions)
    assert post(c, "/rules/reconcile", add, read_only=True).json() == added.json()
    assert post(c, "/rules", add).json() == added.json()
    assert len(c.table.submissions) == writes
    error(post(c, "/rules", update.model_copy(update={"request_id": "stale-update"})), 409, "NATIVE_HITL_RULES_CHANGED")


def test_twenty_rules_fit_and_twenty_first_never_overwrites(hitl):
    c = hitl
    for index in range(20):
        # Worst-case UTF-8 rule length still fits one existing Table entity.
        response = post(c, "/rules", change(c, f"rule-{index}", text="\U0001f642" * 512))
        assert response.status_code == 200, response.text
    current, rows = c.service.rules(c.owner), inventory(c).rows
    error(post(c, "/rules", change(c, "twenty-first")), 409, "NATIVE_HITL_RULE_LIMIT")
    assert c.service.rules(c.owner) == current and inventory(c).rows == rows
    _, pending = create_pending(c)
    error(post(c, "/decisions", decision(c, pending, choice="allow_rule")), 409, "NATIVE_HITL_RULE_LIMIT")
    assert read(c, pending["pending_id"]).json()["status"] == "pending"
    assert post(c, "/decisions", decision(c, pending, request_id="allow-only-this")).status_code == 200
    assert c.service.rules(c.owner) == current


def test_owner_only_and_team_scope_cannot_be_supplied_by_roles_body_or_actor(hitl):
    c = hitl
    command = change(c, "rules-one")
    for principal in (c.agent, c.other_agent, c.other_team):
        error(post(c, "/rules", command, principal, claims={"roles": ["Behavior.Agent", "Behavior.Approve"]}), 403)
    error(post(c, "/rules", command, read_only=True), 403)
    error(c.client.post(PREFIX + "/rules", json=command.model_dump(mode="json")), 401)
    error(post(c, "/rules", command, claims={"oid": UNKNOWN_OID}), 403)
    error(post(c, "/rules", command, claims={"idtyp": "app"}), 403)
    error(c.client.post(PREFIX + "/rules", json={**command.model_dump(mode="json"), "team_id": C.team},
                        headers=c.headers()), 422)
    assert post(c, "/rules", command, claims={"team": C.team, "actor": "system"}).status_code == 200
    assert c.service.rules(c.owner_two).rules == ()
    two = change(c, "rules-one", principal=c.owner_two)
    assert post(c, "/rules", two, c.owner_two).status_code == 200
    assert c.service.rules(c.owner).rules[0].rule_id != c.service.rules(c.owner_two).rules[0].rule_id
    # An internally forged human principal still cannot substitute a different owner.
    with pytest.raises(Problem) as denied:
        c.service.submit(replace(c.agent, kind="human", actor_id=None, actions=c.owner.actions),
                         change(c, "fake-human"))
    assert denied.value.status == 403


def test_agent_can_only_propose_and_read_its_original_native_call(hitl):
    c = hitl
    command, pending = create_pending(c)
    pid = pending["pending_id"]
    error(post(c, "/pending", command), 403)
    error(post(c, "/pending", proposal(c, agent_id="native-b", request_id="wrong-agent"), c.agent), 403)
    error(read(c, pid, c.other_agent), 403)
    error(read(c, pid, c.other_team), 403)
    error(read(c, pid, c.owner_two), 403)
    body = read(c, pid, c.agent)
    assert body.status_code == 200 and body.json()["pending"]["command"] == command.model_dump(mode="json")
    view = NativeHitlReadback.model_validate_json(body.content)
    assert view.attention.type == "approval_required"
    assert view.attention.item_id == pid and view.attention.approval == view.approval
    assert view.grant is None and view.status == "pending"
    error(post(c, "/decisions", decision(c, pending), c.agent), 403)
    error(post(c, "/decisions", decision(c, pending, principal=c.owner_two), c.owner_two), 403)
    # The server-only control rows are not in the generic Team data partition or kind allowlist.
    for kind in ("native_hitl_pending", "native_hitl_rules_head", "native_hitl_decision"):
        error(c.client.put(f"/team-data/v1/records/{kind}/{pid}", json={"data": {"allow": True}},
                           headers={**c.headers(c.agent), "If-None-Match": "*"}), 404)
    harmless = c.client.put(f"/team-data/v1/records/result/{pid}", json={"data": {"choice": "allow_once"}},
                           headers={**c.headers(c.agent), "If-None-Match": "*"})
    assert harmless.status_code == 201 and read(c, pid).json()["status"] == "pending"


def test_allow_once_is_one_retained_choice_not_dispatch_or_a_second_grant(hitl):
    c = hitl
    command, pending = create_pending(c)
    approved = decision(c, pending)
    first = post(c, "/decisions", approved)
    assert first.status_code == 200, first.text
    before = len(c.table.submissions)
    assert post(c, "/decisions", approved).json() == first.json()
    assert post(c, "/decisions/reconcile", approved, read_only=True).json() == first.json()
    assert len(c.table.submissions) == before
    view = NativeHitlReadback.model_validate_json(read(c, pending["pending_id"]).content)
    assert view.status == "allow" and view.decision == approved and view.attention is None
    assert view.grant.uses == 1 and view.grant.approval.call == command.action.call
    assert view.grant.action_digest == command.action_digest
    assert view.grant.purpose == "recorded-choice-not-execution-permission"
    assert not hasattr(c.app.state.native_hitl, "consume")
    error(post(c, "/decisions", decision(c, pending, request_id="double-click")), 409, "NATIVE_HITL_STATE_CONFLICT")
    error(post(c, "/decisions", decision(c, pending, request_id="too-late-deny", choice="deny")), 409)
    assert len([row for row in inventory(c).rows.values()
                if row.payload["data"].get("kind") == "native_hitl_once_grant"]) == 1


def test_allow_rule_is_atomic_and_keeps_the_exact_confirmed_text(hitl):
    c = hitl
    _, pending = create_pending(c)
    command = decision(c, pending, choice="allow_rule")
    response = post(c, "/decisions", command)
    assert response.status_code == 200, response.text
    rules = c.service.rules(c.owner)
    view = c.service.read(c.owner, pending["pending_id"])
    assert rules.revision == view.grant.rules_revision == 1
    assert rules.rules[0].text == command.rule.text
    assert view.pending.command.review.rules_revision == 0
    assert view.decision == command
    snapshot = c.store.read_record(c.service._snapshot_key(c.owner, 1))
    assert _RulesSnapshot.model_validate_json(json.dumps(snapshot.payload["data"])).intent_id == c.service._intent_key(c.owner, command)
    preserved = view.grant
    deletion = change(c, "delete-allowed-rule", "delete", rule_id=rules.rules[0].rule_id)
    assert post(c, "/rules", deletion).status_code == 200
    later = c.service.read(c.owner, pending["pending_id"])
    assert later.grant == preserved and later.decision == command
    assert later.current_rules_revision == 2


def test_latest_rules_required_without_rewriting_pending_review_or_human_decision(hitl):
    c = hitl
    command = proposal(c)
    assert post(c, "/rules", change(c, "rule-added")).status_code == 200
    error(post(c, "/pending", command, c.agent), 409, "NATIVE_HITL_RULES_CHANGED")
    rule_id = c.service.rules(c.owner).rules[0].rule_id
    fresh, pending = create_pending(c, matched=(rule_id,))
    stale_decision = decision(c, pending)
    assert post(c, "/rules", change(c, "rule-updated", "update", rule_id=rule_id, text="Ask before every shell call.")).status_code == 200
    error(post(c, "/decisions", stale_decision), 409, "NATIVE_HITL_RULES_CHANGED")
    approved = decision(c, pending, request_id="current-decision")
    assert post(c, "/decisions", approved).status_code == 200
    view = c.service.read(c.owner, pending["pending_id"])
    assert view.pending.command == fresh and view.grant.rules_revision == 2
    # H2 must still independently review current rules before consuming; H1 reports only the saved human choice.
    assert view.purpose == "readback-only-not-execution-permission"


def test_digest_and_original_call_are_immutable_even_with_new_command_ids(hitl):
    c = hitl
    command, pending = create_pending(c)
    broken = command.model_dump(mode="json")
    broken["action"]["arguments"]["command"] = "different"
    error(c.client.post(PREFIX + "/pending", json=broken, headers=c.headers(c.agent)), 422)
    altered_action = command.action.model_copy(update={"target": "different-workspace"})
    altered = command.model_copy(update={"request_id": "another-intent", "action": altered_action,
                                         "action_digest": altered_action.sha256})
    error(post(c, "/pending", altered, c.agent), 409, "NATIVE_HITL_CALL_EXISTS")
    error(post(c, "/decisions", decision(c, pending).model_copy(update={"pending_digest": "0" * 64})),
          409, "NATIVE_HITL_ACTION_CHANGED")
    assert read(c, pending["pending_id"]).json()["pending"]["command"] == command.model_dump(mode="json")


@pytest.mark.parametrize("choice", ["allow_once", "deny", "allow_rule"])
def test_expiry_refuses_new_decisions_and_read_does_not_mutate_history(hitl, choice):
    c = hitl
    _, pending = create_pending(c)
    c.now += timedelta(seconds=600)
    before = len(c.table.submissions)
    view = read(c, pending["pending_id"]).json()
    assert view["status"] == "pending" and view["expired"] and view["attention"] is None
    assert len(c.table.submissions) == before
    error(post(c, "/decisions", decision(c, pending, choice=choice)), 410, "NATIVE_HITL_EXPIRED")
    close = NativeHitlClose(request_id="expired", pending_id=pending["pending_id"],
                            pending_digest=pending["pending_digest"], status="expired")
    assert post(c, "/observations", close, c.agent).status_code == 200
    assert read(c, pending["pending_id"]).json()["status"] == "expired"
    error(post(c, "/decisions", decision(c, pending, request_id="after-expiry")), 409)


def test_deny_interruption_and_unknown_remain_distinct_and_never_refill_grants(hitl):
    c = hitl
    _, declined = create_pending(c)
    denied = decision(c, declined, choice="deny")
    assert post(c, "/decisions", denied).status_code == 200
    assert read(c, declined["pending_id"]).json()["grant"] is None
    error(post(c, "/decisions", decision(c, declined, request_id="allow-after-deny")), 409)
    _, interrupted = create_pending(c, rid="rid-two", request_id="pending-two")
    close = NativeHitlClose(request_id="interrupt", pending_id=interrupted["pending_id"],
                            pending_digest=interrupted["pending_digest"], status="interrupted")
    error(post(c, "/observations", close), 403)
    assert post(c, "/observations", close, c.agent).status_code == 200
    assert read(c, interrupted["pending_id"]).json()["status"] == "interrupted"
    error(post(c, "/decisions", decision(c, interrupted, request_id="allow-after-interrupt")), 409)
    _, unknown = create_pending(c, rid="rid-three", request_id="pending-three")
    approved = decision(c, unknown, request_id="approve-three")
    assert post(c, "/decisions", approved).status_code == 200
    original_grant = read(c, unknown["pending_id"]).json()["grant"]
    observation = NativeHitlClose(request_id="unknown-effect", pending_id=unknown["pending_id"],
                                  pending_digest=unknown["pending_digest"], status="effect_unknown")
    assert post(c, "/observations", observation, c.agent).status_code == 200
    c.now += timedelta(seconds=1000)
    error(post(c, "/observations", observation.model_copy(update={"request_id": "wash-unknown", "status": "expired"}),
               c.agent), 409)
    state = read(c, unknown["pending_id"]).json()
    assert state["status"] == "effect_unknown" and state["grant"] == original_grant
    assert state["decision"] == approved.model_dump(mode="json")
    assert post(c, "/decisions/reconcile", approved).status_code == 200
    assert read(c, declined["pending_id"]).json()["status"] == "deny"


@pytest.mark.parametrize("stage,after", [(1, True), (2, False), (2, True)])
def test_lost_ack_and_original_intent_only_read_back_without_resubmitting(hitl, monkeypatch, stage, after):
    c = hitl
    _, pending = create_pending(c)
    command = decision(c, pending)
    fail_apply(c, monkeypatch, stage=stage, after=after)
    error(post(c, "/decisions", command), 503, "BEHAVIOR_COMMIT_UNKNOWN")
    saved = _Intent.model_validate_json(
        json.dumps(c.store.read_record(c.service._intent_key(c.owner, command)).payload["data"]))
    assert saved.command == command and saved.principal_oid == HUMAN_OID
    before = len(c.table.submissions)
    for path in ("/decisions", "/decisions/reconcile"):
        response = post(c, path, command)
        if stage == 2 and after:
            assert response.status_code == 200 and response.json()["result"]["status"] == "allow"
        else:
            error(response, 503, "BEHAVIOR_COMMIT_UNKNOWN")
    assert len(c.table.submissions) == before
    changed = command.model_copy(update={"choice": "deny"})
    error(post(c, "/decisions/reconcile", changed), 409, "NATIVE_HITL_COMMAND_CONFLICT")
    assert len(c.table.submissions) == before


def test_later_explicit_decision_never_backfills_an_older_unknown_command(hitl, monkeypatch):
    c = hitl
    _, pending = create_pending(c)
    unknown = decision(c, pending)
    fail_apply(c, monkeypatch, stage=2, after=False)
    error(post(c, "/decisions", unknown), 503)
    newer = decision(c, pending, request_id="new-explicit-denial", choice="deny")
    assert post(c, "/decisions", newer).status_code == 200
    error(post(c, "/decisions/reconcile", unknown), 503, "BEHAVIOR_COMMIT_UNKNOWN")
    error(post(c, "/decisions", unknown), 503, "BEHAVIOR_COMMIT_UNKNOWN")
    assert read(c, pending["pending_id"]).json()["decision"] == newer.model_dump(mode="json")


@pytest.mark.parametrize("after", [False, True])
def test_a_lost_end_or_close_apply_reruns_once_under_its_id_and_keeps_the_owner_decision(hitl, monkeypatch, after):
    c = hitl
    _, decided = create_pending(c)
    chosen = decision(c, decided)
    assert post(c, "/decisions", chosen).status_code == 200
    _, open_ = create_pending(c, call_id="tool-two", request_id="pending-two")
    close = NativeHitlClose(request_id="close-two", pending_id=open_["pending_id"],
                            pending_digest=open_["pending_digest"], status="interrupted")
    end = NativeHitlEnd(request_id="end-one", turn=NativeHitlTurn(
        agent_id="native-a", conversation_id="cid-one", request_id="rid-one"))
    for path, command, field, value in (
            ("/observations", close, "status", "interrupted"), ("/end", end, "closed", True)):
        fail_apply(c, monkeypatch, stage=2, after=after)  # the intent commits; its apply is lost (or its ACK is)
        error(post(c, path, command, c.agent), 503, "BEHAVIOR_COMMIT_UNKNOWN")
        before = len(c.table.submissions)
        for _ in range(2):
            response = post(c, path, command, c.agent)
            assert response.status_code == 200 and response.json()["result"][field] == value, response.text
        assert len(c.table.submissions) == before + (0 if after else 1)
    view = read(c, decided["pending_id"]).json()
    assert view["status"] == "allow" and view["decision"] == chosen.model_dump(mode="json") and view["turn_closed"]
    assert read(c, open_["pending_id"]).json()["status"] == "interrupted"


@pytest.mark.parametrize("choice", ["deny", "allow_once"])
def test_a_rerun_close_never_replaces_a_decision_recorded_after_its_lost_apply(hitl, monkeypatch, choice):
    c = hitl
    _, pending = create_pending(c)
    close = NativeHitlClose(request_id="close-lost", pending_id=pending["pending_id"],
                            pending_digest=pending["pending_digest"], status="interrupted")
    fail_apply(c, monkeypatch, stage=2, after=False)
    error(post(c, "/observations", close, c.agent), 503, "BEHAVIOR_COMMIT_UNKNOWN")
    chosen = decision(c, pending, choice=choice)
    assert post(c, "/decisions", chosen).status_code == 200
    rerun = post(c, "/observations", close, c.agent)
    if choice == "deny":
        error(rerun, 409, "NATIVE_HITL_STATE_CONFLICT")
    else:  # the ordinary Close rule closes an unrun allow, as finish_turn does; the decision record stays
        assert rerun.status_code == 200 and rerun.json()["result"]["status"] == "interrupted", rerun.text
    view = read(c, pending["pending_id"]).json()
    assert view["status"] == ("deny" if choice == "deny" else "interrupted")
    assert view["decision"] == chosen.model_dump(mode="json")


def test_pending_and_rule_lost_ack_share_original_readback_contract(hitl, monkeypatch):
    c = hitl
    fail_apply(c, monkeypatch, stage=2, after=True)
    command = proposal(c)
    error(post(c, "/pending", command, c.agent), 503)
    before = len(c.table.submissions)
    response = post(c, "/pending/reconcile", command, c.agent)
    assert response.status_code == 200 and len(c.table.submissions) == before
    rule = change(c, "rule-lost")
    fail_apply(c, monkeypatch, stage=2, after=True)
    error(post(c, "/rules", rule), 503)
    before = len(c.table.submissions)
    assert post(c, "/rules/reconcile", rule, read_only=True).status_code == 200
    assert len(c.table.submissions) == before


def test_approve_deny_race_has_only_one_winner_without_automatic_retry(hitl):
    c = hitl
    _, pending = create_pending(c)
    allow = decision(c, pending)
    deny = decision(c, pending, request_id="race-deny", choice="deny")
    original = c.table.submit_transaction
    count = 0

    def submit(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            c.table.before_submit = lambda: c.service.submit(c.owner, deny)
        return original(*args, **kwargs)

    c.table.submit_transaction = submit
    error(post(c, "/decisions", allow), 412, "BEHAVIOR_CONFLICT")
    view = c.service.read(c.owner, pending["pending_id"])
    assert view.status == "deny" and view.grant is None and view.decision == deny
    before = len(c.table.submissions)
    error(post(c, "/decisions", allow), 503, "BEHAVIOR_COMMIT_UNKNOWN")
    assert len(c.table.submissions) == before


def test_owner_deactivation_wins_the_same_guard_before_decision_apply(hitl):
    c = hitl
    c.teams.register_owner(c.owner, "original-owner", A.team, HUMAN_OID)
    _, pending = create_pending(c)
    command = decision(c, pending)
    c.table.before_submit = lambda: c.teams.deactivate_owner(c.owner, "disable-owner", HUMAN_OID)
    error(post(c, "/decisions", command), 412)
    error(post(c, "/decisions", command), 403)
    assert c.service.read(c.agent, pending["pending_id"]).status == "pending"


def test_new_service_after_restart_does_not_resume_waits_or_extend_ttl(hitl):
    c = hitl
    command, pending = create_pending(c)
    original = c.service.read(c.owner, pending["pending_id"])
    other = NativeHitlService(c.store, c.verifier, clock=lambda: c.now, resolve_agent=c.config.agent_scope, ttl_seconds=30)
    count = len(c.table.submissions)
    assert other.submit(c.agent, command).payload["result"]["pending_id"] == pending["pending_id"]
    assert other.read(c.agent, pending["pending_id"]) == original
    assert len(c.table.submissions) == count
    error(post(c, "/pending", command.model_copy(update={"request_id": "new-wait-after-restart"}), c.agent), 409)
    c.now += timedelta(seconds=601)
    assert other.read(c.owner, pending["pending_id"]).expired
    assert other.read(c.owner, pending["pending_id"]).pending.expires_at == START + timedelta(seconds=600)


def test_custom_ttl_and_unconfirmed_initial_intent_do_not_invent_a_wait(hitl, monkeypatch):
    c = hitl
    service = NativeHitlService(c.store, c.verifier, clock=lambda: c.now,
                                resolve_agent=c.config.agent_scope, ttl_seconds=30)
    command = proposal(c)
    service.submit(c.agent, command)
    pid = pending_id(TENANT, A.team, command.action.call)
    assert service.read(c.agent, pid).pending.expires_at == START + timedelta(seconds=30)
    fresh = proposal(c, rid="rid-no-intent", request_id="no-intent")
    fail_apply(c, monkeypatch, stage=1, after=False)
    error(post(c, "/pending", fresh, c.agent), 503, "BEHAVIOR_COMMIT_UNKNOWN")
    before = len(c.table.submissions)
    error(post(c, "/pending/reconcile", fresh, c.agent), 503, "BEHAVIOR_COMMIT_UNKNOWN")
    assert c.store.read_record(c.service._intent_key(c.agent, fresh)) is None
    assert c.store.read_record(pending_id(TENANT, A.team, fresh.action.call)) is None
    assert len(c.table.submissions) == before


def test_invalid_and_unknown_review_results_cannot_become_approval(hitl):
    c = hitl
    for source in ("unavailable", "uncertain"):
        review = NativeHitlReview(rules_revision=0, source=source, outcome="ask")
        with pytest.raises(ValidationError):
            NativeHitlReview(rules_revision=0, source=source, outcome="allow")
        request = proposal(c, rid=source, request_id=source).model_copy(update={"review": review})
        assert post(c, "/pending", request, c.agent).status_code == 200
    for outcome in ("allow", "deny"):
        command = proposal(c).model_dump(mode="json")
        command["review"]["outcome"] = outcome
        error(c.client.post(PREFIX + "/pending", json=command, headers=c.headers(c.agent)), 422)
    command = proposal(c).model_dump(mode="json")
    command["action"]["category"] = "memory"
    error(c.client.post(PREFIX + "/pending", json=command, headers=c.headers(c.agent)), 422)
    command = proposal(c).model_dump(mode="json")
    command["review"].update(source="rules", matched_rule_ids=["not-current"])
    error(c.client.post(PREFIX + "/pending", json=command, headers=c.headers(c.agent)), 409)


def test_attention_is_a_persisted_approval_projection_not_a_question_or_grant(hitl):
    c = hitl
    _, pending = create_pending(c)
    item = read(c, pending["pending_id"]).json()["attention"]
    with pytest.raises(ValidationError):
        NativeAttentionItem.model_validate({**item, "type": "assistant_question", "question": "Continue?"})
    with pytest.raises(ValidationError):
        NativeAttentionItem.model_validate({**item, "item_id": "0" * 64})
    error(c.client.post(PREFIX + "/decisions", json=item, headers=c.headers()), 422)
    error(c.client.post(PREFIX + "/decisions?_reconcile=false", json=decision(c, pending).model_dump(mode="json"),
                        headers=c.headers()), 422)
    error(c.client.get(PREFIX + "/rules?team=team-two", headers=c.headers()), 422)


def test_cold_api_import_blocks_all_hosted_and_copilot_packages():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    program = """
import importlib
import importlib.abc
import sys
class ApiOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == 'copilot' or name.startswith(('copilot.', 'azure.ai.agentserver')):
            raise AssertionError('H1 imported a Hosted/SDK dependency: ' + name)
sys.meta_path.insert(0, ApiOnly())
def offline(event, args):
    if event == 'socket.connect':
        raise AssertionError('H1 cold import attempted network I/O')
sys.addaudithook(offline)
sys.path.insert(0, sys.argv[1])
for name in ('native_hitl_wire', 'native_hitl', 'native_hitl_api', 'native_routine_proposals', 'native_routines',
             'behavior_server'):
    importlib.import_module('shared_context.' + name)
print('H1 API-only imports PASS')
"""
    completed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", program, str(source)],
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "H1 API-only imports PASS" in completed.stdout


def test_real_server_factory_keeps_h1_separate(workspace, jwk_client, signing_key, monkeypatch):
    from shared_context import behavior_server
    from test_team_registry import page_inventory
    from test_behavior_server import Harness

    # Tokenization is unrelated to the approval control path; no cache download or model is needed.
    monkeypatch.setattr(behavior_server, "ServerTokenizer", lambda _: SimpleNamespace(count=len))
    monkeypatch.delenv(ENABLED_ENV, raising=False)
    h = Harness(workspace, jwk_client)
    try:
        h.activate()
        inventory_table = SimpleNamespace(
            backing=h.table.backing, table_name=h.config.table.scope.table_name, url=h.config.table.endpoint)
        page_inventory(inventory_table)
        monkeypatch.setattr(h.composition.clients[0], "query_entities", inventory_table.query_entities)
        disabled = behavior_server.application(h.composition)
        assert not hasattr(disabled.state, "native_hitl")
        assert all(not getattr(route, "path", "").startswith(PREFIX) for route in disabled.routes)
        monkeypatch.setenv(ENABLED_ENV, "1")
        app = behavior_server.application(h.composition)
        scope = h.config.agent_scope(h.config.targets[0].agent_id)
        action = NativeHitlAction(
            call=NativeHitlCall(agent_id=scope.target.agent_id, conversation_id="native-cid", request_id="native-rid",
                               sdk_session_id="native-sdk", tool_call_id="native-call"),
            category="desktop", tool="desktop", target="synthetic-viewer", arguments={"action": "click", "x": 1, "y": 2})
        pending_command = NativeHitlPendingCreate(
            request_id="native-pending", action=action, action_digest=action.sha256,
            review=NativeHitlReview(rules_revision=0, source="default", outcome="ask"))
        agent_headers = {"Authorization": "Bearer " + token(signing_key, {
            "oid": str(scope.identity.oid), "roles": ["Behavior.Agent"], "idtyp": "app"}, remove=("scp",))}
        with TestClient(app) as client:
            response = client.post(PREFIX + "/pending", json=pending_command.model_dump(mode="json"), headers=agent_headers)
            assert response.status_code == 200, response.text
            pending = response.json()["result"]
            choice = NativeHitlDecision(
                request_id="native-decision", pending_id=pending["pending_id"], pending_digest=pending["pending_digest"],
                expected_rules_revision=0, expected_rules_etag=None, choice="allow_rule",
                rule=NativeHitlRuleInput(text="Allow this synthetic click.", behavior="allow_automatically"))
            approved = client.post(PREFIX + "/decisions", json=choice.model_dump(mode="json"),
                                   headers=h.headers(signing_key, "Behavior.Read Behavior.Approve"))
            assert approved.status_code == 200, approved.text
            assert h.calls == []
            assert h.runtime.bindings.keys() == disabled.state.composition.runtime.bindings.keys()
    finally:
        h.composition.close()
