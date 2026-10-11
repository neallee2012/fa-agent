"""Explicit human cancellation of a claimed SDK attempt using platform stop.

An installed operator-only service owns the transport and finite permission.
It never accepts a stop receipt, target, budget, policy or evidence from HTTP.
SDK shutdown/idle metadata cannot substitute for the bound stop endpoint result.
Cancellation first fences assistant tools/claims through their existing job gate.
The stop is one-shot; unknown dispatch or receipt persistence never resends it.
"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Annotated, Callable, Literal

from pydantic import Field, field_validator, model_validator

from .behavior_assistant import _ArtifactEffect, _ModelIntent, _Session, key as assistant_key
from .behavior_auth import BehaviorPrincipal
from .behavior_delivery import DeliveryCore, VerifiedWorkResult, _Model, _material, _parse, _update, _utc
from .behavior_foundry import FoundryTarget, NativeLaunchAuthority, NativeRecoveryBinding, _NativeEvidence, _NativeLaunch, _TargetReservation, native_key
from .behavior_migration import code_sha256
from .behavior_operations import control_key
from .behavior_server_config import ServerConfiguration
from .behavior_store import ControlChanges, ControlRequest, ControlWrite, Digest, InlineMaterial, _Receipt, GUARD_KEY
from .behavior_workers import ExecutorBinding, ExecutorEvidence, JobRecord, worker_key
from .errors import Problem
from .models import Identifier
from .source_materials import digest


def recovery_key(kind, identifier):
    return control_key("sdk-recovery-" + kind, identifier)


def denied(code, detail):
    return Problem(409, code, detail)


class RecoveryScope(_Model):
    recovery_id: Identifier
    configuration_sha256: Digest
    job_id: Identifier
    launch_id: Identifier
    model_request_sha256: Digest
    operator_oid: Identifier
    expires_at: datetime
    _time = field_validator("expires_at")(_utc)


class PreSdkRecoveryScope(_Model):
    recovery_id: Identifier
    configuration_sha256: Digest
    job_id: Identifier
    launch_id: Identifier
    model_request_sha256: None
    operator_oid: Identifier
    expires_at: datetime
    recovery_phase: Literal["before_sdk_acquire"]
    _time = field_validator("expires_at")(_utc)


class PreAssignmentRecoveryScope(PreSdkRecoveryScope):
    recovery_phase: Literal["before_sdk_assignment"]


class RecoveryPlan(_Model):
    recovery_id: Identifier
    configuration_sha256: Digest
    code_sha256: Digest
    operator_oid: Identifier
    binding: ExecutorBinding
    launch_id: Identifier
    target: FoundryTarget
    native_invocation_id: Identifier
    model_request_sha256: Digest
    snapshot_sha256: Digest

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class PreSdkRecoveryPlan(_Model):
    recovery_id: Identifier
    configuration_sha256: Digest
    code_sha256: Digest
    operator_oid: Identifier
    binding: ExecutorBinding
    launch_id: Identifier
    target: FoundryTarget
    native_invocation_id: Identifier
    model_request_sha256: None
    snapshot_sha256: Digest
    recovery_phase: Literal["before_sdk_acquire"]

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class PreAssignmentRecoveryPlan(PreSdkRecoveryPlan):
    recovery_phase: Literal["before_sdk_assignment"]
    native_claim_request: ControlRequest
    native_claim_receipt_sha256: Digest


class ModelIntentState(_Model):
    request_sha256: Digest
    consumed: bool


class ModelIntentProof(ModelIntentState):
    image_sha256: Digest
    reserve_request_sha256: Digest
    reserve_receipt_sha256: Digest
    consume_request_sha256: Digest | None
    consume_receipt_sha256: Digest | None

    @model_validator(mode="after")
    def matched_consumption(self):
        if self.consumed != (self.consume_request_sha256 is not None) or self.consumed != (self.consume_receipt_sha256 is not None):
            raise ValueError("consumed intent requires its exact original receipt")
        return self


def _ordered_intents(values):
    hashes = tuple(item.request_sha256 for item in values)
    if hashes != tuple(sorted(set(hashes))):
        raise ValueError("model intents must be unique and sorted")
    return values


class MultiModelRecoveryScope(_Model):
    recovery_id: Identifier
    configuration_sha256: Digest
    job_id: Identifier
    launch_id: Identifier
    operator_oid: Identifier
    expires_at: datetime
    recovery_phase: Literal["after_sdk_model_requests"]
    model_intents: Annotated[tuple[ModelIntentState, ...], Field(min_length=2, max_length=6)]
    _time = field_validator("expires_at")(_utc)
    _ordered = field_validator("model_intents")(_ordered_intents)


class MultiModelRecoveryPlan(_Model):
    recovery_id: Identifier
    configuration_sha256: Digest
    code_sha256: Digest
    operator_oid: Identifier
    binding: ExecutorBinding
    launch_id: Identifier
    target: FoundryTarget
    native_invocation_id: Identifier
    snapshot_sha256: Digest
    recovery_phase: Literal["after_sdk_model_requests"]
    model_intents: Annotated[tuple[ModelIntentProof, ...], Field(min_length=2, max_length=6)]
    inventory_sha256: Digest
    inventory_guard_revision: Annotated[int, Field(ge=0)]
    inventory_guard_sha256: Digest
    inventory_guard_etag: str
    original_job_sha256: Digest
    _ordered = field_validator("model_intents")(_ordered_intents)

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class FinishedResultRecoveryScope(MultiModelRecoveryScope):
    recovery_phase: Literal["after_durable_finish"]
    model_intents: Annotated[tuple[ModelIntentState, ...], Field(min_length=1, max_length=6)]


class FinishedResultProof(_Model):
    session_image_sha256: Digest
    artifact_image_sha256: Digest
    result_image_sha256: Digest
    native_claim_request_sha256: Digest
    native_claim_receipt_sha256: Digest
    assignment_request_sha256: Digest
    assignment_receipt_sha256: Digest
    acquire_request_sha256: Digest
    acquire_receipt_sha256: Digest
    finish_request_sha256: Digest
    finish_receipt_sha256: Digest
    artifact_id: Identifier
    artifact_version: Identifier
    artifact_sha256: Digest
    artifact_blob_etag: Annotated[str, Field(min_length=1, max_length=1024)]
    execution_closure: Literal["platform_stop_after_durable_finish"] = "platform_stop_after_durable_finish"
    sdk_outcome: Literal["not_observed"] = "not_observed"


class FinishedResultRecoveryPlan(MultiModelRecoveryPlan):
    recovery_phase: Literal["after_durable_finish"]
    model_intents: Annotated[tuple[ModelIntentProof, ...], Field(min_length=1, max_length=6)]
    finished_result: FinishedResultProof


class TeamsPreAcquireRecoveryScope(MultiModelRecoveryScope):
    schema_version: Literal["fa-teams-pre-acquire-recovery-scope-v1"] = "fa-teams-pre-acquire-recovery-scope-v1"
    recovery_phase: Literal["teams_before_sdk_acquire"]
    model_intents: tuple[()] = ()
    event_id: Digest


class TeamsPreAcquireRecoveryPlan(MultiModelRecoveryPlan):
    schema_version: Literal["fa-teams-pre-acquire-recovery-plan-v1"] = "fa-teams-pre-acquire-recovery-plan-v1"
    recovery_phase: Literal["teams_before_sdk_acquire"]
    model_intents: tuple[()] = ()
    event_id: Digest
    original_rows: Annotated[dict[Identifier, Digest], Field(min_length=1, max_length=32)]
    original_receipts: Annotated[dict[Identifier, Digest], Field(min_length=1, max_length=16)]
    original_job_state: Literal["starting", "running", "capacity_uncertain"]
    original_last_sequence: Annotated[int, Field(ge=0)]
    original_last_evidence_id: Identifier | None
    original_native_execution_id: Identifier | None
    original_scans_remaining: Annotated[int, Field(ge=0)]


class ExpiredTeamsCleanupObservation(_Model):
    schema_version: Literal["fa-expired-teams-cleanup-observation-v1"] = "fa-expired-teams-cleanup-observation-v1"
    purpose: Literal["stop_original_and_deliver_failure"]
    evidence_id: Identifier
    observer_oid: Identifier
    configuration_sha256: Digest
    code_sha256: Digest
    event_id: Digest
    job_id: Identifier
    launch_id: Identifier
    inventory_sha256: Digest
    guard_revision: Annotated[int, Field(ge=0)]
    target_sha256: Digest
    original_native_evidence_sha256: Digest
    retained_authority_sha256: Digest
    protection_observation_sha256: Digest
    deployment_observation_sha256: Digest
    observed_at: datetime
    expires_at: datetime
    coverage: Literal["original-execution-and-exact-failure-only"]
    proof: Annotated[bytes, Field(min_length=1, max_length=2048)]
    _times = field_validator("observed_at", "expires_at")(_utc)


class ExpiredTeamsCleanupScope(TeamsPreAcquireRecoveryScope):
    schema_version: Literal["fa-expired-teams-cleanup-scope-v1"] = "fa-expired-teams-cleanup-scope-v1"
    recovery_phase: Literal["expired_original_teams_pre_acquire"]
    purpose: Literal["stop_original_and_deliver_failure"]
    not_before: datetime
    observation_sha256: Digest
    _start = field_validator("not_before")(_utc)

    @model_validator(mode="after")
    def finite_cleanup(self):
        if not self.not_before < self.expires_at <= self.not_before + timedelta(minutes=60):
            raise ValueError("cleanup requires an independent window of at most sixty minutes")
        return self


class ExpiredTeamsCleanupPlan(TeamsPreAcquireRecoveryPlan):
    schema_version: Literal["fa-expired-teams-cleanup-plan-v1"] = "fa-expired-teams-cleanup-plan-v1"
    recovery_phase: Literal["expired_original_teams_pre_acquire"]
    purpose: Literal["stop_original_and_deliver_failure"]
    not_before: datetime
    cleanup_expires_at: datetime
    observation: ExpiredTeamsCleanupObservation
    _times = field_validator("not_before", "cleanup_expires_at")(_utc)


def _model_digests(value):
    if isinstance(value, (MultiModelRecoveryScope, MultiModelRecoveryPlan)):
        return tuple(item.request_sha256 for item in value.model_intents)
    return value.model_request_sha256


class RecoveryAuthorization(_Model):
    plan: RecoveryPlan | PreSdkRecoveryPlan | PreAssignmentRecoveryPlan | MultiModelRecoveryPlan | FinishedResultRecoveryPlan | TeamsPreAcquireRecoveryPlan | ExpiredTeamsCleanupPlan
    decision_id: Identifier
    approved_at: datetime
    expires_at: datetime
    _times = field_validator("approved_at", "expires_at")(_utc)


class TeamsPreAcquireRecoveryAuthorization(RecoveryAuthorization):
    schema_version: Literal["fa-teams-pre-acquire-recovery-authorization-v1"] = "fa-teams-pre-acquire-recovery-authorization-v1"
    plan: TeamsPreAcquireRecoveryPlan


class ExpiredTeamsCleanupAuthorization(RecoveryAuthorization):
    schema_version: Literal["fa-expired-teams-cleanup-authorization-v1"] = "fa-expired-teams-cleanup-authorization-v1"
    plan: ExpiredTeamsCleanupPlan


def _authorization_model(plan):
    if isinstance(plan, ExpiredTeamsCleanupPlan):
        return ExpiredTeamsCleanupAuthorization
    return TeamsPreAcquireRecoveryAuthorization if isinstance(plan, TeamsPreAcquireRecoveryPlan) else RecoveryAuthorization


class PlatformStopObservation(_Model):
    target: FoundryTarget
    endpoint: str
    credential_client_id: Identifier
    status_code: Literal[204, 409]
    error_code: Literal["session_already_stopped"] | None = None
    provider_request_id: str | None = None
    observed_at: datetime
    _time = field_validator("observed_at")(_utc)


class _StopIntent(_Model):
    authorization: RecoveryAuthorization | TeamsPreAcquireRecoveryAuthorization | ExpiredTeamsCleanupAuthorization
    fenced_at: datetime
    _time = field_validator("fenced_at")(_utc)


class _PlatformProof(_Model):
    kind: Literal["native_platform_stop"] = "native_platform_stop"
    intent: _StopIntent
    observation: PlatformStopObservation
    cancellation_receipt_sha256: Digest
    stop_receipt_sha256: Digest


class TeamsPreAcquireRecoveryHistory(_PlatformProof):
    schema_version: Literal["fa-teams-pre-acquire-recovery-history-v1"] = "fa-teams-pre-acquire-recovery-history-v1"


class ExpiredTeamsCleanupHistory(_PlatformProof):
    schema_version: Literal["fa-expired-teams-cleanup-history-v1"] = "fa-expired-teams-cleanup-history-v1"


def _proof_model(plan):
    if isinstance(plan, ExpiredTeamsCleanupPlan):
        return ExpiredTeamsCleanupHistory
    return TeamsPreAcquireRecoveryHistory if isinstance(plan, TeamsPreAcquireRecoveryPlan) else _PlatformProof


def _fence_stage(plan):
    return ("cancel-teams-pre-acquire" if isinstance(plan, TeamsPreAcquireRecoveryPlan) else
            "fence-finished-result" if isinstance(plan, FinishedResultRecoveryPlan) else "cancel-model-set")


def stop_endpoint(target):
    return (target.project_endpoint + "/agents/" + target.agent_name + "/endpoint/sessions/"
            + target.session_id + ":stop?api-version=v1")


def _recovery_keys(native, launch, model_digest, *, before_assignment=False):
    binding = launch.start.binding
    sid = assistant_key("session-id", binding.start_request_id)
    return tuple(dict.fromkeys((
        *native._keys(launch), worker_key("pool", native.workers.contract.workspace_id),
        assistant_key("session", sid),
        *(assistant_key("model-intent", sid, value) for value in (
            model_digest if isinstance(model_digest, tuple) else () if model_digest is None else (model_digest,))),
        assistant_key("artifact", binding.work_id), assistant_key("result", binding.work_id),
        *((assistant_key("archive", sid), assistant_key("restore", sid)) if before_assignment else ()),
    )))


def _unassigned_claim(inventory, launch, native_keys, *, original_request=None):
    """Exact complete-inventory absence and immutable native claim provenance."""
    sid = assistant_key("session-id", launch.start.binding.start_request_id)
    absent = tuple(assistant_key(kind, sid) for kind in ("session", "archive", "restore"))
    absent += (assistant_key("artifact", launch.start.binding.work_id), assistant_key("result", launch.start.binding.work_id))
    if any(key in inventory.rows for key in absent) or any(key in inventory.receipts for key in (
            assistant_key("issue", sid), assistant_key("acquire", sid, "sdk-start"))):
        raise denied("RECOVERY_ORIGINAL_CHANGED", "assignment/acquire or SDK material is already retained")
    for key, row in inventory.rows.items():
        request_digest = row.payload["data"].get("request_sha256")
        if isinstance(request_digest, str) and key == assistant_key("model-intent", sid, request_digest):
            raise denied("RECOVERY_ORIGINAL_CHANGED", "original model intent must remain absent")
    if (launch.start.checkpoint is not None or launch.claim_id is None or launch.process_id is None
            or launch.native_invocation_id is None or launch.report_token_digest is None):
        raise denied("RECOVERY_ORIGINAL_CHANGED", "one exact committed fresh native claim is required")
    return _claimed_receipt(inventory, launch, native_keys, original_request=original_request)


def _claimed_receipt(inventory, launch, native_keys, *, original_request=None):
    body = {
        "launch_id": launch.launch_id, "process_id": launch.process_id, "permit_digest": launch.permit_digest,
        "invocation_id": launch.native_invocation_id,
        "identity": {name: getattr(launch.target, name) for name in ("agent_id", "agent_name", "agent_version", "session_id")},
    }
    expected_digest = digest(body)
    matches = []
    for key, row in inventory.receipts.items():
        receipt = _parse(row.payload, _Receipt)
        if (receipt.request.request_digest == expected_digest
                and receipt.result == {"claim_id": launch.claim_id}):
            raw = json.dumps(row.payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            if (receipt.request.request_id != key or receipt.request.read_set != tuple(sorted(set(native_keys)))
                    or receipt.binding != inventory.guard.payload["binding"]
                    or receipt.guard_revision > inventory.guard.payload["revision"]
                    or set(dict(receipt.writes)) != {native_key("launch", launch.launch_id)}
                    or len(receipt.writes) != 1
                    or inventory.entity_sha256.get("behavior:v1:receipt:" + key) != hashlib.sha256(raw).hexdigest()):
                raise denied("RECOVERY_CLAIM_UNVERIFIED", "native claim receipt identity/scope/hash differs")
            matches.append(row)
    if len(matches) != 1 or original_request is not None and matches[0].payload["request"] != original_request.model_dump(mode="json"):
        raise denied("RECOVERY_CLAIM_UNVERIFIED", "one exact original native claim receipt is required")
    return matches[0]


def _multi_models(inventory, config, launch, expected, *, finished=False):
    """Read the complete original intent set and its immutable producer receipts."""
    if config.schema_version == "fa-behavior-server-v2":
        from .behavior_teams_recovery import original_teams_state
        if expected or finished:
            raise denied("RECOVERY_MODEL_SET_CHANGED", "Teams pre-acquire closure requires zero model intents")
        original_teams_state(inventory, config, launch)
        return ()
    from .behavior_recovery_history import _Inventory
    from .behavior_runtime import runtime_key
    view = _Inventory(inventory, config)
    sid, binding = assistant_key("session-id", launch.start.binding.start_request_id), launch.start.binding
    session_key = assistant_key("session", sid)
    session = view.row(session_key, _Session)
    configured = next(item for item in config.bindings if item.job_id == binding.job_id)
    selected = {assistant_key("model-intent", sid, item.request_sha256): item for item in expected}
    actual = {key for key, row in inventory.rows.items()
              if isinstance(value := row.payload["data"].get("request_sha256"), str)
              and key == assistant_key("model-intent", sid, value)}
    if (actual != set(selected) or session.model_requests != len(expected)
            or session.state != ("ready_to_finalize" if finished else "waiting")
            or (session.reason not in (None, "sdk_failed") if finished else session.reason != "sdk_failed")
            or session.execution != binding or session.assignment.resume or launch.start.checkpoint is not None
            or configured.route.channel_id != "authenticated-pull"
            or any(assistant_key(kind, sid) in inventory.rows for kind in ("archive", "restore"))
            or not finished and any(assistant_key(kind, binding.work_id) in inventory.rows for kind in ("artifact", "result"))):
        raise denied("RECOVERY_MODEL_SET_CHANGED", "complete original model/session set differs")
    base = (runtime_key("policy", configured.binding_id), control_key("owner", binding.owner.owner_record_id),
            control_key("policy", binding.owner.policy_id), control_key("budget", binding.owner.root_budget_id),
            worker_key("job", binding.job_id), worker_key("pool", config.workers.workspace_id), session_key,
            assistant_key("delegation", configured.event_id), control_key("delivery-obligation", configured.obligation_id))
    context = view.receipts.get(assistant_key("context", sid, "current-context"))
    if session.context_read and (context is None
            or context.request.request_digest != digest({"request_id": "current-context"})
            or context.request.read_set[:len(base)] != base or context.result != {"state": "accepted"}
            or set(dict(context.writes)) != {session_key}):
        raise denied("RECOVERY_MODEL_RECEIPT_INVALID", "original context read lacks its bound producer receipt")
    proofs = []
    for key, item in selected.items():
        model = view.row(key, _ModelIntent)
        reserve_id = assistant_key("model", sid, item.request_sha256)
        reserve = view.receipts.get(reserve_id)
        consume_id = assistant_key("model-send", sid, item.request_sha256)
        consume = view.receipts.get(consume_id)
        if (model.request_sha256 != item.request_sha256 or model.consumed != item.consumed or reserve is None
                or reserve.request.request_digest != digest({"request_id": item.request_sha256,
                                                            "request_sha256": item.request_sha256})
                or reserve.request.read_set[:len(base)] != base or len(reserve.request.read_set) <= len(base)
                or session.context_read and reserve.request.read_set != context.request.read_set
                or len(set(reserve.request.read_set)) != len(reserve.request.read_set)
                or reserve.result != {"state": "accepted"}
                or set(dict(reserve.writes)) != {key, session_key, control_key("budget", binding.owner.root_budget_id)}
                or dict(reserve.writes)[key] != view.image_hash(key, model.model_copy(update={"consumed": False}))
                or model.consumed != (consume is not None)):
            raise denied("RECOVERY_MODEL_RECEIPT_INVALID", "original reservation/image/consume absence differs")
        if consume is not None and (
                consume.request.read_set != (*base, key, *reserve.request.read_set[len(base):])
                or consume.result != {"state": "accepted"} or consume.writes != ((key, view.hash(key)),)
                or consume.guard_revision <= reserve.guard_revision):
            raise denied("RECOVERY_MODEL_RECEIPT_INVALID", "original consume request/image/causal receipt differs")
        proof = ModelIntentProof(request_sha256=item.request_sha256, consumed=item.consumed, image_sha256=view.hash(key),
            reserve_request_sha256=digest(reserve.request.model_dump(mode="json")),
            reserve_receipt_sha256=digest(inventory.receipts[reserve_id].payload),
            consume_request_sha256=None if consume is None else digest(consume.request.model_dump(mode="json")),
            consume_receipt_sha256=None if consume is None else digest(inventory.receipts[consume_id].payload))
        if isinstance(item, ModelIntentProof) and proof != item:
            raise denied("RECOVERY_MODEL_RECEIPT_INVALID", "original reviewed model receipts changed")
        proofs.append(proof)
    return tuple(proofs)


def _finished_proof(inventory, config, launch, blob_etag, *, expected=None):
    """Pure exact original finish chain; live caller separately verifies M3 bytes."""
    from .behavior_recovery_history import _Inventory
    from .behavior_runtime import runtime_key
    from .behavior_assistant_wire import CHECKS
    view = _Inventory(inventory, config)
    binding = launch.start.binding
    configured = next(item for item in config.bindings if item.job_id == binding.job_id)
    profile = next(item for item in config.assistant.profiles if item.binding_id == configured.binding_id)
    sid = assistant_key("session-id", binding.start_request_id)
    skey, akey, rkey = assistant_key("session", sid), assistant_key("artifact", binding.work_id), assistant_key("result", binding.work_id)
    session = view.row(skey, _Session)
    effect = view.row(akey, _ArtifactEffect)
    result = view.row(rkey, VerifiedWorkResult, material=True)
    if (session.state != "ready_to_finalize" or session.reason not in (None, "sdk_failed") or not session.context_read
            or session.execution != binding or session.assignment.task.work_id != binding.work_id
            or effect.execution != binding or effect.stage != "saved" or effect.authority_source != "human_admission"
            or effect.description_digest != digest(effect.description.model_dump(mode="json"))
            or effect.artifact_sha256 != hashlib.sha256(effect.content).hexdigest()
            or result.evidence_id != rkey or result.state != "succeeded" or result.work_id != binding.work_id
            or result.verifier_version != config.delivery.verifier_version
            or result.definition_digest != digest(launch.definition.model_dump(mode="json"))
            or result.passed_checks != CHECKS or result.known_effects or result.unresolved_operations
            or len(result.artifacts) != 1 or result.artifacts[0].artifact_id != profile.artifact_id
            or result.artifacts[0].version != profile.artifact_version
            or result.artifacts[0].sha256 != effect.artifact_sha256 or result.content != effect.content
            or result.proof != _material({"effect": akey, "description_digest": effect.description_digest,
                "finish_request_id": "finish-task", "criteria_version": launch.definition.criteria_version}).data):
        raise denied("RECOVERY_FINISHED_RESULT_INVALID", "original complete result/artifact/criteria differ")
    base = (runtime_key("policy", configured.binding_id), control_key("owner", binding.owner.owner_record_id),
            control_key("policy", binding.owner.policy_id), control_key("budget", binding.owner.root_budget_id),
            worker_key("job", binding.job_id), worker_key("pool", config.workers.workspace_id), skey,
            assistant_key("delegation", configured.event_id), control_key("delivery-obligation", configured.obligation_id))
    context_id = assistant_key("context", sid, "current-context")
    context = view.receipts.get(context_id)
    if context is None or context.request.read_set[:len(base)] != base:
        raise denied("RECOVERY_FINISHED_RESULT_INVALID", "original reviewed context receipt is missing")
    issue_id, acquire_id, finish_id = (assistant_key(action, sid, *ids) for action, ids in (
        ("issue", ()), ("acquire", ("sdk-start",)), ("finish", ("finish-task",))))
    issue = view.receipt(ControlRequest(request_id=issue_id, request_digest=digest(launch.start.model_dump(mode="json")),
                                       read_set=base), (skey,), result={"session_id": sid})
    acquire = view.receipt(ControlRequest(request_id=acquire_id, request_digest=digest({"request_id": "sdk-start"}),
                                         read_set=base), (skey,), result={"state": "running"})
    finish_reads = tuple(dict.fromkeys((*context.request.read_set, akey,
        control_key("operation", configured.operation_id), rkey, control_key("delivery-work-state", configured.obligation_id))))
    finish = view.receipt(ControlRequest(request_id=finish_id, request_digest=digest({"request_id": "finish-task"}),
                                        read_set=finish_reads), (skey, rkey),
        exact={skey: view.hash(skey), rkey: view.hash(rkey)},
        result={"state": "ready_to_finalize", "receipt_id": rkey, "artifact_sha256": effect.artifact_sha256})
    native_keys = (native_key("launch", launch.launch_id), native_key("grant", launch.grant_id),
                  control_key("owner", binding.owner.owner_record_id), control_key("policy", binding.owner.policy_id),
                  control_key("budget", binding.owner.root_budget_id), worker_key("job", binding.job_id),
                  native_key("target", launch.target.executor_id))
    claimed = _claimed_receipt(inventory, launch, native_keys)
    if not claimed.payload["guard_revision"] < issue.guard_revision < acquire.guard_revision < context.guard_revision < finish.guard_revision:
        raise denied("RECOVERY_FINISHED_RESULT_INVALID", "original claim/assignment/acquire/context/finish order differs")
    value = FinishedResultProof(
        session_image_sha256=view.hash(skey), artifact_image_sha256=view.hash(akey), result_image_sha256=view.hash(rkey),
        native_claim_request_sha256=digest(claimed.payload["request"]), native_claim_receipt_sha256=digest(claimed.payload),
        assignment_request_sha256=digest(issue.request.model_dump(mode="json")),
        assignment_receipt_sha256=digest(inventory.receipts[issue_id].payload),
        acquire_request_sha256=digest(acquire.request.model_dump(mode="json")),
        acquire_receipt_sha256=digest(inventory.receipts[acquire_id].payload),
        finish_request_sha256=digest(finish.request.model_dump(mode="json")),
        finish_receipt_sha256=digest(inventory.receipts[finish_id].payload),
        artifact_id=profile.artifact_id, artifact_version=profile.artifact_version,
        artifact_sha256=effect.artifact_sha256, artifact_blob_etag=blob_etag)
    if expected is not None and value != expected:
        raise denied("RECOVERY_FINISHED_RESULT_INVALID", "reviewed original finish references changed")
    return value


def _multi_inventory(inventory, config, plan, recovery_keys, authorization=None):
    """Undo only this exact recovery's own pre-seal commits, not unrelated writes."""
    from .behavior_recovery_history import _Inventory
    view = _Inventory(inventory, config)
    hashes, revision = dict(inventory.entity_sha256), plan.inventory_guard_revision
    job_key = worker_key("job", plan.binding.job_id)
    fence_stage = _fence_stage(plan)
    stages = ("authorization", fence_stage, "stop-intent", "stop-observation")
    missing = False
    for stage in stages:
        request_id = recovery_key(stage, plan.recovery_id)
        receipt = view.receipts.get(request_id)
        if receipt is None:
            if stage != fence_stage and request_id in inventory.rows:
                raise denied("RECOVERY_HISTORY_INVALID", "recovery row lacks its exact original receipt")
            missing = True
            continue
        if missing or receipt.guard_revision != revision + 1:
            raise denied("RECOVERY_PLAN_STALE", "unrelated authority commit interleaved with original recovery")
        revision += 1
        if stage == "authorization":
            value = view.row(request_id, _authorization_model(plan), material=True)
            if value.plan != plan or authorization is not None and value != authorization:
                raise denied("RECOVERY_AUTHORIZATION_INVALID", "original approval differs")
            expected = ControlRequest(request_id=request_id, request_digest=digest(value.model_dump(mode="json")))
            view.receipt(expected, (request_id,), exact={request_id: view.hash(request_id)},
                         result={"authorization_key": request_id})
        elif stage == fence_stage:
            view.receipt(ControlRequest(request_id=request_id, request_digest=plan.sha256, read_set=recovery_keys),
                         (job_key,))
            hashes["behavior:v1:row:" + job_key] = plan.original_job_sha256
        else:
            model = _StopIntent if stage == "stop-intent" else PlatformStopObservation
            value = view.row(request_id, model, material=True)
            if stage == "stop-intent" and (value.authorization.plan != plan
                    or authorization is not None and value.authorization != authorization):
                raise denied("RECOVERY_AUTHORIZATION_INVALID", "original stop intent differs")
            view.receipt(ControlRequest(request_id=request_id, request_digest=digest(value.model_dump(mode="json")),
                read_set=recovery_keys if stage == "stop-intent" else ()),
                (request_id, job_key) if stage == "stop-intent" else (request_id,),
                exact={request_id: view.hash(request_id)},
                result={"stop_reserved": True} if stage == "stop-intent" else {"provider_stop_observed": True})
            if stage == "stop-intent":
                hashes["behavior:v1:row:" + job_key] = plan.original_job_sha256
        hashes.pop("behavior:v1:receipt:" + request_id)
        if stage != fence_stage:
            hashes.pop("behavior:v1:row:" + request_id)
    hashes[GUARD_KEY] = plan.inventory_guard_sha256
    if (inventory.guard.payload["revision"] != revision or digest(hashes) != plan.inventory_sha256
            or revision == plan.inventory_guard_revision and inventory.guard.etag != plan.inventory_guard_etag):
        raise denied("RECOVERY_PLAN_STALE", "complete original inventory changed outside verified recovery commits")


