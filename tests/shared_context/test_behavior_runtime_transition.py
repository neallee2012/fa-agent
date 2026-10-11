"""Operator transition seam; every provider/Table observation here is offline."""

import pytest
import copy
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from shared_context.behavior_runtime_transition import RuntimeTransition, _without_pin_migration
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_operations import control_key
from shared_context.source_materials import digest
from shared_context.errors import Problem
from test_behavior_migration import fixture
from test_behavior_server import NOW
from shared_context.behavior_server_config import ServerConfiguration, synthetic_configuration
from shared_context.behavior_runtime_transition import RuntimeRetirement, RetiredTarget, transition_key
from shared_context.behavior_migration import MigrationStore, installed_origin, code_sha256, installation_record
from shared_context.behavior_foundry import NativeExecutionGrant, _TargetReservation, native_key
from shared_context.behavior_assistant_wire import Task
from test_behavior_assistant_server import assistant_configuration


def test_pins_only_change_cannot_be_relabelled_as_a_runtime_transition():
    _, store, table, old, pinned, _ = fixture()
    writes = len(table.submissions)
    with pytest.raises(Problem) as denied:
        RuntimeTransition(store, old, pinned, {}, retirement=b"{}", clock=lambda: None)
    assert denied.value.body["error"]["code"] == "TRANSITION_CONFIG_CHANGE_DENIED"
    assert len(table.submissions) == writes


def test_previous_pin_history_is_verified_before_reconstructing_quiescent_business_inventory():
    migration, store, table, original, pinned, principal = fixture()
    plan = migration.plan(principal, migration_id="prior-pin-transition")
    approval = migration.authorize(principal, plan, decision_id="pin-approved",
                                   expires_at=NOW + timedelta(minutes=5))
    migration.apply(principal, plan, approval)
    inventory = store.read_inventory()
    writes = len(table.submissions)
    restored, pointer = _without_pin_migration(store, inventory, pinned)
    assert restored == original
    assert pointer.configuration_sha256 == pinned.sha256
    assert store.read_inventory().entity_sha256 == inventory.entity_sha256
    assert len(table.submissions) == writes


@pytest.mark.parametrize("mutation", ["unknown", "pending", "corrupt-history"])
def test_reconstructed_history_never_hides_current_unknown_or_pending_records(mutation):
    migration, store, table, _, pinned, principal = fixture()
    plan = migration.plan(principal, migration_id="preserve-before-runtime")
    approval = migration.authorize(principal, plan, decision_id="pin-before-runtime",
                                   expires_at=NOW + timedelta(minutes=5))
    migration.apply(principal, plan, approval)
    if mutation == "unknown":
        key, mode, data = "unclassified-work", "create", {"state": "unknown"}
    elif mutation == "pending":
        from shared_context.behavior_workers import worker_key
        key = worker_key("pool", pinned.workers.workspace_id)
        mode, data = "replace", {**store.read_snapshot((key,))[key].payload["data"], "pending": ["unfinished-work"]}
    else:
        from shared_context.behavior_migration import migration_key
        key = migration_key(pinned.table.scope.authority_domain, "history", plan.migration_id)
        mode, data = "replace", {**store.read_snapshot((key,))[key].payload["data"], "authorization_sha256": "f" * 64}
    store.transact(ControlRequest(request_id=control_key("test-transition-precondition", mutation),
                                  request_digest=digest(data), read_set=(key,) if mode == "replace" else ()),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode=mode, data=data),), result={}))
    inventory = store.read_inventory()
    before = len(table.submissions)
    with pytest.raises(Problem):
        _without_pin_migration(store, inventory, pinned)
    assert len(table.submissions) == before


