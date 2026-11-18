"""Target selection readers: what callers see without any selection row.

No deployed producer ever wrote a ``standing-target`` or ``original_sdk_stop_closed`` row (``select_target`` and
``close_original`` were never called by product code, #1041/#1051), and ``platform-stop`` rows came only from the
removed operator recovery (``ClaimedSdkRecovery``, no entry point in the tree). These tests pin what the kept readers return
when no selection row exists. Offline: synthetic Foundry client, local Table I/O, no SDK or model.
"""
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_foundry import FoundryExecutor, FoundryHttpClient, NativeJobRoute, NativeLaunchAuthority
from shared_context.behavior_work_target import activate_target, selected_targets, target_key
from shared_context.behavior_workers import WorkerContract
from shared_context.errors import Problem
from test_behavior_store import FakeTable, SCOPE
from test_conversation_transfer_startup import synthetic_standing_v1
from test_fresh_installation import fresh_case, initialize
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key


def test_without_a_selection_row_the_first_agent_keeps_its_configured_targets(synthetic_standing_v1):
    c = synthetic_standing_v1
    store, config = c.h.runtime.store, c.h.config
    assert store.read_snapshot((target_key(config.table.scope.authority_domain),)) == {
        target_key(config.table.scope.authority_domain): None}
    assert selected_targets(store, config) == config.active_targets
    assert selected_targets(store, config, agent_id=config.targets[0].agent_id) == config.active_targets


def test_without_a_selection_row_activation_changes_nothing(synthetic_standing_v1):
    c = synthetic_standing_v1
    native = c.h.runtime.dep.native_launches
    targets = native.targets
    clients = {client.target.executor_id: client.target for client in c.h.composition.clients
               if isinstance(client, FoundryHttpClient)}
    activate_target(c.h.composition)
    assert native.targets is targets and native.targets == c.h.config.active_targets
    assert {client.target.executor_id: client.target for client in c.h.composition.clients
            if isinstance(client, FoundryHttpClient)} == clients


def test_activation_moves_a_drifted_native_target_back_to_the_configured_one():
    from shared_context.behavior_foundry import FoundryTarget
    from shared_context.behavior_store import BehaviorStore
    configured = FoundryTarget(executor_id="executor-one",
        project_endpoint="https://offline.services.ai.azure.com/api/projects/p",
        agent_id="11111111-1111-1111-1111-111111111111", agent_name="offline-agent",
        agent_version="29", session_id="offline-configured-session",
        agent_oid="22222222-2222-2222-2222-222222222222")
    drifted = configured.model_copy(update={"agent_version": "30", "session_id": "offline-drifted-session"})
    store = BehaviorStore(FakeTable(), SCOPE)
    store.initialize()
    workers = WorkerContract(workspace_id="offline-c1", control_binding=store._binding,
        executor_id="executor-one", executor_version="v1", protection_version="inline-no-gc-v1", capacity=1)
    config = SimpleNamespace(active_targets=(configured,), targets=(configured,), table=SimpleNamespace(scope=SCOPE))
    worker = SimpleNamespace(contract=workers, store=store)
    native = NativeLaunchAuthority(worker, team_id="team-one", controller_id="offline-controller",
        targets=(drifted,), jobs=(NativeJobRoute(job_id="offline-job", api_binding_id="offline-binding",
                                                grant_id="offline-grant"),), verifier_version="v1")

    def denied(request):
        pytest.fail("activation must not call Foundry")
    client = FoundryHttpClient(drifted, credential=object(), transport=httpx.MockTransport(denied))
    try:
        worker.adapter = FoundryExecutor(native, (client,)).adapter()
        policy = SimpleNamespace(agent_id=None, authority_domain=SCOPE.authority_domain)
        runtime = SimpleNamespace(store=store, work_policy=SimpleNamespace(policy=policy),
                                  dep=SimpleNamespace(native_launches=native, workers=worker))
        activate_target(SimpleNamespace(config=config, runtime=runtime))
        assert native.targets == (configured,) and client.target == configured
        assert native.configured_targets == (drifted,)
    finally:
        client.close()


def _denied(call, status, code, message):
    with pytest.raises(Problem) as denied:
        call()
    assert denied.value.status == status
    assert denied.value.body["error"]["code"] == code
    assert denied.value.body["error"]["message"] == message


def _late_row(store, row_id, data):
    from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
    store.transact(ControlRequest(request_id=("late-" + row_id)[:64], request_digest="a" * 64),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=row_id, mode="create", data=data),), result={}))


