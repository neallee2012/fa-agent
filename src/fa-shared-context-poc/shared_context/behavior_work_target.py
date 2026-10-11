"""One explicit successor Hosted target for standing C1 work; old configuration stays immutable."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from .behavior_auth import BehaviorPrincipal
from .behavior_delivery import _Model, _material, _parse, _update
from .behavior_foundry import FoundryTarget, _TargetReservation, native_key
from .behavior_migration import code_sha256
from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest, ControlWrite, Digest
from .behavior_workers import JobRecord, _Pool, worker_key
from .errors import Problem
from .models import Identifier
from .source_materials import digest


def target_key(domain, agent_id=None):
    return control_key("standing-target", domain, *((agent_id,) if agent_id is not None else ()))


def require(condition, message):
    if not condition:
        raise Problem(409, "STANDING_TARGET_DENIED", message)


class TargetSelection(_Model):
    configuration_sha256: Digest
    code_sha256: Digest
    previous: FoundryTarget
    target: FoundryTarget
    image_sha256: Digest
    credential_client_id: Identifier
    session_owner_oid: Identifier
    approval_reference: Identifier


class StandingTarget(_Model):
    kind: Literal["standing_hosted_target"] = "standing_hosted_target"
    command: TargetSelection
    request: ControlRequest
    previous_reservation: _TargetReservation
    original_pool: _Pool
    original_admissions: Annotated[tuple[Identifier, ...], Field(max_length=32)]
    selected_by: Identifier
    selected_at: datetime


def _check(command, config):
    original, = config.active_targets
    fixed = ("executor_id", "project_endpoint", "agent_id", "agent_name", "agent_oid")
    require(command.configuration_sha256 == config.sha256 and command.previous == original
            and all(getattr(command.target, name) == getattr(original, name) for name in fixed)
            and int(command.target.agent_version) > int(original.agent_version)
            and command.target.session_id != original.session_id
            and command.credential_client_id == config.identity.managed_identity_client_id,
            "only a new version/session of the same original agent/API identity is supported")


def selection_record(store, config):
    key = target_key(config.table.scope.authority_domain)
    row = store.read_snapshot((key,))[key]
    if row is None:
        return None
    value = _parse(row.payload["data"], StandingTarget)
    _check(value.command, config)
    require(value.request.request_id == key
            and value.request.request_digest == digest(value.command.model_dump(mode="json"))
            and row.payload["material"] == _material(value.model_dump(mode="json")).model_dump(mode="json"),
            "stored original target selection differs")
    receipt = store.reconcile(value.request)
    import hashlib, json
    writes = dict(receipt.payload["writes"])
    require(set(writes) == {key, native_key("target", value.command.target.executor_id)}
            and writes[key] == hashlib.sha256(json.dumps(row.payload, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "target selection receipt does not bind stored bytes")
    return value


def selected_targets(store, config, *, agent_id=None):
    if agent_id is not None and agent_id != config.targets[0].agent_id:
        from .behavior_agent_registry import read_agent_registry
        return (read_agent_registry(store, config, agent_id).scopes[1].target,)
    record = selection_record(store, config)
    return config.active_targets if record is None else (record.command.target,)


def instruction_hosted_version(control, conversation, *, existing):
    """A new conversation uses the endpoint's compatible served version; an existing one retains its capture.

    Version selection is not read from environment variables or the order of active versions.
    """
    from .behavior_agent_versions import current_hosted_version
    if control.policy.agent_scope is not None:
        from .behavior_agent_registry import registered_agent_scope
        target = registered_agent_scope(control.store, control.policy).target
    else:
        native = control.runtime.dep.native_launches
        target = None if native is None else native.targets[0]
    if existing:
        return conversation.hosted_agent_version
    return current_hosted_version(control.runtime, target)


def select_target(composition, command, principal, *, reconcile=False):
    from .behavior_work_policy import work_policy_key
    from .behavior_work_registry import read_registry, transact
    command = TargetSelection.model_validate(command)
    config, runtime = composition.config, composition.runtime
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
            or principal.oid != config.identity.human_oid or principal.tenant_id != config.identity.tenant_id):
        raise Problem(403, "STANDING_TARGET_OPERATOR_DENIED", "original configured human is required")
    principal.require(config.identity.team_id, "read" if reconcile else "approve")
    _check(command, config)
    if not reconcile:
        require(command.code_sha256 == code_sha256(), "loaded target selection code differs")
    composition.installation()
    require(runtime.work_policy is not None and runtime.mode == "native" and config.workers.capacity == 1,
            "original standing native C1 mode is required")
    store = runtime.store
    key = target_key(config.table.scope.authority_domain)
    retained = selection_record(store, config)
    if retained is not None:
        require(retained.command == command, "one original selection ID cannot bind a different target")
        return store.reconcile(retained.request)
    if reconcile:
        raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "original target selection is not confirmed", request_id=key)
    registrykey = work_policy_key("registry", config.table.scope.authority_domain)
    control = runtime.work_policy
    registry = read_registry(store, control.policy)
    jobkeys = tuple(worker_key("job", record.binding.job_id) for record in control.records.values())
    poolkey = worker_key("pool", config.workers.workspace_id)
    nativekey = native_key("target", command.previous.executor_id)
    reads = (key, nativekey, poolkey, registrykey, *jobkeys)
    request = ControlRequest(request_id=key, request_digest=digest(command.model_dump(mode="json")), read_set=reads)
    def decide(rows):
        target = _parse(rows[nativekey].payload["data"], _TargetReservation)
        pool = _parse(rows[poolkey].payload["data"], _Pool)
        require(rows[key] is None and target.target == command.previous and target.active_launch_id is None
                and not pool.pending and not pool.completions and len(pool.slots) == 1
                and pool.slots[0].binding is None and pool.slots[0].lease_until is None
                and read_registry(store, control.policy, rows) == registry,
                "occupied, queued or changed original C1 cannot switch target")
        for name in jobkeys:
            require(rows[name] is not None, "registered original work must be settled before target switch")
            job = _parse(rows[name].payload["data"], JobRecord)
            require(job.state in {"succeeded", "failed", "cancelled"} and job.result_forwarded
                    and job.completion_credit == 0, "original execution/completion still owns capacity")
        value = StandingTarget(command=command, request=request, previous_reservation=target,
            original_pool=pool, original_admissions=registry.entries,
            selected_by=principal.oid, selected_at=runtime._now())
        return ControlChanges(writes=(
            ControlWrite(row_id=key, mode="create", data=value.model_dump(mode="json"),
                         material=_material(value.model_dump(mode="json"))),
            ControlWrite(row_id=nativekey, mode="replace",
                         data=_update(target, target=command.target).model_dump(mode="json")),
        ), result={"target": command.target.model_dump(mode="json"), "image_sha256": command.image_sha256,
                   "old_results_preserved": True, "budget_reset": False})
    return transact(store, control.policy, request, decide)


def validate_target_history(inventory, config):
    key = target_key(config.table.scope.authority_domain)
    if key not in inventory.rows:
        return None
    from .behavior_recovery_history import _Inventory
    from .behavior_work_policy import work_policy_key
    from .behavior_work_registry import read_inventory_registry
    view = _Inventory(inventory, config)
    value = view.row(key, StandingTarget, material=True)
    _check(value.command, config)
    targetkey = native_key("target", value.command.target.executor_id)
    current = view.row(targetkey, _TargetReservation)
    old, pool = value.previous_reservation, value.original_pool
    require(value.selected_by == config.identity.human_oid and old.target == value.command.previous
            and old.active_launch_id is None and len(pool.slots) == 1
            and pool.slots[0].binding is None and pool.slots[0].lease_until is None
            and not pool.pending and not pool.completions
            and current.target == value.command.target and current.generation >= old.generation,
            "stored target switch is not an empty original C1")
    from .behavior_work_policy import StandingPolicyInstallation
    policy = view.row(work_policy_key("policy", config.table.scope.authority_domain), StandingPolicyInstallation).policy
    registry = read_inventory_registry(inventory, policy)
    require(registry.entries[:len(value.original_admissions)] == value.original_admissions,
            "target switch cannot replace old work registry")
    require(value.request.request_id == key and value.request.request_digest == digest(value.command.model_dump(mode="json")),
            "original target selection request differs")
    view.receipt(value.request, (key, targetkey), exact={
        key: view.hash(key), targetkey: view.image_hash(targetkey, _update(old, target=value.command.target)),
    }, result={"target": value.command.target.model_dump(mode="json"), "image_sha256": value.command.image_sha256,
               "old_results_preserved": True, "budget_reset": False})
    return value


def activate_target(composition):
    runtime = composition.runtime
    if runtime.work_policy is None:
        return
    targets = selected_targets(runtime.store, composition.config, agent_id=runtime.work_policy.policy.agent_id)
    native = runtime.dep.native_launches
    if native.targets == targets:
        return
    from .behavior_foundry import FoundryExecutor
    executor = getattr(runtime.dep.workers.adapter.start, "__self__", None)
    require(isinstance(executor, FoundryExecutor) and executor.authority is native
            and set(executor.clients) == {target.executor_id for target in targets},
            "existing exact Foundry executor clients are required")
    # Identity/origin/executor are unchanged; reuse the original SDK HTTP client and credentials.
    for target in targets:
        executor.clients[target.executor_id].target = target
    native.targets = targets
