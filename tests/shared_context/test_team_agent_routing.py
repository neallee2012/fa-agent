"""CAP-06 PR3b: a registered Team Agent resolves, registers and routes like a configured peer."""

import json
from dataclasses import replace
from types import SimpleNamespace
import asyncio

from fastapi.testclient import TestClient
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from shared_context.behavior_agent_message_api import public_agents
from shared_context.behavior_operations import _write
from shared_context.behavior_peer_runtime import install_peer_routing
from shared_context.behavior_store import ControlChanges, ControlRequest
from shared_context.behavior_team_registry import (
    TeamAgent, TeamRegistry, agent_key, inventory_agent_scope, registered_agent_ids, resolve_agent_scope,
)
from shared_context.behavior_work_target import instruction_hosted_version
from shared_context.errors import Problem
from shared_context.source_materials import digest
import test_conversation_transfer_startup as startup
from test_agent_registry_operator import OperatorTableTransport
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from test_team_registry import AGENT_OID, OTHER_AGENT_OID, _target


@pytest.fixture
def team_agent(request, monkeypatch, signing_key):
    monkeypatch.setattr(startup, "StartupTableTransport", OperatorTableTransport)
    monkeypatch.delenv("FA_INSTRUCTION_HOSTED_VERSIONS", raising=False)
    loop = asyncio.new_event_loop()
    try:
        c = request.getfixturevalue("synthetic_standing_v1")
        a, config = c.h.composition, c.h.config
        ids = config.identity
        teams = TeamRegistry(a.runtime.store, tenant_id=ids.tenant_id, operator_oid=ids.human_oid,
                             legacy_team_id=ids.team_id, legacy_owner_oid=ids.human_oid,
                             agent_oids=tuple(agent.oid for agent in ids.agents), targets=config.targets)
        target = _target(1, AGENT_OID)
        teams.register_agent(c.human, "routing-agent-one", target.agent_id, target)
        # Offline transports are pinned per executor; production uses the real Foundry client.
        transports = next(cell.cell_contents for cell in a._peer_factory.__closure__
                          if isinstance(cell.cell_contents, dict) and config.targets[0].executor_id in cell.cell_contents)
        transports[target.executor_id] = transports[config.targets[0].executor_id]
        authorization = c.h.headers(signing_key, "Behavior.Read Behavior.Approve")["Authorization"]
        yield SimpleNamespace(c=c, a=a, config=config, teams=teams, target=target,
                              authorization=authorization, loop=loop)
    finally:
        if not loop.is_closed():
            loop.close()



def _register_scope(case):
    from shared_context.behavior_agent_registry_operator import operate
    return operate(case.a, "register", case.target.agent_id, case.authorization, request_id="routing-registry-v1")


def register_more_agents(case, count):
    """Register and activate ``count`` more Agents in the served Team; returns their agent IDs."""
    from shared_context.behavior_agent_registry_operator import operate
    transports = next(cell.cell_contents for cell in case.a._peer_factory.__closure__
                      if isinstance(cell.cell_contents, dict) and case.config.targets[0].executor_id in cell.cell_contents)
    added = []
    for n in range(2, 2 + count):
        target = _target(n, f"{n:08d}-abab-abab-abab-abababababab")
        case.teams.register_agent(case.c.human, f"routing-agent-{n}", target.agent_id, target)
        transports[target.executor_id] = transports[case.config.targets[0].executor_id]
        operate(case.a, "register", target.agent_id, case.authorization, request_id=f"routing-registry-{n}")
        added.append(target.agent_id)
    return added


def _entry(case, agent_id):
    return next(entry for entry in public_agents(case.a, case.c.human).agents if entry.agent_id == agent_id)


def test_seam_keeps_configured_agents_at_zero_reads_and_registered_agents_at_two(team_agent):
    case = team_agent
    store, config = case.a.runtime.store, case.config
    reads = case.c.h.table.requests
    before = len(reads)
    assert resolve_agent_scope(store, config, config.targets[1].agent_id) == config.agent_scope(config.targets[1].agent_id)
    assert len(reads) == before
    scope = resolve_agent_scope(store, config, case.target.agent_id)
    assert len(reads) == before + 2, "its target row, then its tombstone"
    assert (scope.target, scope.identity.oid, scope.identity.actor, scope.team_id, scope.legacy_keys) == (
        case.target, AGENT_OID, case.target.agent_id, config.identity.team_id, False)
    assert (scope.configuration_sha256, scope.authority_domain, scope.human_oid) == (
        config.sha256, config.table.scope.authority_domain, config.identity.human_oid)
    assert registered_agent_ids(store, config) == (case.target.agent_id,)
    assert inventory_agent_scope(store.read_inventory(), config, case.target.agent_id) == scope
    for unknown in ("unknown-agent", "bad/agent"):
        with pytest.raises(Problem) as denied:
            resolve_agent_scope(store, config, unknown)
        assert denied.value.status == 403 and denied.value.body["error"]["code"] == "AGENT_SCOPE_DENIED"