def test_fresh_startup_refuses_a_leftover_target_selection_row(fresh_case):
    """Stage's fresh kind reads no whole partition; activate_target's point read stops the API before it serves."""
    from shared_context.behavior_server import application
    h = fresh_case
    initialize(h)
    application(h.composition)  # without the row the same startup succeeds
    _late_row(h.runtime.store, target_key(h.config.table.scope.authority_domain), {"kind": "standing_hosted_target"})
    _denied(lambda: application(h.composition), 502, "DELIVERY_RECORD_INVALID",
            "a retired standing target selection row is stored")


@pytest.fixture
def unbuilt_peer(synthetic_standing_v1, monkeypatch):
    """peer_runtime's registered peer B, before anything has built it (the owner's peer cache is empty)."""
    import json
    from shared_context.behavior_agent_registry import register_agent_scope
    from test_agent_scope_registry import peer_policy
    from assistant_sdk_fixture import review_source
    from hosted_versions_stub import deployed_versions
    c = synthetic_standing_v1
    a = c.h.composition
    policy = peer_policy(c.h.config, c.control.policy)
    original = c.h.config.targets[0]
    deployed_versions(monkeypatch, {original.agent_id: original.agent_version, policy.agent_id: "47"})
    register_agent_scope(a.runtime.store, a.config, policy, c.human, now=c.h.time)
    actor = c.h.config.identity.auth().actors[policy.template.agent_oid]
    c.h.table.backing.apply("Behavior", [("create", {
        "PartitionKey": actor.team, "RowKey": "work:" + policy.profile.legacy_source_work,
        "Payload": json.dumps({"work_id": policy.profile.legacy_source_work, "team": actor.team,
                              "owner": actor.actor, "status": "published", "revision": 1}).encode(),
    })])
    review_source(a.runtime.store, SimpleNamespace(identity=a.config.identity,
        bindings=(policy.template,), policies=(SimpleNamespace(policy=policy.legacy_template_policy),),
        assistant=SimpleNamespace(profiles=(policy.profile,)), review_policy_version=a.config.review_policy_version),
        review_id="b-original-source-review")
    assert policy.agent_id not in a._peers
    return SimpleNamespace(c=c, a=a, policy=policy, key=target_key(policy.authority_domain, policy.agent_id))


def _peer(case):
    """The official peer path (for_agent), as agent messages, native turns and routines take it."""
    return case.a.for_agent(case.policy.agent_id)


def test_without_a_leftover_row_the_peer_builds_as_before(unbuilt_peer):
    case = unbuilt_peer
    peer = _peer(case)
    assert peer.runtime.work_policy.policy.template.agent_oid == case.policy.template.agent_oid
    assert case.policy.agent_id in case.a._peers


def test_a_peer_init_failure_logs_the_problem_code_but_not_its_message(unbuilt_peer, instruction_models, caplog):
    case = unbuilt_peer
    _late_row(case.c.h.runtime.store, case.key, {"kind": "standing_hosted_target"})
    _denied(lambda: _peer(case), 503, "AGENT_RUNTIME_UNAVAILABLE",
            "the selected peer runtime could not be initialized")
    assert "exception_type=Problem code=DELIVERY_RECORD_INVALID" in caplog.text
    assert "a retired standing target selection row is stored" not in caplog.text


def test_a_peer_init_failure_that_is_not_a_problem_logs_no_code_from_it(unbuilt_peer, instruction_models, caplog):
    case = unbuilt_peer
    def broken(_policy):
        raise RuntimeError("SYNTHETIC_PRIVATE_CANARY")
    case.a._peer_factory = broken
    _denied(lambda: case.a.for_agent(case.policy.agent_id), 503, "AGENT_RUNTIME_UNAVAILABLE",
            "the selected peer runtime could not be initialized")
    assert "exception_type=RuntimeError code=UNCLASSIFIED" in caplog.text
    assert "SYNTHETIC_PRIVATE_CANARY" not in caplog.text


def test_a_leftover_peer_selection_row_leaves_the_peer_unusable(unbuilt_peer, instruction_models):
    """A root policy has no agent_id, so only a registered peer has the second key form (register once read it).

    Peers are built by for_agent (not by the startup installation), so build_peer refuses the row; for_agent maps
    the factory failure to 503 and caches nothing, so every later request for this peer fails the same way."""
    case = unbuilt_peer
    assert case.key != target_key(case.policy.authority_domain)
    _late_row(case.c.h.runtime.store, case.key, {"kind": "standing_hosted_target"})
    _denied(lambda: case.a._peer_factory(case.policy), 502, "DELIVERY_RECORD_INVALID",
            "a retired standing target selection row is stored")
    for attempt in ("first", "second"):
        _denied(lambda: _peer(case), 503, "AGENT_RUNTIME_UNAVAILABLE",
                "the selected peer runtime could not be initialized")
        assert case.policy.agent_id not in case.a._peers
