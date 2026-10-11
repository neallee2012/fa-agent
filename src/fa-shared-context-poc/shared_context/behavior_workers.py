"""Finite durable executor scheduling. actual_environment=NOT_RUN.

One WorkerContract.workspace_id maps to ONE BehaviorStore authority for the whole
workspace, not one pool per conversation/process. Provision current OwnerRecord,
PolicyRecord and BudgetRecord through that same guard before enqueue. initialize
creates a finite C=1/C=4 pool, not a table/resource. Local/fake adapters must use
offline_contract. Native mode requires a persisted, independently verified
WorkerReadiness and a server-owned adapter with start/query/stop/verify methods.

start/stop commit the exact binding, immutable input/checkpoint, original request
ID/digest and uncertain state BEFORE calling an adapter, OUTSIDE decide callbacks.
Only the invocation that freshly commits gets a one-use in-memory dispatch right.
Replay/restart/unknown use a separately reserved query, NEVER another start/stop.
Each query reservation permits only its fresh committer to query once; a replay
is refused, not a verified query result or another dispatch right. A negative query,
clock passage, expired owner/lease or cancel acceptance cannot free a slot.
Terminal/parked evidence must fence ALL late starts and the complete executor
tree. Park additionally retains a verified checkpoint. Resume atomically takes
a slot and creates a new generation before the adapter resumes that checkpoint.

Completion, cancellation and stop have independent reserved lanes. Every job's
input, attempts, checkpoints, receipts and completion inbox are retained inline;
there is no delete/GC/refund or hidden business retry. SDK/native activity proof,
JWT endpoints and process supervision are embedding-server responsibilities.

Public Interfaces: WorkerContract, WorkerReadiness, JobSpec, JobRecord, Slot,
ExecutorBinding, ExecutorStart, ExecutorStop, ExecutorEvidence, ExecutorAdapter;
WorkerCore.initialize(), enqueue(spec, context), start(job_id, context),
publish_readiness(evidence_id, supersedes=None),
observe(job_id, evidence_id, context), reconcile(job_id, context),
cancel(job_id, context), stop(job_id, context), adopt(job_id, context),
inspect(job_id), pending(), completions(), verify_work_result(evidence_id),
forward_result(job_id, context, delivery_core).
WorkerConsumer(core, delivery=None).drain(resolve_context, limit=4) is a bounded
supervisor tick. Wire delivery.verify_work_result to core.verify_work_result
before forwarding; DeliveryConsumer prepares final/failure afterward.

Persisted keys use worker_key(kind, *ids) = control_key("worker-" + kind, *ids):
  pool(workspace_id), readiness(workspace_id), readiness-evidence(evidence_id),
  job(job_id)
  admission(obligation_id): original delivery intent -> one stable job identity
  attempt(job_id, generation), stop-intent(job_id, generation)
  evidence(evidence_id), completion(job_id, generation), work-result(evidence_id)
  result-forward(job_id): exact immutable delivery result command/request
Start/stop/query/admission/observe/forward requests also have original immutable
BehaviorStore receipts. The pool separately indexes pending jobs and unforwarded
completion reservations. Completed jobs without verified result evidence stay
in that inventory; they are not rerun or silently forgotten.

C/executor-version changes require an explicit drained migration, not a new
concurrent pool. Native adapters must disable their own blind start retries and
verify native full-tree idle/fencing/checkpoint bindings, including late requests.
Only queued or verifiably parked work can adopt a newer owner epoch; a running or
unknown owner's slot stays reserved. There is no automatic owner lease expiry.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .behavior_delivery import (
    Count, DeliveryCore, VerifiedWorkResult, WorkResultRequest, _Intent, _Model, _Obligation, _check, _digest, _material,
    _parse, _update, _utc, authority_snapshot,
)
from .behavior_operations import (
    BudgetRecord, ExecutionContext, OperationDescription, OwnerRecord, PolicyRecord, _Operation, control_key,
)
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite, Digest, InlineMaterial
from .errors import Problem
from .models import Identifier
from .records import Record

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


def worker_key(kind: str, *ids: str) -> str:
    return control_key("worker-" + kind, *ids)


class WorkerContract(_Model):
    workspace_id: Identifier
    control_binding: Digest
    executor_id: Identifier
    executor_version: Identifier
    protection_version: Identifier
    capacity: Literal[1, 4] = 1
    pending_limit: Annotated[int, Field(ge=1, le=32)] = 16
    max_generations: Annotated[int, Field(ge=1, le=8)] = 4
    control_units: Annotated[int, Field(ge=1, le=16)] = 8
    reservation_units: Annotated[int, Field(ge=1, le=1024)] = 1
    lease_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    queries_per_generation: Annotated[int, Field(ge=1, le=16)] = 8

    @model_validator(mode="after")
    def pinned_versions(self):
        if any(value.lower() in {"latest", "current", "head", "unknown"} for value in (
            self.executor_version, self.protection_version,
        )):
            raise ValueError("executor and protection contracts must use immutable versions")
        return self


class WorkerReadiness(_Model):
    kind: Literal["worker_readiness"] = "worker_readiness"
    evidence_id: Identifier
    configuration_digest: Digest
    control_binding: Digest
    protection_version: Identifier
    executor_id: Identifier
    executor_version: Identifier
    native_binding_reference: Identifier
    supervisor_reference: Identifier
    protection_reference: Identifier
    observed_at: datetime
    expires_at: datetime
    proof: Annotated[bytes, Field(min_length=1, max_length=8192)]

    _times = field_validator("observed_at", "expires_at")(_utc)


class JobSpec(_Model):
    job_id: Identifier
    work_id: Identifier
    input: Annotated[bytes, Field(min_length=1, max_length=16384)]
    parent_job_id: Identifier | None = None
    obligation_id: Identifier | None = None


class ExecutorBinding(_Model):
    job_id: Identifier
    work_id: Identifier
    generation: Annotated[int, Field(ge=1, le=8)]
    owner: ExecutionContext
    executor_id: Identifier
    executor_version: Identifier
    input_digest: Digest
    checkpoint_digest: Digest | None = None
    start_request_id: Identifier


class ExecutorStart(_Model):
    binding: ExecutorBinding
    request: ControlRequest
    input: bytes
    checkpoint: bytes | None = None


class ExecutorStop(_Model):
    binding: ExecutorBinding
    request: ControlRequest
    native_execution_id: Identifier | None = None


class ExecutorEvidence(_Model):
    evidence_id: Identifier
    binding: ExecutorBinding
    sequence: Annotated[int, Field(ge=1)]
    native_execution_id: Identifier | None = None
    state: Literal["running", "unknown", "parked", "succeeded", "failed", "stopped"]
    entire_tree_idle: bool = False
    all_inflight_starts_fenced: bool = False
    checkpoint: Annotated[bytes, Field(min_length=1, max_length=8192)] | None = None
    result: VerifiedWorkResult | None = None
    proof: Annotated[bytes, Field(min_length=1, max_length=8192)]

    @model_validator(mode="after")
    def trustworthy_idle(self):
        if self.state in {"parked", "succeeded", "failed", "stopped"} and (
            not self.entire_tree_idle or not self.all_inflight_starts_fenced
            or self.native_execution_id is None
        ):
            raise ValueError("terminal/parked evidence requires the native binding and full-tree/in-flight fence")
        if (self.state == "parked") != (self.checkpoint is not None):
            raise ValueError("only parked evidence carries a mandatory immutable checkpoint")
        if self.state == "running" and (self.entire_tree_idle or self.native_execution_id is None):
            raise ValueError("running evidence must identify the native executor")
        if self.result is not None and (
            self.result.work_id != self.binding.work_id
            or self.state not in {"succeeded", "failed", "stopped"}
            or self.state == "succeeded" and self.result.state != "succeeded"
        ):
            raise ValueError("work result must belong to this terminal execution")
        return self


@dataclass(frozen=True)
class ExecutorAdapter:
    """Trusted server registration; never accept callables from a client/model."""

    start: Callable[[ExecutorStart], str | None]
    query: Callable[[ExecutorBinding], str]
    stop: Callable[[ExecutorStop], str | None]
    verify: Callable[[str], ExecutorEvidence]
    verify_readiness: Callable[[str], WorkerReadiness] | None = None
    # Read-only: whether an unfinished attempt's session compute is proven stopped (no writes, no invocation).
    stopped_session: Callable[[ExecutorBinding], str | None] | None = None


class Slot(_Model):
    number: Annotated[int, Field(ge=0, le=3)]
    binding: ExecutorBinding | None = None
    lease_until: datetime | None = None

    _time = field_validator("lease_until")(lambda value: None if value is None else _utc(value))


class _Pool(_Model):
    kind: Literal["worker_pool"] = "worker_pool"
    configuration_digest: Digest
    workspace_id: Identifier
    slots: Annotated[tuple[Slot, ...], Field(min_length=1, max_length=4)]
    pending: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    completions: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    job_id: Identifier | None = Field(default=None,exclude_if=lambda value:value is None)


class JobRecord(_Model):
    kind: Literal["worker_job"] = "worker_job"
    configuration_digest: Digest
    spec: JobSpec
    owner: ExecutionContext
    state: Literal["queued", "starting", "running", "capacity_uncertain", "waiting_dependency", "succeeded", "failed", "cancelled"]
    generation: Count = 0
    binding: ExecutorBinding | None = None
    native_execution_id: Identifier | None = None
    checkpoint: bytes | None = None
    last_evidence_id: Identifier | None = None
    last_sequence: Count = 0
    children: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()
    cancel_requested: bool = False
    completion_credit: Literal[0, 1] = 1
    cancel_credit: Literal[0, 1] = 1
    stop_credit: Literal[0, 1] = 1
    control_remaining: Count | None
    queries_reserved: Count = 0
    result_forwarded: bool = False


class _Attempt(_Model):
    kind: Literal["worker_attempt"] = "worker_attempt"
    start: ExecutorStart


class _InactiveCancellation(_Model):
    kind: Literal["worker_inactive_cancel"] = "worker_inactive_cancel"
    evidence_id: Identifier
    spec: JobSpec
    owner: ExecutionContext
    generation: Count
    binding: ExecutorBinding | None
    prior_evidence_id: Identifier | None
    children: tuple[Identifier, ...]


class _ForwardPlan(_Model):
    kind: Literal["worker_result_forward"] = "worker_result_forward"
    job_id: Identifier
    context: ExecutionContext
    command: WorkResultRequest
    request: ControlRequest
    delivery_configuration_digest: Digest


class WorkerCore:
    def __init__(
        self, store: BehaviorStore, contract: WorkerContract, *, adapter: ExecutorAdapter | None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        mode: Literal["native", "offline_contract"] = "native",
    ):
        self.store, self.contract, self.adapter = store, _check(contract, WorkerContract), adapter
        self.work_policy = None
        self.clock, self.mode = clock, mode

    def configuration_digest(self):
        body = self.contract.model_dump(mode="json")
        return _digest(body if self.mode == "offline_contract" else {"contract": body, "mode": "native"})

    def _now(self):
        return _utc(self.clock())

    def _ready(self, job_id=None):
        if self.adapter is None or self.mode not in {"native", "offline_contract"}:
            raise Problem(403, "WORKER_DISABLED", "a bounded executor adapter is not installed")
        if self.mode == "offline_contract":
            return
        if self.instruction(job_id):
            return
        if self.adapter.verify_readiness is None:
            raise Problem(403, "WORKER_NATIVE_DISABLED", "native executor/readiness verifier is missing")
        key = worker_key("readiness", self.contract.workspace_id)
        row = authority_snapshot(self.store, (key,))[key]
        if row is None:
            raise Problem(403, "WORKER_NATIVE_DISABLED", "persisted native prerequisites are missing")
        proof = self._row({key: row}, key, WorkerReadiness)
        verified = _check(self.adapter.verify_readiness(proof.evidence_id), WorkerReadiness)
        evidence_key = worker_key("readiness-evidence", proof.evidence_id)
        retained = authority_snapshot(self.store, (evidence_key,))[evidence_key]
        if (
            proof != verified or proof.configuration_digest != self.configuration_digest()
            or proof.control_binding != self.contract.control_binding
            or proof.protection_version != self.contract.protection_version
            or (proof.executor_id, proof.executor_version) != (self.contract.executor_id, self.contract.executor_version)
            or not proof.observed_at <= self._now() < proof.expires_at
            or retained is None or retained.payload["binding"] != self.contract.control_binding
            or _parse(retained.payload["data"], WorkerReadiness) != proof
            or _parse(retained.payload["material"], InlineMaterial) != _material(proof.model_dump(mode="json"))
        ):
            raise Problem(403, "WORKER_NATIVE_DISABLED", "native executor/protection/supervisor evidence differs")

    def publish_readiness(self, evidence_id: str, *, supersedes: str | None = None) -> Record:
        """Verify an independent native witness, then retain it and advance its pointer.

        This is a trusted deployment-control Interface, never an HTTP model-body
        endpoint. Renewals use new evidence IDs; old evidence is never overwritten.
        It does not acquire native evidence or start a supervisor/executor.
        After a drained contract migration, supersedes names the exact retained
        predecessor. The already-migrated pool is an atomic replacement predicate.
        """
        if self.mode != "native" or self.adapter is None or self.adapter.verify_readiness is None:
            raise Problem(403, "WORKER_NATIVE_DISABLED", "native deployment verifier is missing")
        proof = _check(self.adapter.verify_readiness(evidence_id), WorkerReadiness)
        if (
            proof.evidence_id != evidence_id or proof.configuration_digest != self.configuration_digest()
            or proof.control_binding != self.contract.control_binding
            or proof.protection_version != self.contract.protection_version
            or (proof.executor_id, proof.executor_version) != (self.contract.executor_id, self.contract.executor_version)
        ):
            raise Problem(403, "WORKER_NATIVE_DISABLED", "independent native readiness binding differs")
        key = worker_key("readiness", self.contract.workspace_id)
        pool_key = worker_key("pool", self.contract.workspace_id)
        prior_key = worker_key("readiness-evidence", evidence_id if supersedes is None else supersedes)
        request = (
            self._request("publish-readiness", (self.contract.workspace_id, evidence_id), proof.model_dump(mode="json"), (key,))
            if supersedes is None else
            self._request("supersede-readiness", (self.contract.workspace_id, evidence_id, supersedes),
                          proof.model_dump(mode="json"), (key, pool_key, prior_key))
        )

        def decide(rows):
            if not proof.observed_at <= self._now() < proof.expires_at:
                raise Problem(403, "WORKER_NATIVE_DISABLED", "native readiness witness has expired")
            if supersedes is not None:
                pool = self._pool(rows)
                if pool.pending or pool.completions or any(
                    slot.binding is not None or slot.lease_until is not None for slot in pool.slots
                ):
                    raise Problem(409, "WORKER_MIGRATION_NOT_DRAINED", "readiness replacement requires the migrated empty pool")
                pointer, retained = rows[key], rows[prior_key]
                if any(row is None or row.payload["binding"] != self.contract.control_binding for row in (pointer, retained)):
                    raise Problem(409, "WORKER_READINESS_PREDECESSOR_INVALID", "exact retained predecessor is missing")
                prior = _parse(pointer.payload["data"], WorkerReadiness)
                if (
                    prior.evidence_id != supersedes or prior.control_binding != self.contract.control_binding
                    or prior.configuration_digest == proof.configuration_digest
                    or _parse(retained.payload["data"], WorkerReadiness) != prior
                    or _parse(retained.payload["material"], InlineMaterial) != _material(prior.model_dump(mode="json"))
                ):
                    raise Problem(409, "WORKER_READINESS_PREDECESSOR_INVALID", "readiness predecessor binding or material differs")
                if prior.observed_at >= proof.observed_at:
                    raise Problem(409, "WORKER_READINESS_STALE", "readiness cannot move backward")
            elif rows[key] is not None:
                prior = self._row(rows, key, WorkerReadiness)
                if prior.observed_at >= proof.observed_at:
                    raise Problem(409, "WORKER_READINESS_STALE", "readiness cannot move backward")
            return ControlChanges(writes=(
                self._write(key, proof, rows),
                self._write(worker_key("readiness-evidence", evidence_id), proof,
                            material=_material(proof.model_dump(mode="json"))),
            ), result=self._result(readiness_evidence_id=evidence_id))

        return self.store.transact(request, decide)

    def _row(self, rows, key, model):
        row = rows[key]
        if row is None or row.payload["binding"] != self.contract.control_binding:
            raise Problem(409, "WORKER_RECORD_MISSING", "bound worker authority record is missing")
        value = _parse(row.payload["data"], model)
        if isinstance(value, (_Pool, JobRecord, WorkerReadiness)) and value.configuration_digest != self.configuration_digest():
            raise Problem(409, "WORKER_CONFIGURATION_MISMATCH", "workspace worker contract differs")
        return value

    def _write(self, key, value, rows=None, material=None):
        prior = None if rows is None else rows.get(key)
        if prior is not None and material is None and prior.payload["material"] is not None:
            material = _parse(prior.payload["material"], InlineMaterial)
        return ControlWrite(
            row_id=key, mode="create" if prior is None else "replace",
            data=value.model_dump(mode="json"), material=material,
        )

    def _request(self, action, ids, body, reads=()):
        return ControlRequest(
            request_id=worker_key(action, *ids),
            request_digest=_digest([action, body, self.configuration_digest()]),
            read_set=tuple(sorted(set(reads))),
        )

    def _result(self, **values):
        return {"actual_environment": "NOT_RUN", **values}

    def _owner_keys(self, context):
        return (control_key("owner", context.owner_record_id), control_key("policy", context.policy_id))

    def _owner(self, rows, context, *, policy=True):
        owner = self._row(rows, control_key("owner", context.owner_record_id), OwnerRecord)
        if not owner.active or any(getattr(owner, name) != getattr(context, name) for name in ExecutionContext.model_fields):
            raise Problem(403, "WORKER_OWNER_FENCED", "current owner/epoch differs")
        if policy:
            current = self._row(rows, control_key("policy", context.policy_id), PolicyRecord)
            if current.policy_id != context.policy_id or not current.active:
                raise Problem(403, "WORKER_POLICY_DENIED", "current execution policy is inactive")

    def _scope(self, job, context, *, exact=False):
        names = ExecutionContext.model_fields if exact else tuple(name for name in ExecutionContext.model_fields if name != "epoch")
        if any(getattr(job.owner, name) != getattr(context, name) for name in names):
            raise Problem(403, "WORKER_JOB_FENCED", "job owner/scope differs")

    def instruction(self,job_id):
        record=None if self.work_policy is None or job_id is None else self.work_policy._job_record(job_id)
        return record is not None and record.profile.instruction is not None

    def pool_key(self,job_id=None):
        return worker_key("pool",self.contract.workspace_id,*((job_id,) if self.instruction(job_id) else ()))

    def _pool(self, rows):
        keys=[key for key,row in rows.items() if row is not None and row.payload["data"].get("kind")=="worker_pool"]
        if len(keys)!=1:
            raise Problem(409,"WORKER_POOL_INVALID","one exact work or legacy pool is required")
        pool = self._row(rows,keys[0],_Pool)
        capacity=1 if pool.job_id is not None else self.contract.capacity
        if (
            keys[0]!=self.pool_key(pool.job_id)
            or pool.workspace_id != self.contract.workspace_id or len(pool.slots) != capacity
            or tuple(slot.number for slot in pool.slots) != tuple(range(capacity))
            or len(set(pool.pending)) != len(pool.pending)
            or len(set((*pool.pending, *pool.completions))) != len(pool.pending) + len(pool.completions)
        ):
            raise Problem(409, "WORKER_POOL_INVALID", "finite workspace slot ledger is inconsistent")
        return pool

    def initialize(self) -> Record:
        self._ready()
        key = worker_key("pool", self.contract.workspace_id)
        request = self._request("initialize", (self.contract.workspace_id,), {}, (key,))

        def decide(rows):
            if rows[key] is not None:
                self._pool(rows)
                return ControlChanges(result=self._result(initialized=True))
            pool = _Pool(
                configuration_digest=self.configuration_digest(), workspace_id=self.contract.workspace_id,
                slots=tuple(Slot(number=number) for number in range(self.contract.capacity)),
            )
            return ControlChanges(writes=(self._write(key, pool),), result=self._result(initialized=True))

        return self.store.transact(request, decide)

    def inspect(self, job_id: str) -> JobRecord:
        key = worker_key("job", job_id)
        return self._row(authority_snapshot(self.store, (key,)), key, JobRecord)

    def pending(self) -> tuple[str, ...]:
        key = worker_key("pool", self.contract.workspace_id)
        return (*self._pool(authority_snapshot(self.store,(key,))).pending,*self._instruction_jobs("pending"))

    def completions(self) -> tuple[str, ...]:
        key = worker_key("pool", self.contract.workspace_id)
        return (*self._pool(authority_snapshot(self.store,(key,))).completions,*self._instruction_jobs("completions"))

    def _instruction_jobs(self,field):
        values=[]
        if self.work_policy is not None:
            for record in self.work_policy.records.values():
                if record.profile.instruction is None:continue
                key=self.pool_key(record.binding.job_id)
                rows=authority_snapshot(self.store,(key,))
                if rows[key] is not None:values.extend(getattr(self._pool(rows),field))
        return tuple(values)

    def enqueue(self, spec: JobSpec, context: ExecutionContext) -> Record:
        spec, context = _check(spec, JobSpec), _check(context, ExecutionContext)
        self._ready(spec.job_id)
        instruction=self.instruction(spec.job_id)
        pool_key, job_key = self.pool_key(spec.job_id), worker_key("job", spec.job_id)
        reads = [pool_key, job_key, *self._owner_keys(context)]
        if spec.parent_job_id:
            reads.append(worker_key("job", spec.parent_job_id))
        if spec.obligation_id:
            reads.append(control_key("delivery-obligation", spec.obligation_id))
            ob_key = control_key("delivery-obligation", spec.obligation_id)
            admitted = self._row(authority_snapshot(self.store, (ob_key,)), ob_key, _Obligation)
            intent_key = control_key("delivery-intent", control_key("delivery-intent-id", admitted.event_id))
            reads.append(intent_key)
        request = self._request("enqueue", (spec.job_id,), [spec.model_dump(mode="json"), context.model_dump(mode="json")], reads)

        def decide(rows):
            self._owner(rows, context)
            pool = (_Pool(configuration_digest=self.configuration_digest(),workspace_id=self.contract.workspace_id,
                          job_id=spec.job_id,slots=(Slot(number=0),))
                    if instruction and rows[pool_key] is None else self._pool(rows))
            if len(pool.pending) + len(pool.completions) >= self.contract.pending_limit:
                raise Problem(429, "WORKER_ADMISSION_FULL", "finite pending reservations are exhausted")
            writes = []
            if spec.parent_job_id:
                parent_key = worker_key("job", spec.parent_job_id)
                parent = self._row(rows, parent_key, JobRecord)
                self._scope(parent, context, exact=True)
                if parent.state not in {"running", "starting", "waiting_dependency"} or parent.cancel_requested:
                    raise Problem(409, "WORKER_PARENT_UNAVAILABLE", "parent cannot accept another child")
                if spec.job_id == spec.parent_job_id or spec.parent_job_id in pool.pending and len(parent.children) >= 16:
                    raise Problem(429, "WORKER_CHILD_LIMIT", "bounded parent children are exhausted")
                writes.append(self._write(parent_key, _update(parent, children=(*parent.children, spec.job_id)), rows))
            if spec.obligation_id:
                ob = self._row(rows, control_key("delivery-obligation", spec.obligation_id), _Obligation)
                intent = self._row(rows, intent_key, _Intent)
                if (ob.owner_record_id, ob.conversation_id, ob.root_budget_id, ob.work_id, ob.fulfillment) != (
                    context.owner_record_id, context.conversation_id, context.root_budget_id, spec.work_id, "pending",
                ) or (
                    intent.obligation_id != ob.obligation_id or intent.action != "process_event"
                    or intent.state_row_id != control_key("delivery-work-state", ob.obligation_id)
                    or intent.configuration_digest != ob.configuration_digest
                ):
                    raise Problem(403, "WORKER_INTENT_DENIED", "admission work/owner association differs")
                # Create-only identity prevents a new job ID from rerunning one admission.
                writes.append(ControlWrite(
                    row_id=worker_key("admission", spec.obligation_id), mode="create",
                    data={"job_id": spec.job_id, "intent_id": control_key("delivery-intent-id", ob.event_id),
                          "enqueue_request": request.model_dump(mode="json")},
                ))
            job = JobRecord(
                configuration_digest=self.configuration_digest(), spec=spec, owner=context,
                state="queued", control_remaining=None if instruction else self.contract.control_units,
            )
            writes.extend((
                self._write(job_key, job, material=InlineMaterial(data=spec.input, sha256=hashlib.sha256(spec.input).hexdigest())),
                self._write(pool_key, _update(pool, pending=(*pool.pending, spec.job_id)), rows),
            ))
            return ControlChanges(writes=tuple(writes), result=self._result(job_id=spec.job_id, status="queued"))

        return self.store.transact(request, decide)

    def start(self, job_id: str, context: ExecutionContext) -> Record:
        self._ready(job_id)
        instruction=self.instruction(job_id)
        context = _check(context, ExecutionContext)
        job = self.inspect(job_id)
        if job.state not in {"queued", "waiting_dependency"}:
            # A restarted consumer observes the ORIGINAL binding, never starts anew.
            return self.reconcile(job_id, context)
        generation = job.generation + 1
        policy_observation = None if self.work_policy is None else self.work_policy.start_observation(job_id)
        start_id = worker_key("start", job_id, str(generation))
        binding = ExecutorBinding(
            job_id=job_id, work_id=job.spec.work_id, generation=generation, owner=context,
            executor_id=self.contract.executor_id, executor_version=self.contract.executor_version,
            input_digest=hashlib.sha256(job.spec.input).hexdigest(),
            checkpoint_digest=None if job.checkpoint is None else hashlib.sha256(job.checkpoint).hexdigest(),
            start_request_id=start_id,
        )
        pool_key, job_key, budget_key = (
            self.pool_key(job_id), worker_key("job", job_id), control_key("budget", context.root_budget_id),
        )
        request = self._request("start", (job_id, str(generation)), binding.model_dump(mode="json"), (
            pool_key, job_key, budget_key, *self._owner_keys(context),
            *(worker_key("job", child) for child in job.children),
            *(() if self.work_policy is None else self.work_policy.start_keys(job_id)),
        ))
        dispatch = ExecutorStart(binding=binding, request=request, input=job.spec.input, checkpoint=job.checkpoint)
        fresh = False

        def decide(rows):
            nonlocal fresh
            self._owner(rows, context)
            current = self._row(rows, job_key, JobRecord)
            self._scope(current, context, exact=True)
            pool, budget = self._pool(rows), self._row(rows, budget_key, BudgetRecord)
            if (
                current.state not in {"queued", "waiting_dependency"} or current.cancel_requested
                or current.generation + 1 != generation or not instruction and generation > self.contract.max_generations
                or current.spec != job.spec or current.checkpoint != dispatch.checkpoint
                or current.children != job.children
                or _parse(rows[job_key].payload["material"], InlineMaterial).data != dispatch.input
            ):
                raise Problem(409, "WORKER_START_FENCED", "job generation/input/checkpoint is no longer dispatchable")
            if current.state == "waiting_dependency":
                if current.checkpoint is None or any(
                    self._row(rows, worker_key("job", child), JobRecord).state not in TERMINAL_STATES
                    for child in current.children
                ):
                    raise Problem(409, "WORKER_CHILDREN_PENDING", "parent must stay parked until its children complete")
            available = next((slot for slot in pool.slots if slot.binding is None), None)
            if available is None:
                raise Problem(429, "WORKER_CAPACITY_FULL", "all real executor slots remain reserved")
            if (
                budget.root_budget_id != context.root_budget_id or not budget.active
                or not instruction and (budget.remaining_attempts < 1 or budget.remaining_units < self.contract.reservation_units)
            ):
                raise Problem(429, "WORKER_BUDGET_EXHAUSTED", "executor attempt budget is unavailable")
            slots = tuple(
                _update(slot, binding=binding, lease_until=self._now() + timedelta(seconds=self.contract.lease_seconds))
                if slot.number == available.number else slot for slot in pool.slots
            )
            current = _update(
                current, state="starting", generation=generation, binding=binding,
                native_execution_id=None, last_sequence=0, last_evidence_id=None,
                stop_credit=1, queries_reserved=0,
            )
            fresh = True
            extra=() if self.work_policy is None else self.work_policy.start_writes(rows,dispatch,policy_observation)
            return ControlChanges(writes=(
                self._write(job_key, current, rows),
                self._write(pool_key, _update(pool, slots=slots), rows),
                *(() if instruction else (self._write(budget_key, _update(
                    budget, remaining_attempts=budget.remaining_attempts - 1,
                    remaining_units=budget.remaining_units - self.contract.reservation_units,
                ), rows),)),
                self._write(worker_key("attempt", job_id, str(generation)), _Attempt(start=dispatch),
                            material=_material(dispatch.model_dump(mode="json"))),
                *extra,
            ), result=self._result(status="starting", binding=binding.model_dump(mode="json")))

        receipt = (self.store.transact(request,decide) if self.work_policy is None else
                   self.work_policy.start_commit(job_id,policy_observation,lambda:self.store.transact(request,decide)))
        if not fresh:
            return self.reconcile(job_id, context)
        self._ready(job_id)
        if self.work_policy is not None:
            self.work_policy.check_dispatch(job_id,binding)
        try:
            evidence_id = self.adapter.start(dispatch)
        except Exception:
            # Starting remains unknown. Do not leak arbitrary adapter exceptions.
            raise Problem(503, "WORKER_START_UNKNOWN", "original executor start remains reserved; reconcile its binding",
                          request_id=request.request_id, request_digest=request.request_digest) from None
        return receipt if evidence_id is None else self.observe(job_id, evidence_id, context)

    def observe(self, job_id: str, evidence_id: str, context: ExecutionContext) -> Record:
        if self.adapter is None:
            raise Problem(403, "WORKER_DISABLED", "executor evidence verifier is missing")
        context = _check(context, ExecutionContext)
        proof = _check(self.adapter.verify(evidence_id), ExecutorEvidence)
        if proof.evidence_id != evidence_id or proof.binding.job_id != job_id:
            raise Problem(403, "WORKER_EVIDENCE_DENIED", "executor evidence identity differs")
        job_key, pool_key = worker_key("job", job_id), self.pool_key(job_id)
        attempt_key = worker_key("attempt", job_id, str(proof.binding.generation))
        request = self._request("observe", (job_id, evidence_id), proof.model_dump(mode="json"), (
            job_key, pool_key, attempt_key, *self._owner_keys(context),
        ))

        return self.store.transact(request, lambda rows: self._observe_changes(job_id, proof, context, rows))

    def _observe_changes(self, job_id, proof, context, rows):
        """Pure writes shared with the trusted atomic original-stop closure."""
        evidence_id = proof.evidence_id
        job_key, pool_key = worker_key("job", job_id), self.pool_key(job_id)
        attempt_key = worker_key("attempt", job_id, str(proof.binding.generation))
        self._owner(rows, context, policy=False)
        job = self._row(rows, job_key, JobRecord)
        self._scope(job, context)
        attempt = self._row(rows, attempt_key, _Attempt)
        if (
            attempt.start.binding != proof.binding
            or _parse(rows[attempt_key].payload["material"], InlineMaterial) != _material(attempt.start.model_dump(mode="json"))
        ):
            raise Problem(403, "WORKER_EVIDENCE_DENIED", "native attempt/input/checkpoint/owner binding differs")
        material = _material(proof.model_dump(mode="json"))
        writes = [self._write(worker_key("evidence", evidence_id), proof, material=material)]
        late = proof.binding != job.binding or job.state in TERMINAL_STATES or job.state == "waiting_dependency"
        if late or proof.sequence <= job.last_sequence:
            return ControlChanges(writes=tuple(writes), result=self._result(status="late_evidence_retained"))
        if job.native_execution_id is not None and proof.native_execution_id not in {None, job.native_execution_id}:
            raise Problem(403, "WORKER_NATIVE_BINDING_DENIED", "native executor identity changed")
        pool = self._pool(rows)
        occupied = [slot for slot in pool.slots if slot.binding == proof.binding]
        if len(occupied) != 1:
            raise Problem(409, "WORKER_SLOT_INVALID", "original executor permit is not uniquely reserved")
        idle = proof.state in {"parked", "succeeded", "failed", "stopped"}
        updates = {
            "native_execution_id": proof.native_execution_id or job.native_execution_id,
            "last_evidence_id": evidence_id, "last_sequence": proof.sequence,
        }
        if idle:
            if proof.state == "parked":
                assistant_checkpoint = False
                if not job.children:
                    from .behavior_assistant_wire import Checkpoint, Task
                    try:
                        task = Task.model_validate_json(job.spec.input)
                        checkpoint = Checkpoint.model_validate_json(proof.checkpoint)
                        assistant_checkpoint = (
                            task.job_id == job.spec.job_id and task.work_id == job.spec.work_id
                            and checkpoint.task_digest == _digest(task.model_dump(mode="json"))
                        )
                    except ValueError:
                        pass
                if not (job.children or assistant_checkpoint) or job.control_remaining==0:
                    raise Problem(409, "WORKER_PARK_DENIED", "yield requires registered children and a control reservation")
                updates.update(state="waiting_dependency", checkpoint=proof.checkpoint,
                               control_remaining=None if job.control_remaining is None else job.control_remaining-1)
            else:
                if not job.completion_credit:
                    raise Problem(409, "WORKER_COMPLETION_EXHAUSTED", "completion reservation is missing")
                updates.update(
                    state="cancelled" if proof.state == "stopped" else proof.state, completion_credit=0,
                )
                writes.append(self._write(worker_key("completion", job_id, str(job.generation)), proof, material=material))
                if proof.result is not None:
                    writes.append(self._write(
                        worker_key("work-result", proof.result.evidence_id), proof.result,
                        material=_material(proof.result.model_dump(mode="json")),
                    ))
            slots = tuple(_update(slot, binding=None, lease_until=None) if slot.binding == proof.binding else slot for slot in pool.slots)
            pending = pool.pending if proof.state == "parked" else tuple(value for value in pool.pending if value != job_id)
            completions = (
                (*pool.completions, job_id)
                if proof.state != "parked" and job.spec.obligation_id is not None else pool.completions
            )
            writes.append(self._write(pool_key, _update(pool, slots=slots, pending=pending, completions=completions), rows))
        else:
            if job.control_remaining==0:
                raise Problem(429, "WORKER_CONTROL_EXHAUSTED", "nonterminal observation budget is exhausted; slot stays reserved")
            updates.update(
                state="running" if proof.state == "running" else "capacity_uncertain",
                control_remaining=None if job.control_remaining is None else job.control_remaining-1,
            )
        job = _update(job, **updates)
        writes.append(self._write(job_key, job, rows))
        return ControlChanges(writes=tuple(writes), result=self._result(
            status=job.state, job_id=job_id, cancel_requested=job.cancel_requested,
            completion_evidence_id=evidence_id if job.state in TERMINAL_STATES else None,
        ))

    def reconcile(self, job_id: str, context: ExecutionContext) -> Record:
        if self.adapter is None:
            raise Problem(403, "WORKER_DISABLED", "executor query adapter is missing")
        context = _check(context, ExecutionContext)
        job = self.inspect(job_id)
        self._scope(job, context)
        if job.binding is None:
            raise Problem(409, "WORKER_NOT_STARTED", "there is no executor attempt to reconcile")
        job_key = worker_key("job", job_id)
        request = self._request("query", (job_id, str(job.generation), str(job.queries_reserved)), [
            job.binding.model_dump(mode="json"), job.queries_reserved,
        ], (job_key, *self._owner_keys(context)))
        fresh = False

        def decide(rows):
            nonlocal fresh
            self._owner(rows, context, policy=False)
            current = self._row(rows, job_key, JobRecord)
            self._scope(current, context)
            if current.binding != job.binding or current.queries_reserved != job.queries_reserved:
                raise Problem(409, "WORKER_QUERY_FENCED", "original query generation/reservation differs")
            if not self.instruction(job_id) and current.queries_reserved >= self.contract.queries_per_generation:
                raise Problem(429, "WORKER_QUERY_EXHAUSTED", "bounded query budget exhausted; completion/cancel lanes remain")
            fresh = True
            return ControlChanges(writes=(self._write(
                job_key, _update(current, queries_reserved=current.queries_reserved + 1), rows,
            ),), result=self._result(query_binding=job.binding.model_dump(mode="json")))

        self.store.transact(request, decide)
        # BehaviorStore replays before decide; conflicts/unknown commits raise
        # rather than returning the winning caller's receipt after our decision.
        if not fresh:
            raise Problem(409, "WORKER_QUERY_REPLAY", "original query reservation cannot dispatch again; executor remains reserved",
                          request_id=request.request_id, request_digest=request.request_digest)
        try:
            evidence_id = self.adapter.query(job.binding)
        except Exception:
            raise Problem(503, "WORKER_QUERY_UNKNOWN", "original executor remains reserved") from None
        return self.observe(job_id, evidence_id, context)

    def _cancellation_request(self, job_id, context, expected_binding):
        return self._request(
            "cancel" if expected_binding is None else "cancel-bound",
            (job_id,) if expected_binding is None else (job_id, expected_binding.start_request_id),
            [job_id, context.model_dump(mode="json")] if expected_binding is None
            else [job_id, context.model_dump(mode="json"), expected_binding.model_dump(mode="json")], (
            worker_key("job", job_id), self.pool_key(job_id), *self._owner_keys(context),
        ))

    def read_cancellation_receipt(self, job_id: str, context: ExecutionContext, generation: int) -> Record:
        context = _check(context, ExecutionContext)
        if type(generation) is not int or not 0 <= generation <= 8:
            raise Problem(422, "CANCELLATION_RECEIPT_INVALID", "a supported original generation is required")
        job = self.inspect(job_id)
        self._scope(job, context)
        self._owner(self.store.read_snapshot(self._owner_keys(context)), context, policy=False)
        if (job.generation != generation or (job.binding is None) != (generation == 0)
                or job.binding is not None and job.binding.generation != generation):
            raise Problem(409, "CANCELLATION_RECEIPT_CHANGED", "the original execution generation differs")
        request = self._cancellation_request(job_id, context, job.binding)
        try:
            return self.store.reconcile(request)
        except Problem as error:
            code = error.body["error"]["code"]
            if code == "BEHAVIOR_COMMIT_UNKNOWN":
                raise Problem(503, "CANCELLATION_RECEIPT_UNKNOWN", "the original cancellation receipt remains unknown") from None
            if code == "BEHAVIOR_REQUEST_CONFLICT":
                raise Problem(409, "CANCELLATION_RECEIPT_CHANGED", "the original cancellation request differs") from None
            raise

    def cancel(self, job_id: str, context: ExecutionContext, *, expected_binding: ExecutorBinding | None = None) -> Record:
        context = _check(context, ExecutionContext)
        if expected_binding is not None:
            expected_binding = _check(expected_binding, ExecutorBinding)
        job_key, pool_key = worker_key("job", job_id), self.pool_key(job_id)
        request = self._cancellation_request(job_id, context, expected_binding)

        def decide(rows):
            self._owner(rows, context, policy=False)
            job, pool = self._row(rows, job_key, JobRecord), self._pool(rows)
            self._scope(job, context)
            if expected_binding is not None and job.binding != expected_binding:
                raise Problem(409, "WORKER_CANCEL_FENCED", "the approved original execution binding changed")
            if job.state in TERMINAL_STATES:
                return ControlChanges(result=self._result(status=job.state, cancel_accepted=False))
            if not job.cancel_credit:
                raise Problem(409, "WORKER_CANCEL_RESERVED", "cancellation has already been accepted")
            inactive = job.state in {"queued", "waiting_dependency"}
            if inactive and job.spec.obligation_id is not None:
                job, pool, completion = self._inactive_cancellation(job, pool)
            else:
                completion = ()
            job = _update(job, cancel_requested=True, cancel_credit=0, **({"state": "cancelled"} if inactive else {}))
            writes = [self._write(job_key, job, rows)]
            if inactive:
                writes.append(self._write(pool_key, _update(pool, pending=tuple(value for value in pool.pending if value != job_id)), rows))
            return ControlChanges(writes=(*writes, *completion), result=self._result(
                status=job.state, cancel_accepted=True, executor_stopped=inactive,
            ))

        return self.store.transact(request, decide)

    def _inactive_cancellation(self, job, pool):
        if (
            job.spec.obligation_id is None or not job.completion_credit or job.result_forwarded
            or job.spec.job_id in pool.completions
            or any(slot.binding is not None and slot.binding.job_id == job.spec.job_id for slot in pool.slots)
            or job.binding is None and (job.generation != 0 or job.native_execution_id is not None)
            or job.binding is not None and (job.checkpoint is None or job.last_evidence_id is None)
        ):
            raise Problem(409, "WORKER_CANCELLATION_UNVERIFIED", "cancellation requires an unused completion and no active executor")
        key = worker_key("completion", job.spec.job_id, str(job.generation))
        evidence = _InactiveCancellation(
            evidence_id=worker_key("inactive-cancel", job.spec.job_id, str(job.generation)),
            spec=job.spec, owner=job.owner, generation=job.generation, binding=job.binding,
            prior_evidence_id=job.last_evidence_id, children=job.children,
        )
        updated_pool = _update(
            pool, pending=tuple(value for value in pool.pending if value != job.spec.job_id),
            completions=(*pool.completions, job.spec.job_id),
        )
        if len(updated_pool.pending) + len(updated_pool.completions) > self.contract.pending_limit:
            raise Problem(429, "WORKER_ADMISSION_FULL", "the retained completion must fit its original reservation")
        return (
            _update(job, completion_credit=0, last_evidence_id=evidence.evidence_id), updated_pool,
            (self._write(key, evidence, material=_material(evidence.model_dump(mode="json"))),),
        )

    def reconcile_inactive_cancellation(self, job_id: str, context: ExecutionContext) -> Record:
        """Complete a legacy cancelled admission; never start, stop or re-run work."""
        context = _check(context, ExecutionContext)
        job_key, pool_key = worker_key("job", job_id), self.pool_key(job_id)
        request = self._request("complete-inactive-cancel", (job_id,), context.model_dump(mode="json"),
                                (job_key, pool_key, *self._owner_keys(context)))

        def decide(rows):
            self._owner(rows, context, policy=False)
            job, pool = self._row(rows, job_key, JobRecord), self._pool(rows)
            self._scope(job, context)
            if job.state != "cancelled" or not job.cancel_requested or job.cancel_credit:
                raise Problem(409, "WORKER_CANCELLATION_UNVERIFIED", "an accepted inactive cancellation is required")
            job, pool, completion = self._inactive_cancellation(job, pool)
            return ControlChanges(writes=(
                self._write(job_key, job, rows), self._write(pool_key, pool, rows), *completion,
            ), result=self._result(status="cancelled", completion_retained=True, executor_started=False))

        return self.store.transact(request, decide)

    def stop(self, job_id: str, context: ExecutionContext) -> Record:
        if self.adapter is None:
            raise Problem(403, "WORKER_DISABLED", "executor stop adapter is missing")
        context = _check(context, ExecutionContext)
        job = self.inspect(job_id)
        if job.binding is None or job.state in TERMINAL_STATES or job.state == "waiting_dependency":
            raise Problem(409, "WORKER_NO_ACTIVE_EXECUTOR", "job has no executor requiring a stop")
        job_key = worker_key("job", job_id)
        request = self._request("stop", (job_id, str(job.generation)), job.binding.model_dump(mode="json"), (
            job_key, *self._owner_keys(context),
        ))
        command = ExecutorStop(binding=job.binding, request=request, native_execution_id=job.native_execution_id)
        fresh = False

        def decide(rows):
            nonlocal fresh
            self._owner(rows, context, policy=False)
            current = self._row(rows, job_key, JobRecord)
            self._scope(current, context)
            if (
                not current.cancel_requested or not current.stop_credit or current.binding != command.binding
                or current.native_execution_id != command.native_execution_id
            ):
                raise Problem(409, "WORKER_STOP_FENCED", "original cancellation/stop reservation differs")
            fresh = True
            return ControlChanges(writes=(
                self._write(job_key, _update(current, state="capacity_uncertain", stop_credit=0), rows),
                self._write(worker_key("stop-intent", job_id, str(job.generation)), command,
                            material=_material(command.model_dump(mode="json"))),
            ), result=self._result(status="capacity_uncertain", request_id=request.request_id))

        receipt = self.store.transact(request, decide)
        if not fresh:
            return self.reconcile(job_id, context)
        try:
            evidence_id = self.adapter.stop(command)
        except Exception:
            raise Problem(503, "WORKER_STOP_UNKNOWN", "cancel is accepted; original executor may still be active") from None
        return receipt if evidence_id is None else self.observe(job_id, evidence_id, context)

    def adopt(self, job_id: str, context: ExecutionContext) -> Record:
        """Only already queued or verifiably parked jobs can change owner epoch."""
        context = _check(context, ExecutionContext)
        key = worker_key("job", job_id)
        request = self._request("adopt", (job_id, str(context.epoch)), context.model_dump(mode="json"), (key, *self._owner_keys(context)))

        def decide(rows):
            self._owner(rows, context)
            job = self._row(rows, key, JobRecord)
            self._scope(job, context)
            if job.state not in {"queued", "waiting_dependency"} or context.epoch <= job.owner.epoch:
                raise Problem(409, "WORKER_TAKEOVER_UNCERTAIN", "a stale lease is not proof the old executor stopped")
            return ControlChanges(writes=(self._write(key, _update(job, owner=context), rows),),
                                  result=self._result(status=job.state, owner_epoch=context.epoch))

        return self.store.transact(request, decide)

    def verify_work_result(self, evidence_id: str) -> VerifiedWorkResult:
        key = worker_key("work-result", evidence_id)
        row = authority_snapshot(self.store, (key,))[key]
        result = self._row({key: row}, key, VerifiedWorkResult)
        if result.evidence_id != evidence_id or _parse(row.payload["material"], InlineMaterial) != _material(result.model_dump(mode="json")):
            raise Problem(403, "WORKER_RESULT_DENIED", "saved verified work evidence differs")
        return result

    def _operation_effects(self, rows, operation_ids, context):
        known, unresolved = [], []
        for operation_id in operation_ids:
            key = control_key("operation", operation_id)
            row = rows[key]
            if row is None:
                continue
            if row.payload["data"].get("kind") != "operation":
                unresolved.append(operation_id)
                continue
            operation = self._row(rows, key, _Operation)
            if (operation.operation_id != operation_id or operation.owner_record_id != context.owner_record_id
                    or operation.conversation_id != context.conversation_id or operation.root_budget_id != context.root_budget_id):
                raise Problem(403, "WORKER_OPERATION_FENCED", "retained operation belongs to a different scope")
            material = _parse(row.payload["material"], InlineMaterial)
            if material.sha256 != operation.description_digest:
                raise Problem(409, "WORKER_OPERATION_FENCED", "retained operation description differs")
            try:
                OperationDescription.model_validate_json(material.data)
            except (ValueError, TypeError):
                raise Problem(409, "WORKER_OPERATION_FENCED", "retained operation description is invalid") from None
            if (operation.current_attempt_id is None and not operation.unsettled_attempt_ids
                    and operation.effect_state == "unknown" and operation.business_state == "pending"
                    and operation.successful_result is None):
                continue
            if operation.effect_state == "applied":
                known.append(operation_id)
            if not (operation.effect_state == "applied" and operation.business_state == "succeeded"
                    and operation.current_attempt_id is not None and not operation.unsettled_attempt_ids
                    and operation.successful_result is not None and operation.successful_result.status == "completed"):
                unresolved.append(operation_id)
        return tuple(known), tuple(unresolved)

    def forward_result(self, job_id: str, context: ExecutionContext, delivery: DeliveryCore) -> Record:
        """Consume the durable completion inbox using saved verified bytes only.

        Wire RuntimeRegistration.verify_work_result to verify_work_result (or an
        equally scoped server verifier). No business callback is invoked here.
        A committed forwarding plan survives either process disappearing.
        """
        context = _check(context, ExecutionContext)
        if delivery.contract is None or delivery.contract.control_binding != self.contract.control_binding:
            raise Problem(403, "WORKER_DELIVERY_AUTHORITY", "delivery must use the same control authority")
        job = self.inspect(job_id)
        self._scope(job, context)
        if job.state not in TERMINAL_STATES or job.spec.obligation_id is None:
            raise Problem(409, "WORKER_RESULT_PENDING", "no terminal admission result is ready to forward")
        job_key, pool_key = worker_key("job", job_id), self.pool_key(job_id)
        completion_key = worker_key("completion", job_id, str(job.generation))
        plan_key = worker_key("result-forward", job_id)
        retained = self.store.read_snapshot((completion_key,))[completion_key]
        inactive = retained is not None and retained.payload["data"].get("kind") == "worker_inactive_cancel"
        stopped_without_result = (
            retained is not None and not inactive and retained.payload["data"].get("state") == "stopped"
            and retained.payload["data"].get("result") is None
        )
        reads = (job_key, pool_key, completion_key, plan_key, *self._owner_keys(context))
        operation_ids = ()
        if inactive or stopped_without_result:
            owner_key = control_key("owner", context.owner_record_id)
            owner = self._row(self.store.read_snapshot((owner_key,)), owner_key, OwnerRecord)
            operation_ids = owner.operation_ids
            reads += (control_key("delivery-obligation", job.spec.obligation_id),
                      control_key("delivery-work-state", job.spec.obligation_id),
                      control_key("delivery-conversation", context.conversation_id),
                      *(control_key("operation", operation_id) for operation_id in operation_ids))
        plan_request = self._request("prepare-forward", (job_id,), [job_id, delivery.configuration_digest()], reads)

        def prepare(rows):
            self._owner(rows, context, policy=False)
            current = self._row(rows, job_key, JobRecord)
            self._scope(current, context)
            pool = self._pool(rows)
            proof = self._row(rows, completion_key, _InactiveCancellation if inactive else ExecutorEvidence)
            if (
                current.binding != proof.binding or job_id not in pool.completions
                or current.last_evidence_id != proof.evidence_id
                or _parse(rows[completion_key].payload["material"], InlineMaterial) != _material(proof.model_dump(mode="json"))
            ):
                raise Problem(409, "WORKER_RESULT_PENDING", "verified retained work result is unavailable")
            if inactive or stopped_without_result:
                if current.state != "cancelled" or not current.cancel_requested:
                    raise Problem(403, "WORKER_CANCELLATION_UNVERIFIED", "an accepted verified cancellation is required")
                if inactive and (proof.spec != current.spec or proof.owner != current.owner
                                 or proof.generation != current.generation):
                    raise Problem(403, "WORKER_CANCELLATION_UNVERIFIED", "retained inactive cancellation binding differs")
                if stopped_without_result and (proof.state != "stopped" or proof.result is not None):
                    raise Problem(403, "WORKER_CANCELLATION_UNVERIFIED", "the retained full-tree stop proof differs")
                obligation = delivery._obligation(rows, job.spec.obligation_id, context)
                work = delivery._work(rows, obligation)
                if work.definition.work_id != current.spec.work_id or work.state != "queued":
                    raise Problem(409, "WORKER_RESULT_PENDING", "the admitted work is not awaiting its cancellation result")
                if self._row(rows, control_key("owner", context.owner_record_id), OwnerRecord).operation_ids != operation_ids:
                    raise Problem(409, "WORKER_CANCELLATION_UNVERIFIED", "the operation scope changed during cancellation reconciliation")
                known, pending_operations = self._operation_effects(rows, operation_ids, context)
                unresolved = tuple(dict.fromkeys((
                    *pending_operations,
                    *(proof.children if inactive else current.children),
                )))
                if len(unresolved) > 16 or len(known) > 16:
                    raise Problem(409, "WORKER_CANCELLATION_RECONCILIATION_REQUIRED", "reconcile existing operation/child records before bounded cancellation delivery")
                result = VerifiedWorkResult(
                    evidence_id=(proof.evidence_id if inactive else worker_key("stopped-cancel", proof.evidence_id)),
                    verifier_version=delivery.contract.verifier_version,
                    work_id=work.definition.work_id, definition_digest=work.definition_digest,
                    state="cancelled", passed_checks=(), known_effects=known,
                    unresolved_operations=unresolved,
                    content=(b"Job cancelled with no executor slot held. Prior work and child work are not rolled back."
                             if inactive else
                             b"Job cancelled after verified executor fencing. Existing operation and child records require separate reconciliation; effects are not rolled back."),
                    proof=_material({
                        "cancellation": proof.model_dump(mode="json"), "operation_ids_checked": operation_ids,
                        "known_effects": known, "unreconciled_records": unresolved,
                    }).data,
                )
            else:
                result = proof.result
                if result is None:
                    raise Problem(409, "WORKER_RESULT_PENDING", "verified retained work result is unavailable")
            command = WorkResultRequest(obligation_id=job.spec.obligation_id, evidence_id=result.evidence_id)
            plan = _ForwardPlan(
                job_id=job_id, context=context, command=command,
                request=delivery.request_for(command, context),
                delivery_configuration_digest=delivery.configuration_digest(),
            )
            writes = (self._write(plan_key, plan, material=_material(plan.model_dump(mode="json"))),)
            if inactive or stopped_without_result:
                writes += (self._write(worker_key("work-result", result.evidence_id), result,
                                       material=_material(result.model_dump(mode="json"))),)
            return ControlChanges(writes=writes,
                                  result=self._result(plan_key=plan_key))

        self.store.transact(plan_request, prepare)
        rows = authority_snapshot(self.store, reads)
        self._owner(rows, context, policy=False)
        plan = self._row(rows, plan_key, _ForwardPlan)
        if (
            plan.context != context or plan.delivery_configuration_digest != delivery.configuration_digest()
            or _parse(rows[plan_key].payload["material"], InlineMaterial) != _material(plan.model_dump(mode="json"))
            or delivery.request_for(plan.command, context) != plan.request
        ):
            raise Problem(403, "WORKER_FORWARD_FENCED", "retained forwarding owner/request differs")
        delivered = delivery.record_work_result(plan.command, context)
        finish_request = self._request("forwarded", (job_id,), plan.model_dump(mode="json"), (
            job_key, pool_key, *self._owner_keys(context),
        ))

        def finish(snapshot):
            self._owner(snapshot, context, policy=False)
            current = self._row(snapshot, job_key, JobRecord)
            pool = self._pool(snapshot)
            self._scope(current, context)
            if job_id not in pool.completions:
                raise Problem(409, "WORKER_FORWARD_FENCED", "completion reservation is missing")
            return ControlChanges(writes=(
                self._write(job_key, _update(current, result_forwarded=True), snapshot),
                self._write(pool_key, _update(pool, completions=tuple(value for value in pool.completions if value != job_id)), snapshot),
            ), result=self._result(work_receipt_request_id=delivered.payload["request"]["request_id"], result_forwarded=True))

        return self.store.transact(finish_request, finish)


class WorkerConsumer:
    """One bounded tick; run from an independently supervised process/timer.

    Resume uses only saved JobRecord/attempt rows. Capacity-full jobs remain
    queued; later ticks progress them after verified completion or parent yield.
    Completion forwarding has its own page (up to limit); query/stop then start
    share another page (up to limit). At most 2*limit items run in one tick.
    """

    def __init__(self, core: WorkerCore, delivery: DeliveryCore | None = None):
        self.core, self.delivery = core, delivery

    def drain(self, resolve_context: Callable[[str], ExecutionContext], *, limit: int = 4) -> tuple[dict, ...]:
        if type(limit) is not int or not 1 <= limit <= 32:
            raise Problem(422, "WORKER_SCAN_LIMIT", "worker tick must be bounded to 1..32")
        results = []
        if self.delivery is not None:
            for job_id in self.core.completions()[:limit]:
                try:
                    job = self.core.inspect(job_id)
                    receipt = self.core.forward_result(job_id, resolve_context(job.owner.owner_record_id), self.delivery)
                    results.append(receipt.payload["result"])
                except Problem as exc:
                    results.append(self.core._result(job_id=job_id, problem=exc.body["error"]["code"]))
        jobs = [self.core.inspect(job_id) for job_id in self.core.pending()]
        pending = {job.spec.job_id for job in jobs}
        jobs = [
            job for job in jobs if job.state != "waiting_dependency"
            or not any(child in pending for child in job.children)
        ]
        jobs.sort(key=lambda job: (
            {"queued": 1, "waiting_dependency": 2}.get(job.state, 0), not job.cancel_requested,
        ))
        for job in jobs[:limit]:
            context = resolve_context(job.owner.owner_record_id)
            try:
                if job.cancel_requested and job.state not in TERMINAL_STATES and job.state != "waiting_dependency":
                    receipt = self.core.stop(job.spec.job_id, context)
                elif job.state in {"queued", "waiting_dependency"}:
                    receipt = self.core.start(job.spec.job_id, context)
                else:
                    receipt = self.core.reconcile(job.spec.job_id, context)
                results.append(receipt.payload["result"])
            except Problem as exc:
                results.append(self.core._result(job_id=job.spec.job_id, problem=exc.body["error"]["code"]))
        return tuple(results)
