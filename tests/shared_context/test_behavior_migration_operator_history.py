"""Operator history is preserved, not silently ignored or promoted to current stop authority."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from shared_context.behavior_operations import control_key
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import worker_key
from shared_context import behavior_workers as workers, behavior_foundry as foundry
from shared_context.behavior_migration import _native_post_request
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_migration import fixture, terminal_root_and_child
from test_behavior_server import NOW


def stop_history(store, config, *, wrong_session=False, mutate=None, missing_observation=False, target=None):
    target = target or config.targets[0]
    session = "unregistered-session" if wrong_session else target.session_id
    marker = control_key("c1-session-stop-attempt", session)
    observation = control_key("c1-session-stop-observation", session)
    before = {"session_id": session, "agent_name": target.agent_name,
              "state_before": "idle", "stop_only_no_delete": True}
    value = {
        "agent": target.agent_name, "session": session, "before": "idle", "after": "idle",
        "metadata": {"agent_session_id": session, "version_indicator": {
            "type": "version_ref", "agent_version": target.agent_version},
            "status": "idle", "created_at": 1, "last_accessed_at": 2, "expires_at": 3},
        "stop_only_no_delete": True, "provider_stop_response": {
            "session_id": session, "error_code": "session_already_stopped",
            "request_id": "original-provider-request", "source_log_sha256": "a" * 64, "stop_resent": False},
        "no_cpu_state_claim_from_idle_metadata": True,
    }
    if mutate is not None:
        mutate(value)
    entries = ((marker, before, digest(target.model_dump(mode="json"))),)
    if not missing_observation:
        entries += ((observation, value, digest(value)),)
    for key, body, request_digest in entries:
        store.transact(ControlRequest(request_id=key, request_digest=request_digest),
                       lambda _, k=key, b=body: ControlChanges(
                           writes=(ControlWrite(row_id=k, mode="create", data=b),), result={"recorded": True}))
    return marker, observation


def test_exact_operator_stop_history_is_preserved_by_migration():
    service, store, _, old, _, principal = fixture()
    keys = stop_history(store, old)
    before = store.read_snapshot(keys)
    plan = service.plan(principal, migration_id="with-stop-history")
    approved = service.authorize(principal, plan, decision_id="operator", expires_at=NOW + timedelta(minutes=5))
    service.apply(principal, plan, approved)
    service.verify_applied(principal, plan, approved)
    assert store.read_snapshot(keys) == before


def test_operator_history_for_unregistered_session_does_not_bypass_inventory():
    service, store, _, old, _, principal = fixture()
    stop_history(store, old, wrong_session=True)
    with pytest.raises(Problem):
        service.plan(principal, migration_id="wrong-session")


@pytest.mark.parametrize("mutate", [
    lambda value: value["metadata"]["version_indicator"].update(agent_version="unregistered-version"),
    lambda value: value["metadata"].update(agent_session_id="unregistered-session"),
    lambda value: value["provider_stop_response"].update(stop_resent=True),
    lambda value: value["provider_stop_response"].update(error_code="unknown"),
    lambda value: value.update(no_cpu_state_claim_from_idle_metadata=False),
    lambda value: value.update(unrecognized_extra="not an allowed audit"),
])
def test_changed_operator_history_is_rejected(mutate):
    service, store, _, old, _, principal = fixture()
    stop_history(store, old, mutate=mutate)
    with pytest.raises(Problem):
        service.plan(principal, migration_id="changed-history")


def test_stop_attempt_without_observation_remains_unresolved():
    service, store, _, old, _, principal = fixture()
    stop_history(store, old, missing_observation=True)
    with pytest.raises(Problem):
        service.plan(principal, migration_id="unobserved-stop")


@pytest.mark.parametrize("status", [200, 202, 204, 409])
@pytest.mark.parametrize("request_id", ["provider-request", None])
def test_direct_provider_response_is_retained_as_history_only(status, request_id):
    service, store, _, old, _, principal = fixture()
    response = {"status": status, "request_id": request_id}
    if status == 409:
        response["error_code"] = "session_already_stopped"
    stop_history(store, old, mutate=lambda value: value.update(provider_stop_response=response))
    assert service.plan(principal, migration_id="direct-response")


def test_exact_final_closeout_is_preserved():
    service, store, _, old, _, principal = fixture()
    observations = [stop_history(store, old, target=target)[1] for target in old.targets]
    budget_key = control_key("budget", old.roots[0].root_budget_id)
    rows = store.read_snapshot((*observations, budget_key))
    data = {
        "kind": "c1_final_service_closeout", "observed_at": NOW.isoformat(),
        "configuration_sha256": old.sha256, "jobs": [], "budget": rows[budget_key].payload["data"],
        "pool_empty": True, "delivery_index_empty": True, "root5_work_absent": True,
        "sessions": [rows[key].payload["data"] for key in observations], "data_or_session_deletes": 0,
    }
    key = control_key("c1-final-service-closeout", "r1")
    store.transact(ControlRequest(request_id=key, request_digest=digest(data)),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="create", data=data),), result={}))
    before = store.read_snapshot((key,))
    plan = service.plan(principal, migration_id="final-closeout")
    approval = service.authorize(principal, plan, decision_id="final", expires_at=NOW + timedelta(minutes=5))
    service.apply(principal, plan, approval)
    service.verify_applied(principal, plan, approval)
    assert store.read_snapshot((key,)) == before


def test_historical_idle_does_not_bypass_current_pending_pool():
    service, store, _, old, _, principal = fixture()
    stop_history(store, old)
    key = worker_key("pool", old.workers.workspace_id)
    data = {**store.read_snapshot((key,))[key].payload["data"], "pending": [old.bindings[0].job_id]}
    store.transact(ControlRequest(request_id="pending-after-history", request_digest=digest(data), read_set=(key,)),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="replace", data=data),), result={}))
    with pytest.raises(Problem) as raised:
        service.plan(principal, migration_id="history-not-current-proof")
    assert raised.value.body["error"]["code"] == "MIGRATION_NOT_QUIESCENT"


def bootstrap_history(store, config, *, name="bootstrap-worker", changed_config=False, extra=False):
    data = {
        "kind": "c1_native_bootstrap", "at": NOW.isoformat(), "configuration_sha256": config.sha256,
        "result": {"stage": "worker", "configuration_sha256": "a" * 64 if changed_config else config.sha256,
                   "own_authority_probe": "VERIFIED", "prerequisite_verification": "INCOMPLETE",
                   "actual_environment": "NOT_RUN", "full_scenario_pass": False},
    }
    if extra:
        data["result"]["unrecognized_authority_bypass"] = True
    key = control_key("c1-operator-proof", name)
    request = ControlRequest(request_id=control_key("c1-operator-proof-write", name), request_digest=digest(data))
    store.transact(request, lambda _: ControlChanges(
        writes=(ControlWrite(row_id=key, mode="create", data=data),), result={}))
    return key


def test_exact_bootstrap_audit_is_preserved_not_reexecuted():
    service, store, _, old, _, principal = fixture()
    key = bootstrap_history(store, old)
    before = store.read_snapshot((key,))
    plan = service.plan(principal, migration_id="bootstrap-history")
    approved = service.authorize(principal, plan, decision_id="bootstrap-audit", expires_at=NOW + timedelta(minutes=5))
    service.apply(principal, plan, approved)
    service.verify_applied(principal, plan, approved)
    assert store.read_snapshot((key,)) == before


@pytest.mark.parametrize("kwargs", [
    {"name": "bootstrap-activate"}, {"name": "unclassified-operator-proof"},
    {"changed_config": True}, {"extra": True},
])
def test_operator_proof_must_match_exact_schema_key_and_config(kwargs):
    service, store, _, old, _, principal = fixture()
    bootstrap_history(store, old, **kwargs)
    with pytest.raises(Problem):
        service.plan(principal, migration_id="bad-operator-proof")


def test_unresolved_operator_progress_does_not_disappear_during_migration():
    service, store, _, old, _, principal = fixture()
    key = control_key("c1-operator-attempt", "progress")
    data = {"action": "lifecycle", "configuration_sha256": old.sha256, "consumed": True}
    store.transact(ControlRequest(request_id=key, request_digest=digest([old.sha256, "lifecycle", "progress"])),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="create", data=data),), result={}))
    with pytest.raises(Problem):
        service.plan(principal, migration_id="unresolved-progress")


@pytest.mark.parametrize("wrong_intent", [False, True])
def test_worker_admission_retains_exact_terminal_job_and_enqueue_request(wrong_intent):
    service, store, _, old, _, principal = fixture(with_child=True)
    terminal_root_and_child(store, old)
    binding = old.bindings[0]
    job_key = worker_key("job", binding.job_id)
    job = store.read_snapshot((job_key,))[job_key].payload["data"]
    intent = control_key("delivery-intent-id", binding.event_id)
    reads = tuple(sorted({
        worker_key("pool", old.workers.workspace_id), job_key,
        control_key("owner", binding.owner.owner_record_id), control_key("policy", binding.owner.policy_id),
        control_key("delivery-obligation", binding.obligation_id), control_key("delivery-intent", intent),
    }))
    request = ControlRequest(
        request_id=worker_key("enqueue", binding.job_id),
        request_digest=digest(["enqueue", [job["spec"], job["owner"]],
                               digest({"contract": old.workers.model_dump(mode="json"), "mode": "native"})]),
        read_set=reads,
    )
    key = worker_key("admission", binding.obligation_id)
    data = {"job_id": binding.job_id, "intent_id": "wrong-intent" if wrong_intent else intent,
            "enqueue_request": request.model_dump(mode="json")}
    store.transact(request, lambda _: ControlChanges(
        writes=(ControlWrite(row_id=key, mode="create", data=data),), result={}))
    if wrong_intent:
        with pytest.raises(Problem):
            service.plan(principal, migration_id="wrong-admission")
    else:
        plan = service.plan(principal, migration_id="closed-admission")
        approved = service.authorize(principal, plan, decision_id="admission", expires_at=NOW + timedelta(minutes=5))
        service.apply(principal, plan, approved)
        service.verify_applied(principal, plan, approved)


@pytest.mark.parametrize("capacity", [1, 4])
def test_native_post_request_matches_active_capacity_not_full_identity_catalog(capacity):
    _, store, _, config, _, _ = fixture(with_child=True)
    terminal_root_and_child(store, config)
    config = config.model_copy(update={"workers": config.workers.model_copy(update={"capacity": capacity})})
    key = worker_key("job", config.bindings[0].job_id)
    job = workers.JobRecord.model_validate_json(json.dumps(store.read_snapshot((key,))[key].payload["data"]))
    start = workers.ExecutorStart(
        binding=job.binding, input=job.spec.input,
        request=ControlRequest(request_id=job.binding.start_request_id, request_digest=digest(job.spec.model_dump(mode="json"))),
    )
    launch = SimpleNamespace(launch_id="original-launch", grant_id="original-grant", start=start)
    request = _native_post_request(config, launch)
    target_keys = {foundry.native_key("target", target.executor_id) for target in config.targets}
    assert set(request.read_set) & target_keys == {
        foundry.native_key("target", target.executor_id) for target in config.targets[:capacity]
    }
    assert len(request.read_set) == 8 + capacity
