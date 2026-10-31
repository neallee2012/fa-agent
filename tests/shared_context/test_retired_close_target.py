"""Target selection and original-stop closure readers: what callers see without any such row.

No deployed producer ever wrote a ``standing-target`` or ``original_sdk_stop_closed`` row (``select_target`` and
``close_original`` were never called by product code, #1041/#1051). These tests pin what the kept readers return
when no selection row exists, and that registration receipts with and without the old target read are both
accepted. Offline: synthetic Foundry client, local Table I/O, no SDK or model.
"""
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_foundry import FoundryExecutor, FoundryHttpClient, NativeJobRoute, NativeLaunchAuthority
from shared_context.behavior_migration import MigrationStore
from shared_context.behavior_work_policy import work_policy_key
from shared_context.behavior_work_target import activate_target, selected_targets, target_key
from shared_context.behavior_workers import WorkerContract
from shared_context.errors import Problem
from history_forgery import Forgery
from test_behavior_store import FakeTable, SCOPE
from test_conversation_transfer_startup import synthetic_standing_v1
from test_fresh_installation import fresh_case, initialize
from test_instruction_flow import instruction_models
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


def _registration_read_set(c, change):
    """A forged copy of one registration receipt whose read set is change(read set); hashes stay coherent."""
    config = c.h.config
    binding, _ = c.control.submit(c.human, InstructionSubmit(request_id="retired-target-read",
                                                             content="Synthetic registration read set."))
    key = work_policy_key("registration", c.control.records[binding.binding_id].admission_id)

    def rewrite(payload):
        return {**payload, "request": {**payload["request"], "read_set": list(change(payload["request"]["read_set"]))}}
    inventory = MigrationStore(c.h.composition.clients[0], config.table.scope).read_inventory()
    forged = Forgery(inventory).receipt(key, rewrite).inventory()
    return binding, forged, forged.receipts[key].payload["request"]["read_set"]


def _claimed(c, request_id):
    """claimed_turn's path for a legacy AssistantSubmit: one claimed native launch of the C1 pool."""
    from dataclasses import replace
    from shared_context import behavior_wire as wire
    from shared_context.behavior_assistant_wire import AssistantSubmit
    from shared_context.behavior_auth import BehaviorPrincipal
    from assistant_sdk_fixture import review_source
    workers = c.h.runtime.dep.workers
    review_source(c.h.runtime.store, c.h.config, review_id="retired-close-reviewed",
                  claim_id=c.control.policy.profile.claim_id, text="The accepted metrics source has been reviewed.")
    binding, _ = c.control.submit(c.human, AssistantSubmit(request_id=request_id))
    reservations = []
    original = workers.adapter
    workers.adapter = replace(workers.adapter, start=lambda start: reservations.append(c.native.prepare(start)),
                              verify=c.native.verify)
    try:
        workers.start(binding.job_id, binding.owner)
    finally:
        workers.adapter = original
    launch, permit = reservations[0]
    actor = c.h.config.identity.auth().actors[binding.agent_oid]
    agent = BehaviorPrincipal(binding.tenant_id, binding.agent_oid, binding.team_id, "agent", actor.actor,
                              frozenset({"read", "consume", "execute"}))
    identity = wire.HostedIdentity(**{name: getattr(launch.target, name)
                                    for name in ("agent_id", "agent_name", "agent_version", "session_id")})
    c.native.claim(binding.binding_id, launch.launch_id, wire.HostedClaimRequest(
        request_id="retired-native-claim", invocation_id="retired-original-invocation",
        identity=identity, process_id="retired-no-process", launch_permit=permit), agent)
    return c.native.launch(launch.launch_id)


