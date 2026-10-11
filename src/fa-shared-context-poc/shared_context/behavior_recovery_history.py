"""Read-only, receipt-bound claimed recovery and never-claimed cancellation.

This is not a recovery executor, installation verifier or quiescence decision.
Only the exact supplied assistant configuration/current target generation is
supported; no old-configuration projection or assistant upgrade is inferred.
Historical code/snapshot pins are retained, not replaced with today's values.
Unknown rows outside this history remain the inventory caller's responsibility.
"""
from __future__ import annotations

import hashlib
import json
import weakref
from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING

from . import behavior_delivery as delivery, behavior_foundry as native, behavior_workers as workers
from .behavior_assistant import _ModelIntent, _Session, key as assistant_key
from .behavior_assistant_wire import Task
from .behavior_migration import DEFAULT_INVENTORY_MAX_BYTES, DEFAULT_INVENTORY_MAX_ENTITIES, _parsed, _problem
from .behavior_operations import BudgetRecord, OwnerRecord, PolicyRecord, control_key
from .behavior_recovery import (
    PlatformStopObservation, PreSdkRecoveryPlan, PreAssignmentRecoveryPlan, RecoveryAuthorization, _PlatformProof, _StopIntent,
    _cancelled_result, _unassigned_claim, recovery_key, stop_endpoint,
    MultiModelRecoveryPlan, _model_digests, _multi_models, _multi_inventory,
    FinishedResultRecoveryPlan, _finished_proof,
    TeamsPreAcquireRecoveryPlan, TeamsPreAcquireRecoveryAuthorization, _authorization_model, _proof_model, _fence_stage,
    ExpiredTeamsCleanupPlan,
)
from .behavior_runtime import RuntimePolicy, runtime_key
from .behavior_server_config import ServerConfiguration
from .behavior_store import ControlRequest, _Guard, _Receipt, _Row, GUARD_KEY
from .source_materials import digest

if TYPE_CHECKING:
    from .behavior_migration import AuthorityInventory

RecoveryHistoryRow = RecoveryAuthorization | _StopIntent | PlatformStopObservation | _PlatformProof | _Session | _ModelIntent


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _require(condition, detail):
    if not condition:
        raise _problem("MIGRATION_RECOVERY_HISTORY_INVALID", detail)


_RECEIPT_OWNERS = {}


def _checked_receipts(inventory, binding, revision):
    # Each startup validator builds its own view (often of a replace()d copy sharing the same receipts);
    # parsing and hashing every receipt again per view multiplied startup time and memory.
    source = inventory.receipts
    owner = _RECEIPT_OWNERS.get(id(source))
    owner = None if owner is None else owner()
    cached = None if owner is None else owner.__dict__.get("_checked_receipts")
    if (cached is not None and cached[0] == (binding, revision) and cached[1] is source
            and cached[2] is inventory.entity_sha256):
        return cached[3], cached[4]
    receipts, coverage = {}, set()
    for key, record in source.items():
        receipt = _parsed(record.payload, _Receipt)
        _require(receipt.request.request_id == key and receipt.binding == binding
                 and receipt.guard_revision <= revision
                 and len(dict(receipt.writes)) == len(receipt.writes)
                 and inventory.entity_sha256.get("behavior:v1:receipt:" + key) == _hash(record.payload),
                 "recovery inventory receipt identity, hash or guard differs")
        receipts[key] = receipt
        coverage.update(receipt.writes)
    receipts, coverage = MappingProxyType(receipts), frozenset(coverage)
    if isinstance(source, MappingProxyType):
        # The checked copy lives exactly as long as the inventory that owns it.
        key = id(source)
        object.__setattr__(inventory, "_checked_receipts",
                           ((binding, revision), source, inventory.entity_sha256, receipts, coverage))

        def forget(ref):
            if _RECEIPT_OWNERS.get(key) is ref:
                del _RECEIPT_OWNERS[key]
        _RECEIPT_OWNERS[key] = weakref.ref(inventory, forget)
    return receipts, coverage


class _Inventory:
    def __init__(self, inventory, config):
        self.inventory, self.config = inventory, config
        self.guard = _parsed(inventory.guard.payload, _Guard)
        _require(inventory.scope == config.table.scope
                 and self.guard.binding == config.workers.control_binding == config.delivery.control_binding
                 and inventory.installation_configuration_sha256 in {None, config.sha256},
                 "recovery history requires the exact supplied authority/configuration")
        self.checked_receipts = set()
        self.receipts, self.coverage = _checked_receipts(inventory, self.guard.binding, self.guard.revision)

    def row(self, key, model, *, material=None):
        record = self.inventory.rows.get(key)
        _require(record is not None, "required recovery history row is missing: " + key)
        envelope = _parsed(record.payload, _Row)
        fingerprint = _hash(record.payload)
        _require(envelope.row_id == key and envelope.binding == self.guard.binding
                 and self.inventory.entity_sha256.get("behavior:v1:row:" + key) == fingerprint
                 and (key, fingerprint) in self.coverage,
                 "recovery history row lacks its exact bound write receipt")
        value = _parsed(envelope.data, model)
        if material is not None:
            expected = value.model_dump(mode="json") if material is True else material
            _require(envelope.material == delivery._material(expected), "recovery history material differs")
        return value

    def image_hash(self, key, value):
        envelope = _parsed(self.inventory.rows[key].payload, _Row)
        return _hash(envelope.model_copy(update={"data": value.model_dump(mode="json")}).model_dump(mode="json"))

    def receipt(self, request, write_keys, *, exact=None, result=None):
        receipt = self.receipts.get(request.request_id)
        _require(receipt is not None and receipt.request == request
                 and set(dict(receipt.writes)) == set(write_keys),
                 "original recovery request/read-set/write-set receipt differs")
        for key, fingerprint in (exact or {}).items():
            _require(dict(receipt.writes)[key] == fingerprint, "recovery receipt write image differs")
        if result is not None:
            _require(receipt.result == result, "original recovery receipt result differs")
        self.checked_receipts.add(request.request_id)
        return receipt

    def hash(self, key):
        return _hash(self.inventory.rows[key].payload)


def _keys(launch, config, model_digest, *, before_assignment=False, instruction=False):
    binding, owner = launch.start.binding, launch.start.binding.owner
    sid = assistant_key("session-id", binding.start_request_id)
    native_keys = (
        native.native_key("launch", launch.launch_id), native.native_key("grant", launch.grant_id),
        control_key("owner", owner.owner_record_id), control_key("policy", owner.policy_id),
        control_key("budget", owner.root_budget_id), workers.worker_key("job", binding.job_id),
        native.native_key("target", launch.target.executor_id, *((binding.job_id,) if instruction else ())),
    )
    recovery_keys = tuple(dict.fromkeys((
        *native_keys, workers.worker_key("pool", config.workers.workspace_id),
        assistant_key("session", sid),
        *(assistant_key("model-intent", sid, value) for value in (
            model_digest if isinstance(model_digest, tuple) else () if model_digest is None else (model_digest,))),
        assistant_key("artifact", binding.work_id), assistant_key("result", binding.work_id),
        *((assistant_key("archive", sid), assistant_key("restore", sid)) if before_assignment else ()),
    )))
    return native_keys, recovery_keys


def _check_unclaimed_fence_limit(fences):
    # Routine pre-SDK refusals must not consume the legacy manual-cancellation allowance.
    _require(sum(fence.failure_code is None for fence in fences) <= 32,
             "manual unclaimed cancellation history exceeds its bound")