def _cancel_request(native, plan):
    if isinstance(plan, TeamsPreAcquireRecoveryPlan):
        from .behavior_teams_recovery import recovery_keys
        return ControlRequest(request_id=recovery_key(_fence_stage(plan), plan.recovery_id),
                              request_digest=plan.sha256,
                              read_set=recovery_keys(native.assistant.runtime.teams_metrics.config,
                                                     native.launch(plan.launch_id), native.store.read_inventory()))
    if isinstance(plan, (PreAssignmentRecoveryPlan, MultiModelRecoveryPlan)):
        return ControlRequest(request_id=recovery_key(
            "fence-finished-result" if isinstance(plan, FinishedResultRecoveryPlan) else
            "cancel-model-set" if isinstance(plan, MultiModelRecoveryPlan) else "cancel-before-assignment", plan.recovery_id),
                              request_digest=plan.sha256,
                              read_set=_recovery_keys(native, native.launch(plan.launch_id), _model_digests(plan),
                                                     before_assignment=True))
    return native.workers._request(
        "cancel-bound", (plan.binding.job_id, plan.binding.start_request_id),
        [plan.binding.job_id, plan.binding.owner.model_dump(mode="json"), plan.binding.model_dump(mode="json")],
        (worker_key("job", plan.binding.job_id), worker_key("pool", native.workers.contract.workspace_id),
         *native.workers._owner_keys(plan.binding.owner)))


