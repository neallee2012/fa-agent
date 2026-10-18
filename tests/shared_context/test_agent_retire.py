"""Retiring a registered Team Agent: an append-only tombstone that every live path honours. Local fakes only."""

from dataclasses import replace
from datetime import datetime, timezone
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from conftest import TENANT
from shared_context.behavior_agent_message_api import public_agents
from shared_context.behavior_operations import _parse
from shared_context.behavior_peer_runtime import install_peer_routing
from shared_context.behavior_team_registry import (
    IdentityRegistration, TeamAgentRetired, TeamRegistry, _retire_request, identity_key, registered_agent_ids,
    retired_agent_ids, retired_key, team_recipient_ids, team_recipient_ready, validate_team_registry_history,
)
from shared_context.errors import Problem
from shared_context.native_routine_clock import NativeRoutineClock
from test_behavior_server import workspace  # noqa: F401  (fixture)
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401  (fixture)
from test_instruction_flow import instruction_models  # noqa: F401  (fixture)
from test_m3_auth import HUMAN_OID, jwk_client, signing_key  # noqa: F401  (fixtures)
from test_team_agent_routing import _register_scope, register_more_agents, team_agent  # noqa: F401  (fixture)
from test_team_registry import AGENT_OID, OTHER_AGENT_OID, _app, _target, _verifier, activated, registry  # noqa: F401

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)

def _code(failure):
    return failure.value.status, failure.value.body["error"]["code"]


# ----- the tombstone ------------------------------------------------------------------------------------------------