def validate_unclaimed_history(
    inventory, config, *, complete=False, max_entities=DEFAULT_INVENTORY_MAX_ENTITIES, bindings=None,
):
    """Verify the original claim-gate fence; never infer a provider/CPU stop.

    The existing cancellation, fence, observe and forwarding producers are
    unchanged. Each committed intermediate stage remains readable, but only
    complete forwarding qualifies for retirement; delivery is checked there.
    Standing bindings must first pass the complete standing-policy history check.
    """
    from .behavior_migration import _UnclaimedFence, _NativePostIntent, _native_post_request

    fences = {key: row for key, row in inventory.rows.items()
              if row.payload["data"].get("kind") == "terminal_unclaimed_launch_fence"}
    launches = (_parsed(row.payload["data"], native._NativeLaunch) for row in inventory.rows.values()
                if row.payload["data"].get("kind") == "native_launch")
    sealed = {launch.launch_id for launch in launches
              if launch.sealed_evidence_id == native.native_key("unclaimed-fence-evidence", launch.launch_id)}
    if not fences and not sealed:
        return MappingProxyType({})
    _require(max_entities is None or type(max_entities) is int and max_entities > 0,"invalid unclaimed entity bound")
    _require(config.workload_version == "assistant-v1.2"
             and (max_entities is None or len(inventory.rows) + len(inventory.receipts) <= max_entities),
             "unclaimed assistant history exceeds its exact profile/bound")
    _require(sum(len(json.dumps(row.payload, ensure_ascii=False, separators=(",", ":")).encode())
                 for row in (*inventory.rows.values(), *inventory.receipts.values())) <= DEFAULT_INVENTORY_MAX_BYTES,
             "unclaimed history exceeds inventory bytes")
    view = _Inventory(inventory, config)
    parsed_fences = {key: view.row(key, _UnclaimedFence, material=True) for key in fences}
    _check_unclaimed_fence_limit(parsed_fences.values())
    classified, seen = {}, set()
    for fence_key, fence in parsed_fences.items():
        failed = fence.failure_code is not None
        launch_key = native.native_key("launch", fence.launch_id)
        launch = view.row(launch_key, native._NativeLaunch)
        binding, owner = launch.start.binding, launch.start.binding.owner
        configured = next((entry for entry in (config.bindings if bindings is None else bindings)
                           if entry.job_id == binding.job_id), None)
        record = None
        if configured is not None and configured not in config.bindings:
            from .behavior_work_policy import WorkRegistration
            registered = [view.row(key, WorkRegistration, material=True) for key, row in inventory.rows.items()
                          if row.payload["data"].get("kind") == "steady_work_registration"
                          and row.payload["data"].get("binding", {}).get("binding_id") == configured.binding_id]
            _require(len(registered) == 1 and registered[0].binding == configured,
                     "unclaimed standing work lacks its exact registered binding")
            record = registered[0]
        instruction = record is not None and record.profile.instruction is not None
        peer_work = instruction and record.profile.instruction.peer_work is not None
        grant_key = native.native_key("grant", launch.grant_id)
        grant = view.row(grant_key, native.NativeExecutionGrant)
        targets = config.active_targets if record is None else grant.targets
        _require(configured is not None and launch.launch_id not in seen
                 and fence_key == native.native_key("unclaimed-fence", launch.launch_id)
                 and launch.launch_id == native.native_key("launch-id", binding.start_request_id)
                 and fence.original_start_request_id == binding.start_request_id
                 and launch.api_binding_id == configured.binding_id and launch.definition == configured.work
                 and binding.owner == configured.owner and binding.work_id == configured.work.work_id
                 and launch.target in targets and launch.workload_version == "assistant-v1.2",
                 "unclaimed fence is outside the original exact binding")
        seen.add(launch.launch_id)
        sid = assistant_key("session-id", binding.start_request_id)
        _require(all(assistant_key(kind, sid) not in inventory.rows for kind in ("session", "archive", "restore"))
                 and all(assistant_key(kind, binding.work_id) not in inventory.rows for kind in ("artifact", "result"))
                 and assistant_key("issue", sid) not in inventory.receipts
                 and assistant_key("acquire", sid, "sdk-start") not in inventory.receipts,
                 "unclaimed fence cannot hide assignment/acquire/result material")
        for row_id, row in inventory.rows.items():
            request_hash = row.payload["data"].get("request_sha256")
            _require(not isinstance(request_hash, str) or row_id != assistant_key("model-intent", sid, request_hash),
                     "unclaimed fence cannot hide a model intent")
        _require(not any("claim_id" in receipt.result and launch_key in dict(receipt.writes)
                         for receipt in view.receipts.values()), "a claim receipt prevents unclaimed retirement")
        worker_digest = digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
        job_key = workers.worker_key("job", binding.job_id)
        pool_key = workers.worker_key("pool", config.workers.workspace_id, *((binding.job_id,) if instruction else ()))
        job, pool = view.row(job_key, workers.JobRecord), view.row(pool_key, workers._Pool)
        attempt_key = workers.worker_key("attempt", binding.job_id, str(binding.generation))
        attempt = view.row(attempt_key, workers._Attempt, material=launch.start.model_dump(mode="json"))
        target_key = native.native_key("target", launch.target.executor_id, *((binding.job_id,) if instruction else ()))
        target = view.row(target_key, native._TargetReservation)
        native_keys, _ = _keys(launch, config, None, instruction=instruction)
        owner_keys = (control_key("owner", owner.owner_record_id), control_key("policy", owner.policy_id))
        current_owner = view.row(owner_keys[0], OwnerRecord)
        _require(launch.start == attempt.start and job.binding == binding and job.owner == owner
                 and job.spec.job_id == binding.job_id and job.spec.work_id == binding.work_id
                 and job.spec.obligation_id == (None if peer_work else configured.obligation_id)
                 and job.spec.input == launch.start.input
                 and job.generation == binding.generation and job.spec.parent_job_id is None and not job.children
                 and hashlib.sha256(job.spec.input).hexdigest() == binding.input_digest
                 and (failed or job.cancel_requested) and job.cancel_credit == (0 if job.cancel_requested else 1)
                 and target.target == launch.target and target.generation == launch.process_generation
                 and target.job_id == pool.job_id == (binding.job_id if instruction else None)
                 and target.active_launch_id is None and current_owner.active
                 and all(getattr(current_owner, name) == value for name, value in owner.model_dump().items())
                 and grant.controller_id == config.controller_id and grant.grant_id == launch.grant_id
                 and grant.owner == owner and grant.job_id == binding.job_id and grant.definition == launch.definition
                 and grant.input_digest == binding.input_digest and grant.workload_version == launch.workload_version
                 and launch.target in grant.targets
                 and all(value.configuration_digest == worker_digest for value in (launch, target, job, pool)),
                 "original unclaimed job/attempt/grant/target or owner differs")
        policy_model = RuntimePolicy
        start_extra_reads, start_extra_writes = (), ()
        if record is None:
            policy = next(item.policy for item in config.policies if item.binding_id == configured.binding_id)
        else:
            from .behavior_work_policy import WorkPolicyControl, PerWorkRuntimePolicy, WorkDispatchPermission, work_policy_key
            control = object.__new__(WorkPolicyControl)
            control.policy = SimpleNamespace(authority_domain=config.table.scope.authority_domain)
            control.records = {configured.binding_id: record}
            control.runtime = SimpleNamespace(mode="native")
            dispatch_key = work_policy_key("dispatch", record.admission_id)
            permission = view.row(dispatch_key, WorkDispatchPermission, material=True)
            _require(permission.start == launch.start and permission.admission_id == record.admission_id,
                     "unclaimed dispatch differs from its registered work")
            policy, policy_model = permission.policy_after, PerWorkRuntimePolicy
            start_extra_reads = control.start_keys(binding.job_id)
            start_extra_writes = (runtime_key("policy", configured.binding_id), dispatch_key, grant_key,
                                 *((control.cost_key,) if not instruction else ()))
        _require(view.row(runtime_key("policy", configured.binding_id), policy_model).model_dump(mode="json")
                 == policy.model_dump(mode="json")
                 and view.row(owner_keys[1], PolicyRecord).policy_id == owner.policy_id
                 and view.row(control_key("budget", owner.root_budget_id), BudgetRecord).root_budget_id == owner.root_budget_id,
                 "unclaimed cancellation cannot renew policy or substitute root")
        def worker_request(action, ids, body, reads):
            return ControlRequest(request_id=workers.worker_key(action, *ids),
                request_digest=digest([action, body, worker_digest]), read_set=tuple(sorted(set(reads))))
        start = view.receipt(attempt.start.request,
            (job_key, pool_key, attempt_key, *start_extra_writes,
             *((control_key("budget", owner.root_budget_id),) if not instruction else ())),
            exact={attempt_key: view.hash(attempt_key)},
            result={"actual_environment": "NOT_RUN", "status": "starting", "binding": binding.model_dump(mode="json")})
        _require(attempt.start.request == worker_request("start", (binding.job_id, str(binding.generation)),
            binding.model_dump(mode="json"), (job_key, pool_key, control_key("budget", owner.root_budget_id),
                                             *owner_keys, *start_extra_reads)),
            "unclaimed start request identity or read-set differs")
        post_key = native.native_key("post-intent", launch.launch_id)
        post = view.row(post_key, _NativePostIntent)
        prepared = view.receipt(_native_post_request(config, launch, targets=targets, instruction=instruction),
                               (launch_key, target_key, grant_key, post_key),
                               exact={post_key: view.hash(post_key)},
                               result={"launch_id": launch.launch_id, "actual_environment": "NOT_RUN"})
        _require(post.request == prepared.request and post.launch_id == launch.launch_id
                 and post.target == launch.target and post.post_digest == launch.post_digest
                 and start.guard_revision < prepared.guard_revision, "original native POST reservation differs")
        cancel = None
        absent = ()
        fence_body = binding.model_dump(mode="json")
        if failed:
            absent = native._unused_assignment_keys(binding, current_owner.operation_ids)
            _require(binding.generation == 1 and launch.start.checkpoint is None
                     and all(key not in inventory.rows for key in absent),
                     "version refusal cannot hide prior SDK, operation or result state")
            fence_body = [fence_body, fence.observed_agent_version]
        if not failed or job.cancel_requested:
            cancel = view.receipt(worker_request("cancel-bound", (binding.job_id, binding.start_request_id),
                [binding.job_id, owner.model_dump(mode="json"), binding.model_dump(mode="json")],
                (job_key, pool_key, *owner_keys)), (job_key,))
            _require(cancel.result in [
                {"actual_environment": "NOT_RUN", "status": state, "cancel_accepted": True, "executor_stopped": False}
                for state in ("starting", "running", "capacity_uncertain")
            ], "unclaimed cancellation lacks exact-binding acceptance")
        original_id = native.native_key("unclaimed-fence-evidence", launch.launch_id)
        original_key = native.native_key("evidence", original_id)
        original = view.row(original_key, native._NativeEvidence, material=True)
        native.NativeLaunchAuthority._verify_unclaimed_fence(None, launch, original, inventory.rows[fence_key])
        _require(original.evidence.evidence_id == original_id,
                 "unclaimed fence changed its immutable original evidence identity")
        if failed:
            _require(original.evidence.result.verifier_version == config.delivery.verifier_version,
                     "version refusal result belongs to another verifier")
        sealed_launch = launch.model_copy(update={"evidence_sequence": original.evidence.sequence})
        seal = view.receipt(ControlRequest(
            request_id=native.native_key("fence-unclaimed", launch.launch_id),
            request_digest=digest(fence_body), read_set=tuple(sorted(set((*native_keys, fence_key, *absent))))),
            (launch_key, target_key, fence_key, original_key), exact={
                launch_key: view.image_hash(launch_key, sealed_launch), target_key: view.hash(target_key),
                fence_key: view.hash(fence_key), original_key: view.hash(original_key)},
            result={"evidence_id": original_id})
        ordered = (prepared.guard_revision < seal.guard_revision if failed
                   else prepared.guard_revision < cancel.guard_revision < seal.guard_revision)
        if failed and cancel is not None:
            ordered = ordered and seal.guard_revision < cancel.guard_revision
        _require(ordered
                 and original.configuration_digest == worker_digest
                 and original.observed_at + timedelta(seconds=60) == original.expires_at,
                 "unclaimed fence receipt order/contract differs")
        classified[fence_key] = fence
        evidence_by_id = {original_id: (original, seal.guard_revision)}
        refreshed = []
        for row_id, record in inventory.rows.items():
            data = record.payload["data"]
            if data.get("kind") != "native_executor_evidence" or data.get("launch_id") != launch.launch_id or row_id == original_key:
                continue
            value = view.row(row_id, native._NativeEvidence, material=True)
            proof = value.evidence
            native.NativeLaunchAuthority._verify_unclaimed_fence(None, launch, value, inventory.rows[fence_key])
            _require(proof == original.evidence.model_copy(update={"evidence_id": proof.evidence_id, "sequence": proof.sequence})
                     and proof.evidence_id == native.native_key("terminal-verification-id", launch.launch_id, str(proof.sequence))
                     and row_id == native.native_key("evidence", proof.evidence_id)
                     and value.configuration_digest == worker_digest
                     and value.observed_at + timedelta(seconds=60) == value.expires_at,
                     "unclaimed refresh changed its original no-provider-stop evidence")
            receipt = view.receipt(ControlRequest(
                request_id=native.native_key("reconcile-sealed", launch.launch_id, str(proof.sequence)),
                request_digest=digest([binding.model_dump(mode="json"), original_id]),
                read_set=tuple(sorted((*native_keys, original_key, fence_key)))),
                (launch_key, row_id), exact={row_id: view.hash(row_id),
                    launch_key: view.image_hash(launch_key, launch.model_copy(update={"evidence_sequence": proof.sequence}))},
                result={"evidence_id": proof.evidence_id})
            refreshed.append((proof.sequence, receipt.guard_revision, value.observed_at))
            evidence_by_id[proof.evidence_id] = value, receipt.guard_revision
        refreshed.sort()
        _require(len(refreshed) == launch.evidence_sequence - original.evidence.sequence,
                 "unclaimed refresh history is incomplete")
        revision, observed_at = seal.guard_revision, original.observed_at
        for sequence, (actual, next_revision, at) in enumerate(refreshed, original.evidence.sequence + 1):
            _require(actual == sequence and revision < next_revision and observed_at <= at,
                     "unclaimed refresh sequence/order differs")
            revision, observed_at = next_revision, at
        _validate_unclaimed_completion(view, configured, launch, job, pool, evidence_by_id, cancel,
                                       worker_request, complete=complete, failed=failed, peer_work=peer_work)
    _require(seen == sealed, "unmatched unclaimed launch/fence history")
    return MappingProxyType(classified)