def test_registered_agent_completes_operator_registration_and_builds_its_own_runtime(team_agent):
    case = team_agent
    agent_id = case.target.agent_id
    pending = _entry(case, agent_id)
    assert (pending.state, pending.display_name, pending.reason) == (
        "unavailable", case.target.agent_name, "AGENT_SCOPE_MISSING")
    with pytest.raises(Problem) as missing:
        case.a.for_agent(agent_id)
    assert missing.value.body["error"]["code"] == "AGENT_SCOPE_MISSING"

    registered = _register_scope(case)
    assert registered["state"] == "registered" and registered["agent_id"] == agent_id
    assert registered["original_target_version"] == case.target.agent_version
    peer = case.a.for_agent(agent_id)
    assert peer is not case.a and peer.runtime.store is case.a.runtime.store
    assert peer.runtime.dep.native_launches.targets == (case.target,)
    assert peer.runtime.work_policy.policy.template.agent_oid == AGENT_OID
    assert peer.runtime.work_policy.cost_key == case.c.control.cost_key
    assert case.a.for_agent(agent_id) is peer
    ready = _entry(case, agent_id)
    assert (ready.state, ready.display_name, ready.api_prefix) == ("ready", case.target.agent_name, "/agents/" + agent_id)
    assert public_agents(case.a, case.c.human).default_agent_id == case.config.targets[0].agent_id
    assert case.c.sessions.calls == []


def test_registered_agent_version_is_the_registration_offline_and_ignores_the_environment(team_agent, monkeypatch):
    case = team_agent
    _register_scope(case)
    control = case.a.for_agent(case.target.agent_id).runtime.work_policy
    assert instruction_hosted_version(control, None, existing=False) == case.target.agent_version
    for raw in (json.dumps({case.target.agent_id: "5"}), json.dumps({case.target.agent_id: "2"}), "not-json"):
        monkeypatch.setenv("FA_INSTRUCTION_HOSTED_VERSIONS", raw)
        monkeypatch.setenv("FA_INSTRUCTION_HOSTED_VERSION", "9")
        assert instruction_hosted_version(control, None, existing=False) == case.target.agent_version


def _agent(key, oid):
    return {"Authorization": "Bearer " + token(key, {"oid": oid, "idtyp": "app"}, remove=("scp",))}


def test_registered_agent_token_routes_only_to_its_own_runtime(team_agent, signing_key):
    case = team_agent
    _register_scope(case)
    agent_id = case.target.agent_id
    peer = case.a.for_agent(agent_id)

    def factory(composition):
        async def where(request):
            return JSONResponse({"agent_id": composition.agent_id})
        return Starlette(routes=[Route("/{path:path}", where)])

    async def problem(request, error):
        return JSONResponse(error.body, status_code=error.status)

    app = Starlette(routes=[Route("/{path:path}", lambda request: JSONResponse({"agent_id": None}))],
                    exception_handlers={Problem: problem})
    install_peer_routing(app, case.a, factory)
    flat = f"/behavior/v1/bindings/{peer.runtime.work_policy.policy.template.binding_id}/probe"
    human = case.c.h.headers(signing_key, "Behavior.Read Behavior.Approve")
    with TestClient(app, backend_options={"loop_factory": lambda: case.loop}) as client:
        own = client.get("/agents/" + agent_id + "/probe", headers=_agent(signing_key, AGENT_OID))
        assert own.status_code == 200 and own.json() == {"agent_id": agent_id}
        assert client.get(flat, headers=_agent(signing_key, AGENT_OID)).json() == {"agent_id": agent_id}
        assert client.get("/agents/" + agent_id + "/probe", headers=human).json() == {"agent_id": agent_id}
        for path in ("/agents/" + case.config.targets[1].agent_id + "/probe",
                     "/agents/" + case.config.targets[0].agent_id + "/probe"):
            denied = client.get(path, headers=_agent(signing_key, AGENT_OID))
            assert denied.status_code == 403 and denied.json()["error"]["code"] == "BEHAVIOR_BINDING_DENIED"
        configured = client.get("/agents/" + agent_id + "/probe", headers={
            "Authorization": "Bearer " + token(signing_key, {
                "oid": case.config.targets[1].agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
            }, remove=("scp",))})
        assert configured.status_code == 403
        unknown = client.get("/agents/unregistered-agent/probe", headers=human)
        assert unknown.status_code == 403 and unknown.json()["error"]["code"] == "AGENT_SCOPE_DENIED"
        stranger = client.get("/agents/" + agent_id + "/probe", headers=_agent(signing_key, OTHER_AGENT_OID))
        assert stranger.status_code == 403