def transition_fixture():
    template = assistant_configuration()
    baseline = synthetic_configuration().model_dump(mode="json")
    baseline["workers"]["capacity"] = 1
    baseline["identity"]["managed_identity_client_id"] = template["identity"]["managed_identity_client_id"]
    with patch("test_behavior_migration.synthetic_configuration",
               return_value=ServerConfiguration.model_validate_json(json.dumps(baseline))):
        previous, store, table, original, pinned, principal = fixture()
    target = _TargetReservation(target=original.targets[0], generation=7,
                                configuration_digest=digest({"contract": original.workers.model_dump(mode="json"),
                                                             "mode": "native"}))
    target_key = native_key("target", target.target.executor_id)
    store.transact(ControlRequest(request_id="seed-retired-target", request_digest=digest(target.model_dump(mode="json"))),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=target_key, mode="create",
                                                                 data=target.model_dump(mode="json")),), result={}))
    pin_plan = previous.plan(principal, migration_id="pin-before-sdk")
    approval = previous.authorize(principal, pin_plan, decision_id="pin-before-sdk-approved",
                                  expires_at=NOW + timedelta(minutes=5))
    previous.apply(principal, pin_plan, approval)
    raw = pinned.model_dump(mode="json")
    raw["deployment_id"] = "assistant-transition"
    raw["workload_version"] = "assistant-v1.2"
    raw["assistant"] = copy.deepcopy(template["assistant"])
    raw["assistant"]["m3_context"] = {
        "account_resource_id": pinned.table.account_resource_id, "endpoint": pinned.table.endpoint,
        "table_name": pinned.table.scope.table_name,
    }
    raw["assistant"]["artifact_container_url"] = pinned.sources.protection.container_url
    raw["assistant"]["provider_endpoint"] = pinned.targets[0].project_endpoint + "/openai/v1/chat/completions"
    for field in ("executor_version", "protection_version"):
        raw["workers"][field] = template["workers"][field]
    raw["delivery"]["protection_version"] = template["delivery"]["protection_version"]
    raw["delivery"]["verifier_version"] = "assistant-result-verifier-v1"
    raw["targets"][0].update(agent_version="99", session_id="assistant-new-owned-session")
    binding = copy.deepcopy(template["bindings"][0])
    for field in ("binding_id", "event_id", "job_id", "operation_id", "attempt_id", "grant_id"):
        binding[field] = "sdk-" + binding[field]
    for field in ("owner_record_id", "conversation_id", "policy_id"):
        binding["owner"][field] = "sdk-" + binding["owner"][field]
    binding["owner"]["root_budget_id"] = pinned.roots[0].root_budget_id
    binding["root_request_id"] = pinned.roots[0].root_request_id
    binding["work"]["work_id"] = "sdk-" + binding["work"]["work_id"]
    raw["bindings"] = [binding]
    profile = raw["assistant"]["profiles"][0]
    profile["binding_id"] = binding["binding_id"]
    policy = copy.deepcopy(template["policies"][0])
    policy["binding_id"] = binding["binding_id"]
    policy["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", binding])
    policy["policy"]["valid_until"] = (NOW + timedelta(hours=1)).isoformat()
    raw["policies"] = [policy]
    raw["operations"] = [{**template["operations"][0], "binding_id": binding["binding_id"]}]
    updated = ServerConfiguration.model_validate_json(json.dumps(raw))
    retired = RuntimeRetirement(
        operator_oid=principal.oid, authority_domain=pinned.table.scope.authority_domain,
        old_configuration_sha256=pinned.sha256, new_configuration_sha256=updated.sha256,
        old_code_sha256=pin_plan.code_sha256, controller_resource_id=pinned.controller_resource_id,
        api_resource_id=pinned.api_resource_id, no_active_controller_revision=True, no_running_controller_jobs=True,
        old_business_admission_disabled=True, compute_observation_sha256="e" * 64,
        targets=tuple(RetiredTarget(target=item, provider_session_status="idle", provider_observation_sha256="f" * 64)
                      for item in pinned.active_targets), observed_at=NOW, valid_until=NOW + timedelta(minutes=15),
    )
    b, p = updated.bindings[0], updated.assistant.profiles[0]
    task = Task(job_id=b.job_id, work_id=b.work.work_id, definition_digest=digest(b.work.model_dump(mode="json")),
                profile_digest=digest(p.model_dump(mode="json")), source=p.source, artifact_id=p.artifact_id,
                artifact_version=p.artifact_version, operation_id=b.operation_id)
    grant = NativeExecutionGrant(
        grant_id=native_key("bootstrap-grant", b.job_id), controller_id=updated.controller_id, owner=b.owner,
        policy_revision=1, job_id=b.job_id, definition=b.work, input_digest=hashlib.sha256(task.model_dump_json().encode()).hexdigest(),
        targets=updated.active_targets, active=True, remaining_uses=2, not_before=NOW, expires_at=NOW + timedelta(minutes=30),
        workload_version="assistant-v1.2",
    )
    transition = RuntimeTransition(store, pinned, updated,
                                   {lane: observation.model_dump_json().encode() for lane, observation in previous.observations.items()},
                                   retirement=retired.model_dump_json().encode(), native_grants=(grant,), clock=lambda: NOW)
    return transition, store, table, original, pinned, updated, principal