def _validate_unclaimed_completion(view, configured, launch, job, pool, evidence_by_id, cancel, worker_request, *, complete,
                                  failed=False, peer_work=False):
    inventory, config = view.inventory, view.config
    binding, owner = launch.start.binding, launch.start.binding.owner
    job_key = workers.worker_key("job", binding.job_id)
    pool_key = workers.worker_key("pool", config.workers.workspace_id, *((pool.job_id,) if pool.job_id is not None else ()))
    owner_keys = (control_key("owner", owner.owner_record_id), control_key("policy", owner.policy_id))
    attempt_key = workers.worker_key("attempt", binding.job_id, str(binding.generation))
    completion_key = workers.worker_key("completion", binding.job_id, str(binding.generation))
    terminal = "failed" if failed else "cancelled"
    if completion_key not in inventory.rows:
        _require(not complete and job.state in {"starting", "running", "capacity_uncertain"}
                 and not job.result_forwarded and job.completion_credit == 1
                 and binding.job_id in pool.pending and binding.job_id not in pool.completions
                 and sum(slot.binding == binding for slot in pool.slots) == 1,
                 "unclaimed fence without observation is not released/forwarded")
        if not failed:
            _require(dict(cancel.writes)[job_key] == view.hash(job_key), "cancelled job changed before observation")
        return
    proof = view.row(completion_key, workers.ExecutorEvidence, material=True)
    retained = evidence_by_id.get(proof.evidence_id)
    _require(retained is not None and proof == retained[0].evidence
             and proof.evidence_id == job.last_evidence_id and proof.sequence == job.last_sequence
             and job.native_execution_id == binding.start_request_id and job.state == terminal
             and job.completion_credit == 0 and all(slot.binding != binding for slot in pool.slots)
             and binding.job_id not in pool.pending
             and (binding.job_id not in pool.completions if peer_work
                  else (binding.job_id in pool.completions) != job.result_forwarded),
             "unclaimed completion is not its exact worker observation")
    evidence_key = workers.worker_key("evidence", proof.evidence_id)
    native_result_key = None if not failed else workers.worker_key("work-result", proof.result.evidence_id)
    _require(view.row(evidence_key, workers.ExecutorEvidence, material=True) == proof, "worker fence evidence differs")
    observed = view.receipt(worker_request("observe", (binding.job_id, proof.evidence_id), proof.model_dump(mode="json"),
        (job_key, pool_key, attempt_key, *owner_keys)),
        (job_key, pool_key, evidence_key, completion_key, *((native_result_key,) if failed else ())), exact={
            evidence_key: view.hash(evidence_key), completion_key: view.hash(completion_key),
            **({native_result_key: view.hash(native_result_key)} if failed else {}),
            job_key: view.image_hash(job_key, job.model_copy(update={"result_forwarded": False}))},
        result={"actual_environment": "NOT_RUN", "status": terminal, "job_id": binding.job_id,
                "cancel_requested": job.cancel_requested, "completion_evidence_id": proof.evidence_id})
    _require(retained[1] < observed.guard_revision, "worker release precedes its retained claim fence")
    if failed and cancel is not None:
        _require(cancel.guard_revision < observed.guard_revision,
                 "late cancellation must follow the version fence and precede worker observation")
    if peer_work:
        from .behavior_agent_message_runtime import PeerResultForward, stopped_peer_digest
        forward_key = control_key("agent-message-result-forward", binding.job_id)
        if job.result_forwarded:
            forwarded = view.row(forward_key, PeerResultForward, material=True)
            _require(forwarded.job_id == binding.job_id, "peer forwarding belongs to another original job")
            if proof.result is None:
                _require(forwarded.result_sha256 is None
                         and forwarded.stop_evidence_sha256 == stopped_peer_digest(job, proof),
                         "peer cancellation requires its original verified stop forwarding receipt")
            else:
                _require(forwarded.stop_evidence_sha256 is None
                         and forwarded.result_sha256 == digest(proof.result.model_dump(mode="json")),
                         "peer version refusal requires its original result forwarding receipt")
        else:
            _require(not complete and forward_key not in inventory.rows, "unforwarded peer result is not complete")
        return
    forward_key = workers.worker_key("result-forward", binding.job_id)
    if forward_key not in inventory.rows:
        _require(not complete and not job.result_forwarded, "unclaimed completion has no durable report plan")
        return
    forward = view.row(forward_key, workers._ForwardPlan, material=True)
    result_key = workers.worker_key("work-result", forward.command.evidence_id)
    result = view.row(result_key, delivery.VerifiedWorkResult, material=True)
    operation_ids = view.row(owner_keys[0], OwnerRecord).operation_ids
    _require(all(control_key("operation", oid) not in inventory.rows
                 and not any(control_key("operation", oid) in dict(receipt.writes)
                             for receipt in view.receipts.values()) for oid in operation_ids),
             "unclaimed assistant closure cannot hide unrelated operation responsibilities")
    work_key = control_key("delivery-work-state", configured.obligation_id)
    work = view.row(work_key, delivery._Work)
    expected = proof.result if failed else delivery.VerifiedWorkResult(
        evidence_id=workers.worker_key("stopped-cancel", proof.evidence_id), verifier_version=config.delivery.verifier_version,
        work_id=binding.work_id, definition_digest=digest(configured.work.model_dump(mode="json")), state="cancelled",
        passed_checks=(), known_effects=(), unresolved_operations=(),
        content=b"Job cancelled after verified executor fencing. Existing operation and child records require separate reconciliation; effects are not rolled back.",
        proof=delivery._material({"cancellation": proof.model_dump(mode="json"), "operation_ids_checked": operation_ids,
                                 "known_effects": (), "unreconciled_records": ()}).data)
    _require(result == expected and forward.job_id == binding.job_id and forward.context == owner
             and forward.command == delivery.WorkResultRequest(obligation_id=configured.obligation_id, evidence_id=result.evidence_id)
             and work.definition == configured.work and work.event_id == configured.event_id,
             "unclaimed cancellation report differs from the original producer bytes")
    delivery_digest = digest({"contract": config.delivery.model_dump(mode="json"), "mode": "native"})
    delivery_request = ControlRequest(
        request_id=control_key("delivery-request", "work-result", configured.obligation_id, result.evidence_id),
        request_digest=digest(["work-result", forward.command.model_dump(mode="json"), owner.model_dump(mode="json"), delivery_digest]),
        read_set=tuple(sorted((owner_keys[0], control_key("delivery-conversation", owner.conversation_id),
            control_key("delivery-index", owner.root_budget_id),
            control_key("delivery-obligation", configured.obligation_id), work_key))))
    _require(forward.request == delivery_request and forward.delivery_configuration_digest == delivery_digest,
             "unclaimed report forwarding scope differs")
    prepared = view.receipt(worker_request("prepare-forward", (binding.job_id,), [binding.job_id, delivery_digest],
        (job_key, pool_key, completion_key, forward_key, *owner_keys,
         *((control_key("delivery-obligation", configured.obligation_id), work_key,
            control_key("delivery-conversation", owner.conversation_id),
            *(control_key("operation", oid) for oid in operation_ids)) if not failed else ()))),
        (forward_key, *((result_key,) if not failed else ())),
        exact={forward_key: view.hash(forward_key), **({result_key: view.hash(result_key)} if not failed else {})},
        result={"actual_environment": "NOT_RUN", "plan_key": forward_key})
    _require(observed.guard_revision < prepared.guard_revision, "forward plan precedes unclaimed completion")
    if delivery_request.request_id not in view.receipts:
        _require(not complete and not job.result_forwarded and work.state == "queued" and work.result is None,
                 "unconfirmed unclaimed result was promoted to delivered")
        return
    _require(work.state == terminal and work.result == result, "original unclaimed result was not forwarded")
    view.row(work_key, delivery._Work, material=result.model_dump(mode="json"))
    delivered = view.receipt(delivery_request, (work_key,), exact={work_key: view.hash(work_key)}, result={
        "actual_environment": "NOT_RUN", "execution_enabled": False, "work_state": terminal,
        "obligation_fulfilled": False, "evidence_digest": delivery._material(result.model_dump(mode="json")).sha256})
    _require(prepared.guard_revision < delivered.guard_revision, "work forwarding precedes its original plan")
    if not job.result_forwarded:
        _require(not complete, "unclaimed completion inbox has not been acknowledged")
        return
    finished = view.receipt(worker_request("forwarded", (binding.job_id,), forward.model_dump(mode="json"),
        (job_key, pool_key, *owner_keys)), (job_key, pool_key), exact={job_key: view.hash(job_key)},
        result={"actual_environment": "NOT_RUN", "work_receipt_request_id": delivery_request.request_id, "result_forwarded": True})
    _require(delivered.guard_revision < finished.guard_revision, "completion removal precedes work forwarding")