def _cancelled_result(native, launch, proof, *, retained_result=None):
    plan = proof.intent.authorization.plan
    if isinstance(plan, TeamsPreAcquireRecoveryPlan):
        provenance = ({"schema_version": "fa-expired-teams-cleanup-result-proof-v1",
                       "platform_proof_key": native_key("platform-stop", plan.launch_id),
                       "platform_proof_sha256": digest(proof.model_dump(mode="json")),
                       "original_work_id": plan.binding.work_id, "model_outcome": "not_requested"}
                      if isinstance(plan, ExpiredTeamsCleanupPlan) else proof.model_dump(mode="json"))
        return VerifiedWorkResult(
            evidence_id=native_key("platform-stop-result", plan.launch_id), verifier_version=native.verifier_version,
            work_id=plan.binding.work_id, definition_digest=digest(launch.definition.model_dump(mode="json")),
            state="cancelled", passed_checks=(), known_effects=(), unresolved_operations=(),
            content=_material({"state": "cancelled", "work_id": plan.binding.work_id,
                "model_outcome": "not_requested", "budget_refunded": False,
                "reason": "Original Teams assignment closed before SDK acquisition after verified platform stop."}).data,
            proof=_material(provenance).data)
    if isinstance(plan, FinishedResultRecoveryPlan):
        result_key = assistant_key("result", plan.binding.work_id)
        record = retained_result if retained_result is not None else native.store.read_snapshot((result_key,))[result_key]
        if record is None:
            raise denied("RECOVERY_FINISHED_RESULT_INVALID", "original finished result is missing")
        raw = json.dumps(record.payload, ensure_ascii=False, separators=(",", ":")).encode()
        result = _parse(record.payload["data"], VerifiedWorkResult)
        if (hashlib.sha256(raw).hexdigest() != plan.finished_result.result_image_sha256
                or record.payload["material"] != _material(result.model_dump(mode="json")).model_dump(mode="json")
                or result.state != "succeeded"):
            raise denied("RECOVERY_FINISHED_RESULT_INVALID", "original finished result bytes changed")
        return result
    if isinstance(plan, MultiModelRecoveryPlan):
        return VerifiedWorkResult(
            evidence_id=native_key("platform-stop-result", plan.launch_id), verifier_version=native.verifier_version,
            work_id=plan.binding.work_id, definition_digest=digest(launch.definition.model_dump(mode="json")),
            state="cancelled", passed_checks=(), unresolved_operations=_model_digests(plan),
            content=json.dumps({"state": "cancelled", "work_id": plan.binding.work_id, "result_available": False,
                "model_outcome": "unknown", "budget_refunded": False, "model_intents": [
                    {"request_sha256": item.request_sha256, "consumed": item.consumed, "model_outcome": "unknown"}
                    for item in plan.model_intents],
                "reason": "Human cancelled after unresolved SDK model requests; reservations are not refunded."}).encode(),
            proof=_material(proof.model_dump(mode="json")).data)
    before_acquire = isinstance(plan, PreSdkRecoveryPlan)
    return VerifiedWorkResult(
        evidence_id=native_key("platform-stop-result", plan.launch_id), verifier_version=native.verifier_version,
        work_id=plan.binding.work_id, definition_digest=digest(launch.definition.model_dump(mode="json")),
        state="cancelled", passed_checks=(),
        unresolved_operations=() if before_acquire else (plan.model_request_sha256,),
        content=json.dumps({"state": "cancelled", "work_id": plan.binding.work_id, "result_available": False,
                            "model_request_sha256": plan.model_request_sha256,
                            "model_outcome": "not_requested" if before_acquire else "unknown",
                            "budget_refunded": False,
                            "reason": ("Human cancelled before SDK assignment; no SDK or model request was recorded."
                                       if isinstance(plan, PreAssignmentRecoveryPlan) else
                                       "Human cancelled before SDK acquisition; no model request was recorded."
                                       if before_acquire else "Human cancelled after unresolved SDK failure.")}).encode(),
        proof=_material(proof.model_dump(mode="json")).data,
    )