def test_registered_agent_mounts_the_real_peer_application(team_agent, signing_key):
    """Live 104e: the real peer app refused a registry Agent with 503 BEHAVIOR_CONFIGURATION."""
    from shared_context.behavior_api import behavior_router
    from shared_context.behavior_server import application
    from shared_context.auth import Actor
    case = team_agent
    _register_scope(case)
    peer = case.a.for_agent(case.target.agent_id)
    assert peer.registered_actor == Actor(AGENT_OID, case.target.agent_id, case.config.identity.team_id)
    # _PeerRouting.application_for builds exactly this mounted app for the selected peer.
    assert application(peer) is not None
    # The extra actor only admits its own verified peer; nothing else is widened.
    with pytest.raises(Problem) as missing:
        behavior_router(peer.runtime, peer.verifier)
    assert missing.value.body["error"]["code"] == "BEHAVIOR_CONFIGURATION"
    for wrong in (Actor(OTHER_AGENT_OID, case.target.agent_id, case.config.identity.team_id),
                  Actor(AGENT_OID, case.target.agent_id, "another-team")):
        with pytest.raises(Problem) as refused:
            behavior_router(peer.runtime, peer.verifier, registered_actor=wrong)
        assert refused.value.body["error"]["code"] == "BEHAVIOR_CONFIGURATION"
    configured = case.config.identity.agents[1]
    with pytest.raises(Problem) as reused:
        behavior_router(peer.runtime, peer.verifier, registered_actor=Actor(
            configured.oid, configured.actor, case.config.identity.team_id))
    assert reused.value.body["error"]["code"] == "BEHAVIOR_CONFIGURATION"
    assert case.a.registered_actor is None
    assert case.c.sessions.calls == []


def test_registration_outside_the_served_team_never_resolves(team_agent):
    case = team_agent
    store, config, ids = case.a.runtime.store, case.config, case.config.identity
    stranger = _target(2, OTHER_AGENT_OID)
    key = agent_key(ids.tenant_id, stranger.agent_id)
    current = store.read_record(agent_key(ids.tenant_id, case.target.agent_id)).payload["data"]
    data = {**current, "agent_id": stranger.agent_id, "team_id": "team-two",
            "target": stranger.model_dump(mode="json"), "request_id": "foreign-team-agent"}
    store.transact(ControlRequest(request_id="test-foreign-team-agent", request_digest=digest(data), read_set=(key,)),
                   lambda rows: ControlChanges(writes=(_write(key, TeamAgent.model_validate(data), "create"),),
                                               result={}))
    with pytest.raises(Problem) as denied:
        resolve_agent_scope(store, config, stranger.agent_id)
    assert denied.value.status == 403 and denied.value.body["error"]["code"] == "AGENT_SCOPE_DENIED"
    with pytest.raises(Problem):
        case.a.for_agent(stranger.agent_id)


def test_registered_agent_scope_passes_the_real_startup_readers_and_a_missing_target_is_refused(team_agent):
    from shared_context.behavior_agent_registry import validate_agent_registry_history
    from shared_context.behavior_migration import MigrationStore, _quiescence, installed_origin
    from test_resident_worker_poc import outcome_of
    case = team_agent
    config = case.config

    def readers():
        store = MigrationStore(case.c.h.composition.clients[0], config.table.scope)
        return (outcome_of(lambda: read_startup(case.c)), outcome_of(lambda: _quiescence(store.read_inventory(), config)),
                outcome_of(lambda: installed_origin(store, config)))
    baseline = readers()
    assert baseline[0] == "ok"
    _register_scope(case)
    case.a.for_agent(case.target.agent_id)
    assert readers() == baseline
    inventory = case.a.runtime.store.read_inventory()
    assert inventory_agent_scope(inventory, config, case.target.agent_id).target == case.target
    akey = agent_key(config.identity.tenant_id, case.target.agent_id)
    missing = replace(inventory, rows={key: value for key, value in inventory.rows.items() if key != akey})
    with pytest.raises(Problem) as refused:
        validate_agent_registry_history(missing, config)
    assert refused.value.status == 409