def validate_recovery_history(
    inventory: AuthorityInventory, config: ServerConfiguration, *, max_histories: int = 32,
    max_entities: int | None = DEFAULT_INVENTORY_MAX_ENTITIES,
) -> Mapping[str, RecoveryHistoryRow]:
    """Classify complete histories, including their exact failed session/model rows.

    Pure bounded inventory input: shared reader entity/byte bounds,
    <=32 recoveries (the caller may lower the last limit). No clocks, clients,
    code hashing, policy renewal, writes or dispatch. Expired evidence/approval
    is checked at its original observation, never refreshed here.
    """
    _require(type(max_histories) is int and 1 <= max_histories <= 32, "invalid recovery history bound")
    _require(max_entities is None or type(max_entities) is int and max_entities > 0,"invalid recovery entity bound")
    _require(max_entities is None or len(inventory.rows) + len(inventory.receipts) <= max_entities,
             "recovery inventory exceeds entity bound")
    size = 0
    for record in (*inventory.rows.values(), *inventory.receipts.values()):
        size += len(json.dumps(record.payload, ensure_ascii=False, separators=(",", ":"),
                               allow_nan=False).encode())
        _require(size <= DEFAULT_INVENTORY_MAX_BYTES, "recovery inventory exceeds byte bound")
    candidates, authorizations, stopped_launches = {}, {}, set()
    for key, row in inventory.rows.items():
        data = row.payload["data"]
        model = None
        if data.get("kind") == "native_platform_stop":
            model = _PlatformProof
        elif "plan" in data and ("decision_id" in data or "approved_at" in data):
            model = RecoveryAuthorization
        elif "authorization" in data and "fenced_at" in data:
            model = _StopIntent
        elif "endpoint" in data and ("status_code" in data or "credential_client_id" in data):
            model = PlatformStopObservation
        if model is not None:
            value = _parsed(data, model)
            candidates[key] = value
            if isinstance(value, RecoveryAuthorization):
                authorizations[key] = value
        if data.get("kind") == "native_launch" and data.get("sealed_evidence_id") == native.native_key(
                "platform-stop-evidence", data.get("launch_id", "")):
            stopped_launches.add(data["launch_id"])
    if not candidates and not stopped_launches:
        return MappingProxyType({})
    _require(0 < len(authorizations) <= max_histories, "missing or excessive recovery authorizations")
    config = _parsed(config.model_dump(mode="json"), ServerConfiguration)
    _require(config.workload_version == "assistant-v1.2", "recovery history cannot bind a checksum configuration")
    view = _Inventory(inventory, config)
    classified, seen_launches = {}, set()
    for key, authorization in authorizations.items():
        _validate(view, key, authorization, classified, seen_launches)
    _require(set(candidates) <= classified.keys() and seen_launches == stopped_launches,
             "unmatched, duplicate or partial recovery history")
    return MappingProxyType(classified)