class ClaimedSdkRecovery:
    def __init__(self, configuration: ServerConfiguration, native: NativeLaunchAuthority,
                 scope: RecoveryScope | PreSdkRecoveryScope | PreAssignmentRecoveryScope | MultiModelRecoveryScope,
                 stop: Callable[[FoundryTarget, datetime], PlatformStopObservation], *,
                 delivery: DeliveryCore,
                 clock=lambda: datetime.now(timezone.utc)):
        if native.recovery_binding is None:
            raise Problem(403, "RECOVERY_NATIVE_DISABLED", "this deployment has no exact SDK recovery identity binding")
        if (configuration.sha256 != scope.configuration_sha256 or configuration.identity.human_oid != scope.operator_oid
                or native.store._binding != configuration.workers.control_binding
                or native.recovery_binding != NativeRecoveryBinding(
                    configuration_sha256=configuration.sha256, operator_oid=scope.operator_oid,
                    credential_client_id=configuration.identity.managed_identity_client_id)
                or scope.job_id not in {binding.job_id for binding in configuration.bindings}
                or delivery.store is not native.store or delivery.contract != configuration.delivery):
            raise denied("RECOVERY_SCOPE_INVALID", "recovery is not bound to this installed original authority")
        self.config, self.native, self.scope, self.stop, self.clock = configuration, native, scope, stop, clock
        self.workers, self.store = native.workers, native.store
        self.delivery = delivery

    def _operator(self, principal, *, historical=False):
        if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
                or principal.oid != self.scope.operator_oid or principal.tenant_id != self.config.identity.tenant_id):
            raise Problem(403, "RECOVERY_OPERATOR_DENIED", "the configured human must authorize this recovery")
        principal.require(self.config.identity.team_id, "read")
        if not historical:
            principal.require(self.config.identity.team_id, "approve")
            if not self.clock() < self.scope.expires_at <= self.clock() + timedelta(hours=1):
                raise Problem(403, "RECOVERY_PERMISSION_EXPIRED", "the independent operator permission expired")

    def _keys(self, launch, model_digest):
        return _recovery_keys(self.native, launch, model_digest,
                              before_assignment=isinstance(self.scope, (PreAssignmentRecoveryScope, MultiModelRecoveryScope)))

    def _finished_live(self, principal, inventory, launch, *, expected=None):
        from .m3_models import ArtifactDocument
        authority = self.native.assistant
        if authority is None:
            raise denied("RECOVERY_FINISHED_RESULT_INVALID", "original assistant verifier is unavailable")
        sid = assistant_key("session-id", launch.start.binding.start_request_id)
        session = _parse(inventory.rows[assistant_key("session", sid)].payload["data"], _Session)
        binding = self.config.bindings[0]
        profile = authority.runtime.profiles[binding.binding_id]
        actor = authority.actors[binding.agent_oid]
        metrics, _, source_snapshot = authority._reviewed(session, principal)
        source_rows = {key: inventory.rows.get(key) for key in source_snapshot}
        authority._current_sources(source_rows, source_snapshot)
        document = ArtifactDocument.model_validate(authority.artifacts.get(actor, binding.team_id, profile.artifact_id).payload)
        if (document.owner_oid != actor.oid or document.source_work != profile.legacy_source_work
                or document.kind != "deliverable" or document.title != "Reviewed metrics result" or document.pending is not None):
            raise denied("RECOVERY_FINISHED_ARTIFACT_INVALID", "original immutable artifact owner/version differs")
        version = authority.artifacts.version(actor, binding.team_id, profile.artifact_id, profile.artifact_version)
        _, content = authority.artifacts.download(actor, binding.team_id, profile.artifact_id,
                                                  profile.artifact_version, version.blob.etag)
        effect = _parse(inventory.rows[assistant_key("artifact", binding.work.work_id)].payload["data"], _ArtifactEffect)
        expected_content = _material({
            "item_count": metrics.item_count, "completed_count": metrics.completed_count,
            "remaining_count": metrics.item_count - metrics.completed_count,
            "source_id": profile.source.source_id, "source_version": profile.source.version,
            "source_sha256": profile.source.sha256,
        }).data
        if (version.sha256 != effect.artifact_sha256 or version.source_work != profile.legacy_source_work
                or content != effect.content or content != expected_content
                or hashlib.sha256(content).hexdigest() != version.sha256):
            raise denied("RECOVERY_FINISHED_ARTIFACT_INVALID", "original M3 version/ETag/SHA/bytes differ")
        authority.runtime.authorize(binding.binding_id, principal, "read")
        authority._current_sources(source_rows, source_snapshot)
        return _finished_proof(inventory, self.config, launch, version.blob.etag, expected=expected)

    def _snapshot(self, launch, model_digest, *, after_fence=False, after_reservation=False):
        before_assignment = isinstance(self.scope, PreAssignmentRecoveryScope)
        multi = isinstance(self.scope, MultiModelRecoveryScope)
        finished = isinstance(self.scope, FinishedResultRecoveryScope)
        if before_assignment or multi:
            inventory = self.store.read_inventory()
            if multi:
                _multi_models(inventory, self.config, launch, self.scope.model_intents, finished=finished)
            else:
                _unassigned_claim(inventory, launch, self.native._keys(launch))
            rows = {key: inventory.rows.get(key) for key in self._keys(launch, model_digest)}
        else:
            rows = self.store.read_snapshot(self._keys(launch, model_digest))
        job_key = worker_key("job", launch.start.binding.job_id)
        job = self.workers._row(rows, job_key, JobRecord)
        pool = self.workers._pool(rows)
        configured = next(binding for binding in self.config.bindings if binding.job_id == self.scope.job_id)
        reservation = self.native._row(rows, native_key("target", launch.target.executor_id), _TargetReservation)
        if job.binding is None:
            raise denied("RECOVERY_ORIGINAL_CHANGED", "original claimed binding is missing")
        sid = assistant_key("session-id", job.binding.start_request_id)
        if not before_assignment and rows[assistant_key("session", sid)] is None:
            raise denied("RECOVERY_ORIGINAL_CHANGED", "original SDK session/model intent is missing")
        session = None if before_assignment else _parse(rows[assistant_key("session", sid)].payload["data"], _Session)
        if before_assignment:
            if model_digest is not None or launch.start.checkpoint is not None:
                raise denied("RECOVERY_ORIGINAL_CHANGED", "missing assignment cannot cover a resumed SDK/model")
        elif isinstance(self.scope, PreSdkRecoveryScope):
            if (model_digest is not None or session.state != "accepted" or session.reason is not None
                    or session.context_read or session.model_requests != 0 or session.assignment.resume
                    or launch.start.checkpoint is not None):
                raise denied("RECOVERY_ORIGINAL_CHANGED", "the original assignment is not an unacquired SDK start")
            # Only the underlying exact receipt lookup distinguishes a 404 from
            # an unavailable read. Public reconcile deliberately calls absence unknown.
            acquire_key = "behavior:v1:receipt:" + assistant_key("acquire", sid, "sdk-start")
            if self.store._read(acquire_key, _Receipt) is not None:
                raise denied("RECOVERY_ORIGINAL_CHANGED", "the original SDK acquire receipt must remain absent")
        elif not multi:
            model_key = assistant_key("model-intent", sid, model_digest)
            if rows[model_key] is None:
                raise denied("RECOVERY_ORIGINAL_CHANGED", "original SDK model intent is missing")
            model = _parse(rows[model_key].payload["data"], _ModelIntent)
            if (session.state != "waiting" or session.reason != "sdk_failed" or session.model_requests != 1
                    or not model.consumed or model.request_sha256 != model_digest):
                raise denied("RECOVERY_ORIGINAL_CHANGED", "the exact consumed model intent differs")
        if (job.binding != launch.start.binding or job.state not in {"starting", "running", "capacity_uncertain"}
                or launch.phase != "claimed" or launch.native_invocation_id is None
                or job.spec.job_id != self.scope.job_id or launch.launch_id != self.scope.launch_id
                or launch.workload_version != "assistant-v1.2" or launch.target not in self.config.active_targets
                or reservation.active_launch_id != launch.launch_id or reservation.generation != launch.process_generation
                or reservation.target != launch.target or job.children
                or job.spec.obligation_id != configured.obligation_id or launch.definition != configured.work
                or job.native_execution_id not in {None, launch.native_invocation_id}
                or session is not None and session.execution != job.binding
                or not finished and (rows[assistant_key("artifact", job.spec.work_id)] is not None
                                     or rows[assistant_key("result", job.spec.work_id)] is not None)
                or len(pool.slots) != 1 or pool.slots[0].binding != job.binding
                or pool.pending != (job.spec.job_id,) or pool.completions
                or job.cancel_requested != after_fence or job.cancel_credit != (0 if after_fence else 1)
                or job.stop_credit != (0 if after_reservation else 1) or not job.completion_credit):
            raise denied("RECOVERY_ORIGINAL_CHANGED", "the exact failed SDK attempt or its reservations changed")
        self.native._current(rows, launch)
        normalized = {key: None if row is None else row.payload for key, row in rows.items()}
        if after_fence:
            normalized[job_key] = {**rows[job_key].payload, "data": _update(
                job, cancel_requested=False, cancel_credit=1, stop_credit=1,
            ).model_dump(mode="json")}
        return rows, digest(normalized)

    def plan(self, principal):
        self._operator(principal)
        launch = self.native.launch(self.scope.launch_id)
        _, snapshot = self._snapshot(launch, _model_digests(self.scope))
        if isinstance(self.scope, MultiModelRecoveryScope):
            inventory = self.store.read_inventory()
            finished = isinstance(self.scope, FinishedResultRecoveryScope)
            proofs = _multi_models(inventory, self.config, launch, self.scope.model_intents, finished=finished)
            finished_proof = self._finished_live(principal, inventory, launch) if finished else None
            if finished and self.store.read_inventory().entity_sha256 != inventory.entity_sha256:
                raise denied("RECOVERY_PLAN_STALE", "original authority changed during artifact verification")
            rows = {key: None if inventory.rows.get(key) is None else inventory.rows[key].payload
                    for key in self._keys(launch, _model_digests(self.scope))}
            if digest(rows) != snapshot:
                raise denied("RECOVERY_PLAN_STALE", "model snapshot changed during plan")
            return (FinishedResultRecoveryPlan if finished else MultiModelRecoveryPlan)(
                recovery_id=self.scope.recovery_id, configuration_sha256=self.config.sha256,
                code_sha256=code_sha256(), operator_oid=self.scope.operator_oid, binding=launch.start.binding,
                launch_id=launch.launch_id, target=launch.target, native_invocation_id=launch.native_invocation_id,
                snapshot_sha256=snapshot, recovery_phase=self.scope.recovery_phase, model_intents=proofs,
                inventory_sha256=digest(dict(inventory.entity_sha256)),
                inventory_guard_revision=inventory.guard.payload["revision"],
                inventory_guard_sha256=inventory.entity_sha256[GUARD_KEY], inventory_guard_etag=inventory.guard.etag,
                original_job_sha256=inventory.entity_sha256["behavior:v1:row:" + worker_key("job", self.scope.job_id)],
                **({"finished_result": finished_proof} if finished else {}))
        before_acquire = isinstance(self.scope, PreSdkRecoveryScope)
        before_assignment = isinstance(self.scope, PreAssignmentRecoveryScope)
        model = PreAssignmentRecoveryPlan if before_assignment else PreSdkRecoveryPlan if before_acquire else RecoveryPlan
        claim = (_unassigned_claim(self.store.read_inventory(), launch, self.native._keys(launch))
                 if before_assignment else None)
        return model(
            recovery_id=self.scope.recovery_id, configuration_sha256=self.config.sha256,
            code_sha256=code_sha256(), operator_oid=self.scope.operator_oid,
            binding=launch.start.binding, launch_id=launch.launch_id, target=launch.target,
            native_invocation_id=launch.native_invocation_id, model_request_sha256=self.scope.model_request_sha256,
            snapshot_sha256=snapshot,
            **({"recovery_phase": "before_sdk_assignment", "native_claim_request": _parse(
                claim.payload["request"], ControlRequest), "native_claim_receipt_sha256": digest(claim.payload)}
               if before_assignment else {"recovery_phase": "before_sdk_acquire"} if before_acquire else {}),
        )

    def authorize(self, principal, plan, decision_id):
        self._operator(principal)
        if plan != self.plan(principal):
            raise denied("RECOVERY_PLAN_STALE", "the exact reviewed recovery plan changed")
        approved_at = self.clock()
        limit = timedelta(minutes=60 if isinstance(plan, ExpiredTeamsCleanupPlan) else 15)
        value = _authorization_model(plan)(plan=plan, decision_id=decision_id, approved_at=approved_at,
                                      expires_at=min(self.scope.expires_at, approved_at + limit))
        key = recovery_key("authorization", plan.recovery_id)
        request = ControlRequest(request_id=key, request_digest=digest(value.model_dump(mode="json")))
        options = {}
        if isinstance(plan, MultiModelRecoveryPlan):
            inventory = self.store.read_inventory()
            _multi_inventory(inventory, self.config, plan, self._keys(self.native.launch(plan.launch_id), _model_digests(plan)))
            options["expected_guard"] = inventory.guard
            if isinstance(plan, TeamsPreAcquireRecoveryPlan):
                options["before_send"] = lambda: self._approval_gate(principal, plan)
        self.store.transact(request, lambda _: ControlChanges(writes=(ControlWrite(
            row_id=key, mode="create", data=value.model_dump(mode="json"), material=_material(value.model_dump(mode="json")),
        ),), result={"authorization_key": key}), **options)
        return value

    def _authorization(self, principal, authorization, *, historical=False):
        self._operator(principal, historical=historical)
        plan = authorization.plan
        key = recovery_key("authorization", plan.recovery_id)
        row = self.store.read_snapshot((key,))[key]
        if (plan.configuration_sha256 != self.config.sha256 or plan.recovery_id != self.scope.recovery_id
                or plan.launch_id != self.scope.launch_id or plan.binding.job_id != self.scope.job_id
                or _model_digests(plan) != _model_digests(self.scope)
                or isinstance(plan, MultiModelRecoveryPlan) and tuple(
                    (item.request_sha256, item.consumed) for item in plan.model_intents) != tuple(
                    (item.request_sha256, item.consumed) for item in self.scope.model_intents)
                or getattr(plan, "recovery_phase", None) != getattr(self.scope, "recovery_phase", None)
                or plan.operator_oid != self.scope.operator_oid or row is None
                or row.payload["data"] != authorization.model_dump(mode="json")
                or row.payload["material"] != _material(authorization.model_dump(mode="json")).model_dump(mode="json")
                or not historical and (plan.code_sha256 != code_sha256()
                                      or not authorization.approved_at <= self.clock() < authorization.expires_at)):
            raise Problem(403, "RECOVERY_AUTHORIZATION_INVALID", "exact current human authorization is required")

    def _cancel(self, principal, authorization):
        plan = authorization.plan
        if not isinstance(plan, (PreAssignmentRecoveryPlan, MultiModelRecoveryPlan)):
            return self.workers.cancel(plan.binding.job_id, plan.binding.owner, expected_binding=plan.binding)
        request = _cancel_request(self.native, plan)
        self._authorization(principal, authorization)
        prior = self.store._receipt(request)
        if prior is not None:
            return prior
        launch = self.native.launch(plan.launch_id)
        inventory = self.store.read_inventory()
        if isinstance(plan, MultiModelRecoveryPlan):
            _multi_models(inventory, self.config, launch, plan.model_intents, finished=isinstance(plan, FinishedResultRecoveryPlan))
            if isinstance(plan, FinishedResultRecoveryPlan):
                self._finished_live(principal, inventory, launch, expected=plan.finished_result)
            _multi_inventory(inventory, self.config, plan, request.read_set, authorization)
        else:
            claim = _unassigned_claim(inventory, launch, self.native._keys(launch), original_request=plan.native_claim_request)
            if digest(claim.payload) != plan.native_claim_receipt_sha256:
                raise denied("RECOVERY_CLAIM_UNVERIFIED", "original claim receipt changed")
        def fence(rows):
            self._authorization(principal, authorization)
            normalized = {key: None if row is None else row.payload for key, row in rows.items()}
            if digest(normalized) != plan.snapshot_sha256:
                raise denied("RECOVERY_PLAN_STALE", "original missing-assignment snapshot changed")
            job_key = worker_key("job", plan.binding.job_id)
            job = self.workers._row(rows, job_key, JobRecord)
            self.workers._owner(rows, plan.binding.owner, policy=False)
            if job.binding != plan.binding or job.cancel_requested or not job.cancel_credit:
                raise denied("RECOVERY_ORIGINAL_CHANGED", "original unassigned claim is not cancellable")
            return ControlChanges(writes=(self.workers._write(
                job_key, _update(job, cancel_requested=True, cancel_credit=0), rows),),
                result=self.workers._result(status=job.state, cancel_accepted=True, executor_stopped=False))
        options = {"before_send": lambda: self._write_gate(principal, authorization)} if isinstance(plan, TeamsPreAcquireRecoveryPlan) else {}
        return self.store.transact(request, fence, expected_guard=inventory.guard, **options)

    def execute(self, principal, authorization):
        self._authorization(principal, authorization)
        plan = authorization.plan
        intent_key = recovery_key("stop-intent", plan.recovery_id)
        if self.store.read_snapshot((intent_key,))[intent_key] is not None:
            return self.reconcile(principal, authorization)
        launch = self.native.launch(plan.launch_id)
        current = self.workers.inspect(plan.binding.job_id)
        _, snapshot = self._snapshot(launch, _model_digests(plan), after_fence=current.cancel_requested)
        if snapshot != plan.snapshot_sha256:
            raise denied("RECOVERY_PLAN_STALE", "original authority data changed")
        cancellation = self._cancel(principal, authorization)
        intent = _StopIntent(authorization=authorization, fenced_at=self.clock())
        keys = self._keys(launch, _model_digests(plan))
        job_key = worker_key("job", plan.binding.job_id)
        request = ControlRequest(request_id=intent_key, request_digest=digest(intent.model_dump(mode="json")), read_set=keys)
        fresh = False

        def reserve(rows):
            nonlocal fresh
            self._authorization(principal, authorization)
            job = self.workers._row(rows, job_key, JobRecord)
            if job.binding != plan.binding or not job.cancel_requested or not job.stop_credit:
                raise denied("RECOVERY_STOP_FENCED", "original cancellation or stop reservation changed")
            if isinstance(plan, (PreAssignmentRecoveryPlan, MultiModelRecoveryPlan)):
                normalized = {key: None if row is None else row.payload for key, row in rows.items()}
                normalized[job_key] = {**rows[job_key].payload, "data": _update(
                    job, cancel_requested=False, cancel_credit=1, stop_credit=1).model_dump(mode="json")}
                if digest(normalized) != plan.snapshot_sha256:
                    raise denied("RECOVERY_STOP_FENCED", "missing-assignment predicates changed before stop reservation")
            fresh = True
            return ControlChanges(writes=(
                self.workers._write(job_key, _update(job, stop_credit=0), rows),
                ControlWrite(row_id=intent_key, mode="create", data=intent.model_dump(mode="json"),
                             material=_material(intent.model_dump(mode="json"))),
            ), result={"stop_reserved": True})

        options = {}
        if isinstance(plan, MultiModelRecoveryPlan):
            inventory = self.store.read_inventory()
            _multi_models(inventory, self.config, launch, plan.model_intents, finished=isinstance(plan, FinishedResultRecoveryPlan))
            _multi_inventory(inventory, self.config, plan, keys, authorization)
            options["expected_guard"] = inventory.guard
            if isinstance(plan, TeamsPreAcquireRecoveryPlan):
                options["before_send"] = lambda: self._write_gate(principal, authorization)
        self.store.transact(request, reserve, **options)
        if not fresh:
            return self.reconcile(principal, authorization)
        _, snapshot = self._snapshot(self.native.launch(plan.launch_id), _model_digests(plan),
                                     after_fence=True, after_reservation=True)
        if snapshot != plan.snapshot_sha256:
            raise denied("RECOVERY_PLAN_STALE", "fenced attempt changed before the external stop")
        self._authorization(principal, authorization)
        if isinstance(plan, MultiModelRecoveryPlan):
            inventory = self.store.read_inventory()
            if isinstance(plan, FinishedResultRecoveryPlan):
                self._finished_live(principal, inventory, self.native.launch(plan.launch_id), expected=plan.finished_result)
            _multi_inventory(inventory, self.config, plan, keys, authorization)
            if isinstance(plan, FinishedResultRecoveryPlan) and self.store.read_inventory().entity_sha256 != inventory.entity_sha256:
                raise denied("RECOVERY_PLAN_STALE", "finished-result authority changed before stop")
        try:
            observation = self.stop(plan.target, authorization.expires_at)
        except Problem:
            raise
        except Exception:
            raise Problem(503, "RECOVERY_STOP_UNKNOWN", "original platform stop is unresolved; never resend") from None
        self._check_stop(intent, observation)
        stop_key = recovery_key("stop-observation", plan.recovery_id)
        stop_request = ControlRequest(request_id=stop_key, request_digest=digest(observation.model_dump(mode="json")))
        options = {}
        if isinstance(plan, TeamsPreAcquireRecoveryPlan):
            self._write_gate(principal, authorization)
            inventory = self.store.read_inventory()
            _multi_inventory(inventory, self.config, plan, keys, authorization)
            options = {"expected_guard": inventory.guard,
                       "before_send": lambda: self._write_gate(principal, authorization)}
        self.store.transact(stop_request, lambda _: ControlChanges(writes=(ControlWrite(
            row_id=stop_key, mode="create", data=observation.model_dump(mode="json"),
            material=_material(observation.model_dump(mode="json")),
        ),), result={"provider_stop_observed": True}), **options)
        return self.finish(principal, authorization)

    def _check_stop(self, intent, observation):
        observation = _parse(observation.model_dump(mode="json"), PlatformStopObservation)
        if (observation.target != intent.authorization.plan.target
                or observation.endpoint != stop_endpoint(observation.target)
                or observation.credential_client_id != self.config.identity.managed_identity_client_id
                or not intent.fenced_at <= observation.observed_at < intent.authorization.expires_at
                or (observation.status_code, observation.error_code) not in {(204, None), (409, "session_already_stopped")}):
            raise denied("RECOVERY_STOP_UNVERIFIED", "a bound terminal stop response, not idle/202, is required")

    def reconcile(self, principal, authorization):
        self._authorization(principal, authorization, historical=True)
        keys = {name: recovery_key(name, self.scope.recovery_id) for name in ("stop-intent", "stop-observation")}
        rows = self.store.read_snapshot(tuple(keys.values()))
        return {"recovery_id": self.scope.recovery_id,
                **{name: rows[key] is not None for name, key in keys.items()},
                "sealed": self.native.launch(self.scope.launch_id).phase == "sealed",
                "job_state": self.workers.inspect(self.scope.job_id).state, "stop_resent": False}

    def finish(self, principal, authorization):
        self._authorization(principal, authorization)
        plan = authorization.plan
        intent_key = recovery_key("stop-intent", plan.recovery_id)
        stop_key = recovery_key("stop-observation", plan.recovery_id)
        rows = self.store.read_snapshot((intent_key, stop_key))
        if any(row is None for row in rows.values()):
            raise denied("RECOVERY_STOP_UNKNOWN", "the original stop outcome is unconfirmed; never resend")
        intent = _parse(rows[intent_key].payload["data"], _StopIntent)
        observation = _parse(rows[stop_key].payload["data"], PlatformStopObservation)
        if intent.authorization != authorization:
            raise denied("RECOVERY_STOP_UNVERIFIED", "stop intent belongs to another approval")
        self._check_stop(intent, observation)
        launch = self.native.launch(plan.launch_id)
        proof_key = native_key("platform-stop", plan.launch_id)
        if self.store.read_snapshot((proof_key,))[proof_key] is None:
            _, snapshot = self._snapshot(launch, _model_digests(plan), after_fence=True, after_reservation=True)
            if snapshot != plan.snapshot_sha256:
                raise denied("RECOVERY_PLAN_STALE", "fenced original data changed")
            cancel_receipt = self._cancel(principal, authorization)
            stop_receipt = self.store.reconcile(ControlRequest(
                request_id=stop_key, request_digest=digest(observation.model_dump(mode="json")),
            ))
            proof = _proof_model(plan)(intent=intent, observation=observation,
                                   cancellation_receipt_sha256=digest(cancel_receipt.payload),
                                   stop_receipt_sha256=digest(stop_receipt.payload))
            evidence_id = native_key("platform-stop-evidence", plan.launch_id)
            body = proof.model_dump(mode="json")
            result = _cancelled_result(self.native, launch, proof)
            evidence = ExecutorEvidence(evidence_id=evidence_id, binding=plan.binding, sequence=launch.evidence_sequence + 1,
                                        native_execution_id=plan.native_invocation_id,
                                        state="succeeded" if isinstance(plan, FinishedResultRecoveryPlan) else "stopped",
                                        entire_tree_idle=True, all_inflight_starts_fenced=True, result=result,
                                        proof=_material(body).data)
            saved = _NativeEvidence(launch_id=plan.launch_id, configuration_digest=self.workers.configuration_digest(),
                                    probe_id=proof_key, evidence=evidence, observed_at=self.clock(),
                                    expires_at=self.clock() + timedelta(seconds=60), report_digest=digest(body))
            launch_key, target_key = native_key("launch", plan.launch_id), native_key("target", plan.target.executor_id)
            keys = (*self._keys(launch, _model_digests(plan)), proof_key, intent_key, stop_key)
            request = ControlRequest(request_id=recovery_key("sealed", plan.recovery_id),
                                     request_digest=digest(body), read_set=tuple(dict.fromkeys(keys)))

            def seal(snapshot):
                current = self.native._row(snapshot, launch_key, _NativeLaunch)
                target = self.native._row(snapshot, target_key, _TargetReservation)
                job = self.workers._row(snapshot, worker_key("job", plan.binding.job_id), JobRecord)
                if (current != launch or not job.cancel_requested or job.binding != plan.binding
                        or target.active_launch_id != plan.launch_id or target.target != plan.target):
                    raise denied("RECOVERY_SEAL_STALE", "original claimed execution changed before sealing")
                normalized = {key: None if snapshot[key] is None else snapshot[key].payload
                              for key in self._keys(launch, _model_digests(plan))}
                job_key = worker_key("job", plan.binding.job_id)
                normalized[job_key] = {**snapshot[job_key].payload, "data": _update(
                    job, cancel_requested=False, cancel_credit=1, stop_credit=1,
                ).model_dump(mode="json")}
                if digest(normalized) != plan.snapshot_sha256:
                    raise denied("RECOVERY_SEAL_STALE", "original data changed before the atomic stop seal")
                return ControlChanges(writes=(
                    ControlWrite(row_id=proof_key, mode="create", data=body, material=_material(body)),
                    self.native._write(native_key("evidence", evidence_id), saved, material=_material(saved.model_dump(mode="json"))),
                    self.native._write(launch_key, _update(current, phase="sealed", sealed_evidence_id=evidence_id,
                                                         evidence_sequence=evidence.sequence, pending_control="cancel"), snapshot),
                    self.native._write(target_key, _update(target, active_launch_id=None), snapshot),
                ), result={"platform_stop_evidence_id": evidence_id, "sdk_graceful_exit_claim": False})

            options = {}
            if isinstance(plan, MultiModelRecoveryPlan):
                inventory = self.store.read_inventory()
                _multi_models(inventory, self.config, launch, plan.model_intents, finished=isinstance(plan, FinishedResultRecoveryPlan))
                if isinstance(plan, FinishedResultRecoveryPlan):
                    self._finished_live(principal, inventory, launch, expected=plan.finished_result)
                _multi_inventory(inventory, self.config, plan, self._keys(launch, _model_digests(plan)), authorization)
                options["expected_guard"] = inventory.guard
            self.store.transact(request, seal, **options)
        evidence_id = self.native.reconcile_sealed(plan.binding)
        self.workers.observe(plan.binding.job_id, evidence_id, plan.binding.owner)
        if not self.workers.inspect(plan.binding.job_id).result_forwarded:
            self.workers.forward_result(plan.binding.job_id, plan.binding.owner, self.delivery)
        return {"recovery_id": plan.recovery_id, "state": self.workers.inspect(plan.binding.job_id).state,
                "platform_stop_evidence_id": evidence_id,
                "model_outcome": "not_requested" if isinstance(plan, (PreSdkRecoveryPlan, TeamsPreAcquireRecoveryPlan)) else "unknown",
                "stop_resent": False,
                "delivered": False,
                **({"execution_closure": "platform_stop_after_durable_finish", "sdk_graceful_exit_claim": False}
                   if isinstance(plan, FinishedResultRecoveryPlan) else {})}