def test_drained_runtime_transition_is_one_atomic_write_and_preserves_old_budgets_and_history():
    transition, store, table, original, pinned, updated, principal = transition_fixture()
    before = store.read_inventory()
    plan = transition.plan(principal, transition_id="sdk-cutover")
    approval = transition.authorize(principal, plan, decision_id="sdk-cutover-approved",
                                    expires_at=NOW + timedelta(minutes=10))
    writes = len(table.submissions)
    transition.apply(principal, plan, approval)
    assert len(table.submissions) == writes + 1
    result = transition.verify(principal, plan, approval)
    assert result["counter_sha256"] == plan.counter_sha256
    assert result["activation_performed"] is False
    assert installed_origin(MigrationStore(table, store._scope), updated) == original
    after = store.read_inventory()
    for key, receipt in before.receipts.items():
        assert after.receipts[key] == receipt
    for key, row in before.rows.items():
        if row.payload["data"].get("kind") in {"operation_budget", "operation_owner", "operation_policy"}:
            assert after.rows[key] == row
    reservation = after.rows[native_key("target", updated.targets[0].executor_id)].payload["data"]
    assert reservation["generation"] == 7 and reservation["active_launch_id"] is None
    assert reservation["target"] == updated.targets[0].model_dump(mode="json")
    stale = MigrationStore(table, store._scope, expected_installation=(installation_record(pinned)[0], pinned.sha256))
    with pytest.raises(Problem):
        stale.transact(ControlRequest(request_id="stale-runtime-write", request_digest="a" * 64),
                       lambda _: ControlChanges(result={}))


def test_lost_transition_ack_uses_only_original_receipt_and_never_reapplies():
    transition, store, table, _, _, _, principal = transition_fixture()
    plan = transition.plan(principal, transition_id="sdk-lost-ack")
    approval = transition.authorize(principal, plan, decision_id="sdk-lost-ack-approved",
                                    expires_at=NOW + timedelta(minutes=10))
    table.failure = "after"
    with pytest.raises(Problem) as error:
        transition.apply(principal, plan, approval)
    assert error.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    table.failure = None
    writes = len(table.submissions)
    assert transition.reconcile(principal, plan, approval).payload["result"]["new_configuration_sha256"] == transition.new.sha256
    transition.verify(principal, plan, approval)
    assert len(table.submissions) == writes


@pytest.mark.parametrize("field", ["budget", "pending", "target"])
def test_current_changes_after_approval_never_partially_apply_a_transition(field):
    transition, store, table, _, _, _, principal = transition_fixture()
    plan = transition.plan(principal, transition_id="sdk-stale-" + field)
    approval = transition.authorize(principal, plan, decision_id="sdk-stale-approved-" + field,
                                    expires_at=NOW + timedelta(minutes=10))
    if field == "budget":
        key = control_key("budget", transition.old.roots[0].root_budget_id)
        data = store.read_snapshot((key,))[key].payload["data"]
        data = {**data, "remaining_units": data["remaining_units"] - 1}
    elif field == "pending":
        from shared_context.behavior_workers import worker_key
        key = worker_key("pool", transition.old.workers.workspace_id)
        data = {**store.read_snapshot((key,))[key].payload["data"], "pending": ["late-job"]}
    else:
        key = native_key("target", transition.old.targets[0].executor_id)
        data = {**store.read_snapshot((key,))[key].payload["data"], "active_launch_id": "late-launch"}
    store.transact(ControlRequest(request_id="legitimate-concurrent-" + field, request_digest=digest(data), read_set=(key,)),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="replace", data=data),), result={}))
    before = dict(store.read_inventory().entity_sha256)
    with pytest.raises(Problem):
        transition.apply(principal, plan, approval)
    assert dict(store.read_inventory().entity_sha256) == before
    assert store.read_snapshot((transition_key("history", plan.transition_id),))[transition_key("history", plan.transition_id)] is None


@pytest.mark.parametrize("field", ["roots", "workspace", "identity", "source-protection", "old-binding-id"])
def test_new_runtime_cannot_refill_budget_change_authority_or_reuse_old_identity(field):
    from shared_context.behavior_runtime_transition import _config_change
    transition, _, _, _, old, new, _ = transition_fixture()
    raw = new.model_dump(mode="json")
    if field == "roots":
        raw["roots"][0]["units"] += 1
    elif field == "workspace":
        raw["workers"]["workspace_id"] = "unrelated-pool"
    elif field == "identity":
        raw["identity"]["admin_client_id"] = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    elif field == "source-protection":
        raw["sources"]["protection"]["evidence_sha256"] = "f" * 64
    else:
        raw["bindings"][0]["operation_id"] = old.bindings[0].operation_id
        raw["policies"][0]["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", raw["bindings"][0]])
    changed = ServerConfiguration.model_validate_json(json.dumps(raw))
    with pytest.raises(Problem) as error:
        _config_change(old, changed)
    assert error.value.body["error"]["code"] == "TRANSITION_CONFIG_CHANGE_DENIED"


