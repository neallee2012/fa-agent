"""Bounded client/server wire contracts; only stdlib and pydantic dependencies.

No DTO authenticates an actor, grants approval, chooses an owner/policy/budget,
or supplies a trusted clock. Echoed delivery bindings are data to verify against
the server's immutable outbox, not route configuration. Bytes use JSON base64.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ModelDeployment = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
MemoryKind = Literal["profile", "episode", "note"]
BackupId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Version = Annotated[int, Field(ge=1, le=2**31 - 1)]
SdkStartProfile = Literal["fa-sdk-start-gate-v1", "fa-sdk-start-gate-v2"]
# ADR-0005 PoC: a successful turn may release its still-running SDK to the conversation's next turn.
RESIDENT_WORKER_PROFILE = "fa-resident-worker-poc-v1"
WorkerProfile = Literal["fa-resident-worker-poc-v1"]
Checks = Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]
NATIVE_LAUNCH_PERMIT_HEADER = "x-client-behavior-launch-permit"
NATIVE_PROBE_HEADER = "x-client-behavior-probe-id"
NATIVE_KEEPALIVE_HEADER = "x-client-behavior-keepalive-launch"


def sdk_start_version(profile: SdkStartProfile) -> Literal["v1", "v2"]:
    if profile == "fa-sdk-start-gate-v1":
        return "v1"
    if profile == "fa-sdk-start-gate-v2":
        return "v2"
    raise ValueError("Unknown SDK start-gate profile")


class WireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False,
        ser_json_bytes="base64", val_json_bytes="base64",
    )


class HostedKeepalive(WireModel):
    """A read-only ingress observation, never a work result or stopping proof."""
    launch_id: Identifier
    invocation_id: Identifier
    session_id: Identifier
    read_only: Literal[True]


class Command(WireModel):
    request_id: Identifier


class ExactSourceRef(WireModel):
    source_id: Identifier
    version: Version
    sha256: Digest
    byte_length: Annotated[int, Field(ge=0, le=8192)]


class SourcePrepare(Command):
    """Optional UTC applicability deadline; never a client clock or delete schedule."""

    version: Version
    original: Annotated[bytes, Field(min_length=1, max_length=8192)]
    necessary: Annotated[tuple[ExactSourceRef, ...], Field(max_length=8)] = ()
    source_schema: Literal["fa-artifact-metrics-v1", "fa-human-text-v1"] = "fa-artifact-metrics-v1"
    retention_consent_sha256: Digest | None = None
    expires_at: datetime | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("source expiry must be explicit UTC")
        return value


class SnapshotCapture(Command):
    roots: Annotated[tuple[ExactSourceRef, ...], Field(min_length=1, max_length=8)]


class ClaimDraft(Command):
    version: Version
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    supporting: Annotated[tuple[ExactSourceRef, ...], Field(min_length=1, max_length=8)]
    effective_from: datetime
    expires_at: datetime | None = None
    supersedes: Digest | None = None
    kind: MemoryKind = Field(default="note", exclude_if=lambda value:value == "note")
    

class ClaimReview(ClaimDraft):
    decision_id: Identifier
    candidate_sha256: Digest
    evidence_sha256: Digest
    policy_version: Identifier
    reviewed_checks: tuple[Literal["necessity", "support", "contradiction", "sensitivity", "consent"], ...]

    @model_validator(mode="after")
    def checks(self):
        if len(self.reviewed_checks) != 5 or set(self.reviewed_checks) != {
            "necessity", "support", "contradiction", "sensitivity", "consent",
        }:
            raise ValueError("all five human review checks are required")
        for value in (self.effective_from, self.expires_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("claim applicability times must include a timezone")
        return self


class SourceWithdrawal(Command):
    withdrawal_id: Identifier
    reference: ExactSourceRef
    expected_access_revision: Version
    reason: Literal["Consent withdrawn by the configured human."] = "Consent withdrawn by the configured human."


class BackupPrepare(Command):
    snapshot_id: Identifier
    snapshot_request_id: Identifier


class QuarantineRestore(Command):
    """An independently retained reference is data, never recovery permission."""

    reference: dict[str, JsonValue]


class ContextRequest(Command):
    required_claim_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]


class MemoryProjectionCommand(Command):
    projection_id: Digest


class MemoryProjectionQuery(MemoryProjectionCommand):
    query: Annotated[str, Field(min_length=1, max_length=8192)]


class OperationPrepare(Command):
    input_ref: Identifier


class OperationApproval(Command):
    decision_id: Identifier
    description_digest: Digest
    adapter_contract_digest: Digest
    expires_at: datetime
    uses: Literal[1] = 1
    units: Literal[1] = 1

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("approval expiry must be explicit UTC")
        return value


class OperationRevocation(Command):
    decision_id: Identifier
    description_digest: Digest
    expected_revocation_revision: Annotated[int, Field(ge=0)]
    reason: Literal["Operation grant revoked by the configured human."] = "Operation grant revoked by the configured human."


class OperationRejection(Command):
    schema_version: Literal["fa-operation-rejection-v1"]
    decision_id: Identifier
    description_digest: Digest
    adapter_contract_digest: Digest


class OperationGrantObservation(WireModel):
    grant_id: Identifier
    approval_id: Identifier
    not_before: datetime
    expires_at: datetime
    policy_revision: Annotated[int, Field(ge=0, le=2**63 - 1)]
    revocation_revision: Annotated[int, Field(ge=0, le=2**63 - 1)]
    revoked: bool
    remaining_uses: Annotated[int, Field(ge=0, le=2**63 - 1)]
    remaining_units: Annotated[int, Field(ge=0, le=2**63 - 1)]


class OperationRejectionObservation(WireModel):
    decision_id: Identifier
    rejected_at: datetime


class OperationDecisionState(WireModel):
    schema_version: Literal["fa-operation-decision-state-v1"] = "fa-operation-decision-state-v1"
    purpose: Literal["observation-only-not-operation-permission"] = "observation-only-not-operation-permission"
    binding_id: Identifier
    operation_id: Identifier
    description_digest: Digest
    adapter_contract_digest: Digest
    observed_at: datetime
    current_policy_revision: Annotated[int, Field(ge=0, le=2**63 - 1)]
    effect_state: Literal["unknown", "not_applied", "applied", "partial"]
    business_state: Literal["pending", "succeeded", "failed"]
    current_attempt_id: Identifier | None
    unsettled_attempt_ids: Annotated[tuple[Identifier, ...], Field(max_length=16)]
    grant: OperationGrantObservation | None
    rejection: OperationRejectionObservation | None


class SubmitWork(Command):
    content: Literal["Run the deterministic lifecycle proof."] = "Run the deterministic lifecycle proof."


class ArtifactRef(WireModel):
    artifact_id: Identifier
    version: Identifier
    sha256: Digest


class DeliveryRoute(WireModel):
    tenant_id: Identifier
    account_id: Identifier
    recipient_id: Identifier
    channel_id: Identifier
    thread_id: Identifier


class DeliveryBinding(WireModel):
    obligation_id: Identifier
    delivery_id: Identifier
    version: Version
    payload_digest: Digest
    route: DeliveryRoute
    channel_version: Identifier
    artifacts: Annotated[tuple[ArtifactRef, ...], Field(max_length=16)] = ()


class PullAcknowledgment(Command):
    exchange_id: Identifier
    acknowledgment_token: Identifier
    binding: DeliveryBinding
    confirmation_level: Literal["client_received"] = "client_received"


class ExecutorJobInput(WireModel):
    schema_version: Literal["fa-executor-job-v1"] = "fa-executor-job-v1"
    job_id: Identifier
    work_id: Identifier
    definition_digest: Digest
    required_checks: Checks
    result_text: Literal["Deterministic lifecycle proof completed."] = "Deterministic lifecycle proof completed."


class ExecutorJobResult(WireModel):
    schema_version: Literal["fa-executor-job-v1"] = "fa-executor-job-v1"
    job_id: Identifier
    work_id: Identifier
    definition_digest: Digest
    passed_checks: Checks
    result_text: Literal["Deterministic lifecycle proof completed."] = "Deterministic lifecycle proof completed."


class JobStatus(WireModel):
    job_id: Identifier
    work_id: Identifier
    state: Literal["queued", "starting", "running", "capacity_uncertain", "waiting_dependency", "succeeded", "failed", "cancelled"]
    generation: Annotated[int, Field(ge=0, le=8)]
    result_forwarded: bool
    delivery_id: Identifier | None = None
    obligation_fulfilled: bool = False


class ChildJobStatus(WireModel):
    job_id: Identifier
    work_id: Identifier
    parent_job_id: Identifier
    state: Literal["queued", "starting", "running", "capacity_uncertain", "waiting_dependency", "succeeded", "failed", "cancelled"]
    generation: Annotated[int, Field(ge=0, le=8)]
    has_delivery_obligation: Literal[False] = False


class ChecksumCheckpoint(WireModel):
    workload_version: Literal["checksum-count-v1"] = "checksum-count-v1"
    input_digest: Digest
    processed_bytes: Annotated[int, Field(ge=0, le=8192)]
    prefix_digest: Digest
    newline_count: Annotated[int, Field(ge=0, le=8192)]


class ChecksumResult(WireModel):
    workload_version: Literal["checksum-count-v1"] = "checksum-count-v1"
    input_digest: Digest
    byte_count: Annotated[int, Field(ge=0, le=8192)]
    newline_count: Annotated[int, Field(ge=0, le=8192)]


class AssistantExecutionResult(WireModel):
    workload_version: Literal["assistant-v1.2"] = "assistant-v1.2"
    session_id: Digest
    result_digest: Digest


class SdkStartBinding(WireModel):
    schema_version: Literal["fa-sdk-start-binding-v1", "fa-sdk-start-binding-v2"]
    profile: SdkStartProfile
    session_id: Digest
    sdk_session_id: Identifier
    job_id: Identifier
    task_digest: Digest
    profile_digest: Digest
    execution_digest: Digest
    worker_configuration_digest: Digest
    code_sha256: Digest

    @property
    def version(self) -> Literal["v1", "v2"]:
        return sdk_start_version(self.profile)

    @model_validator(mode="after")
    def profile_version(self):
        if self.schema_version != "fa-sdk-start-binding-" + self.version:
            raise ValueError("SDK gate binding and server profile versions must agree")
        return self


class SdkStartOwner(WireModel):
    kind: Literal["local-offline", "hosted"]
    supervisor_id: Identifier
    launch_id: Identifier | None = None
    claim_id: Identifier | None = None
    process_generation: Version | None = None

    @model_validator(mode="after")
    def exact_owner(self):
        fields = (self.launch_id, self.claim_id, self.process_generation)
        if (self.kind == "hosted" and any(value is None for value in fields)
                or self.kind == "local-offline" and any(value is not None for value in fields)):
            raise ValueError("SDK gate owner must have its exact local or native claim scope")
        return self


class SdkVmStartOwner(WireModel):
    kind: Literal["vm-turn"] = "vm-turn"
    supervisor_id: Identifier
    ticket_id: Digest
    start_receipt_sha256: Digest


SdkRefusalCode = Literal[
    "SKILL_ACCESS_DISABLED", "SKILL_IDENTITY_DENIED", "SKILL_APPROVAL_DENIED",
    "SKILL_SOURCE_DENIED", "SKILL_SOURCE_CHANGED", "SKILL_CATALOG_LIMIT",
]

SdkMemoryRefusalCode = Literal[
    "WORK_MEMORY_READ_DISABLED", "WORK_MEMORY_SCOPE_DENIED", "WORK_MEMORY_SOURCE_CHANGED", "WORK_MEMORY_SOURCE_LIMIT",
    "MEMORY_CLAIM_REVOKED", "MEMORY_CLAIM_STALE",
    "MEMORY_CLAIM_NOT_EFFECTIVE", "MEMORY_CLAIM_EXPIRED", "MEMORY_REQUIRED_CONTEXT_TOO_LARGE",
    "HUMAN_REVIEW_REVOKED", "HUMAN_REVIEW_NOT_APPROVED",
    "SOURCE_EXPIRED", "SOURCE_WITHDRAWN", "SOURCE_STALE", "SOURCE_SCOPE_DENIED", "SOURCE_ACCESS_CHANGED",
    "ASSISTANT_SOURCE_DENIED", "ASSISTANT_CONTEXT_CHANGED", "ASSISTANT_CLAIM_EXPIRED",
    "BEHAVIOR_SOURCE_DENIED", "BEHAVIOR_UNSAFE_CONTENT",
]
SDK_MEMORY_REFUSAL_CODES = frozenset(get_args(SdkMemoryRefusalCode))


def sdk_refusal_allowed(profile: SdkStartProfile, code: str) -> bool:
    version = sdk_start_version(profile)
    return code in (get_args(SdkRefusalCode) if version == "v1" else SDK_MEMORY_REFUSAL_CODES)


class SdkStartRecord(WireModel):
    schema_version: Literal["fa-sdk-start-gate-record-v1", "fa-sdk-start-gate-record-v2"]
    binding: SdkStartBinding
    owner: SdkStartOwner | SdkVmStartOwner
    state: Literal["open", "entry_attempted", "closed_before_entry"]
    refusal_code: SdkRefusalCode | SdkMemoryRefusalCode | None

    @model_validator(mode="after")
    def closed_reason(self):
        if self.schema_version != "fa-sdk-start-gate-record-" + self.binding.version:
            raise ValueError("SDK gate record and original binding versions differ")
        if (self.state == "closed_before_entry") != (self.refusal_code is not None):
            raise ValueError("Only a closed SDK entry gate has a definite refusal")
        if self.refusal_code is not None and not sdk_refusal_allowed(self.binding.profile, self.refusal_code):
            raise ValueError("This refusal is not enabled by the original gate version")
        return self


class SdkNoStart(WireModel):
    schema_version: Literal["fa-sdk-no-start-v1", "fa-sdk-no-start-v2"]
    gate: SdkStartRecord
    owned_thread_completed: Literal[True]

    @field_validator("owned_thread_completed", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("Thread completion must be a typed observation")
        return value

    @model_validator(mode="after")
    def closed_gate(self):
        if (self.gate.state != "closed_before_entry"
                or self.schema_version != "fa-sdk-no-start-" + self.gate.binding.version
                or isinstance(self.gate.owner, SdkVmStartOwner)):
            raise ValueError("No-SDK disposition requires an acknowledged closed entry gate")
        return self


class SdkVmNoStart(WireModel):
    schema_version: Literal["fa-sdk-vm-no-start-v1"] = "fa-sdk-vm-no-start-v1"
    gate: SdkStartRecord
    owned_turn_completed: Literal[True]

    @field_validator("owned_turn_completed", mode="before")
    @classmethod
    def exact_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("Turn completion must be a typed observation")
        return value

    @model_validator(mode="after")
    def closed_turn_gate(self):
        if self.gate.state != "closed_before_entry" or not isinstance(self.gate.owner, SdkVmStartOwner):
            raise ValueError("VM no-SDK disposition requires its original closed turn gate")
        return self


class AssistantSdkStop(WireModel):
    """Owned SDK turn end: either the runtime's graceful exit or, for the resident PoC, its idle release.

    A release keeps the process running but unbinds every tool, permission and model callback from
    this job; only a task captured with the resident worker profile may present it.
    """
    runtime_version: Literal["1.0.83"] = "1.0.83"
    pid: Annotated[int, Field(ge=1)]
    exit_code: int | None = Field(default=None, exclude_if=lambda value: value is None)
    graceful: Literal[True] = True
    release: WorkerProfile | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def exit_or_release(self):
        if (self.exit_code is None) == (self.release is None):
            raise ValueError("SDK turn end requires exactly one of an exit code or a resident release")
        return self


class HostedIdentity(WireModel):
    agent_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
    agent_name: Identifier
    agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    session_id: Identifier


class HostedAssignment(WireModel):
    """Server response only; native identifiers are NOT client authorization."""

    launch_id: Identifier
    claim_id: Identifier
    invocation_id: Identifier
    identity: HostedIdentity
    process_generation: Version
    process_id: Identifier
    binding_digest: Digest
    input: Annotated[bytes, Field(min_length=1, max_length=16384)]
    input_digest: Digest
    checkpoint: Annotated[bytes, Field(max_length=8192)] | None = None
    checkpoint_digest: Digest | None = None
    previous_launch_id: Identifier | None = None
    chunk_bytes: Annotated[int, Field(ge=1, le=1024)] = 64
    checkpoint_after_bytes: Annotated[int, Field(ge=1, le=8192)] | None = None
    workload_version: Literal["checksum-count-v1", "assistant-v1.2"] = Field(
        default="checksum-count-v1", exclude_if=lambda value: value == "checksum-count-v1",
    )

    @model_validator(mode="after")
    def exact_material(self):
        if hashlib.sha256(self.input).hexdigest() != self.input_digest:
            raise ValueError("assignment input digest differs")
        if self.checkpoint is None:
            if self.checkpoint_digest is not None or self.previous_launch_id is not None:
                raise ValueError("resume requires retained checkpoint bytes")
        elif self.checkpoint_digest != hashlib.sha256(self.checkpoint).hexdigest() or self.previous_launch_id is None:
            raise ValueError("checkpoint binding differs")
        return self


class HostedClaimBody(Command):
    """Untrusted observations; the private permit is a separate HTTP header."""

    invocation_id: Identifier
    identity: HostedIdentity
    process_id: Identifier


class HostedClaimRequest(HostedClaimBody):
    """Credential-boundary DTO; normal serialization masks the capability."""

    launch_permit: SecretStr


class HostedClaimReceipt(WireModel):
    assignment: HostedAssignment
    claim_state: Literal["fresh", "replay"]
    report_token: SecretStr | None = None


class HostedProbeBody(HostedClaimBody):
    claim_id: Identifier


class HostedProbeRequest(HostedProbeBody):
    report_token: SecretStr


class HostedProbe(WireModel):
    probe_id: Identifier
    launch_id: Identifier
    claim_id: Identifier
    invocation_id: Identifier
    identity: HostedIdentity
    process_id: Identifier
    process_generation: Version
    binding_digest: Digest
    directive: Literal["observe", "park", "cancel"]
    control_request_id: Identifier | None
    expires_at: datetime


class HostedReportBody(HostedProbeBody):
    """Not fencing evidence without the server probe AND process capability."""

    probe_id: Identifier
    process_generation: Version
    binding_digest: Digest
    supervisor_id: Identifier
    sequence: Version
    phase: Literal["spawn_unknown", "running", "result_saved", "sealed", "sdk_not_started"]
    child_pid: Annotated[int, Field(ge=1)] | None = None
    outcome: Literal["succeeded", "parked", "stopped", "failed"] | None = None
    checkpoint: Annotated[bytes, Field(max_length=8192)] | None = None
    result: ChecksumResult | AssistantExecutionResult | None = None
    sdk_stop: AssistantSdkStop | None = Field(default=None, exclude_if=lambda value: value is None)
    sdk_no_start: SdkNoStart | None = Field(default=None, exclude_if=lambda value: value is None)
    exit_code: Annotated[int, Field(ge=-2**31, le=2**31 - 1)] | None = None
    child_exited: bool = False

    @model_validator(mode="after")
    def no_sdk_is_not_an_exit(self):
        if self.phase == "sdk_not_started":
            if (self.sdk_no_start is None or self.child_pid is not None or self.sdk_stop is not None
                    or self.child_exited or self.exit_code is not None or self.outcome != "failed"
                    or self.result is not None or self.checkpoint is not None):
                raise ValueError("No-SDK disposition cannot claim a child exit, result or SDK archive")
        elif self.sdk_no_start is not None:
            raise ValueError("No-SDK disposition needs its explicit report phase")
        return self


class HostedReport(HostedReportBody):
    report_token: SecretStr


class HostedEvidenceReceipt(WireModel):
    launch_id: Identifier
    probe_id: Identifier
    evidence_id: Identifier
    state: Literal["running", "unknown", "parked", "succeeded", "failed", "stopped"]