def verify_platform_stop(native, launch, saved, proof_row):
    data = proof_row.payload["data"]
    model = (ExpiredTeamsCleanupHistory if data.get("schema_version") == "fa-expired-teams-cleanup-history-v1" else
             TeamsPreAcquireRecoveryHistory if data.get("schema_version") == "fa-teams-pre-acquire-recovery-history-v1" else _PlatformProof)
    proof = _parse(data, model)
    plan, observation = proof.intent.authorization.plan, proof.observation
    if isinstance(plan, TeamsPreAcquireRecoveryPlan):
        from .behavior_teams_recovery import verify_plan_original
        verify_plan_original(native.store.read_inventory(), native.assistant.runtime.teams_metrics.config, launch, plan)
    elif isinstance(plan, PreAssignmentRecoveryPlan):
        claim = _unassigned_claim(native.store.read_inventory(), launch, native._keys(launch),
                                  original_request=plan.native_claim_request)
        if digest(claim.payload) != plan.native_claim_receipt_sha256:
            raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "original native claim receipt differs")
    elif isinstance(plan, MultiModelRecoveryPlan):
        # Native verification has no server configuration handle. The original
        # immutable model set is also checked by the recovery/history readers.
        inventory = native.store.read_inventory()
        sid = assistant_key("session-id", plan.binding.start_request_id)
        expected = {assistant_key("model-intent", sid, item.request_sha256): item for item in plan.model_intents}
        actual = {key for key, row in inventory.rows.items()
                  if isinstance(value := row.payload["data"].get("request_sha256"), str)
                  and key == assistant_key("model-intent", sid, value)}
        if actual != set(expected):
            raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "original model set changed after stop")
        for key, item in expected.items():
            raw = json.dumps(inventory.rows[key].payload, ensure_ascii=False, separators=(",", ":")).encode()
            if hashlib.sha256(raw).hexdigest() != item.image_sha256:
                raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "original model intent image changed after stop")
            for action, request_hash, receipt_hash in (
                    ("model", item.reserve_request_sha256, item.reserve_receipt_sha256),
                    ("model-send", item.consume_request_sha256, item.consume_receipt_sha256)):
                retained = inventory.receipts.get(assistant_key(action, sid, item.request_sha256))
                if request_hash is None:
                    if retained is not None:
                        raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "late model consume receipt")
                elif retained is None or digest(retained.payload) != receipt_hash or digest(retained.payload["request"]) != request_hash:
                    raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "original model receipt changed after stop")
        if isinstance(plan, FinishedResultRecoveryPlan):
            refs = plan.finished_result
            for kind, identifier, fingerprint in (
                    ("session", sid, refs.session_image_sha256),
                    ("artifact", plan.binding.work_id, refs.artifact_image_sha256),
                    ("result", plan.binding.work_id, refs.result_image_sha256)):
                record = inventory.rows.get(assistant_key(kind, identifier))
                if record is None or hashlib.sha256(json.dumps(
                        record.payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() != fingerprint:
                    raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "original finished row changed after stop")
            claim = _claimed_receipt(inventory, launch, native._keys(launch))
            if digest(claim.payload) != refs.native_claim_receipt_sha256 or digest(claim.payload["request"]) != refs.native_claim_request_sha256:
                raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "original finished claim changed")
            for action, ids, request_hash, receipt_hash in (
                    ("issue", (), refs.assignment_request_sha256, refs.assignment_receipt_sha256),
                    ("acquire", ("sdk-start",), refs.acquire_request_sha256, refs.acquire_receipt_sha256),
                    ("finish", ("finish-task",), refs.finish_request_sha256, refs.finish_receipt_sha256)):
                record = inventory.receipts.get(assistant_key(action, sid, *ids))
                if record is None or digest(record.payload["request"]) != request_hash or digest(record.payload) != receipt_hash:
                    raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "original finish/acquire/assignment receipt changed")
    job = native.workers.inspect(plan.binding.job_id)
    expected_binding = NativeRecoveryBinding(
        configuration_sha256=plan.configuration_sha256, operator_oid=plan.operator_oid,
        credential_client_id=observation.credential_client_id,
    )
    if (native.recovery_binding != expected_binding
            or proof_row.payload["material"] != _material(proof.model_dump(mode="json")).model_dump(mode="json")
            or plan.launch_id != launch.launch_id or plan.binding != launch.start.binding
            or plan.native_invocation_id != launch.native_invocation_id or observation.target != launch.target
            or observation.endpoint != stop_endpoint(launch.target)
            or (observation.status_code, observation.error_code) not in {(204, None), (409, "session_already_stopped")}
            or not proof.intent.authorization.approved_at <= proof.intent.fenced_at
            or not proof.intent.fenced_at <= observation.observed_at < proof.intent.authorization.expires_at
            or not job.cancel_requested or job.binding != plan.binding or launch.phase != "sealed"
            or saved.evidence.state != ("succeeded" if isinstance(plan, FinishedResultRecoveryPlan) else "stopped")
            or saved.evidence.checkpoint is not None
            or not saved.evidence.entire_tree_idle or not saved.evidence.all_inflight_starts_fenced
            or saved.evidence.result != _cancelled_result(native, launch, proof)
            or saved.evidence.native_execution_id != plan.native_invocation_id
            or saved.evidence.proof != _material(proof.model_dump(mode="json")).data
            or saved.report_digest != digest(proof.model_dump(mode="json"))):
        raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "permanent original cancellation/platform-stop proof differs")
    for name, expected in (("authorization", proof.intent.authorization.model_dump(mode="json")),
                           ("stop-intent", proof.intent.model_dump(mode="json")),
                           ("stop-observation", observation.model_dump(mode="json"))):
        key = recovery_key(name, plan.recovery_id)
        row = native.store.read_snapshot((key,))[key]
        if row is None or row.payload["data"] != expected or row.payload["material"] != _material(expected).model_dump(mode="json"):
            raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "exact immutable cancellation/stop provenance is missing")
    cancel = _cancel_request(native, plan)
    stop = ControlRequest(request_id=recovery_key("stop-observation", plan.recovery_id),
                          request_digest=digest(observation.model_dump(mode="json")))
    reservation = ControlRequest(
        request_id=recovery_key("stop-intent", plan.recovery_id),
        request_digest=digest(proof.intent.model_dump(mode="json")),
        read_set=(_cancel_request(native, plan).read_set if isinstance(plan, TeamsPreAcquireRecoveryPlan)
                  else _recovery_keys(native, launch, _model_digests(plan),
                                     before_assignment=isinstance(plan, (PreAssignmentRecoveryPlan, MultiModelRecoveryPlan)))),
    )
    if (digest(native.store.reconcile(cancel).payload) != proof.cancellation_receipt_sha256
            or digest(native.store.reconcile(stop).payload) != proof.stop_receipt_sha256
            or native.store.reconcile(reservation).payload["result"] != {"stop_reserved": True}):
        raise denied("RECOVERY_PLATFORM_PROOF_INVALID", "original cancellation and stop receipts differ")