@pytest.mark.parametrize("kind", ["history", "authorization"])
def test_new_runtime_writer_cannot_modify_either_immutable_lineage(kind):
    transition, store, table, _, _, new, principal = transition_fixture()
    plan = transition.plan(principal, transition_id="sdk-protected-history")
    approval = transition.authorize(principal, plan, decision_id="sdk-protected-approved",
                                    expires_at=NOW + timedelta(minutes=10))
    transition.apply(principal, plan, approval)
    writer = MigrationStore(table, store._scope)
    installed_origin(writer, new)
    key = transition_key(kind, plan.transition_id if kind == "history" else approval.decision_id)
    original = writer.read_snapshot((key,))[key]
    with pytest.raises(Problem) as error:
        writer.transact(ControlRequest(request_id="mutate-new-" + kind, request_digest="b" * 64, read_set=(key,)),
                        lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="replace",
                                                                      data={**original.payload["data"], "tamper": True}),),
                                                result={}))
    assert error.value.body["error"]["code"] == "MIGRATION_OPERATOR_REQUIRED"
    assert writer.read_snapshot((key,))[key] == original


def captured_route_history(store, original, pinned, *, wrong_user=False):
    from shared_context.teams_personal import _RouteWitness
    from shared_context.teams_capture_server import _RouteBinding
    from shared_context.teams_identity import TeamsRouteRecord
    from shared_context.behavior_delivery import _material
    from shared_context.behavior_migration import migration_key
    current_key = migration_key(pinned.table.scope.authority_domain, "current")
    pointer = store.read_snapshot((current_key,))[current_key].payload["data"]
    personal_digest = "b" * 64
    route_key = control_key("teams-personal-route", personal_digest)
    binding_key = control_key("teams-capture-route-binding", personal_digest)
    route = TeamsRouteRecord(
        tenant_id=pinned.identity.tenant_id, bot_app_id=pinned.identity.managed_identity_client_id,
        bot_id="28:" + pinned.identity.managed_identity_client_id, user_id="29:synthetic-user",
        aad_object_id="wrong-user" if wrong_user else pinned.identity.human_oid,
        conversation_id="19:synthetic-personal-route", conversation_type="personal",
        service_url="https://smba.trafficmanager.net/amer/",
    )
    witness = _RouteWitness(configuration_digest=personal_digest, route=route, event_id="c" * 64,
                            body_digest="d" * 64, observed_at=NOW, evidence_mode="connector_http")
    witness_data = witness.model_dump(mode="json")
    binding = _RouteBinding(
        server_configuration_sha256=pinned.sha256, personal_configuration_sha256=personal_digest,
        original_configuration_sha256=original.sha256, control_binding=pinned.workers.control_binding,
        installation_row_id=installation_record(pinned)[0], migration_current_row_id=current_key,
        history_row_id=pointer["history_key"], history_sha256=pointer["history_sha256"],
        authorization_row_id=pointer["authorization_key"], route_row_id=route_key,
        route_material_sha256=_material(witness_data).sha256,
    )
    data = binding.model_dump(mode="json")
    request = ControlRequest(request_id=control_key("teams-personal-enroll", personal_digest),
                             request_digest=digest(witness_data), read_set=(route_key,))
    store.transact(request, lambda _: ControlChanges(writes=(
        ControlWrite(row_id=route_key, mode="create", data=witness_data, material=_material(witness_data)),
        ControlWrite(row_id=binding_key, mode="create", data=data, material=_material(data)),
    ), result={
        "status": "route_recorded_not_admitted", "route_id": route.route_id, "evidence_mode": "connector_http",
        "work_admitted": False, "native_gate": "closed", "actual_environment": "NOT_RUN",
    }))
    return route_key, binding_key


@pytest.mark.parametrize("wrong_user", [False, True])
def test_native_route_history_is_preserved_but_never_promoted_to_runtime_authorization(wrong_user):
    transition, store, _, original, pinned, _, principal = transition_fixture()
    keys = captured_route_history(store, original, pinned, wrong_user=wrong_user)
    before = store.read_snapshot(keys)
    if wrong_user:
        with pytest.raises(Problem):
            transition.plan(principal, transition_id="sdk-wrong-route")
        return
    plan = transition.plan(principal, transition_id="sdk-preserve-route")
    approval = transition.authorize(principal, plan, decision_id="sdk-preserve-route-approved",
                                    expires_at=NOW + timedelta(minutes=10))
    transition.apply(principal, plan, approval)
    transition.verify(principal, plan, approval)
    assert store.read_snapshot(keys) == before