def test_tombstone_is_create_only_with_the_identity_deactivation_and_a_rerun_returns_the_original(registry):
    c = registry
    target = _target(1, AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    value, created = c.value.retire_agent(c.operator, "retire-agent-one", target.agent_id, "dev test agent", now=NOW)
    assert created and (value.operator_oid, value.request_id, value.retired_at, value.reason) == (
        HUMAN_OID, "retire-agent-one", NOW, "dev test agent")
    tkey, ikey = retired_key(TENANT, target.agent_id), identity_key(TENANT, AGENT_OID)
    receipt = c.store.reconcile(_retire_request(value, AGENT_OID))
    assert set(dict(receipt.payload["writes"])) == {tkey, ikey}
    identity = _parse(c.store.read_record(ikey).payload["data"], IdentityRegistration)
    assert (identity.active, identity.deactivation_id) == (False, "retire-agent-one")
    count, guard = len(c.table.submissions), c.store._guard()
    for request_id in ("retire-agent-one", "a-later-rerun"):
        again, created = c.value.retire_agent(c.operator, request_id, target.agent_id, "another reason",
                                              now=NOW.replace(hour=10))
        assert again == value and created is False
    assert len(c.table.submissions) == count and c.store._guard() == guard
    assert _parse(c.store.read_record(tkey).payload["data"], TeamAgentRetired) == value


def test_only_the_operator_retires_and_only_a_registered_agent(registry):
    c = registry
    target = _target(1, AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    count = len(c.table.submissions)
    with pytest.raises(Problem) as denied:
        c.value.retire_agent(replace(c.operator, oid=OTHER_AGENT_OID), "retire-denied", target.agent_id, "x", now=NOW)
    assert denied.value.status == 403
    with pytest.raises(Problem) as unknown:
        c.value.retire_agent(c.operator, "retire-unknown", _target(2, OTHER_AGENT_OID).agent_id, "x", now=NOW)
    assert _code(unknown) == (404, "TEAM_AGENT_NOT_FOUND")
    with pytest.raises(Problem) as invalid:
        c.value.retire_agent(c.operator, "retire-bad", target.agent_id, "line\nbreak", now=NOW)
    assert _code(invalid) == (422, "TEAM_OPERATOR_COMMAND")
    assert len(c.table.submissions) == count


def test_a_lost_retire_ack_converges_on_rerun_without_a_second_write(registry):
    c = registry
    target = _target(1, AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    c.table.failure = "after"
    with pytest.raises(Problem) as lost:
        c.value.retire_agent(c.operator, "retire-agent-one", target.agent_id, "dev test agent", now=NOW)
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    c.table.failure = None
    count = len(c.table.submissions)
    value, created = c.value.retire_agent(c.operator, "retire-agent-one", target.agent_id, "dev test agent", now=NOW)
    assert created is False and value.request_id == "retire-agent-one" and len(c.table.submissions) == count


def test_a_retired_agent_token_is_refused_at_the_api_entry(registry, jwk_client, signing_key):
    c = registry
    target = _target(1, AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    verifier = _verifier(c, jwk_client)
    assert verifier(_app(signing_key, AGENT_OID)).actor_id == target.agent_id
    c.value.retire_agent(c.operator, "retire-agent-one", target.agent_id, "dev test agent", now=NOW)
    with pytest.raises(Problem) as refused:
        verifier(_app(signing_key, AGENT_OID))
    assert refused.value.body["error"]["code"] == "BEHAVIOR_ACTOR_NOT_ALLOWED"


@pytest.mark.parametrize("reader", ["migration-quiescence", "rotation-additions"])
def test_a_tombstone_passes_both_restart_readers_and_a_tampered_one_is_refused(activated, reader):
    from shared_context.behavior_migration import _quiescence
    from shared_context.behavior_runtime_rotation_history import validate_additions
    from shared_context.behavior_auth import BehaviorPrincipal
    h = activated
    ids, store = h.config.identity, h.runtime.store
    baseline = store.read_inventory()
    registry = TeamRegistry(store, tenant_id=ids.tenant_id, operator_oid=ids.human_oid,
                            legacy_team_id=ids.team_id, legacy_owner_oid=ids.human_oid,
                            agent_oids=tuple(agent.oid for agent in ids.agents), targets=h.config.targets)
    human = BehaviorPrincipal(ids.tenant_id, ids.human_oid, ids.team_id, "human", None,
                              frozenset({"read", "approve", "submit"}))
    for n, oid in ((1, AGENT_OID), (2, OTHER_AGENT_OID)):
        target = _target(n, oid)
        registry.register_agent(human, f"restart-agent-{n}", target.agent_id, target)
    retired = _target(1, AGENT_OID).agent_id
    registry.retire_agent(human, "restart-retire-1", retired, "dev test agent", now=NOW)

    def read(inventory):
        if reader == "migration-quiescence":
            _quiescence(inventory, h.config)
        else:
            validate_additions(inventory, h.config, h.config, set(baseline.rows), set())

    inventory = store.read_inventory()
    read(inventory)
    classified = validate_team_registry_history(inventory, h.config)
    assert classified[retired_key(ids.tenant_id, retired)].request_id == "restart-retire-1"
    tkey = retired_key(ids.tenant_id, retired)
    row = inventory.rows[tkey]
    tampered = replace(inventory, rows={**inventory.rows, tkey: replace(row, payload={
        **row.payload, "data": {**row.payload["data"], "reason": "rewritten"}})})
    with pytest.raises(Problem):
        read(tampered)
    missing = replace(inventory, rows={key: value for key, value in inventory.rows.items() if key != tkey})
    with pytest.raises(Problem):
        read(missing)


# ----- every live path honours it -----------------------------------------------------------------------------------


def _retire(case, agent_id=None):
    return case.teams.retire_agent(case.c.human, "retire-routing-agent", agent_id or case.target.agent_id,
                                   "dev test agent", now=NOW)


def test_a_fully_onboarded_agent_retired_still_passes_the_real_startup_readers(team_agent):
    """The dev case: the retired Agent also has its runtime registration, policy and a built peer."""
    from shared_context.behavior_migration import MigrationStore, _quiescence, installed_origin
    from test_conversation_transfer_startup import read_startup
    from test_resident_worker_poc import outcome_of
    case = team_agent
    config = case.config

    def readers():
        store = MigrationStore(case.c.h.composition.clients[0], config.table.scope)
        return (outcome_of(lambda: read_startup(case.c)), outcome_of(lambda: _quiescence(store.read_inventory(), config)),
                outcome_of(lambda: installed_origin(store, config)))
    _register_scope(case)
    case.a.for_agent(case.target.agent_id)
    baseline = readers()
    assert baseline[0] == "ok"
    _retire(case)
    assert readers() == baseline


def test_a_concurrent_retire_returns_the_first_tombstone(registry):
    c = registry
    target = _target(1, AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    first = {}
    c.table.before_submit = lambda: first.setdefault("value", c.value.retire_agent(
        c.operator, "retire-first", target.agent_id, "first operator", now=NOW)[0])
    value, created = c.value.retire_agent(c.operator, "retire-second", target.agent_id, "second operator", now=NOW)
    assert created is False and value == first["value"] and value.request_id == "retire-first"


def test_a_retired_agent_is_hidden_from_the_workbench_directory(team_agent, caplog):
    case = team_agent
    _register_scope(case)
    agent_id = case.target.agent_id
    listed = {entry.agent_id: entry.state for entry in public_agents(case.a, case.c.human).agents}
    assert listed[agent_id] == "ready"
    _retire(case)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        directory = public_agents(case.a, case.c.human)
    assert agent_id not in {entry.agent_id for entry in directory.agents}
    assert directory.default_agent_id == case.config.targets[0].agent_id
    assert f"AGENT_DIRECTORY_UNAVAILABLE agent_id={agent_id}" not in caplog.text
    store, config = case.a.runtime.store, case.config
    assert (registered_agent_ids(store, config), retired_agent_ids(store, config)) == ((), (agent_id,))


def test_a_retired_agent_is_never_invoked_synced_or_followed(team_agent):
    from shared_context.behavior_agent_versions import _agents
    from shared_context.native_environment_sync import follow_agent, follow_all
    case = team_agent
    _register_scope(case)
    agent_id = case.target.agent_id
    case.a.for_agent(agent_id)
    kept = register_more_agents(case, 1)[0]
    _retire(case)
    with pytest.raises(Problem) as invoked:
        case.a.for_agent(agent_id)
    assert _code(invoked) == (410, "AGENT_RETIRED")
    served = [target.agent_id for target, _ in _agents(case.a)[1]]
    assert agent_id not in served and kept in served
    assert follow_all(case.a, agent_id=agent_id) == [{"agent_id": agent_id, "state": "failed", "reason": "AGENT_RETIRED"}]
    with pytest.raises(Problem) as followed:
        follow_agent(case.a, agent_id)
    assert _code(followed) == (410, "AGENT_RETIRED")
    assert case.a.for_agent(kept) is not None
    assert case.c.sessions.calls == []


def test_sends_to_a_retired_agent_get_the_retired_outcome(team_agent, signing_key):
    case = team_agent
    _register_scope(case)
    agent_id = case.target.agent_id
    store, config = case.a.runtime.store, case.config
    sender = dict(sender_oid=config.targets[0].agent_oid, team_id=config.identity.team_id)
    assert team_recipient_ready(store, config, agent_id, **sender).target == case.target
    _retire(case)
    with pytest.raises(Problem) as agent_send:
        team_recipient_ready(store, config, agent_id, **sender)
    assert _code(agent_send) == (410, "AGENT_RETIRED")
    assert agent_id not in {identifier for identifier, _ in team_recipient_ids(store, config, **sender)}

    async def problem(request, error):
        return JSONResponse(error.body, status_code=error.status)

    app = Starlette(routes=[Route("/{path:path}", lambda request: JSONResponse({"agent_id": None}))],
                    exception_handlers={Problem: problem})
    install_peer_routing(app, case.a, lambda composition: Starlette(routes=[
        Route("/{path:path}", lambda request: JSONResponse({"agent_id": composition.agent_id}))]))
    human = case.c.h.headers(signing_key, "Behavior.Read Behavior.Approve")
    with TestClient(app, backend_options={"loop_factory": lambda: case.loop}) as client:
        sent = client.post("/agents/" + agent_id + "/work-policy/v1/native-turns/submit", headers=human, json={})
    assert sent.status_code == 410 and sent.json()["error"]["code"] == "AGENT_RETIRED"
    assert "retired" in sent.json()["error"]["message"]
    assert case.c.sessions.calls == []


def test_the_routine_clock_skips_a_retired_agent_and_says_so_once(team_agent, caplog):
    case = team_agent
    _register_scope(case)
    _retire(case)
    visited = []
    service = SimpleNamespace(owner=case.a, due=lambda principal, agent_id, after: visited.append(agent_id)
                              or SimpleNamespace(issues=(), occurrences=(), next_after=None))
    clock = NativeRoutineClock(service)
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            assert clock.tick()["agent_failures"] == 0
    assert case.target.agent_id not in visited and visited
    lines = [record.getMessage() for record in caplog.records if "FA_NATIVE_ROUTINE_CLOCK_RETIRED" in record.getMessage()]
    assert lines == [f"FA_NATIVE_ROUTINE_CLOCK_RETIRED agent={case.target.agent_id[:8]} skipped"]


def test_group_activity_skips_a_retired_member_and_keeps_the_live_turns_readable(monkeypatch, caplog):
    from shared_context import native_group_api
    from shared_context.behavior_team_registry import agent_retired
    from test_native_turn_wire import receipt_for, submit
    group_id, live, gone = "d" * 64, "live-member", "retired-member"
    receipt = receipt_for(submit("live-turn"), "completed")
    group = SimpleNamespace(orchestrator_agent_id=live, deliveries=(
        SimpleNamespace(recipient_agent_id=gone, message_id="gone-turn", refusal_code=None),
        SimpleNamespace(recipient_agent_id=gone, message_id="gone-again", refusal_code=None)))

    class Turns:
        def __init__(self, composition, enabled):
            pass

        def read(self, principal, command):
            if hasattr(command, "conversation_id"):
                return SimpleNamespace(inflight_request_id=None, last_request_id=group_id), None
            return receipt, None

    def for_agent(agent_id):
        if agent_id == gone:
            raise agent_retired()
        return object()

    monkeypatch.setattr(native_group_api, "NativeTurnGateway", Turns)
    monkeypatch.setattr(native_group_api, "native_turn_root",
                        lambda value: SimpleNamespace(agent_id=live, request_id=group_id))
    gateway = object.__new__(native_group_api.NativeGroupGateway)
    gateway.owner = SimpleNamespace(for_agent=for_agent)
    gateway._authorize = lambda principal, owner=False: None
    gateway._store = lambda principal: SimpleNamespace(read=lambda identifier: group)
    with caplog.at_level(logging.INFO):
        activity = gateway.activity(None, group_id)
    assert activity.turns == (receipt,) and activity.unconfirmed_request_ids == ()
    assert caplog.text.count("NATIVE_GROUP_ACTIVITY_RETIRED_MEMBER agent_id=" + gone) == 2

def test_a_retired_agents_binding_path_answers_retired_after_a_restart(team_agent, signing_key):
    from shared_context.behavior_peer_runtime import _agent_bindings
    case = team_agent
    _register_scope(case)
    agent_id = case.target.agent_id
    binding_id = sorted(_agent_bindings(case.a, agent_id))[0]
    _retire(case)

    async def problem(request, error):
        return JSONResponse(error.body, status_code=error.status)

    # A fresh router is the restart: its binding map is empty, so only the registry scan can find the binding.
    app = Starlette(routes=[Route("/{path:path}", lambda request: JSONResponse({"agent_id": None}), methods=["GET"])],
                    exception_handlers={Problem: problem})
    install_peer_routing(app, case.a, lambda composition: Starlette(routes=[
        Route("/{path:path}", lambda request: JSONResponse({"agent_id": composition.agent_id}), methods=["GET"])]))
    human = case.c.h.headers(signing_key, "Behavior.Read Behavior.Approve")
    with TestClient(app, backend_options={"loop_factory": lambda: case.loop}) as client:
        retired = client.get("/behavior/v1/bindings/" + binding_id + "/status", headers=human)
        unknown = client.get("/behavior/v1/bindings/never-bound/status", headers=human)
    assert retired.status_code == 410 and retired.json()["error"]["code"] == "AGENT_RETIRED"
    assert unknown.status_code == 200 and unknown.json() == {"agent_id": None}

def test_the_teams_relay_answers_a_retired_agent_in_words(team_agent):
    from shared_context.native_teams_relay import TEXT, RelayDecision, RelayTurn, TeamsRelay
    case = team_agent
    _register_scope(case)
    _retire(case)
    relay_oid, owner = "33333333-3333-3333-3333-333333333333", case.config.identity.human_oid
    relay = TeamsRelay(case.a, {relay_oid: case.target.agent_id}, records=None)
    sent = relay.turn(relay_oid, RelayTurn(activity_id="act-1", chat_id="chat-1", user_oid=owner, text="hello"))
    decided = relay.decision(relay_oid, RelayDecision(activity_id="act-2", chat_id="chat-1", user_oid=owner,
                                                      choice="allow_once", code="123456"))
    assert sent == decided and (sent.state, sent.text) == ("refused", TEXT["retired"]) and sent.confirm is None
    assert case.c.sessions.calls == []


def test_the_keepalive_skips_a_retired_peer_and_says_so_once(caplog):
    from shared_context.behavior_session_keepalive import InstructionSessionKeepalive
    from shared_context.behavior_team_registry import agent_retired

    def peer_read(*args, **kwargs):
        raise agent_retired()  # what for_agent() raises for the retired peer inside the peer read

    composition = SimpleNamespace(config=object(), runtime=SimpleNamespace(
        mode="native", store=SimpleNamespace(consistent_read=peer_read)))
    keepalive = InstructionSessionKeepalive(composition, "binding-1")
    keepalive._tick = lambda: None
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            keepalive.tick()
    lines = [record.getMessage() for record in caplog.records if "HOSTED_SESSION_KEEPALIVE" in record.getMessage()]
    assert lines == ["HOSTED_SESSION_KEEPALIVE_PEER_RETIRED binding_id=binding-1 skipped"]