def _multi_anchor(view, authorization):
    plan = authorization.plan
    hashes, images = {GUARD_KEY: plan.inventory_guard_sha256}, {}
    for key, receipt in view.receipts.items():
        if receipt.guard_revision <= plan.inventory_guard_revision:
            hashes["behavior:v1:receipt:" + key] = view.inventory.entity_sha256["behavior:v1:receipt:" + key]
            for row_id, fingerprint in receipt.writes:
                if row_id not in images or images[row_id][0] < receipt.guard_revision:
                    images[row_id] = receipt.guard_revision, fingerprint
    hashes.update({"behavior:v1:row:" + key: value[1] for key, value in images.items()})
    guard = dict(view.inventory.guard.payload)
    guard["revision"] = plan.inventory_guard_revision
    if view.inventory.installation_configuration_sha256 is not None:
        from .behavior_migration import _MigratedGuard
        guard = _MigratedGuard(binding=guard["binding"], revision=plan.inventory_guard_revision,
                               configuration_sha256=plan.configuration_sha256).model_dump(mode="json")
    _require(digest(hashes) == plan.inventory_sha256,
             "original complete inventory cannot be reconstructed from immutable receipts")
    _require(_hash(guard) == plan.inventory_guard_sha256, "original model-set guard encoding differs")
    _require(hashes["behavior:v1:row:" + workers.worker_key("job", plan.binding.job_id)] == plan.original_job_sha256,
             "original model-set job image differs")


def _multi_pending(view, authorization_key, authorization, classified):
    plan, inventory, config = authorization.plan, view.inventory, view.config
    launch = view.row(native.native_key("launch", plan.launch_id), native._NativeLaunch)
    finished = isinstance(plan, FinishedResultRecoveryPlan)
    _multi_models(inventory, config, launch, plan.model_intents, finished=finished)
    if finished:
        _finished_proof(inventory, config, launch, plan.finished_result.artifact_blob_etag, expected=plan.finished_result)
    if isinstance(plan, TeamsPreAcquireRecoveryPlan):
        from .behavior_teams_recovery import recovery_keys, verify_plan_original
        verify_plan_original(inventory, config, launch, plan)
        keys = recovery_keys(config, launch, inventory)
    else:
        _, keys = _keys(launch, config, _model_digests(plan), before_assignment=True)
    _multi_inventory(inventory, config, plan, keys, authorization)
    auth = view.row(authorization_key, _authorization_model(plan), material=True)
    job_key = workers.worker_key("job", plan.binding.job_id)
    job = view.row(job_key, workers.JobRecord)
    cancelled = recovery_key(_fence_stage(plan), plan.recovery_id) in view.receipts
    intent_key, stop_key = (recovery_key(stage, plan.recovery_id) for stage in ("stop-intent", "stop-observation"))
    reserved = intent_key in inventory.rows
    _require(auth == authorization and launch.phase == "claimed" and launch.start.binding == plan.binding
             and launch.target == plan.target and launch.native_invocation_id == plan.native_invocation_id
             and job.binding == plan.binding and job.state in {"starting", "running", "capacity_uncertain"}
             and job.cancel_requested == cancelled and job.cancel_credit == (0 if cancelled else 1)
             and job.stop_credit == (0 if reserved else 1) and job.completion_credit == 1 and not job.result_forwarded,
             "pending model-set recovery has changed or prematurely released its original job")
    rows = {key: None if inventory.rows.get(key) is None else inventory.rows[key].payload for key in keys}
    rows[job_key] = {**inventory.rows[job_key].payload, "data": job.model_copy(update={
        "cancel_requested": False, "cancel_credit": 1, "stop_credit": 1}).model_dump(mode="json")}
    _require(digest(rows) == plan.snapshot_sha256, "pending model-set original snapshot differs")
    classified[authorization_key] = authorization
    if reserved:
        intent = view.row(intent_key, _StopIntent, material=True)
        _require(intent.authorization == authorization
                 and authorization.approved_at <= intent.fenced_at < authorization.expires_at,
                 "pending stop reservation belongs to another approval/window")
        classified[intent_key] = intent
    if stop_key in inventory.rows:
        observation = view.row(stop_key, PlatformStopObservation, material=True)
        _require(reserved and observation.target == plan.target and observation.endpoint == stop_endpoint(plan.target)
                 and observation.credential_client_id == config.identity.managed_identity_client_id
                 and intent.fenced_at <= observation.observed_at < authorization.expires_at
                 and (observation.status_code, observation.error_code) in {(204, None), (409, "session_already_stopped")},
                 "pending stop observation differs from exact original target/permission")
        classified[stop_key] = observation


