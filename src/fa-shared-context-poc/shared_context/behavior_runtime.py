"""Authenticated composition of the delivered behavior modules, not replacements.

All dependencies and bindings are server objects. bootstrap() creates only
bounded runtime policy/index rows in an EXISTING authority; it never creates a
table, role, owner, grant, resource or native-readiness witness. The owner, policy,
budget, delivery quotas/index and worker pool must already be provisioned.
Native mode calls the actual module readiness checks and never synthesizes proof.

Scope-policy changes must update RuntimePolicy AND the referenced PolicyRecord
revision in the SAME BehaviorStore EGT. Out-of-band writers are unsupported.
Every request rechecks both records. Identity, owner/root/adapter selection and
source scope are not HTTP payload fields. A human submission delegates to the
binding's fixed agent owner; it does not become an operation approval.

Command plans retain the original bytes, identity and ControlRequest BEFORE
invocation. A separate one-consume marker means repeated uncertain commits use
read-back only. Missing receipts remain unknown. Plans/index entries are retained
without GC and bounded at 64 per binding. No model repair, arbitrary HTTP/write,
large/unclassified source content or native activation endpoint is exposed.

Default source intake is strict ArtifactMetrics JSON. An explicit server policy
also permits signed-human UTF-8 text with exact-byte retention consent; this does
not approve claims or operations, and is not a universal secret classifier.
Required original bytes are returned whole or rejected; never truncated.
Supervision requires an explicit controller_id at construction. Only the minted
runtime.controller handle can step/drain; no JWT role or HTTP body creates it.
Generic agent consume no longer exposes start, adopt, claim-slot or drain.
Optional registered child jobs are fixed leaf work definitions under one root
binding/owner/budget. HTTP may only select a child ID and command ID. Enqueue
retains the exact WorkerCore EGT request; children never own or fulfill the root
delivery obligation. Controller park stages the native intent then uses the
existing bounded worker query/idle/checkpoint lane. Empty child configuration is
omitted from serialization so deployed binding/configuration digests do not drift.

Native callbacks optionally reuse NativeLaunchAuthority and its EXACT
FoundryExecutor. Only assigned claim/probe/report callbacks are routed. A launch
permit goes exclusively through the trusted native invocation header, is stored
only as a digest, and is consumed atomically against its fixed target/attempt.
Session/process fields are observations to compare, not an MI-derived fence.
The fresh report capability stays in private headers; replay never reissues it.
Fresh native stop/park/exit evidence remains mandatory to release a predecessor.
actual_environment=NOT_RUN describes this implementation's local evidence, not a
deployment probe. The work receiver retains its documented UTC/send-window
contract; strict Table service-commit-time expiry is NOT supported.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError, field_validator, model_serializer, model_validator

from . import behavior_wire as wire
from .behavior_auth import Action, BehaviorPrincipal
from .behavior_delivery import (
    AdmissionRequest, DeliveryBinding, DeliveryConsumer, DeliveryCore, IncomingEvent,
    Route, VerifiedIngress, WorkDefinition, _Obligation, _Outbox,
    admission_digest, obligation_id_for,
)
from .behavior_dispatch import ArtifactMetrics
from .behavior_effects import WorkStateDispatcher
from .behavior_ingress import METRICS_SCHEMA, TEXT_SCHEMA, checked_human_text, require_text_consent
from .behavior_foundry import FoundryExecutor, NativeLaunchAuthority, native_key
from .behavior_inbox import (
    INBOX_CHANNEL_ID, INBOX_VERSION, InboxAcknowledgment, InboxFetch, PullInbox, VerifiedInboxAccess,CurrentWorkInboxAccess,
)
from .behavior_operations import (
    AuthorizationRequest, BudgetRecord, ExecutionContext, GrantRecord, GrantSubject,
    HumanOperationRejection, HumanOperationRejectionIntent,
    OutcomeRequest, OwnerRecord, PolicyRecord, ToolBrokerCore, ToolIntent, _Model, _checked, _digest,
    _json, _parse, _row, _updated, _utc, _write, control_key,
)
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, Digest, InlineMaterial
from .behavior_workers import JobRecord, JobSpec, WorkerConsumer, WorkerCore, worker_key
from .errors import Problem
from .memory_governance import ClaimCandidate, ClaimPlan, ContextVersions, MemoryGovernance
from .memory_projection import NativeMemoryProjection, ProjectionQuery
from .models import Identifier
from .records import Record
from .source_materials import (
    AuthenticatedSourceContext, SourceEvent, SourceInput, SourceMaterials, SourcePlan,
    SourceReference, SourceSnapshot, SourceSnapshotPlan, SourceWithdrawalInput,
)
from .source_reviews import DurableHumanReviewVerifier, HumanReviewCommand, HumanReviewPlan
from .source_backup import SourceBackupPlan, SourceBackupService

RUNTIME_VERSION = "fa-behavior-api-v1"


def runtime_key(kind: str, *ids: str) -> str:
    return control_key("runtime-" + kind, *ids)


def material(value: object) -> InlineMaterial:
    data = _json(value)
    return InlineMaterial(data=data, sha256=hashlib.sha256(data).hexdigest())


def executor_job_input(job_id: str, definition: WorkDefinition) -> bytes:
    return wire.ExecutorJobInput(
        job_id=job_id, work_id=definition.work_id,
        definition_digest=_digest(definition.model_dump(mode="json")), required_checks=definition.required_checks,
    ).model_dump_json().encode()


class RuntimeChildJob(_Model):
    job_id: Identifier
    work: WorkDefinition


class RuntimeBinding(_Model):
    binding_id: Identifier
    tenant_id: Identifier
    team_id: Identifier
    human_oid: Identifier
    agent_oid: Identifier
    owner: ExecutionContext
    ingress_id: Identifier
    source_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]
    claim_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]
    subject: Identifier
    event_id: Identifier
    root_request_id: Identifier
    job_id: Identifier
    work: WorkDefinition
    route: Route
    operation_id: Identifier
    attempt_id: Identifier
    grant_id: Identifier
    allowed_input_refs: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]
    context_versions: ContextVersions
    child_jobs: Annotated[tuple[RuntimeChildJob, ...], Field(max_length=16)] = ()

    @model_serializer(mode="wrap")
    def omit_empty_children(self, handler):
        value = handler(self)
        if not self.child_jobs:
            value.pop("child_jobs", None)
        return value

    @property
    def registered_jobs(self):
        return (RuntimeChildJob(job_id=self.job_id, work=self.work), *self.child_jobs)

    @model_validator(mode="after")
    def exact_bindings(self):
        if (
            self.agent_oid != self.owner.principal_id or self.agent_oid == self.human_oid
            or self.route.tenant_id != self.tenant_id or self.route.recipient_id != self.human_oid
            or self.route.channel_id != INBOX_CHANNEL_ID
            or any(len(set(items)) != len(items) for items in (self.source_ids, self.claim_ids, self.allowed_input_refs))
            or len({job.job_id for job in self.registered_jobs}) != len(self.registered_jobs)
            or len({job.work.work_id for job in self.registered_jobs}) != len(self.registered_jobs)
        ):
            raise ValueError("invalid fixed principal, route or resource bindings")
        return self

    @property
    def obligation_id(self):
        return obligation_id_for(self.event_id)


class RuntimePolicy(_Model):
    binding_digest: Digest
    operation_policy_revision: Annotated[int, Field(ge=0)]
    active: bool
    source_visibility: Literal["team", "private"]
    purpose: Identifier
    allowed_purposes: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=8)]
    max_commands: Annotated[int, Field(ge=1, le=64)] = 64
    max_grant_seconds: Annotated[int, Field(ge=1, le=600)] = 300
    valid_until: datetime | None
    human_text_enabled: bool = False
    text_policy_version: Identifier | None = None

    _time = field_validator("valid_until")(lambda value: None if value is None else _utc(value))

    @model_validator(mode="after")
    def purposes(self):
        if self.purpose not in self.allowed_purposes:
            raise ValueError("default source purpose is not allowed")
        if self.human_text_enabled != (self.text_policy_version is not None):
            raise ValueError("human text intake requires an explicit versioned server policy")
        return self


class _PlanIndex(_Model):
    keys: tuple[Identifier, ...] = ()


class CommandPlan(_Model):
    binding_id: Identifier
    binding_digest: Digest
    kind: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,96}$")]
    request_id: Identifier
    principal_oid: Identifier
    input_digest: Digest
    context: ExecutionContext
    data: dict[str, JsonValue]
    request: ControlRequest


class RuntimeEvidenceAuthority:
    """Durable evidence minted only from an authenticated runtime route."""

    def __init__(self, store: BehaviorStore, *, clock: Callable[[], datetime], authentication_version: str):
        self.store, self.clock, self.authentication_version = store, clock, authentication_version

    def _save(self, kind, evidence, binding, context, runtime):
        key = runtime_key(kind, evidence.evidence_id)
        request = ControlRequest(
            request_id=runtime_key("evidence", kind, evidence.evidence_id),
            request_digest=_digest(evidence.model_dump(mode="json")),
            read_set=runtime._gate_keys(binding),
        )

        def decide(rows):
            runtime._state(binding, rows, use="read" if kind == "inbox-access" else "execute")
            return ControlChanges(writes=(_write(key, evidence, "create", material(evidence.model_dump(mode="json"))),),
                                  result={"evidence_id": evidence.evidence_id})

        return self.store.transact(request, decide)

    def verify_ingress(self, evidence_id: str) -> VerifiedIngress:
        return self._read("ingress", evidence_id, VerifiedIngress)

    def verify_access(self, evidence_id: str) -> VerifiedInboxAccess | CurrentWorkInboxAccess:
        return self._read("inbox-access", evidence_id, VerifiedInboxAccess)

    def _read(self, kind, evidence_id, model):
        key = runtime_key(kind, evidence_id)
        rows = self.store.read_snapshot((key,))
        if (kind=="inbox-access" and rows[key] is not None
                and rows[key].payload["data"].get("schema_version")=="fa-current-work-result-access-v1"):
            model=CurrentWorkInboxAccess
        evidence = _row(rows, key, model)
        if evidence.evidence_id != evidence_id or _parse(rows[key].payload["material"], InlineMaterial) != material(evidence.model_dump(mode="json")):
            raise Problem(403, "BEHAVIOR_EVIDENCE_INVALID", "retained authenticated evidence differs")
        return evidence


@dataclass(frozen=True)
class RuntimeDependencies:
    store: BehaviorStore
    sources: SourceMaterials
    memory: MemoryGovernance
    broker: ToolBrokerCore
    delivery: DeliveryCore
    inbox: PullInbox
    workers: WorkerCore
    evidence: RuntimeEvidenceAuthority
    claim_reviewer: DurableHumanReviewVerifier | None = None
    native_launches: NativeLaunchAuthority | None = None
    source_backups: SourceBackupService | None = None
    memory_projections: Mapping[str, NativeMemoryProjection] | None = None


@dataclass(frozen=True)
class RuntimeController:
    """Opaque server handle; never deserialize, route, or derive it from a JWT."""

    _runtime: BehaviorRuntime
    controller_id: str

    def step(self, binding_id: str):
        return self._runtime._controller_step(binding_id, self)

    def park(self, binding_id: str):
        return self._runtime._controller_park(binding_id, self)

    def drain(self, *, limit: int = 4):
        return self._runtime._controller_drain(self, limit=limit)


class BehaviorRuntime:
    def __init__(
        self, dependencies: RuntimeDependencies, bindings: tuple[RuntimeBinding, ...], *,
        clock: Callable[[], datetime], mode: Literal["native", "offline_contract"],
        controller_id: str | None = None,
    ):
        expected = (
            (dependencies.store, BehaviorStore), (dependencies.sources, SourceMaterials),
            (dependencies.memory, MemoryGovernance), (dependencies.broker, ToolBrokerCore),
            (dependencies.delivery, DeliveryCore), (dependencies.inbox, PullInbox),
            (dependencies.workers, WorkerCore), (dependencies.evidence, RuntimeEvidenceAuthority),
        )
        if any(not isinstance(value, model) for value, model in expected):
            raise Problem(503, "BEHAVIOR_CONFIGURATION", "all concrete behavior dependencies are required")
        if not bindings or len(bindings) > 16 or len({b.binding_id for b in bindings}) != len(bindings):
            raise Problem(422, "BEHAVIOR_CONFIGURATION", "one to sixteen unique server bindings are required")
        self.dep, self.store, self.clock, self.mode = dependencies, dependencies.store, clock, mode
        self.work_policy = None
        self.historical_binding_ids = frozenset()
        try:
            checked_controller = None if controller_id is None else TypeAdapter(wire.Identifier).validate_python(controller_id, strict=True)
        except ValidationError:
            raise Problem(503, "BEHAVIOR_CONFIGURATION", "controller must be an explicit server identifier") from None
        self.controller = None if checked_controller is None else RuntimeController(self, checked_controller)
        self.bindings = MappingProxyType({b.binding_id: _checked(b, RuntimeBinding) for b in bindings})
        registered = [job for binding in self.bindings.values() for job in binding.registered_jobs]
        if (len(registered) > 32 or len({job.job_id for job in registered}) != len(registered)
                or len({job.work.work_id for job in registered}) != len(registered)):
            raise Problem(503, "BEHAVIOR_CONFIGURATION", "root/child job and work identities must be distinct and bounded")
        for selector in (
            lambda b: b.owner.owner_record_id, lambda b: b.work.work_id, lambda b: b.event_id,
            lambda b: b.job_id, lambda b: b.operation_id, lambda b: b.grant_id,
        ):
            if len({selector(binding) for binding in bindings}) != len(bindings):
                raise Problem(503, "BEHAVIOR_CONFIGURATION", "work, owner, job and operation bindings must be unambiguous")
        objects = (
            dependencies.sources, dependencies.memory, dependencies.broker,
            dependencies.delivery, dependencies.inbox, dependencies.workers, dependencies.evidence,
        )
        if (
            any(item.store is not self.store for item in objects)
            or dependencies.memory.sources is not dependencies.sources
            or dependencies.inbox.core is not dependencies.delivery
            or dependencies.delivery.registration is None
            or dependencies.delivery.registration.mode != mode or dependencies.workers.mode != mode
            or dependencies.delivery.registration.channel_adapter is not dependencies.inbox
            or dependencies.delivery.registration.verify_ingress != dependencies.evidence.verify_ingress
            or dependencies.delivery.registration.verify_work_result != dependencies.workers.verify_work_result
            or dependencies.delivery.registration.verify_receipt != dependencies.inbox.verify_receipt
            or dependencies.inbox.registration.verify_access != dependencies.evidence.verify_access
            or dependencies.inbox.registration.authentication_version != dependencies.evidence.authentication_version
            or dependencies.delivery.contract.channel.channel_id != INBOX_CHANNEL_ID
            or dependencies.delivery.contract.channel.version != INBOX_VERSION
            or dependencies.workers.contract.control_binding != self.store._binding
            or dependencies.delivery.contract.control_binding != self.store._binding
            or any(binding.team_id != dependencies.delivery.contract.team_id for binding in bindings)
            or dependencies.claim_reviewer is not None and (
                not isinstance(dependencies.claim_reviewer, DurableHumanReviewVerifier)
                or dependencies.claim_reviewer.sources is not dependencies.sources
                or dependencies.memory._review is not dependencies.claim_reviewer
            )
        ):
            raise Problem(503, "BEHAVIOR_CONFIGURATION", "explicit, identically bound behavior dependencies are required")
        native = dependencies.native_launches
        if mode == "native" and native is None:
            raise Problem(503, "BEHAVIOR_CONFIGURATION", "native mode requires the concrete permit-bound launch authority")
        if native is not None:
            executor = getattr(dependencies.workers.adapter.start, "__self__", None) if dependencies.workers.adapter else None
            if (
                not isinstance(native, NativeLaunchAuthority) or native.workers is not dependencies.workers
                or self.controller is None or native.controller_id != self.controller.controller_id
                or native.team_id != dependencies.delivery.contract.team_id
                or not isinstance(executor, FoundryExecutor) or executor.authority is not native
                or dependencies.workers.adapter.query != executor.query
                or dependencies.workers.adapter.stop != executor.stop
                or dependencies.workers.adapter.verify != native.verify
                or set(native.jobs) != {job.job_id for job in registered}
                or any(native.jobs[job.job_id].api_binding_id != binding.binding_id
                       for binding in self.bindings.values() for job in binding.registered_jobs)
            ):
                raise Problem(503, "BEHAVIOR_CONFIGURATION", "native callbacks require the exact configured controller and native invocation adapter")
        backups = dependencies.source_backups
        if backups is not None and (
            not isinstance(backups, SourceBackupService) or backups.sources is not dependencies.sources
            or backups.settings.authority_domain != self.store._scope.authority_domain
            or any(backups.settings.tenant_id != binding.tenant_id for binding in self.bindings.values())
        ):
            raise Problem(503, "BEHAVIOR_CONFIGURATION", "backup upload must use the exact live source authority")
        if dependencies.memory_projections is not None:
            for binding_id, projection in dependencies.memory_projections.items():
                binding = self.bindings.get(binding_id)
                if (binding is None or not isinstance(projection, NativeMemoryProjection)
                        or projection.governance is not dependencies.memory
                        or projection.setup.binding_id != binding_id
                        or projection.setup.subject != binding.subject
                        or projection.setup.scope.tenant_id != binding.tenant_id
                        or projection.setup.scope.team_id != binding.team_id
                        or not set(projection.setup.claim_ids) <= set(binding.claim_ids)):
                    raise Problem(503, "BEHAVIOR_CONFIGURATION", "native memory projection binding differs")

    def binding_digest(self, binding: RuntimeBinding):
        return _digest([RUNTIME_VERSION, self.mode, binding.model_dump(mode="json")])

    def _now(self):
        try:
            return _utc(self.clock())
        except Exception:
            raise Problem(503, "BEHAVIOR_CLOCK_UNAVAILABLE", "trusted UTC is unavailable") from None

    def _gate_keys(self, binding):
        return tuple(dict.fromkeys((
            runtime_key("policy", binding.binding_id), control_key("owner", binding.owner.owner_record_id),
            control_key("policy", binding.owner.policy_id), control_key("budget", binding.owner.root_budget_id),
            *(self.work_policy.gate_keys(binding) if self.work_policy is not None else ()),
        )))

    def _state(self, binding, rows, *, use="execute"):
        from .behavior_work_policy import PerWorkRuntimePolicy,WorkPolicyControl
        policy_key = runtime_key("policy", binding.binding_id)
        scoped = rows[policy_key] is not None and rows[policy_key].payload["data"].get(
            "schema_version") == "fa-per-work-runtime-policy-v1"
        policy = _row(rows, policy_key, PerWorkRuntimePolicy if scoped else RuntimePolicy)
        if scoped:
            if type(self.work_policy) is not WorkPolicyControl or self.work_policy.runtime is not self:
                raise Problem(403,"WORK_POLICY_DISABLED","per-work policy requires its original installed authority")
            self.work_policy.check(binding,policy,rows,use=use)
        current = _row(rows, control_key("policy", binding.owner.policy_id), PolicyRecord)
        owner = _row(rows, control_key("owner", binding.owner.owner_record_id), OwnerRecord)
        budget = _row(rows, control_key("budget", binding.owner.root_budget_id), BudgetRecord)
        if (
            policy.binding_digest != self.binding_digest(binding) or not policy.active
            or not current.active or current.policy_id != binding.owner.policy_id
            or policy.operation_policy_revision != current.revision
            or not scoped and (policy.valid_until is None or self._now() >= policy.valid_until)
            or not owner.active or any(
                getattr(owner, name) != getattr(binding.owner, name) for name in ExecutionContext.model_fields if name != "epoch"
            )
            or binding.operation_id not in owner.operation_ids
            or budget.root_budget_id != binding.owner.root_budget_id
        ):
            raise Problem(403, "BEHAVIOR_CURRENT_POLICY_DENIED", "current owner, policy, purpose or binding is unavailable")
        context = ExecutionContext(**{field: getattr(owner, field) for field in ExecutionContext.model_fields})
        return policy, context

    def authorize(self, binding_id: str, principal: BehaviorPrincipal, action: Action, *, historical_status=False):
        self.refresh_bindings(binding_id)
        binding = self.bindings.get(binding_id)
        if binding is None or not isinstance(principal, BehaviorPrincipal):
            raise Problem(403, "BEHAVIOR_BINDING_DENIED", "unknown authorized binding")
        principal.require(binding.team_id, action)
        if (
            principal.tenant_id != binding.tenant_id
            or principal.oid != (binding.human_oid if principal.kind == "human" else binding.agent_oid)
        ):
            raise Problem(403, "BEHAVIOR_BINDING_DENIED", "principal is not assigned to this binding")
        if binding_id in self.historical_binding_ids:
            if action != "read" or not historical_status:
                raise Problem(403, "CHANNELS_RETIRED_BINDING", "原已結案 binding 只保留狀態與專用回條讀取，不恢復來源或執行權")
            rows = self.store.read_snapshot(self._gate_keys(binding))
            policy = _row(rows, runtime_key("policy", binding.binding_id), RuntimePolicy)
            owner = _row(rows, control_key("owner", binding.owner.owner_record_id), OwnerRecord)
            context = ExecutionContext(**{field: getattr(owner, field) for field in ExecutionContext.model_fields})
        else:
            policy, context = self._state(binding, self.store.read_snapshot(self._gate_keys(binding)),
                                          use="read" if action in {"read","acknowledge"} else "execute")
        return binding, policy, context

    def refresh_bindings(self,binding_id=None):
        if self.work_policy is not None:
            self.work_policy.refresh(binding_id)

    def delivery_for_obligation(self, obligation_id):
        key = control_key("delivery-obligation", obligation_id)
        ob = self.dep.delivery._row(self.store.read_snapshot((key,)), key, _Obligation)
        if ob.route.channel_id == INBOX_CHANNEL_ID:
            return self.dep.delivery
        raise Problem(403, "DELIVERY_CHANNEL_MISMATCH", "交付責任不屬於已安裝通道")

    def resolve_owner(self, owner_record_id: str, *, binding_id=None) -> ExecutionContext:
        """Server supervisor seam for existing WorkerConsumer/DeliveryConsumer."""
        self.refresh_bindings(binding_id)
        matches = [binding for binding in self.bindings.values() if binding.owner.owner_record_id == owner_record_id]
        if len(matches) != 1:
            raise Problem(403, "BEHAVIOR_BINDING_DENIED", "supervisor owner requires one configured binding")
        binding = matches[0]
        return self._state(binding, self.store.read_snapshot(self._gate_keys(binding)),use="control")[1]

    def _require_controller(self, controller):
        if self.controller is None or controller is not self.controller:
            raise Problem(403, "BEHAVIOR_CONTROLLER_REQUIRED", "a configured internal controller handle is required")

    def executor_callback(self, binding_id: str, launch_id: str, principal: BehaviorPrincipal, *, claim=False):
        """Assigned executor callback gate, NOT supervisor start/claim/adopt/drain."""
        self.refresh_bindings(binding_id)
        binding = self.bindings.get(binding_id)
        native = self.dep.native_launches
        if binding is None or native is None or not isinstance(principal, BehaviorPrincipal):
            raise Problem(403, "BEHAVIOR_EXECUTOR_DENIED", "no configured native callback binding")
        principal.require(binding.team_id, "consume")
        launch = native.launch(launch_id)
        registered = next((job for job in binding.registered_jobs if job.job_id == launch.start.binding.job_id), None)
        if (
            registered is None
            or principal.kind != "agent" or principal.tenant_id != binding.tenant_id
            or principal.oid != launch.target.agent_oid or launch.target not in native.targets_for(registered.job_id)
            or launch.api_binding_id != binding_id
            or launch.start.binding.work_id != registered.work.work_id or launch.definition != registered.work
            or any(
                getattr(launch.start.binding.owner, name) != getattr(binding.owner, name)
                for name in ExecutionContext.model_fields if name != "epoch"
            )
        ):
            raise Problem(403, "BEHAVIOR_EXECUTOR_DENIED", "native callback belongs to another assigned job/attempt")
        if registered.job_id != binding.job_id:
            child = self.dep.workers.inspect(registered.job_id)
            self._child_record(binding, registered, child)
            if child.owner != launch.start.binding.owner or launch.start.input != child.spec.input:
                raise Problem(403, "BEHAVIOR_EXECUTOR_DENIED", "child callback owner/input differs from its saved assignment")
        if claim:
            self._state(binding, self.store.read_snapshot(self._gate_keys(binding)))
        return native

    def bootstrap(self, policies: Mapping[str, RuntimePolicy]) -> None:
        """Explicit server initialization only; NOT an HTTP route or native proof."""
        if set(policies) != set(self.bindings):
            raise Problem(422, "BEHAVIOR_CONFIGURATION", "every binding requires an explicit runtime policy")
        for binding_id, binding in self.bindings.items():
            policy = _checked(policies[binding_id], RuntimePolicy)
            if policy.binding_digest != self.binding_digest(binding):
                raise Problem(409, "BEHAVIOR_CONFIGURATION", "bootstrap policy is not bound to this configuration")
            policy_key, index_key = runtime_key("policy", binding_id), runtime_key("plan-index", binding_id)
            request = ControlRequest(
                request_id=runtime_key("bootstrap", binding_id),
                request_digest=_digest(policy.model_dump(mode="json")),
            )
            self.store.transact(request, lambda _, p=policy, pk=policy_key, ik=index_key: ControlChanges(
                writes=(_write(pk, p, "create"), _write(ik, _PlanIndex(), "create")),
                result={"binding_id": binding_id},
            ))

    def validate_startup(self) -> None:
        self.refresh_bindings()
        if self.work_policy is not None:
            self.dep.workers.pending()
            return
        self.dep.workers._ready()
        self.dep.workers.pending()  # Requires the real persisted, bounded pool.
        self.dep.delivery._readiness()
        if self.mode == "native":
            self.dep.inbox.check_installation(self.dep.delivery)
        for binding in self.bindings.values():
            if binding.binding_id in self.historical_binding_ids:
                continue
            self._state(binding, self.store.read_snapshot(self._gate_keys(binding)))
            index = runtime_key("plan-index", binding.binding_id)
            _row(self.store.read_snapshot((index,)), index, _PlanIndex)

    def _source_context(self, binding, policy, principal):
        return AuthenticatedSourceContext(
            binding.tenant_id, binding.team_id, principal.source_actor_id,
            binding.ingress_id, policy.allowed_purposes,
        )

    @staticmethod
    def _current_source_scope(context, policy, scope):
        if scope != context.scope(policy.source_visibility):
            raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "saved source scope is no longer allowed by current policy")

    def _reference(self, reference: wire.ExactSourceRef, binding, policy, principal):
        if reference.source_id not in binding.source_ids:
            raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "source is not in this binding")
        context = self._source_context(binding, policy, principal)
        return SourceReference(scope=context.scope(policy.source_visibility), **reference.model_dump())

    def _plan_key(self, binding, kind, request_id):
        return runtime_key("plan", binding.binding_id, kind, request_id)

    def _existing(self, binding, kind, principal, command):
        key = self._plan_key(binding, kind, command.request_id)
        row = self.store.read_snapshot((key,))[key]
        if row is None:
            return None
        plan = _parse(row.payload["data"], CommandPlan)
        if (
            plan.principal_oid != principal.oid or plan.binding_digest != self.binding_digest(binding)
            or plan.input_digest != _digest(command.model_dump(mode="json"))
            or _parse(row.payload["material"], InlineMaterial) != material(plan.model_dump(mode="json"))
        ):
            raise Problem(409, "BEHAVIOR_COMMAND_CONFLICT", "original command ID is bound to different bytes or identity")
        return plan

    def _decision_plan_index(self, binding, kind, command, request):
        index_key = runtime_key("plan-index", binding.binding_id)
        index = _row(self.store.read_snapshot((index_key,)), index_key, _PlanIndex)
        for key in index.keys:
            original = _row(self.store.read_snapshot((key,)), key, CommandPlan)
            if (original.kind == kind and original.request.request_id == request.request_id
                    and original.request_id != command.request_id):
                raise Problem(409, "BEHAVIOR_DECISION_CONFLICT", "the decision ID belongs to its original command request")
        return index

    def _save_plan(self, binding, kind, principal, command, context, data, request, *,
                   extra_writes=(), expected_index: _PlanIndex | None = None):
        prior = self._existing(binding, kind, principal, command)
        if prior is not None:
            return prior
        plan = CommandPlan(
            binding_id=binding.binding_id, binding_digest=self.binding_digest(binding),
            kind=kind, request_id=command.request_id, principal_oid=principal.oid,
            input_digest=_digest(command.model_dump(mode="json")), context=context,
            data=data, request=request,
        )
        key, index_key = self._plan_key(binding, kind, command.request_id), runtime_key("plan-index", binding.binding_id)
        reserve = ControlRequest(
            request_id=runtime_key("plan-reserve", key),
            request_digest=_digest([plan.input_digest, principal.oid, plan.binding_digest]),
            read_set=(*self._gate_keys(binding), index_key),
        )

        def decide(rows):
            policy, _ = self._state(binding, rows)
            index = _row(rows, index_key, _PlanIndex)
            if expected_index is not None and index != expected_index:
                raise Problem(412, "BEHAVIOR_CONFLICT", "command plans changed while preparing this decision")
            if not getattr(policy,"instruction",False) and len(index.keys) >= policy.max_commands:
                raise Problem(429, "BEHAVIOR_COMMAND_LIMIT", "durable command capacity is exhausted")
            return ControlChanges(writes=(
                _write(key, plan, "create", material(plan.model_dump(mode="json"))),
                _write(index_key, _updated(index, keys=(*index.keys, key)), "replace"),
                *extra_writes,
            ), result={"plan_key": key})

        self.store.transact(reserve, decide)
        return self._existing(binding, kind, principal, command)

    def _load(self, binding, kind, request_id, principal):
        key = self._plan_key(binding, kind, request_id)
        rows = self.store.read_snapshot((key,))
        plan = _row(rows, key, CommandPlan)
        if (
            plan.binding_id != binding.binding_id or plan.binding_digest != self.binding_digest(binding)
            or plan.principal_oid != principal.oid
            or _parse(rows[key].payload["material"], InlineMaterial) != material(plan.model_dump(mode="json"))
        ):
            raise Problem(403, "BEHAVIOR_PLAN_DENIED", "plan does not belong to this authenticated binding")
        return plan

    def _run(self, binding, plan, invoke, reconcile=None):
        key = runtime_key("consume", self._plan_key(binding, plan.kind, plan.request_id))
        command = ControlRequest(
            request_id=key, request_digest=_digest(plan.model_dump(mode="json")),
            read_set=self._gate_keys(binding),
        )
        fresh = False

        def decide(rows):
            nonlocal fresh
            self._state(binding, rows)
            fresh = True
            return ControlChanges(writes=(_write(key, plan, "create", material(plan.model_dump(mode="json"))),),
                                  result={"responsibility": "reconcile_original", "dispatch_permission": False})

        self.store.transact(command, decide)
        if not fresh:
            return (reconcile or (lambda: self.store.reconcile(plan.request)))()
        return invoke()

    @staticmethod
    def plan_result(plan):
        return {
            "request_id": plan.request_id, "control_request_id": plan.request.request_id,
            "control_request_digest": plan.request.request_digest, "prepared": True,
        }

    def prepare_source(self, binding_id, source_id, command: wire.SourcePrepare, principal):
        binding, policy, context = self.authorize(binding_id, principal, "submit")
        from .memory_bridge_settings import human_memory_source_write
        human_memory_source_write(self, binding, source_id, principal)
        if source_id not in binding.source_ids:
            raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "source is not in this binding")
        kind = "source-" + source_id
        prior = self._existing(binding, kind, principal, command)
        if prior is not None:
            return self.plan_result(prior)
        if command.source_schema == TEXT_SCHEMA:
            if not policy.human_text_enabled:
                raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "human text intake is disabled by current server policy")
            require_text_consent(command.original, command.retention_consent_sha256, human=principal.kind == "human")
        else:
            if command.retention_consent_sha256 is not None:
                raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "metrics intake does not accept text-consent fields")
            self._safe_original(command.original)
        source_context = self._source_context(binding, policy, principal)
        source = SourceInput(
            source_id=source_id, version=command.version, visibility=policy.source_visibility,
            purpose=policy.purpose, schema_id=command.source_schema,
            media_type="text/plain" if command.source_schema == TEXT_SCHEMA else "application/json",
            necessary=tuple(self._reference(ref, binding, policy, principal) for ref in command.necessary),
            expires_at=command.expires_at,
        )
        original = self.dep.sources.plan(source_context, source, command.original)
        plan = self._save_plan(binding, kind, principal, command, context,
                               {"source_plan": original.model_dump(mode="json")}, original.request)
        return {**self.plan_result(plan), "reference": original.reference.model_dump(mode="json")}

    def commit_source(self, binding_id, source_id, command: wire.Command, principal, *, reconcile=False):
        binding, policy, _ = self.authorize(binding_id, principal, "submit" if not reconcile else "read")
        if not reconcile:
            from .memory_bridge_settings import human_memory_source_write
            human_memory_source_write(self, binding, source_id, principal)
        plan = self._load(binding, "source-" + source_id, command.request_id, principal)
        original = _parse(plan.data["source_plan"], SourcePlan)
        source_context = self._source_context(binding, policy, principal)
        self._current_source_scope(source_context, policy, original.reference.scope)
        if reconcile:
            return self.dep.sources.reconcile(source_context, original)
        if original.source.schema_id == TEXT_SCHEMA:
            if not policy.human_text_enabled or principal.kind != "human":
                raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "current text-retention policy no longer permits this commit")
            checked_human_text(original.original)
        return self._run(binding, plan, lambda: self.dep.sources.commit(source_context, original))

    @staticmethod
    def _safe_original(original):
        def unique(pairs):
            value = dict(pairs)
            if len(value) != len(pairs):
                raise ValueError("duplicate source JSON field")
            return value

        try:
            if len(original) > 8192:
                raise ValueError("oversized original")
            ArtifactMetrics.model_validate(json.loads(original, object_pairs_hook=unique))
        except (ValueError, TypeError, ValidationError):
            raise Problem(422, "BEHAVIOR_UNSAFE_CONTENT", "only complete metrics JSON is supported") from None

    def read_source(self, binding_id, source_id, version, principal):
        binding, policy, _ = self.authorize(binding_id, principal, "read")
        if source_id not in binding.source_ids:
            raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "source is not in this binding")
        context = self._source_context(binding, policy, principal)
        scope = context.scope(policy.source_visibility)
        key = control_key("source-version", scope.key, source_id, str(version))
        event = _row(self.store.read_snapshot((key,)), key, SourceEvent)
        exact = self.dep.sources.read_exact(context, event.reference)
        self._validate_source_output(exact, policy)
        return exact

    def _validate_source_output(self, exact, policy):
        if exact.event.schema_id == TEXT_SCHEMA and policy.human_text_enabled:
            checked_human_text(exact.original)
        elif exact.event.schema_id == METRICS_SCHEMA:
            self._safe_original(exact.original)
        else:
            raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "source schema is not enabled by current output policy")

    @staticmethod
    def _validate_claim_output(text, policy):
        if text == "The accepted metrics source has been reviewed.":
            return
        if not policy.human_text_enabled:
            raise Problem(422, "BEHAVIOR_UNSAFE_CONTENT", "general claim text is not enabled")
        checked_human_text(text.encode("utf-8"))

    def withdraw_source(self, binding_id, source_id, command: wire.SourceWithdrawal, principal, *, reconcile=False):
        binding, policy, context = self.authorize(binding_id, principal, "approve")
        if command.reference.source_id != source_id:
            raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "withdrawal source differs from path")
        source_context = self._source_context(binding, policy, principal)
        withdrawal = SourceWithdrawalInput(
            withdrawal_id=command.withdrawal_id, reference=self._reference(command.reference, binding, policy, principal),
            expected_access_revision=command.expected_access_revision, reason=command.reason,
        )
        request = self.dep.sources.withdrawal_request(principal, source_context, withdrawal)
        plan = self._save_plan(binding, "withdraw-" + source_id, principal, command, context,
                               {"withdrawal": withdrawal.model_dump(mode="json")}, request)
        saved = _parse(plan.data["withdrawal"], SourceWithdrawalInput)
        if reconcile:
            return self.dep.sources.reconcile_withdrawal(principal, source_context, saved)
        return self._run(binding, plan, lambda: self.dep.sources.withdraw_source(principal, source_context, saved))

    def capture_snapshot(self, binding_id, snapshot_id, command: wire.SnapshotCapture, principal):
        binding, policy, context = self.authorize(binding_id, principal, "submit")
        kind = "snapshot-" + snapshot_id
        prior = self._existing(binding, kind, principal, command)
        source_context = self._source_context(binding, policy, principal)
        if prior is None:
            roots = tuple(self._reference(ref, binding, policy, principal) for ref in command.roots)
            original = self.dep.sources.plan_snapshot(
                source_context,
                snapshot_id=snapshot_id, scope=source_context.scope(policy.source_visibility),
                conversation_id=context.conversation_id, runtime_version=RUNTIME_VERSION,
                roots=roots,
            )
            prior = self._save_plan(binding, kind, principal, command, context,
                                    {"manifest": original.manifest.model_dump(mode="json")}, original.request)
        manifest = _parse(prior.data["manifest"], SourceSnapshot)
        original = SourceSnapshotPlan(manifest=manifest, request=prior.request)
        self._current_source_scope(source_context, policy, manifest.scope)
        return self._run(binding, prior, lambda: self.dep.sources.commit_snapshot(source_context, original),
                         lambda: self.dep.sources.reconcile_snapshot(source_context, original))

    def verify_snapshot(self, binding_id, snapshot_id, command: wire.Command, principal):
        binding, policy, _ = self.authorize(binding_id, principal, "read")
        plan = self._load(binding, "snapshot-" + snapshot_id, command.request_id, principal)
        manifest = _parse(plan.data["manifest"], SourceSnapshot)
        source_context = self._source_context(binding, policy, principal)
        self._current_source_scope(source_context, policy, manifest.scope)
        receipt = self.store.reconcile(plan.request)
        restored = self.dep.sources.restore_snapshot(
            source_context, scope=manifest.scope, snapshot_id=snapshot_id, receipt=receipt,
        )
        for node in restored.manifest.nodes:
            exact = self.dep.sources.read_exact(source_context, node.reference)
            if exact.original != restored.originals[node.reference.key]:
                raise Problem(409, "BEHAVIOR_SOURCE_CHANGED", "snapshot original differs from its verified source")
            self._validate_source_output(exact, policy)
        return restored

    def prepare_backup(self, binding_id, backup_id, command: wire.BackupPrepare, principal):
        binding, policy, context = self.authorize(binding_id, principal, "backup")
        service = self.dep.source_backups
        if service is None:
            raise Problem(503, "BEHAVIOR_BACKUP_UNAVAILABLE", "protected source backup is not configured")
        kind = "backup-" + backup_id
        prior = self._existing(binding, kind, principal, command)
        if prior is not None:
            return self._backup_plan_result(prior)
        snapshot = self._load(binding, "snapshot-" + command.snapshot_id, command.snapshot_request_id, principal)
        manifest = _parse(snapshot.data["manifest"], SourceSnapshot)
        source_context = self._source_context(binding, policy, principal)
        self._current_source_scope(source_context, policy, manifest.scope)
        original = service.plan(
            principal, source_context, backup_id=backup_id,
            scope=manifest.scope, snapshot_id=command.snapshot_id,
            snapshot_receipt=self.store.reconcile(snapshot.request),
        )
        plan = self._save_plan(binding, kind, principal, command, context,
                               {"backup_plan": original.model_dump(mode="json")}, original.request)
        return self._backup_plan_result(plan)

    def _backup_plan_result(self, plan):
        original = _parse(plan.data["backup_plan"], SourceBackupPlan)
        return {
            **self.plan_result(plan), "backup_id": original.description.backup_id,
            "archive_sha256": original.description.archive_sha256,
            "archive_bytes": original.description.byte_length,
        }

    def upload_backup(self, binding_id, backup_id, command: wire.Command, principal, *, reconcile=False):
        binding, policy, _ = self.authorize(binding_id, principal, "backup")
        service = self.dep.source_backups
        if service is None:
            raise Problem(503, "BEHAVIOR_BACKUP_UNAVAILABLE", "protected source backup is not configured")
        plan = self._load(binding, "backup-" + backup_id, command.request_id, principal)
        original = _parse(plan.data["backup_plan"], SourceBackupPlan)
        context = self._source_context(binding, policy, principal)
        self._current_source_scope(context, policy, original.description.scope)
        if reconcile:
            return service.reconcile_upload(principal, context, original)

        def no_repeat():
            raise Problem(409, "SOURCE_BACKUP_RECONCILE_REQUIRED", "use explicit original backup reconciliation")

        return self._run(binding, plan, lambda: service.upload(principal, context, original), no_repeat)

    def _candidate(self, binding, policy, principal, claim_id, command):
        if claim_id not in binding.claim_ids or self.dep.claim_reviewer is None:
            raise Problem(503, "BEHAVIOR_REVIEW_UNAVAILABLE", "configured durable human review is required")
        self._validate_claim_output(command.text, policy)
        return ClaimCandidate(
            claim_id=claim_id, version=command.version, subject=binding.subject,
            visibility=policy.source_visibility, purpose=policy.purpose, kind=command.kind, text=command.text,
            supporting=tuple(self._reference(ref, binding, policy, principal) for ref in command.supporting),
            effective_from=command.effective_from, expires_at=command.expires_at, supersedes=command.supersedes,
        )

    def preview_claim(self, binding_id, claim_id, command: wire.ClaimDraft, principal):
        binding, policy, _ = self.authorize(binding_id, principal, "approve")
        candidate = self._candidate(binding, policy, principal, claim_id, command)
        return self.dep.claim_reviewer.preview(principal, self._source_context(binding, policy, principal), candidate)

    def review_claim(self, binding_id, claim_id, command: wire.ClaimReview, principal):
        binding, policy, context = self.authorize(binding_id, principal, "approve")
        prior = self._existing(binding, "claim-" + claim_id, principal, command)
        if prior is not None:
            return self.plan_result(prior)
        source_context = self._source_context(binding, policy, principal)
        candidate = self._candidate(binding, policy, principal, claim_id, command)
        review_plan = self._existing(binding, "human-review-" + claim_id, principal, command)
        reviewer = self.dep.claim_reviewer
        if review_plan is None:
            review = reviewer.plan_decision(principal, source_context, HumanReviewCommand(
                review_id=command.decision_id, candidate=candidate, candidate_sha256=command.candidate_sha256,
                evidence_sha256=command.evidence_sha256, policy_version=command.policy_version,
                decision="approve", reviewed_checks=command.reviewed_checks,
                reason="Explicit authenticated human review.",
            ))
            review_plan = self._save_plan(binding, "human-review-" + claim_id, principal, command, context,
                                          {"human_review": review.model_dump(mode="json")}, review.request)
        review = _parse(review_plan.data["human_review"], HumanReviewPlan)
        self._run(binding, review_plan, lambda: reviewer.commit_decision(principal, source_context, review),
                  lambda: reviewer.reconcile_decision(principal, source_context, review))
        reviewed = self.dep.memory.plan_claim(source_context, candidate)
        plan = self._save_plan(binding, "claim-" + claim_id, principal, command, context,
                               {"claim_plan": reviewed.model_dump(mode="json")}, reviewed.request)
        return self.plan_result(plan)

    def commit_claim(self, binding_id, claim_id, command: wire.Command, principal, *, reconcile=False):
        binding, policy, _ = self.authorize(binding_id, principal, "read" if reconcile else "submit")
        plan = self._load(binding, "claim-" + claim_id, command.request_id, principal)
        original = _parse(plan.data["claim_plan"], ClaimPlan)
        context = self._source_context(binding, policy, principal)
        self._current_source_scope(context, policy, original.reference.scope)
        if reconcile:
            return self.dep.memory.reconcile_claim(context, original)
        return self._run(binding, plan, lambda: self.dep.memory.commit_claim(context, original))

    def current_claim(self, binding_id, claim_id, principal):
        binding, policy, _ = self.authorize(binding_id, principal, "read")
        if claim_id not in binding.claim_ids:
            raise Problem(403, "BEHAVIOR_CLAIM_DENIED", "claim is not in this binding")
        context = self._source_context(binding, policy, principal)
        result = self.dep.memory.get_current(context, scope=context.scope(policy.source_visibility),
                                            subject=binding.subject, claim_id=claim_id)
        self._validate_claim_output(result.candidate.text, policy)
        return result

    def context(self, binding_id, command: wire.ContextRequest, principal):
        binding, policy, _ = self.authorize(binding_id, principal, "read")
        if not set(command.required_claim_ids) <= set(binding.claim_ids):
            raise Problem(403, "BEHAVIOR_CLAIM_DENIED", "necessary claims are outside this binding")
        return self._reviewed_context(binding,policy,principal,command.required_claim_ids)

    def _reviewed_context(self,binding,policy,principal,required_claim_ids):
        context = self._source_context(binding, policy, principal)
        result = self.dep.memory.assemble(
            context, scope=context.scope(policy.source_visibility), subject=binding.subject,
            required_claim_ids=required_claim_ids, versions=binding.context_versions,
        )
        for reference in result.manifest.sources:
            self._validate_source_output(self.dep.sources.read_exact(context, reference, require_current=True), policy)
        for claim in result.claims:
            self._validate_claim_output(claim.candidate.text, policy)
        return result

    def memory_projection(self, binding_id, command, principal, *, action: str):
        allowed = {"plan", "start", "poll", "reconcile", "search", "context"}
        if action not in allowed:
            raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "unsupported native memory operation")
        binding, policy, _ = self.authorize(
            binding_id, principal, "submit" if action in {"plan", "start", "poll"} else "read",
        )
        configured = self.dep.memory_projections or {}
        projection = configured.get(binding_id)
        if projection is None:
            raise Problem(503, "BEHAVIOR_MEMORY_DISABLED", "no governed native memory projection is configured")
        source_context = self._source_context(binding, policy, principal)
        self._current_source_scope(source_context, policy, projection.setup.scope)
        if action == "plan":
            return projection.plan(source_context).payload
        if action in {"search", "context"}:
            query = ProjectionQuery(query=command.query)
            method = projection.search if action == "search" else projection.assemble
            return method(source_context, command.projection_id, query).payload
        return getattr(projection, action)(source_context, command.projection_id).payload

    def prepare_operation(self, binding_id, command: wire.OperationPrepare, principal):
        binding, _, context = self.authorize(binding_id, principal, "approve" if principal.kind == "human" else "execute")
        if self.dep.broker.dispatcher is None:
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "only a concrete bounded adapter may prepare API operations")
        if command.input_ref not in binding.allowed_input_refs:
            raise Problem(403, "BEHAVIOR_OPERATION_DENIED", "input alias is not in this binding")
        intent = ToolIntent(operation_id=binding.operation_id, input_ref=command.input_ref)
        original = self.dep.broker.reconciliation_request(intent, context)
        plan = self._save_plan(binding, "operation-prepare", principal, command, context,
                               {"intent": intent.model_dump(mode="json")}, original)
        saved = _parse(plan.data["intent"], ToolIntent)
        return self._run(binding, plan, lambda: self.dep.broker.prepare(saved, plan.context))

    def operation_preview(self, binding_id, principal):
        binding, _, context = self.authorize(binding_id, principal, "read")
        if self.dep.broker.dispatcher is None:
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "no concrete execution window contract is configured")
        keys = (control_key("operation", binding.operation_id),)
        operation, prepared, description = self.dep.broker._operation(
            self.store.read_snapshot(keys), binding.operation_id, context,
        )
        result = {
            "operation_id": binding.operation_id, "description_digest": prepared.sha256,
            "adapter_contract_digest": operation.adapter_contract_digest,
            "canonicalization_version": description.canonicalization_version,
            "content": prepared.model_dump(mode="json"), "effect_state": operation.effect_state,
            "execution_window_contract": self.dep.broker.dispatcher.adapter.execution_contract,
            "strict_service_commit_time_expiry_supported": False,
        }
        if operation.human_rejection is not None:
            result["human_rejection"] = operation.human_rejection.model_dump(
                mode="json", exclude={"rejected_by_oid"},
            )
        return result

    def approve_operation(self, binding_id, command: wire.OperationApproval, principal):
        binding, _, context = self.authorize(binding_id, principal, "approve")
        op_key, grant_key = control_key("operation", binding.operation_id), control_key("grant", binding.grant_id)
        intent_key = runtime_key("human-operation-rejection-intent", binding.binding_id, command.decision_id)
        current = self.store.read_snapshot((op_key, intent_key))
        operation, _, _ = self.dep.broker._operation(current, binding.operation_id, context)
        prior = self._existing(binding, "operation-approve", principal, command)
        if prior is not None and (
            operation.human_rejection is not None
            or intent_key not in prior.request.read_set or current[intent_key] is not None
        ):
            return self.store.reconcile(prior.request)
        if operation.human_rejection is not None:
            raise Problem(409, "OPERATION_HUMAN_REJECTED", "the original declined operation cannot be approved again")
        if current[intent_key] is not None:
            raise Problem(409, "BEHAVIOR_DECISION_CONFLICT", "the decision ID belongs to an original rejection request")
        decision_key = runtime_key("human-operation-decision", binding.binding_id, command.decision_id)
        request = ControlRequest(
            request_id=runtime_key("operation-approve", binding.binding_id, command.decision_id),
            request_digest=_digest([principal.oid, command.model_dump(mode="json"), self.binding_digest(binding)]),
            read_set=(*self._gate_keys(binding), op_key, grant_key, intent_key),
        )
        plan = prior
        if plan is None:
            index = self._decision_plan_index(binding, "operation-approve", command, request)
            plan = self._save_plan(binding, "operation-approve", principal, command, context,
                                  {"approval": command.model_dump(mode="json")}, request, expected_index=index)

        def commit():
            def decide(rows):
                policy, current_context = self._state(binding, rows)
                operation, prepared, _ = self.dep.broker._operation(rows, binding.operation_id, current_context)
                if operation.human_rejection is not None:
                    raise Problem(409, "OPERATION_HUMAN_REJECTED", "the original declined operation cannot be approved again")
                if rows[intent_key] is not None:
                    raise Problem(409, "BEHAVIOR_DECISION_CONFLICT", "the decision ID belongs to an original rejection request")
                now = self._now()
                expiry = _utc(command.expires_at)
                if (
                    command.description_digest != prepared.sha256
                    or command.adapter_contract_digest != self.dep.broker._contract_digest()
                    or not now < expiry <= (now + timedelta(seconds=policy.max_grant_seconds) if policy.valid_until is None
                                           else min(policy.valid_until, now + timedelta(seconds=policy.max_grant_seconds)))
                    or command.units != self.dep.broker._enabled()[1].reservation_units
                ):
                    raise Problem(403, "BEHAVIOR_APPROVAL_DENIED", "human decision must pin current exact material and limits")
                grant = GrantRecord(
                    grant_id=binding.grant_id, approval_source="human", approval_id=command.decision_id,
                    subjects=(GrantSubject(principal_id=current_context.principal_id, conversation_id=current_context.conversation_id),),
                    root_budget_id=current_context.root_budget_id, description_digest=prepared.sha256,
                    adapter_contract_digest=command.adapter_contract_digest, policy_id=current_context.policy_id,
                    policy_revision=policy.operation_policy_revision, revocation_revision=0, revoked=False,
                    not_before=now, expires_at=expiry, remaining_uses=1, remaining_units=command.units,
                )
                return ControlChanges(writes=(
                    _write(grant_key, grant, "create"),
                    _write(decision_key, grant, "create", material({
                        "authenticated_human_oid": principal.oid, "binding_id": binding.binding_id,
                        "decision": command.model_dump(mode="json"), "prepared": prepared.model_dump(mode="json"),
                    })),
                ), result={"approval_id": command.decision_id, "grant_id": binding.grant_id,
                           "description_digest": prepared.sha256, "approved_uses": 1})

            return self.store.transact(plan.request, decide)

        return self._run(binding, plan, commit)

    def operation_decision_state(self, binding_id, principal):
        def read():
            binding, policy, context = self.authorize(binding_id, principal, "read")
            if principal.kind != "human" or principal.actor_id is not None:
                raise Problem(403, "BEHAVIOR_OPERATION_READ_DENIED", "a configured human observation is required")
            op_key, grant_key = control_key("operation", binding.operation_id), control_key("grant", binding.grant_id)
            rows = self.store.read_snapshot((op_key, grant_key))
            operation, prepared, _ = self.dep.broker._operation(rows, binding.operation_id, context)
            observed_grant = None
            if rows[grant_key] is not None:
                grant = _row(rows, grant_key, GrantRecord)
                if (grant.grant_id != binding.grant_id or grant.root_budget_id != context.root_budget_id
                        or grant.description_digest != prepared.sha256
                        or grant.adapter_contract_digest != operation.adapter_contract_digest
                        or grant.policy_id != context.policy_id
                        or GrantSubject(principal_id=context.principal_id,
                                        conversation_id=context.conversation_id) not in grant.subjects):
                    raise Problem(403, "BEHAVIOR_OPERATION_VIEW_DENIED", "grant observation differs from this operation")
                observed_grant = wire.OperationGrantObservation(
                    grant_id=grant.grant_id, approval_id=grant.approval_id,
                    not_before=grant.not_before, expires_at=grant.expires_at, policy_revision=grant.policy_revision,
                    revocation_revision=grant.revocation_revision, revoked=grant.revoked,
                    remaining_uses=grant.remaining_uses, remaining_units=grant.remaining_units,
                )
            rejected = operation.human_rejection
            rejection = None if rejected is None else wire.OperationRejectionObservation(
                decision_id=rejected.decision_id, rejected_at=rejected.rejected_at,
            )
            return wire.OperationDecisionState(
                binding_id=binding.binding_id, operation_id=binding.operation_id,
                description_digest=prepared.sha256, adapter_contract_digest=operation.adapter_contract_digest,
                observed_at=self._now(), current_policy_revision=policy.operation_policy_revision,
                effect_state=operation.effect_state, business_state=operation.business_state,
                current_attempt_id=operation.current_attempt_id, unsettled_attempt_ids=operation.unsettled_attempt_ids,
                grant=observed_grant, rejection=rejection,
            )

        return self.store.consistent_read(read)

    def reject_operation(self, binding_id, command: wire.OperationRejection, principal):
        binding, _, context = self.authorize(binding_id, principal, "approve")
        if principal.kind != "human" or principal.actor_id is not None:
            raise Problem(403, "BEHAVIOR_REJECTION_DENIED", "a configured human decision is required")
        op_key, grant_key = control_key("operation", binding.operation_id), control_key("grant", binding.grant_id)
        decision_key = runtime_key("human-operation-decision", binding.binding_id, command.decision_id)
        request = ControlRequest(
            request_id=runtime_key("operation-reject", binding.binding_id, command.decision_id),
            request_digest=_digest([principal.oid, command.model_dump(mode="json"), self.binding_digest(binding)]),
            read_set=(*self._gate_keys(binding), op_key, grant_key),
        )

        def pending(rows, current_context):
            operation, prepared, _ = self.dep.broker._operation(rows, binding.operation_id, current_context)
            if (command.description_digest != prepared.sha256
                    or command.adapter_contract_digest != operation.adapter_contract_digest):
                raise Problem(403, "BEHAVIOR_REJECTION_DENIED", "human rejection must pin the original exact material")
            if (operation.human_rejection is not None or rows[grant_key] is not None
                    or operation.current_attempt_id is not None or operation.unsettled_attempt_ids
                    or operation.effect_state != "unknown" or operation.business_state != "pending"
                    or operation.successful_result is not None):
                raise Problem(409, "BEHAVIOR_REJECTION_CONFLICT", "only an unapproved, unattempted operation may be declined")
            return operation, prepared

        intent_key = runtime_key("human-operation-rejection-intent", binding.binding_id, command.decision_id)
        intent = HumanOperationRejectionIntent(
            binding_id=binding.binding_id, binding_digest=self.binding_digest(binding),
            operation_id=binding.operation_id, decision_id=command.decision_id,
            request_id=command.request_id, principal_oid=principal.oid,
            input_digest=_digest(command.model_dump(mode="json")),
        )
        intent_rows = self.store.read_snapshot((intent_key,))
        if intent_rows[intent_key] is not None and _row(intent_rows, intent_key, HumanOperationRejectionIntent) != intent:
            raise Problem(409, "BEHAVIOR_COMMAND_CONFLICT", "the rejection decision is bound to its original request")
        prior = self._existing(binding, "operation-reject", principal, command)
        if prior is not None and intent_rows[intent_key] is None:
            # Older plans without the decision binding may only read their original receipt.
            return self.store.reconcile(prior.request)
        if prior is None:
            if intent_rows[intent_key] is not None:
                raise Problem(409, "BEHAVIOR_REJECTION_CONFLICT", "the original rejection intent is missing its command plan")
            pending(self.store.read_snapshot((op_key, grant_key)), context)
        plan = prior or self._save_plan(
            binding, "operation-reject", principal, command, context,
            {"rejection": command.model_dump(mode="json")}, request,
            extra_writes=(_write(intent_key, intent, "create", material(intent.model_dump(mode="json"))),),
        )

        def commit():
            def decide(rows):
                _, current_context = self._state(binding, rows)
                operation, prepared = pending(rows, current_context)
                rejection = HumanOperationRejection(
                    schema_version=command.schema_version, operation_id=binding.operation_id,
                    decision_id=command.decision_id, description_digest=prepared.sha256,
                    adapter_contract_digest=operation.adapter_contract_digest,
                    rejected_by_oid=principal.oid, rejected_at=self._now(),
                )
                return ControlChanges(writes=(
                    _write(op_key, _updated(operation, human_rejection=rejection), "replace", prepared),
                    _write(decision_key, rejection, "create", material({
                        "authenticated_human_oid": principal.oid, "binding_id": binding.binding_id,
                        "decision": command.model_dump(mode="json"), "prepared": prepared.model_dump(mode="json"),
                    })),
                ), result={"operation_id": binding.operation_id, "decision_id": command.decision_id,
                           "description_digest": prepared.sha256, "rejected": True})

            return self.store.transact(plan.request, decide)

        return self._run(binding, plan, commit)

    def _reconcile_operation_decision(self, binding_id, command, principal, *, kind):
        def read():
            binding, _, _ = self.authorize(binding_id, principal, "read")
            if principal.kind != "human" or principal.actor_id is not None:
                code = "BEHAVIOR_REJECTION_DENIED" if kind == "operation-reject" else "BEHAVIOR_OPERATION_READ_DENIED"
                raise Problem(403, code, "only the original configured human may reconcile")
            plan = self._existing(binding, kind, principal, command)
            if plan is None:
                subject = "rejection" if kind == "operation-reject" else "operation decision"
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN",
                              f"original {subject} plan is unavailable; lookup does not submit a decision",
                              original_request_id=command.request_id, automatic_retry=False)
            return self.store.reconcile(plan.request)

        return self.store.consistent_read(read)

    def reconcile_operation_rejection(self, binding_id, command: wire.OperationRejection, principal):
        return self._reconcile_operation_decision(binding_id, command, principal, kind="operation-reject")

    def reconcile_operation_approval(self, binding_id, command: wire.OperationApproval, principal):
        return self._reconcile_operation_decision(binding_id, command, principal, kind="operation-approve")

    def reconcile_operation_revocation(self, binding_id, command: wire.OperationRevocation, principal):
        return self._reconcile_operation_decision(binding_id, command, principal, kind="operation-revoke")

    def execute_operation(self, binding_id, command: wire.Command, principal, *, reconcile=False):
        binding, _, context = self.authorize(binding_id, principal, "read" if reconcile else "execute")
        dispatcher = self.dep.broker.dispatcher
        if dispatcher is None:
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "no concrete adapter is configured")
        if reconcile:
            if hasattr(dispatcher, "receiver"):
                try:
                    return dispatcher.reconcile(binding.operation_id, binding.attempt_id, context)
                except Problem as exc:
                    if exc.body["error"]["code"] == "OPERATION_RECORD_MISSING":
                        raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "original dispatch evidence is unavailable") from None
                    raise
            outcome = OutcomeRequest(
                operation_id=binding.operation_id, attempt_id=binding.attempt_id,
                evidence_id=control_key("dispatch-result", binding.operation_id, binding.attempt_id),
            )
            return self.store.reconcile(dispatcher.completion_request(outcome, context))
        authorization = AuthorizationRequest(operation_id=binding.operation_id, attempt_id=binding.attempt_id, grant_id=binding.grant_id)
        if isinstance(dispatcher, WorkStateDispatcher):
            dispatcher.check_mutable_target(
                authorization, context,
                fixed_work_ids=tuple(job.work.work_id for registered in self.bindings.values()
                                     for job in registered.registered_jobs),
            )
        plan = self._save_plan(
            binding, "operation-execute", principal, command, context,
            {"authorization": authorization.model_dump(mode="json")},
            dispatcher.reconciliation_request(authorization, context, stage="consume"),
        )
        return self._run(
            binding, plan, lambda: dispatcher.execute(authorization, plan.context),
            lambda: self.execute_operation(binding_id, command, principal, reconcile=True),
        )

    def revoke_operation(self, binding_id, command: wire.OperationRevocation, principal):
        binding, _, context = self.authorize(binding_id, principal, "approve")
        grant_key = control_key("grant", binding.grant_id)
        decision_key = runtime_key("human-operation-revocation", binding.binding_id, command.decision_id)
        request = ControlRequest(
            request_id=runtime_key("operation-revoke", binding.binding_id, command.decision_id),
            request_digest=_digest([principal.oid, command.model_dump(mode="json"), self.binding_digest(binding)]),
            read_set=(*self._gate_keys(binding), grant_key),
        )
        plan = self._existing(binding, "operation-revoke", principal, command)
        if plan is None:
            index = self._decision_plan_index(binding, "operation-revoke", command, request)
            plan = self._save_plan(binding, "operation-revoke", principal, command, context,
                                  {"revocation": command.model_dump(mode="json")}, request, expected_index=index)

        def commit():
            def decide(rows):
                self._state(binding, rows)
                grant = _row(rows, grant_key, GrantRecord)
                if (
                    grant.grant_id != binding.grant_id or grant.revoked
                    or grant.description_digest != command.description_digest
                    or grant.revocation_revision != command.expected_revocation_revision
                ):
                    raise Problem(409, "BEHAVIOR_REVOCATION_CONFLICT", "revocation must identify the exact current grant")
                revoked = _updated(grant, revoked=True, revocation_revision=grant.revocation_revision + 1)
                return ControlChanges(writes=(
                    _write(grant_key, revoked, "replace"),
                    _write(decision_key, revoked, "create", material({
                        "authenticated_human_oid": principal.oid, "decision": command.model_dump(mode="json"),
                    })),
                ), result={"grant_id": binding.grant_id, "revoked": True, "revocation_revision": revoked.revocation_revision})

            return self.store.transact(plan.request, decide)

        return self._run(binding, plan, commit)

    def submit(self, binding_id, command: wire.SubmitWork, principal, *, reconcile=False):
        binding, policy, context = self.authorize(binding_id, principal, "read" if reconcile else "submit")
        plan = self._existing(binding, "admission", principal, command)
        if plan is None:
            if reconcile:
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "original admission plan is missing")
            now = self._now()
            deadline = (None if getattr(policy,"instruction",False) else
                        min(policy.valid_until, now + timedelta(seconds=self.dep.delivery.contract.max_queue_seconds)))
            ingress_id = runtime_key("authenticated-ingress", binding.binding_id, command.request_id)
            admission = AdmissionRequest(
                event=IncomingEvent(event_id=binding.event_id, root_request_id=binding.root_request_id,
                                    conversation_id=context.conversation_id, content=command.content.encode()),
                work=binding.work, route=binding.route, required_confirmation_level="client_received",
                deadline=deadline, expected_sequence=0, ingress_evidence_id=ingress_id,
            )
            registered = None if self.work_policy is None else self.work_policy.records.get(binding_id)
            routine = None if registered is None else registered.routine
            proof = VerifiedIngress(
                evidence_id=ingress_id, admission_digest=admission_digest(admission),
                verifier_version=self.dep.delivery.contract.verifier_version, actor_id=principal.source_actor_id,
                on_behalf_of_principal_id=context.principal_id,
                source_kind="routine" if routine is not None else "human" if principal.kind == "human" else "agent",
                policy_revision=policy.operation_policy_revision, observed_at=now,
                expires_at=deadline if deadline is not None else now+timedelta(seconds=self.dep.delivery.contract.max_queue_seconds),
                proof=_json({"oid": principal.oid, "tenant_id": principal.tenant_id,
                    "authentication": self.dep.evidence.authentication_version,
                    **({} if routine is None else {"routine":routine.model_dump(mode="json")})}),
            )
            plan = self._save_plan(binding, "admission", principal, command, context,
                                   {"admission": admission.model_dump(mode="json")}, self.dep.delivery.request_for(admission, context),
                                   extra_writes=(_write(runtime_key("ingress", ingress_id), proof, "create",
                                                        material(proof.model_dump(mode="json"))),))
        admission = _parse(plan.data["admission"], AdmissionRequest)
        if reconcile:
            self.store.reconcile(plan.request)
        else:
            admit=lambda:self.dep.delivery.admit(admission,plan.context)
            # An explicit same-command submit may complete its local admission EGT.
            # This does not repeat a worker start, model call, or external effect.
            original=(lambda:self.store._receipt(plan.request) or admit()) if getattr(policy,"instruction",False) else None
            self._run(binding,plan,admit,reconcile=original)
        self._enqueue(binding,principal,command,plan.context,
                      retry_original=not reconcile and getattr(policy,"instruction",False))
        return self.store.consistent_read(lambda: self.job_status(binding_id, principal), max_keys=None)

    def _enqueue(self, binding, principal, command, context, *, retry_original=False):
        worker = self.dep.workers
        spec = JobSpec(job_id=binding.job_id, work_id=binding.work.work_id,
                       input=self.job_input(binding), obligation_id=binding.obligation_id)
        reads = (
            worker_key("pool", worker.contract.workspace_id), worker_key("job", binding.job_id),
            *worker._owner_keys(context), control_key("delivery-obligation", binding.obligation_id),
            control_key("delivery-intent", control_key("delivery-intent-id", binding.event_id)),
        )
        request = worker._request("enqueue", (binding.job_id,), [spec.model_dump(mode="json"), context.model_dump(mode="json")], reads)
        plan = self._save_plan(binding, "enqueue", principal, command, context,
                               {"job": spec.model_dump(mode="json")}, request)
        enqueue=lambda:worker.enqueue(_parse(plan.data["job"],JobSpec),plan.context)
        original=(lambda:self.store._receipt(plan.request) or enqueue()) if retry_original else None
        self._run(binding,plan,enqueue,reconcile=original)

    def job_input(self, binding: RuntimeBinding) -> bytes:
        """Server-only profile seam; the legacy checksum definition is unchanged."""
        return executor_job_input(binding.job_id, binding.work)

    def _registered_child(self, binding, child_id):
        child = next((job for job in binding.child_jobs if job.job_id == child_id), None)
        if child is None:
            raise Problem(403, "BEHAVIOR_CHILD_DENIED", "child is not registered in this root binding")
        return child

    def _child_spec(self, binding, child):
        return JobSpec(job_id=child.job_id, work_id=child.work.work_id,
                       input=executor_job_input(child.job_id, child.work), parent_job_id=binding.job_id)

    def _child_record(self, binding: RuntimeBinding, child: RuntimeChildJob, job: JobRecord):
        if (job.spec != self._child_spec(binding, child) or any(
                getattr(job.owner, name) != getattr(binding.owner, name)
                for name in ExecutionContext.model_fields if name != "epoch")):
            raise Problem(403, "BEHAVIOR_CHILD_DENIED", "saved child parent/work/owner/input differs from server configuration")

    def enqueue_child(self, binding_id, child_id, command: wire.Command, principal, *, reconcile=False):
        binding, _, context = self.authorize(binding_id, principal, "read" if reconcile else "submit")
        child = self._registered_child(binding, child_id)
        worker = self.dep.workers
        kind = "child-enqueue-" + child_id
        plan = self._existing(binding, kind, principal, command)
        if plan is None:
            if reconcile:
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "original child enqueue plan is missing")
            parent = worker.inspect(binding.job_id)
            worker._scope(parent, context, exact=True)
            if parent.state not in {"starting", "running"} or parent.binding is None or parent.cancel_requested:
                raise Problem(409, "BEHAVIOR_PARENT_UNAVAILABLE", "only an already-started parent may accept a child")
            spec = self._child_spec(binding, child)
            reads = (worker_key("pool", worker.contract.workspace_id), worker_key("job", child_id),
                     *worker._owner_keys(context), worker_key("job", binding.job_id))
            request = worker._request("enqueue", (child_id,), [spec.model_dump(mode="json"), context.model_dump(mode="json")], reads)
            plan = self._save_plan(binding, kind, principal, command, context, {"job": spec.model_dump(mode="json")}, request)
        if _parse(plan.data["job"], JobSpec) != self._child_spec(binding, child):
            raise Problem(403, "BEHAVIOR_CHILD_DENIED", "child plan no longer matches its configured definition")
        if reconcile:
            self.store.reconcile(plan.request)
        else:
            self._run(binding, plan, lambda: worker.enqueue(_parse(plan.data["job"], JobSpec), plan.context))
        return self.child_status(binding_id, child_id, principal)

    def child_status(self, binding_id, child_id, principal):
        binding, _, context = self.authorize(binding_id, principal, "read")
        child = self._registered_child(binding, child_id)
        job = self.dep.workers.inspect(child_id)
        self.dep.workers._scope(job, context)
        self._child_record(binding, child, job)
        return wire.ChildJobStatus(
            job_id=child_id, work_id=child.work.work_id, parent_job_id=binding.job_id,
            state=job.state, generation=job.generation,
        )

    def job_status(self, binding_id, principal):
        binding, _, context = self.authorize(binding_id, principal, "read", historical_status=True)
        job_key = worker_key("job", binding.job_id)
        key = control_key("delivery-obligation", binding.obligation_id)
        rows = self.store.read_snapshot((job_key, key))
        job = self.dep.workers._row(rows, job_key, JobRecord)
        self.dep.workers._scope(job, context)
        profile = getattr(self, "profiles", {}).get(binding_id)
        if profile is not None and profile.instruction is not None and profile.instruction.peer_work is not None:
            if job.spec.obligation_id is not None or rows[key] is not None:
                raise Problem(409, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "a peer work cannot acquire a human delivery obligation")
            return wire.JobStatus(job_id=job.spec.job_id, work_id=job.spec.work_id, state=job.state,
                generation=job.generation, result_forwarded=job.result_forwarded,
                delivery_id=None, obligation_fulfilled=False)
        ob = _row(rows, key, _Obligation)
        return wire.JobStatus(
            job_id=job.spec.job_id, work_id=job.spec.work_id, state=job.state, generation=job.generation,
            result_forwarded=job.result_forwarded, delivery_id=ob.final_delivery_id,
            obligation_fulfilled=ob.fulfillment == "fulfilled",
        )

    def _controller_step(self, binding_id, controller):
        self._require_controller(controller)
        self.refresh_bindings(binding_id)
        binding = self.bindings.get(binding_id)
        if binding is None:
            raise Problem(403, "BEHAVIOR_BINDING_DENIED", "controller binding is not configured")
        if binding_id in self.historical_binding_ids:
            raise Problem(403, "CHANNELS_RETIRED_BINDING", "原已結案工作不可重新執行")
        if getattr(self, "agent_message_composition", None) is not None:
            from .behavior_agent_message_runtime import advance
            peer_status = advance(self, binding)
            if peer_status is not None:
                return peer_status
        context = self.store.consistent_read(lambda: self.resolve_owner(binding.owner.owner_record_id,binding_id=binding_id))
        job = self.dep.workers.inspect(binding.job_id)
        if (job.state in {"starting", "running", "capacity_uncertain"} and not job.cancel_requested
                and job.children and set(job.children).intersection(self.dep.workers.pending())):
            return self._controller_park(binding_id, controller)
        if job.state in {"queued", "waiting_dependency"}:
            self.dep.workers.start(binding.job_id, context)
        elif job.state not in {"succeeded", "failed", "cancelled"}:
            if self.dep.workers.instruction(binding.job_id) and job.cancel_requested and job.stop_credit:
                self.dep.workers.stop(binding.job_id,context)
            elif not (self.dep.workers.instruction(binding.job_id) and not job.cancel_requested
                      and job.owner == context and self.dep.native_launches is not None
                      and self.dep.native_launches.instruction_recently_active(job)):
                self.dep.workers.reconcile(binding.job_id, context)
        job = self.dep.workers.inspect(binding.job_id)
        if job.state in {"succeeded", "failed", "cancelled"}:
            delivery = self.dep.delivery
            if not job.result_forwarded:
                self.dep.workers.forward_result(binding.job_id, context, delivery)
            DeliveryConsumer(delivery).consume(binding.obligation_id, context, "behavior-api-consumer")
            if getattr(self, "agent_message_composition", None) is not None:
                from .behavior_agent_message_runtime import complete_origin
                complete_origin(self, binding)
        job = self.dep.workers.inspect(binding.job_id)
        key = control_key("delivery-obligation", binding.obligation_id)
        ob = _row(self.store.read_snapshot((key,)), key, _Obligation)
        return wire.JobStatus(
            job_id=job.spec.job_id, work_id=job.spec.work_id, state=job.state,
            generation=job.generation, result_forwarded=job.result_forwarded,
            delivery_id=ob.final_delivery_id, obligation_fulfilled=ob.fulfillment == "fulfilled",
        )

    def _controller_park(self, binding_id, controller):
        self._require_controller(controller)
        binding = self.bindings.get(binding_id)
        if binding is None:
            raise Problem(403, "BEHAVIOR_BINDING_DENIED", "controller binding is not configured")
        workers = self.dep.workers
        context = self.resolve_owner(binding.owner.owner_record_id)
        parent = workers.inspect(binding.job_id)
        workers._scope(parent, context, exact=True)
        children = []
        for child_id in parent.children:
            definition = self._registered_child(binding, child_id)
            child = workers.inspect(child_id)
            self._child_record(binding, definition, child)
            children.append(child)
        if (parent.binding is None or parent.cancel_requested
                or parent.state not in {"starting", "running", "capacity_uncertain", "waiting_dependency"}
                or not any(child.state not in {"succeeded", "failed", "cancelled"} for child in children)):
            raise Problem(409, "BEHAVIOR_PARENT_UNAVAILABLE", "park requires a started parent and its registered unfinished children")
        if parent.state != "waiting_dependency":
            executor = getattr(workers.adapter.start, "__self__", None) if workers.adapter else None
            if not isinstance(executor, FoundryExecutor) or executor.authority is not self.dep.native_launches:
                raise Problem(503, "BEHAVIOR_CONFIGURATION", "parent park requires the configured native executor")
            launch = executor.authority.launch(native_key("launch-id", parent.binding.start_request_id))
            if launch.pending_control not in {"park", "cancel"}:
                executor.authority.probe(parent.binding, "park")
            # The native intent selects park; the existing query lane reserves
            # its finite budget before any RPC and verifies idle/checkpoint proof.
            workers.reconcile(binding.job_id, context)
            parent = workers.inspect(binding.job_id)
        key = control_key("delivery-obligation", binding.obligation_id)
        obligation = _row(self.store.read_snapshot((key,)), key, _Obligation)
        return wire.JobStatus(
            job_id=parent.spec.job_id, work_id=parent.spec.work_id, state=parent.state, generation=parent.generation,
            result_forwarded=parent.result_forwarded, delivery_id=obligation.final_delivery_id,
            obligation_fulfilled=obligation.fulfillment == "fulfilled",
        )

    def _controller_drain(self, controller, *, limit):
        self._require_controller(controller)
        if type(limit) is not int or not 1 <= limit <= 32:
            raise Problem(422, "WORKER_SCAN_LIMIT", "runtime controller tick must be bounded to 1..32")
        workers = self.dep.workers
        pending = set(workers.pending())
        parks = []
        for binding in self.bindings.values():
            if not binding.child_jobs or binding.job_id not in pending:
                continue
            parent = workers.inspect(binding.job_id)
            if (parent.state in {"starting", "running", "capacity_uncertain"} and not parent.cancel_requested
                    and any(child in pending for child in parent.children)):
                parks.append(binding.binding_id)
        if parks:
            results = []
            for binding_id in parks[:limit]:
                try:
                    status = self._controller_park(binding_id, controller)
                    results.append(workers._result(job_id=status.job_id, status=status.state))
                except Problem as error:
                    results.append(workers._result(job_id=self.bindings[binding_id].job_id,
                                                   problem=error.body["error"]["code"]))
            return tuple(results)
        return WorkerConsumer(workers, self.dep.delivery).drain(self.resolve_owner, limit=limit)

    def _inbox_access(self, binding, policy, context, principal, command, action):
        key = control_key("delivery-obligation", binding.obligation_id)
        ob = _row(self.store.read_snapshot((key,)), key, _Obligation)
        if ob.final_delivery_id is None or principal.oid != binding.route.recipient_id:
            raise Problem(403, "BEHAVIOR_DELIVERY_DENIED", "no prepared delivery for this authenticated recipient")
        outbox_key = control_key("delivery-outbox", ob.final_delivery_id)
        outbox = _row(self.store.read_snapshot((outbox_key,)), outbox_key, _Outbox)
        from .behavior_work_policy import PerWorkRuntimePolicy
        current = type(policy) is PerWorkRuntimePolicy
        evidence_id = (runtime_key("current-pull-access",binding.binding_id,action) if current else
                       runtime_key("pull-access", binding.binding_id, action, command.request_id))
        if current and outbox.binding.version > 1:
            evidence_id = runtime_key("current-pull-access", binding.binding_id, action, outbox.binding.delivery_id)
        evidence_key = runtime_key("inbox-access", evidence_id)
        prior = self.store.read_snapshot((evidence_key,))[evidence_key]
        if prior is None:
            now = self._now()
            values = dict(
                evidence_id=evidence_id, principal_id=principal.oid,
                authentication_version=self.dep.evidence.authentication_version, binding=outbox.binding, action=action,
                policy_revision=policy.operation_policy_revision,
                proof=(_json({"oid":principal.oid,"action":action,"work_policy_id":policy.policy_id,
                               "binding_digest":self.binding_digest(binding)}) if current else
                       _json({"oid": principal.oid, "action": action, "request_digest": _digest(command.model_dump(mode="json"))})),
            )
            proof = (CurrentWorkInboxAccess(**values,work_policy_id=policy.policy_id,runtime_binding_id=binding.binding_id,
                        authenticated_at=now) if current else
                     VerifiedInboxAccess(**values,observed_at=now,expires_at=min(policy.valid_until,now+timedelta(seconds=60))))
            self.dep.evidence._save("inbox-access", proof, binding, context, self)
        return InboxFetch(obligation_id=binding.obligation_id, delivery_id=ob.final_delivery_id, access_evidence_id=evidence_id)

    def fetch(self, binding_id, command: wire.Command, principal, *, continuation=False):
        binding, policy, context = self.authorize(binding_id, principal, "read")
        from .behavior_result_delivery import continuation_key
        key = continuation_key(binding_id)
        if (not continuation and self.work_policy is not None and binding_id in self.work_policy.records
                and self.store.read_snapshot((key,))[key] is not None):
            raise Problem(409, "ORIGINAL_FETCH_CONTINUATION_REQUIRED",
                          "use explicit continuation of the original failed fetch, not a new fetch ID")
        access = self._inbox_access(binding, policy, context, principal, command, "fetch")
        return self.dep.inbox.fetch(access, context)

    def acknowledge(self, binding_id, command: wire.PullAcknowledgment, principal):
        binding, policy, context = self.authorize(binding_id, principal, "acknowledge")
        access = self._inbox_access(binding, policy, context, principal, command, "acknowledge")
        acknowledged = InboxAcknowledgment(
            **access.model_dump(), exchange_id=command.exchange_id, acknowledgment_token=command.acknowledgment_token,
            binding=DeliveryBinding.model_validate_json(command.binding.model_dump_json()),
            confirmation_level=command.confirmation_level,
        )
        return self.dep.inbox.acknowledge(acknowledged, context)

    def continue_fetch(self, binding_id, command: wire.Command, principal):
        from .behavior_result_delivery import require_continuation
        require_continuation(self, binding_id, command, principal)
        return self.fetch(binding_id, command, principal, continuation=True)

    def reconcile_fetch(self, binding_id, command: wire.Command, principal):
        from .behavior_result_delivery import require_continuation
        value, binding, context = require_continuation(self, binding_id, command, principal)
        evidence_id = runtime_key("current-pull-access", binding_id, "fetch", value.current.delivery_id)
        key = runtime_key("inbox-access", evidence_id)
        if self.store.read_snapshot((key,))[key] is None:
            exchange = control_key("inbox-exchange", value.current.delivery_id)
            if self.store.read_snapshot((exchange,))[exchange] is not None:
                raise Problem(503, "INBOX_FETCH_UNKNOWN", "exchange exists without its original access; do not resend")
            raise Problem(409, "INBOX_FETCH_NOT_STARTED", "no exchange has been created for the continued original result")
        return self.dep.inbox.reconcile_fetch(InboxFetch(
            obligation_id=binding.obligation_id, delivery_id=value.current.delivery_id,
            access_evidence_id=evidence_id), context)