@pytest.fixture(params=["without-proof-row", "with-proof-row"])
def closed_launch(synthetic_standing_v1, request):
    """A claimed launch sealed on original-stop-close evidence, written the way the removed close_original sealed it.

    The saved evidence is otherwise fresh and coherent, so only the retired-closure rule can refuse it."""
    from datetime import timedelta
    from shared_context.behavior_delivery import _material
    from shared_context.behavior_foundry import _NativeEvidence, native_key
    from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
    from shared_context.behavior_workers import ExecutorEvidence
    c = synthetic_standing_v1
    native, store, now = c.native, c.h.runtime.store, c.h.time
    launch = _claimed(c, "retired-original-close")
    eid = native_key("original-stop-close-evidence", launch.launch_id)
    proof_key = native_key("original-stop-close", launch.launch_id)
    evidence = ExecutorEvidence(evidence_id=eid, binding=launch.start.binding, sequence=launch.evidence_sequence + 1,
        native_execution_id=launch.native_invocation_id, state="stopped", entire_tree_idle=True,
        all_inflight_starts_fenced=True, proof=b'{"original_stop_close":"retired"}')
    saved = _NativeEvidence(launch_id=launch.launch_id,
        configuration_digest=c.h.runtime.dep.workers.configuration_digest(), probe_id=proof_key, evidence=evidence,
        observed_at=now, expires_at=now + timedelta(seconds=60), report_digest="d" * 64)
    launchkey = native_key("launch", launch.launch_id)
    proof = (() if request.param == "without-proof-row" else
             (ControlWrite(row_id=proof_key, mode="create", data={"kind": "original_sdk_stop_closed"}),))
    store.transact(ControlRequest(request_id="retired-close-seal", request_digest="a" * 64, read_set=(launchkey,)),
                   lambda current: ControlChanges(writes=(
                       native._write(native_key("evidence", eid), saved, material=_material(saved.model_dump(mode="json"))),
                       native._write(launchkey, launch.model_copy(update={
                           "phase": "sealed", "sealed_evidence_id": eid, "evidence_sequence": evidence.sequence,
                           "pending_control": "cancel"}), current),
                       *proof,
                   ), result={}))
    return SimpleNamespace(native=native, eid=eid, binding=launch.start.binding)


def _denied(call, status, code, message):
    with pytest.raises(Problem) as denied:
        call()
    assert denied.value.status == status
    assert denied.value.body["error"]["code"] == code
    assert denied.value.body["error"]["message"] == message


def test_verify_refuses_a_launch_sealed_on_a_retired_original_stop_closure(closed_launch):
    h = closed_launch
    _denied(lambda: h.native.verify(h.eid), 403, "NATIVE_EVIDENCE_INVALID",
            "original stop closures are retired and cannot be verified")


def test_sealed_reconcile_refuses_a_retired_original_stop_closure(closed_launch):
    h = closed_launch
    _denied(lambda: h.native.reconcile_sealed(h.binding), 403, "NATIVE_TERMINAL_INVALID",
            "original stop closures are retired and cannot be reconciled")


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


def _peer_register(case, request_id):
    """The official peer path (for_agent, as agent messages, native turns and routines take it), then register."""
    import hashlib
    command = InstructionSubmit(request_id=request_id, content="Synthetic peer B work.")
    peer = case.a.for_agent(case.policy.agent_id)
    return peer.runtime.work_policy.register(case.c.human, command.request_id,
        hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)


def test_without_a_leftover_row_the_peer_builds_and_registers_as_before(unbuilt_peer, instruction_models):
    case = unbuilt_peer
    record = _peer_register(case, "b-retired-target-none")
    assert record.binding.agent_oid == case.policy.template.agent_oid
    assert case.policy.agent_id in case.a._peers


def test_a_peer_init_failure_logs_the_problem_code_but_not_its_message(unbuilt_peer, instruction_models, caplog):
    case = unbuilt_peer
    _late_row(case.c.h.runtime.store, case.key, {"kind": "standing_hosted_target"})
    _denied(lambda: _peer_register(case, "b-retired-target-log"), 503, "AGENT_RUNTIME_UNAVAILABLE",
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
        _denied(lambda: _peer_register(case, "b-retired-target-" + attempt), 503, "AGENT_RUNTIME_UNAVAILABLE",
                "the selected peer runtime could not be initialized")
        assert case.policy.agent_id not in case.a._peers
