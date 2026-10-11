"""Minimal one-target continuation with real guarded Table transactions, offline only."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import copy
import json

import pytest

from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_foundry import FoundryTarget, _TargetReservation, native_key
from shared_context.behavior_workers import WorkerContract, JobRecord, JobSpec, _Pool, Slot, worker_key
from shared_context.behavior_operations import ExecutionContext
from shared_context.behavior_store import BehaviorStore, ControlRequest, ControlChanges, ControlWrite
from shared_context.behavior_work_policy import WorkRegistry, work_policy_key
from shared_context.source_materials import digest
from shared_context.errors import Problem
from test_behavior_store import FakeTable, SCOPE


@pytest.mark.parametrize("paged", [False, True])
def test_next_standing_target_keeps_configuration_budget_and_old_results(monkeypatch, paged):
    from shared_context.behavior_work_target import TargetSelection, select_target, target_key, selected_targets
    from shared_context.behavior_migration import code_sha256
    store = BehaviorStore(FakeTable(), SCOPE)
    store.initialize()
    old = FoundryTarget(executor_id="executor-one",
        project_endpoint="https://offline.services.ai.azure.com/api/projects/p",
        agent_id="11111111-1111-1111-1111-111111111111", agent_name="offline-agent",
        agent_version="29", session_id="offline-old-session",
        agent_oid="22222222-2222-2222-2222-222222222222")
    new = old.model_copy(update={"agent_version": "30", "session_id": "offline-new-session"})
    workers = WorkerContract(workspace_id="offline-c1", control_binding=store._binding,
        executor_id="executor-one", executor_version="v1", protection_version="inline-no-gc-v1", capacity=1)
    policy = SimpleNamespace(authority_domain=SCOPE.authority_domain, scope_ids=(SCOPE.authority_domain,),
        policy_id="OFFLINE-policy", sha256="a"*64, control_binding=store._binding, agent_id=None)
    registry_key = work_policy_key("registry", SCOPE.authority_domain)
    reservation = _TargetReservation(target=old, generation=1, active_launch_id=None, configuration_digest="d" * 64)
    pool = _Pool(workspace_id=workers.workspace_id, configuration_digest="d" * 64,
                 slots=(Slot(number=0),), pending=(), completions=())
    keys = (native_key("target", old.executor_id), worker_key("pool", workers.workspace_id), registry_key)
    records = (reservation, pool, WorkRegistry(policy_id="OFFLINE-policy",
                                              entries=() if paged else ("offline-prior-admission",)))
    store.transact(ControlRequest(request_id="OFFLINE-seed", request_digest="a" * 64),
        lambda _: ControlChanges(writes=tuple(ControlWrite(row_id=k, mode="create", data=v.model_dump(mode="json"))
                                               for k, v in zip(keys, records)), result={}))
    if paged:
        from shared_context.behavior_work_registry import WRITER_ENV, append_writes, transact
        monkeypatch.setenv(WRITER_ENV, "true")
        request=ControlRequest(request_id="OFFLINE-paged-index",request_digest="c"*64,read_set=(registry_key,))
        transact(store,policy,request,lambda rows:ControlChanges(
            writes=append_writes(policy,rows,"offline-prior-admission"),result={}),append=True)
    cfg = SimpleNamespace(sha256="b" * 64, active_targets=(old,), workers=workers,
        table=SimpleNamespace(scope=SCOPE),
        identity=SimpleNamespace(tenant_id="offline-tenant", human_oid="offline-human", team_id="team-one",
                                 managed_identity_client_id="33333333-3333-3333-3333-333333333333"))
    owner = ExecutionContext(owner_record_id="offline-owner", conversation_id="offline-conversation",
        epoch=1, principal_id="offline-agent", policy_id="offline-policy", root_budget_id="offline-root")
    job = JobRecord(configuration_digest="d" * 64,
        spec=JobSpec(job_id="offline-prior-job", work_id="offline-prior-work", input=b"offline"),
        owner=owner, state="cancelled", cancel_requested=True, cancel_credit=0, stop_credit=0,
        completion_credit=0, control_remaining=0, result_forwarded=True)
    store.transact(ControlRequest(request_id="OFFLINE-old-job", request_digest="f" * 64),
        lambda _: ControlChanges(writes=(ControlWrite(row_id=worker_key("job", job.spec.job_id),
            mode="create", data=job.model_dump(mode="json")),), result={}))
    # No receipt/ACK field is a target-switch prerequisite.
    control = SimpleNamespace(policy=policy, registry_key=registry_key, records={
        "offline-prior-binding": SimpleNamespace(binding=SimpleNamespace(job_id=job.spec.job_id))})
    runtime = SimpleNamespace(store=store, work_policy=control, mode="native",
        _now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc), dep=SimpleNamespace())
    composition = SimpleNamespace(config=cfg, runtime=runtime, installation=lambda: None)
    principal = BehaviorPrincipal(cfg.identity.tenant_id, cfg.identity.human_oid, cfg.identity.team_id,
                                  "human", None, frozenset({"read", "approve"}))
    command = TargetSelection(configuration_sha256=cfg.sha256, code_sha256=code_sha256(),
        previous=old, target=new, image_sha256="e" * 64,
        credential_client_id=cfg.identity.managed_identity_client_id,
        session_owner_oid="44444444-4444-4444-4444-444444444444",
        approval_reference="OFFLINE-explicit-new-target")
    before = copy.deepcopy(store._client.backing.rows)
    for invalid in (
        command.model_copy(update={"target": new.model_copy(update={"project_endpoint": "https://elsewhere.invalid"})}),
        command.model_copy(update={"target": new.model_copy(update={"session_id": old.session_id})}),
        command.model_copy(update={"credential_client_id": "different-api-identity"}),
    ):
        with pytest.raises(Problem):
            select_target(composition, invalid, principal)
        assert store._client.backing.rows == before
    # Pending work prevents switching even if no process currently occupies the slot.
    rowkey = ("Behavior", store._scope.partition_key, "behavior:v1:row:" + keys[1])
    original_pool_bytes = store._client.backing.rows[rowkey]["Payload"]
    blocked = json.loads(original_pool_bytes)
    blocked["data"]["pending"] = ["offline-queued-work"]
    store._client.backing.rows[rowkey]["Payload"] = json.dumps(blocked).encode()
    with pytest.raises(Problem, match="occupied, queued"):
        select_target(composition, command, principal)
    store._client.backing.rows[rowkey]["Payload"] = original_pool_bytes
    assert store._client.backing.rows == before
    from test_behavior_store import conflict
    store._client.before_submit = lambda: (_ for _ in ()).throw(conflict(412))
    with pytest.raises(Problem) as changed:
        select_target(composition, command, principal)
    assert changed.value.body["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert store._client.backing.rows == before
    store._client.failure = "after"
    with pytest.raises(Problem) as lost:
        select_target(composition, command, principal)
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    count = len(store._client.submissions)
    record = select_target(composition, command, principal, reconcile=True)
    assert len(store._client.submissions) == count
    store._client.failure = None
    assert record.payload["result"]["target"] == new.model_dump(mode="json")
    assert selected_targets(store, cfg) == (new,)
    after = copy.deepcopy(store._client.backing.rows)
    assert select_target(composition, command, principal) == record
    assert store._client.backing.rows == after
    from shared_context.behavior_foundry import NativeLaunchAuthority, NativeJobRoute, FoundryExecutor, FoundryHttpClient
    from shared_context.behavior_work_target import activate_target
    import httpx
    worker = SimpleNamespace(contract=workers, store=store)
    native = NativeLaunchAuthority(worker, team_id=cfg.identity.team_id, controller_id="offline-controller",
        targets=(old,), jobs=(NativeJobRoute(job_id=job.spec.job_id, api_binding_id="offline-binding",
                                            grant_id="offline-grant"),), verifier_version="v1")
    def denied(request):
        pytest.fail("activation must not call Foundry")
    client = FoundryHttpClient(old, credential=object(), transport=httpx.MockTransport(denied))
    try:
        executor = FoundryExecutor(native, (client,))
        worker.adapter = executor.adapter()
        runtime.dep.native_launches, runtime.dep.workers = native, worker
        activate_target(composition)
        assert native.targets == (new,) and client.target == new
        assert native.configured_targets == (old,)
        assert cfg.active_targets == (old,)
        assert store._client.backing.rows == after
    finally:
        client.close()
    old_reservation = store.read_snapshot((keys[0],))[keys[0]].payload["data"]
    assert old_reservation["generation"] == 1 and old_reservation["active_launch_id"] is None
    assert cfg.active_targets == (old,) and cfg.sha256 == "b" * 64
    for key, value in before.items():
        if key[-1] != "behavior:v1:guard" and key[-1] != "behavior:v1:row:" + keys[0]:
            assert after[key] == value
    with pytest.raises(Problem):
        select_target(composition, command.model_copy(update={"target": old}), principal)
    assert store._client.backing.rows == after