def _validate(view, authorization_key, authorization, classified, seen_launches):
    config, inventory = view.config, view.inventory
    plan, owner = authorization.plan, authorization.plan.binding.owner
    binding = plan.binding
    teams = isinstance(plan, TeamsPreAcquireRecoveryPlan)
    if teams:
        from .behavior_teams_recovery import configured_work, verify_plan_original
        configured, _ = configured_work(config, plan.event_id)
    else:
        configured = next((entry for entry in config.bindings if entry.job_id == binding.job_id), None)
    _require(configured is not None and plan.launch_id not in seen_launches
             and plan.configuration_sha256 == config.sha256 and plan.operator_oid == config.identity.human_oid
             and binding.owner == configured.owner and binding.work_id == configured.work.work_id
             and binding.executor_id == config.workers.executor_id
             and binding.executor_version == config.workers.executor_version
             and plan.target in config.active_targets and plan.target.agent_oid == configured.agent_oid
             and authorization.approved_at < authorization.expires_at,
             "recovery original configuration/operator/execution/approval window differs")
    multi = isinstance(plan, MultiModelRecoveryPlan)
    finished = isinstance(plan, FinishedResultRecoveryPlan)
    terminal = "succeeded" if finished else "cancelled"
    if multi:
        _multi_anchor(view, authorization)
        if native.native_key("platform-stop", plan.launch_id) not in inventory.rows:
            _multi_pending(view, authorization_key, authorization, classified)
            return
    seen_launches.add(plan.launch_id)
    expected_auth_key = recovery_key("authorization", plan.recovery_id)
    intent_key, stop_key = (recovery_key(kind, plan.recovery_id) for kind in ("stop-intent", "stop-observation"))
    proof_key = native.native_key("platform-stop", plan.launch_id)
    _require(authorization_key == expected_auth_key, "recovery authorization key differs")
    authorization = view.row(authorization_key, _authorization_model(plan), material=True)
    intent = view.row(intent_key, _StopIntent, material=True)
    observation = view.row(stop_key, PlatformStopObservation, material=True)
    proof = view.row(proof_key, _proof_model(plan), material=True)
    # The original publisher read its clock twice. Preserve its exact recorded
    # expiry, but require the historical action itself to fit the strict window.
    effective_until = min(authorization.expires_at, authorization.approved_at + timedelta(
        minutes=60 if isinstance(plan, ExpiredTeamsCleanupPlan) else 15))
    _require(intent.authorization == authorization and proof.intent == intent and proof.observation == observation
             and observation.target == plan.target and observation.endpoint == stop_endpoint(plan.target)
             and observation.credential_client_id == config.identity.managed_identity_client_id
             and authorization.approved_at <= intent.fenced_at <= observation.observed_at < effective_until
             and (observation.status_code, observation.error_code) in {(204, None), (409, "session_already_stopped")},
             "recovery authorization, stop endpoint/outcome/time or protected proof differs")
    classified.update({authorization_key: authorization, intent_key: intent, stop_key: observation, proof_key: proof})
    worker_digest = digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
    delivery_digest = digest({"contract": config.delivery.model_dump(mode="json"), "mode": "native"})
    launch_key, target_key = native.native_key("launch", plan.launch_id), native.native_key("target", plan.target.executor_id)
    launch = view.row(launch_key, native._NativeLaunch)
    target = view.row(target_key, native._TargetReservation)
    job_key, pool_key = workers.worker_key("job", binding.job_id), workers.worker_key("pool", config.workers.workspace_id)
    job, pool = view.row(job_key, workers.JobRecord), view.row(pool_key, workers._Pool)
    attempt_key = workers.worker_key("attempt", binding.job_id, str(binding.generation))
    attempt = view.row(attempt_key, workers._Attempt)
    view.row(attempt_key, workers._Attempt, material=attempt.start.model_dump(mode="json"))
    _require(plan.launch_id == native.native_key("launch-id", binding.start_request_id)
             and launch.launch_id == plan.launch_id and launch.start.binding == binding
             and launch.start == attempt.start and launch.start.input == job.spec.input
             and hashlib.sha256(launch.start.input).hexdigest() == binding.input_digest
             and launch.definition == configured.work and launch.api_binding_id == configured.binding_id
             and launch.target == target.target == plan.target and target.generation == launch.process_generation
             and target.active_launch_id is None and launch.phase == "sealed" and launch.pending_control == "cancel"
             and launch.workload_version == "assistant-v1.2" and launch.claim_id is not None
             and launch.process_id is not None and launch.report_token_digest is not None
             and launch.native_invocation_id == plan.native_invocation_id
             and job.spec.job_id == binding.job_id and job.spec.work_id == binding.work_id
             and job.spec.obligation_id == configured.obligation_id and job.spec.parent_job_id is None
             and job.binding == binding and job.owner == owner and job.generation == binding.generation
             and (job.native_execution_id in {None, plan.native_invocation_id} if multi else
                  job.native_execution_id == plan.native_invocation_id) and not job.children
             and (job.state in {"starting", "running", "capacity_uncertain", terminal} if multi else
                  job.state == "cancelled" and job.result_forwarded)
             and job.cancel_requested and job.cancel_credit == job.stop_credit == 0
             and (job.completion_credit in {0, 1} if multi else job.completion_credit == 0)
             and pool.workspace_id == config.workers.workspace_id
             and (multi or all(slot.binding != binding for slot in pool.slots)
                  and binding.job_id not in (*pool.pending, *pool.completions))
             and all(value.configuration_digest == worker_digest for value in (launch, target, job, pool)),
             "original claimed launch, generation, attempt, cancelled job or released capacity differs")
    grant = view.row(native.native_key("grant", launch.grant_id), native.NativeExecutionGrant)
    _require(grant.grant_id == launch.grant_id and grant.controller_id == config.controller_id
             and grant.job_id == binding.job_id and grant.owner == owner and grant.definition == launch.definition
             and grant.input_digest == binding.input_digest and grant.workload_version == launch.workload_version
             and launch.target in grant.targets, "original native grant binding differs")
    current_owner = view.row(control_key("owner", owner.owner_record_id), OwnerRecord)
    _require(all(getattr(current_owner, name) == value for name, value in owner.model_dump().items()),
             "recovery owner binding changed")
    _require(view.row(control_key("policy", owner.policy_id), PolicyRecord).policy_id == owner.policy_id
             and view.row(control_key("budget", owner.root_budget_id), BudgetRecord).root_budget_id == owner.root_budget_id,
             "recovery budget/policy identity differs")
    policy = (view.row(runtime_key("policy", configured.binding_id), RuntimePolicy) if teams else
              next(entry.policy for entry in config.policies if entry.binding_id == configured.binding_id))
    _require(view.row(runtime_key("policy", configured.binding_id), RuntimePolicy).model_dump(mode="json")
             == policy.model_dump(mode="json"),
             "recovery cannot renew or replace the original RuntimePolicy")
    sid = assistant_key("session-id", binding.start_request_id)
    session_key = assistant_key("session", sid)
    before_assignment = isinstance(plan, PreAssignmentRecoveryPlan)
    session = None if before_assignment else view.row(session_key, _Session)
    task = Task.model_validate_json(launch.start.input)
    _require((session is None or session.binding_id == session.assignment.binding_id == configured.binding_id
             and session.execution == binding and session.assignment.session_id == sid and session.assignment.task == task)
             and task.job_id == binding.job_id and task.work_id == binding.work_id
             and task.definition_digest == digest(launch.definition.model_dump(mode="json"))
             and (finished or assistant_key("artifact", binding.work_id) not in inventory.rows
                  and assistant_key("result", binding.work_id) not in inventory.rows),
             "original SDK assignment or absent result material differs")
    if session is not None:
        classified[session_key] = session
    if before_assignment:
        native_keys, _ = _keys(launch, config, None, before_assignment=True)
        claim = _unassigned_claim(inventory, launch, native_keys, original_request=plan.native_claim_request)
        _require(digest(claim.payload) == plan.native_claim_receipt_sha256, "missing-assignment claim receipt changed")
    elif isinstance(plan, PreSdkRecoveryPlan):
        _require(session.state == "accepted" and session.reason is None and session.model_requests == 0
                 and not session.context_read and not session.assignment.resume and launch.start.checkpoint is None
                 and assistant_key("acquire", sid, "sdk-start") not in inventory.receipts,
                 "pre-SDK recovery cannot hide a consumed acquire or later SDK activity")
        for key, row in inventory.rows.items():
            request_digest = row.payload["data"].get("request_sha256")
            _require(not isinstance(request_digest, str)
                     or key != assistant_key("model-intent", sid, request_digest),
                     "pre-SDK recovery cannot hide any original model intent")
    elif multi:
        _multi_models(inventory, config, launch, plan.model_intents, finished=finished)
        if teams:
            verify_plan_original(inventory, config, launch, plan)
        classified.update({assistant_key("model-intent", sid, item.request_sha256):
                           view.row(assistant_key("model-intent", sid, item.request_sha256), _ModelIntent)
                           for item in plan.model_intents})
        if finished:
            from .behavior_assistant import _ArtifactEffect
            _finished_proof(inventory, config, launch, plan.finished_result.artifact_blob_etag, expected=plan.finished_result)
            classified[assistant_key("artifact", binding.work_id)] = view.row(assistant_key("artifact", binding.work_id), _ArtifactEffect)
            classified[assistant_key("result", binding.work_id)] = view.row(
                assistant_key("result", binding.work_id), delivery.VerifiedWorkResult, material=True)
    else:
        model_key = assistant_key("model-intent", sid, plan.model_request_sha256)
        model = view.row(model_key, _ModelIntent)
        _require(session.state == "waiting" and session.reason == "sdk_failed" and session.model_requests == 1
                 and model.consumed and model.request_sha256 == plan.model_request_sha256,
                 "original failed SDK session/consumed unknown model request differs")
        classified[model_key] = model
    obligation = view.row(control_key("delivery-obligation", configured.obligation_id), delivery._Obligation)
    _require(obligation.configuration_digest == delivery_digest and obligation.evidence_mode == "native"
             and obligation.obligation_id == configured.obligation_id and obligation.work_id == binding.work_id
             and obligation.event_id == configured.event_id and obligation.root_request_id == configured.root_request_id
             and obligation.owner_record_id == owner.owner_record_id and obligation.conversation_id == owner.conversation_id
             and obligation.root_budget_id == owner.root_budget_id and obligation.route == configured.route,
             "original cancellation delivery responsibility is missing or differently bound")
    native_keys, recovery_keys = _keys(launch, config, _model_digests(plan), before_assignment=before_assignment or multi)
    if teams:
        from .behavior_teams_recovery import recovery_keys as teams_keys
        recovery_keys = teams_keys(config, launch, inventory)
    owner_keys = (control_key("owner", owner.owner_record_id), control_key("policy", owner.policy_id))

    def worker_request(action, ids, body, reads):
        return ControlRequest(request_id=workers.worker_key(action, *ids),
                              request_digest=digest([action, body, worker_digest]), read_set=tuple(sorted(set(reads))))

    auth_receipt = view.receipt(ControlRequest(request_id=authorization_key,
        request_digest=digest(authorization.model_dump(mode="json"))), (authorization_key,),
        exact={authorization_key: view.hash(authorization_key)}, result={"authorization_key": authorization_key})
    cancel_request = (ControlRequest(request_id=recovery_key(
                                     "cancel-teams-pre-acquire" if teams else "fence-finished-result" if finished else
                                     "cancel-model-set" if multi else "cancel-before-assignment", plan.recovery_id),
                                     request_digest=plan.sha256, read_set=recovery_keys)
                      if before_assignment or multi else worker_request("cancel-bound", (binding.job_id, binding.start_request_id),
        [binding.job_id, owner.model_dump(mode="json"), binding.model_dump(mode="json")],
        (job_key, pool_key, *owner_keys)))
    cancel = view.receipt(cancel_request, (job_key,))
    _require(cancel.result in [
        {"actual_environment": "NOT_RUN", "status": state, "cancel_accepted": True, "executor_stopped": False}
        for state in ("starting", "running", "capacity_uncertain")
    ], "original bound cancellation was not accepted for a claimed active attempt")
    reservation = view.receipt(ControlRequest(request_id=intent_key,
        request_digest=digest(intent.model_dump(mode="json")), read_set=recovery_keys), (intent_key, job_key),
        exact={intent_key: view.hash(intent_key)}, result={"stop_reserved": True})
    stop = view.receipt(ControlRequest(request_id=stop_key,
        request_digest=digest(observation.model_dump(mode="json"))), (stop_key,),
        exact={stop_key: view.hash(stop_key)}, result={"provider_stop_observed": True})
    _require(digest(inventory.receipts[cancel.request.request_id].payload) == proof.cancellation_receipt_sha256
             and digest(inventory.receipts[stop_key].payload) == proof.stop_receipt_sha256
             and auth_receipt.guard_revision < cancel.guard_revision < reservation.guard_revision < stop.guard_revision,
             "original cancellation/stop receipt hashes or causal order differ")
    original_id = native.native_key("platform-stop-evidence", plan.launch_id)
    original_key = native.native_key("evidence", original_id)
    original = view.row(original_key, native._NativeEvidence, material=True)
    result = _cancelled_result(SimpleNamespace(verifier_version=config.delivery.verifier_version), launch, proof,
                              retained_result=inventory.rows.get(assistant_key("result", binding.work_id)) if finished else None)
    _require(launch.sealed_evidence_id == original_id and original.evidence.evidence_id == original_id
             and original.evidence.sequence <= launch.evidence_sequence
             and observation.observed_at <= original.observed_at < authorization.expires_at,
             "original terminal proof/seal time differs")
    sealed_launch = launch.model_copy(update={"evidence_sequence": original.evidence.sequence})
    seal = view.receipt(ControlRequest(request_id=recovery_key("sealed", plan.recovery_id),
        request_digest=digest(proof.model_dump(mode="json")),
        read_set=tuple(dict.fromkeys((*recovery_keys, proof_key, intent_key, stop_key)))),
        (proof_key, original_key, launch_key, target_key), exact={
            proof_key: view.hash(proof_key), original_key: view.hash(original_key),
            launch_key: view.image_hash(launch_key, sealed_launch), target_key: view.hash(target_key),
        }, result={"platform_stop_evidence_id": original_id, "sdk_graceful_exit_claim": False})
    _require(stop.guard_revision < seal.guard_revision, "stop seal precedes the retained observation")
    evidence_by_id, refreshed = {}, []
    evidence_revisions = {original_id: seal.guard_revision}
    for key, row in inventory.rows.items():
        data = row.payload["data"]
        if data.get("kind") != "native_executor_evidence" or data.get("launch_id") != plan.launch_id:
            continue
        value = view.row(key, native._NativeEvidence, material=True)
        evidence = value.evidence
        if evidence.sequence < original.evidence.sequence:
            _require(value.probe_id != proof_key, "platform-stop evidence predates its original terminal proof")
            continue
        expected = original.evidence.model_copy(update={"evidence_id": evidence.evidence_id, "sequence": evidence.sequence})
        _require(key == native.native_key("evidence", evidence.evidence_id)
                 and value.launch_id == plan.launch_id and value.configuration_digest == worker_digest
                 and value.probe_id == proof_key and value.report_digest == digest(proof.model_dump(mode="json"))
                 and original.evidence.binding == binding and original.evidence.native_execution_id == plan.native_invocation_id
                 and original.evidence.state == ("succeeded" if finished else "stopped") and original.evidence.checkpoint is None
                 and original.evidence.entire_tree_idle and original.evidence.all_inflight_starts_fenced
                 and original.evidence.result == result and original.evidence.proof == delivery._material(proof.model_dump(mode="json")).data
                 and evidence == expected and original.observed_at <= value.observed_at
                 and (value.observed_at + timedelta(seconds=60) <= value.expires_at
                      <= authorization.expires_at + timedelta(seconds=60) if key == original_key
                      else value.expires_at == value.observed_at + timedelta(seconds=60)),
                 "original or refreshed terminal proof/result/material differs")
        evidence_by_id[evidence.evidence_id] = value
        if key == original_key:
            continue
        _require(evidence.sequence > original.evidence.sequence
                 and evidence.evidence_id == native.native_key("terminal-verification-id", plan.launch_id, str(evidence.sequence)),
                 "refreshed terminal evidence ID/generation differs")
        request = ControlRequest(
            request_id=native.native_key("reconcile-sealed", plan.launch_id, str(evidence.sequence)),
            request_digest=digest([binding.model_dump(mode="json"), original_id]),
            read_set=tuple(sorted(set((*native_keys, original_key, proof_key)))),
        )
        receipt = view.receipt(request, (launch_key, key), exact={
            key: view.hash(key), launch_key: view.image_hash(launch_key, launch.model_copy(update={"evidence_sequence": evidence.sequence})),
        }, result={"evidence_id": evidence.evidence_id})
        evidence_revisions[evidence.evidence_id] = receipt.guard_revision
        refreshed.append((evidence.sequence, receipt.guard_revision, value.observed_at))
    refreshed.sort()
    _require(len(refreshed) == launch.evidence_sequence - original.evidence.sequence,
             "missing retained terminal revalidation")
    previous_revision, previous_time = seal.guard_revision, original.observed_at
    for sequence, (actual, revision, observed_at) in enumerate(refreshed, original.evidence.sequence + 1):
        _require(sequence == actual and previous_revision < revision and previous_time <= observed_at,
                 "terminal revalidation sequence/time/order differs")
        previous_revision, previous_time = revision, observed_at
    completion_key = workers.worker_key("completion", binding.job_id, str(binding.generation))
    if multi and completion_key not in inventory.rows:
        _require(job.state in {"starting", "running", "capacity_uncertain"} and job.completion_credit == 1
                 and not job.result_forwarded and binding.job_id in pool.pending
                 and binding.job_id not in pool.completions and sum(slot.binding == binding for slot in pool.slots) == 1
                 and dict(reservation.writes)[job_key] == view.hash(job_key),
                 "sealed model-set without worker observation cannot release capacity")
        return
    latest = evidence_by_id.get(job.last_evidence_id)
    _require(latest is not None and latest.evidence.sequence == job.last_sequence,
             "worker completion has no exact original/refreshed terminal evidence")
    if multi:
        _require(job.state == terminal and job.completion_credit == 0
                 and binding.job_id not in pool.pending and all(slot.binding != binding for slot in pool.slots)
                 and (binding.job_id in pool.completions) != job.result_forwarded,
                 "observed model-set completion or capacity state differs")
    worker_evidence_key = workers.worker_key("evidence", job.last_evidence_id)
    result_key = workers.worker_key("work-result", result.evidence_id)
    for key in (completion_key, worker_evidence_key):
        _require(view.row(key, workers.ExecutorEvidence, material=True) == latest.evidence,
                 "worker evidence/completion differs from native terminal proof")
    _require(view.row(result_key, delivery.VerifiedWorkResult, material=True) == result,
             "retained cancelled work result differs")
    observe = view.receipt(worker_request("observe", (binding.job_id, job.last_evidence_id),
        latest.evidence.model_dump(mode="json"), (job_key, pool_key, attempt_key, *owner_keys)),
        (worker_evidence_key, completion_key, result_key, pool_key, job_key), exact={
            **{key: view.hash(key) for key in (worker_evidence_key, completion_key, result_key)},
            job_key: view.image_hash(job_key, job.model_copy(update={"result_forwarded": False})),
        }, result={"actual_environment": "NOT_RUN", "status": terminal, "job_id": binding.job_id,
                   "cancel_requested": True, "completion_evidence_id": job.last_evidence_id})
    _require(evidence_revisions[job.last_evidence_id] < observe.guard_revision,
             "worker release precedes its original or refreshed terminal verification")
    work_key = control_key("delivery-work-state", configured.obligation_id)
    forward_key = workers.worker_key("result-forward", binding.job_id)
    if multi and forward_key not in inventory.rows:
        work = view.row(work_key, delivery._Work)
        _require(not job.result_forwarded and work.state == "queued" and work.result is None,
                 "unprepared model-set report cannot claim result forwarding")
        return
    forward = view.row(forward_key, workers._ForwardPlan, material=True)
    command = delivery.WorkResultRequest(obligation_id=configured.obligation_id, evidence_id=result.evidence_id)
    expected_request = ControlRequest(
        request_id=control_key("delivery-request", "work-result", configured.obligation_id, result.evidence_id),
        request_digest=digest(["work-result", command.model_dump(mode="json"), owner.model_dump(mode="json"), delivery_digest]),
        read_set=tuple(sorted((control_key("owner", owner.owner_record_id),
            control_key("delivery-conversation", owner.conversation_id), control_key("delivery-index", owner.root_budget_id),
            control_key("delivery-obligation", configured.obligation_id), work_key))),
    )
    _require(forward.job_id == binding.job_id and forward.context == owner and forward.command == command
             and forward.delivery_configuration_digest == delivery_digest and forward.request == expected_request,
             "original forwarding plan differs")
    prepared = view.receipt(worker_request("prepare-forward", (binding.job_id,), [binding.job_id, delivery_digest],
        (job_key, pool_key, completion_key, forward_key, *owner_keys)), (forward_key,),
        exact={forward_key: view.hash(forward_key)}, result={"actual_environment": "NOT_RUN", "plan_key": forward_key})
    if multi and expected_request.request_id not in view.receipts:
        work = view.row(work_key, delivery._Work)
        _require(not job.result_forwarded and work.state == "queued" and work.result is None
                 and observe.guard_revision < prepared.guard_revision,
                 "prepared model-set report lacks its original work-result receipt")
        return
    work = view.row(work_key, delivery._Work, material=result.model_dump(mode="json"))
    _require(work.configuration_digest == delivery_digest and work.evidence_mode == "native"
             and work.definition == launch.definition and work.definition_digest == result.definition_digest
             and work.event_id == configured.event_id and work.state == terminal and work.result == result,
             "forwarded work must preserve the cancelled result and unresolved model request")
    delivered = view.receipt(expected_request, (work_key,), exact={work_key: view.hash(work_key)},
        result={"actual_environment": "NOT_RUN", "execution_enabled": False, "work_state": terminal,
                "obligation_fulfilled": False, "evidence_digest": delivery._material(result.model_dump(mode="json")).sha256})
    if multi and not job.result_forwarded:
        _require(observe.guard_revision < prepared.guard_revision < delivered.guard_revision,
                 "work-result precedes original model-set report plan")
        return
    forwarded = view.receipt(worker_request("forwarded", (binding.job_id,), forward.model_dump(mode="json"),
        (job_key, pool_key, *owner_keys)), (job_key, pool_key), exact={job_key: view.hash(job_key)},
        result={"actual_environment": "NOT_RUN", "work_receipt_request_id": expected_request.request_id, "result_forwarded": True})
    _require(observe.guard_revision < prepared.guard_revision < delivered.guard_revision < forwarded.guard_revision,
             "completion/result forwarding receipt order differs")