def test_a_binding_path_finds_its_peer_without_rescanning_the_team(team_agent, signing_key, monkeypatch):
    case = team_agent
    _register_scope(case)
    agent_id = case.target.agent_id
    binding = case.a.for_agent(agent_id).runtime.work_policy.policy.template.binding_id

    def factory(composition):
        async def where(request):
            return JSONResponse({"agent_id": composition.agent_id})
        return Starlette(routes=[Route("/{path:path}", where)])

    async def problem(request, error):
        return JSONResponse(error.body, status_code=error.status)

    app = Starlette(routes=[Route("/{path:path}", lambda request: JSONResponse({"agent_id": None}))],
                    exception_handlers={Problem: problem})
    install_peer_routing(app, case.a, factory)
    # The middleware (and its router) is built on the first request.
    store, calls = case.a.runtime.store, []
    for name in ("read_record", "read_snapshot"):
        original = getattr(store, name)

        def counted(*args, _original=original, _name=name, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(store, name, counted)
    human = case.c.h.headers(signing_key, "Behavior.Read Behavior.Approve")
    flat = f"/behavior/v1/bindings/{binding}/probe"

    with TestClient(app, backend_options={"loop_factory": lambda: case.loop}) as client:
        def reads(path):
            del calls[:]
            response = client.get(path, headers=human)
            return response, len(calls)

        first, _ = reads(flat)
        router = case.a._peer_router
        assert first.json() == {"agent_id": agent_id} and router.binding_agents == {binding: agent_id}
        added = register_more_agents(case, 5)
        last = case.a.for_agent(added[-1]).runtime.work_policy.policy.template.binding_id
        assert reads(f"/behavior/v1/bindings/{last}/probe")[0].json() == {"agent_id": added[-1]}
        _, front = reads(flat)
        routed, back = reads(f"/behavior/v1/bindings/{last}/probe")
        assert routed.json() == {"agent_id": added[-1]}
        assert back == front, "the last of 7 Agents is routed with the same reads as the first; no Team scan"
        assert router.binding_agents == {binding: agent_id, last: added[-1]}

        # A binding no peer of this Team holds still falls to A's own routes, which refuse it; nothing is cached.
        unknown = client.get("/behavior/v1/bindings/not-a-team-binding/probe", headers=human)
        assert unknown.json() == {"agent_id": None} and set(router.binding_agents) == {binding, last}

        # A map entry the peer does not confirm is refused and dropped; the next request scans again.
        router.binding_agents[binding] = added[0]
        stale = client.get(flat, headers=human)
        assert stale.status_code == 403 and stale.json()["error"]["code"] == "BEHAVIOR_BINDING_DENIED"
        assert binding not in router.binding_agents
        assert client.get(flat, headers=human).json() == {"agent_id": agent_id}
        assert router.binding_agents[binding] == agent_id


def test_the_memory_relay_derives_the_private_scope_hosted_uses(team_agent, monkeypatch):
    # ADR-0011 §4: the API's scope_for(target.agent_name) equals Hosted's scope_for(FOUNDRY_AGENT_NAME).
    from shared_context import memory_relay
    from shared_context.behavior_auth import BehaviorPrincipal
    from shared_context.behavior_private_memory import scope_for
    monkeypatch.setattr(memory_relay, "_AGENT_NAMES", {})
    case = team_agent
    store, config, ids = case.a.runtime.store, case.config, case.config.identity
    relay = memory_relay.MemoryRelay(None, targets=config.targets, environ={},
                                     resolve_agent=lambda agent_id: resolve_agent_scope(store, config, agent_id))
    actions = frozenset({"read", "submit", "execute", "consume"})
    configured = config.targets[1]
    actor = next(agent.actor for agent in ids.agents if agent.oid == configured.agent_oid)
    cases = ((BehaviorPrincipal(str(ids.tenant_id), configured.agent_oid, ids.team_id, "agent", actor, actions),
              configured.agent_name),
             (BehaviorPrincipal(str(ids.tenant_id), AGENT_OID, ids.team_id, "agent", case.target.agent_id, actions),
              case.target.agent_name))
    for principal, foundry_agent_name in cases:
        assert relay.agent_name(principal) == foundry_agent_name
        assert relay.allowed(principal, scope_for(foundry_agent_name)) == scope_for(foundry_agent_name)
    assert scope_for(configured.agent_name) != scope_for(case.target.agent_name)
