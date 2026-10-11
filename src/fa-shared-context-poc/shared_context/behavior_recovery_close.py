"""Trusted operator, Table-only adoption of an already completed original stop."""
import hashlib
import json
from datetime import datetime, timedelta
from typing import Annotated, Literal
from types import SimpleNamespace

from pydantic import Field

from .behavior_assistant import _ArtifactEffect, _ModelIntent, _Session, key as assistant_key
from .behavior_auth import BehaviorPrincipal
from .behavior_delivery import _Model, _material, _parse, _update, VerifiedWorkResult
from .behavior_foundry import FoundryTarget, _NativeEvidence, _NativeLaunch, _TargetReservation, native_key
from .behavior_migration import code_sha256
from .behavior_operations import ExecutionContext, OwnerRecord, control_key
from .behavior_recovery import ModelIntentState, PlatformStopObservation, stop_endpoint
from .behavior_store import ControlChanges, ControlRequest, ControlWrite, Digest
from .behavior_workers import ExecutorBinding, ExecutorEvidence, ExecutorStop, JobRecord, _Pool, worker_key
from .behavior_work_policy import WorkRegistration, work_policy_key
from .errors import Problem
from .models import Identifier
from .source_materials import digest


def require(condition, detail):
    if not condition:
        raise Problem(409, "ORIGINAL_CLOSE_DENIED", detail)


def read_stop_receipt(raw, *, job_id, invocation_id, target, credential_client_id):
    """Parse retained SDK/transport output, not a new provider request or idle proof."""
    require(isinstance(raw, bytes) and len(raw) <= 65536, "bounded retained stop record required")
    target = FoundryTarget.model_validate(target)
    try:
        def unique(pairs):
            value = dict(pairs)
            if len(value) != len(pairs):
                raise ValueError("duplicate field")
            return value
        records = json.loads(raw, object_pairs_hook=unique)["records"]
        receipt = records["standing_normal_platform_stop_receipt"]
        final = records["standing_normal_platform_stop"]
        require(receipt["job_id"] == final["job_id"] == job_id
                and receipt["original_invocation"] == final["original_invocation"] == invocation_id
                and receipt["session_id"] == target.session_id
                and receipt["stop_returned_204"] is True and receipt["automatic_retry"] is False
                and final["outcome"] == "STOP_RETURNED_204", "original stop identity differs")
        for observation in (final["before"], final["after"]):
            require(observation["agent_session_id"] == target.session_id
                    and observation["version_indicator"]["agent_version"] == target.agent_version,
                    "original session version differs")
        calls = receipt["calls"]
        require(len(calls) == 2 and calls[0]["method"] == "GET"
                and calls[0]["action"] == "original-status" and calls[0]["status_code"] == 200
                and calls[1]["method"] == "POST" and calls[1]["action"] == "stop-session"
                and calls[1]["status_code"] == 204 and final["calls"][:2] == calls
                and sum(call["method"] == "POST" for call in final["calls"]) == 1,
                "one original positive stop response is required")
        result = PlatformStopObservation.model_validate_json(json.dumps({
            "target": target.model_dump(mode="json"), "endpoint": stop_endpoint(target),
            "credential_client_id": credential_client_id, "status_code": 204,
            "provider_request_id": calls[1].get("request_id"), "observed_at": receipt["at"],
        }))
    except (ValueError, TypeError, KeyError, IndexError):
        raise Problem(422, "ORIGINAL_CLOSE_INPUT_INVALID", "invalid retained stop record") from None
    return result, hashlib.sha256(raw).hexdigest()


class CloseOriginal(_Model):
    configuration_sha256: Digest
    code_sha256: Digest
    job_id: Identifier
    launch_id: Identifier
    owner: ExecutionContext
    registration_sha256: Digest
    job_sha256: Digest
    invocation_id: Identifier
    stop: PlatformStopObservation
    retained_receipt_sha256: Digest
    approval_reference: Identifier

    @property
    def request_id(self):
        # Deliberately excludes the supplied body: a changed body cannot select a new close ID.
        return native_key("original-stop-close", self.launch_id)


class OriginalStopClosed(_Model):
    kind: Literal["original_sdk_stop_closed"] = "original_sdk_stop_closed"
    command: CloseOriginal
    request: ControlRequest
    operator_oid: Identifier
    binding: ExecutorBinding
    registration_key: Identifier
    original_launch: _NativeLaunch
    original_job: JobRecord
    original_pool: _Pool
    original_target: _TargetReservation
    preserved: Annotated[dict[Identifier, Digest], Field(min_length=3, max_length=12)]
    model_intents: Annotated[tuple[ModelIntentState, ...], Field(min_length=1, max_length=6)]
    known_effects: tuple[Identifier, ...]
    unresolved_effects: tuple[Identifier, ...]
    recorded_at: datetime


def _evidence_id(launch_id):
    return native_key("original-stop-close-evidence", launch_id)


def _result(proof, verifier_version):
    binding, command = proof.binding, proof.command
    models = tuple(item.request_sha256 for item in proof.model_intents)
    return VerifiedWorkResult(
        evidence_id=native_key("original-stop-close-result", command.launch_id),
        verifier_version=verifier_version, work_id=binding.work_id,
        definition_digest=digest(proof.original_launch.definition.model_dump(mode="json")),
        state="cancelled", passed_checks=(), known_effects=proof.known_effects,
        unresolved_operations=tuple(dict.fromkeys((*models, *proof.unresolved_effects))),
        content=_material({
            "state": "cancelled", "work_id": binding.work_id, "artifact_stage": "saved",
            "finish_verified": False, "model_outcome": "unknown", "budget_refunded": False,
            "model_intents": [item.model_dump(mode="json") for item in proof.model_intents],
            "reason": "Original work cancelled after the retained platform stop; saved artifact is not a finished result.",
        }).data,
        proof=_material({"original_stop_close": command.request_id,
                         "proof_sha256": digest(proof.model_dump(mode="json"))}).data,
    )


def _request(command, reads=()):
    return ControlRequest(request_id=command.request_id,
                          request_digest=digest(command.model_dump(mode="json")), read_set=tuple(sorted(set(reads))))


def _operator(composition, command, principal, *, apply):
    command = CloseOriginal.model_validate(command)
    cfg = composition.config
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
            or principal.tenant_id != cfg.identity.tenant_id or principal.oid != cfg.identity.human_oid):
        raise Problem(403, "ORIGINAL_CLOSE_OPERATOR_DENIED", "original configured human is required")
    principal.require(cfg.identity.team_id, "approve" if apply else "read")
    require(command.configuration_sha256 == cfg.sha256, "installed configuration differs")
    if apply:
        require(command.code_sha256 == code_sha256(), "loaded close code differs")
    composition.installation()
    return command


def _retained(composition, command):
    store = composition.runtime.store
    prior = store.read_snapshot((command.request_id,))[command.request_id]
    if prior is None:
        return None
    proof = _parse(prior.payload["data"], OriginalStopClosed)
    require(proof.command == command, "original close ID is bound to another input")
    receipt = store.reconcile(proof.request)
    verify_closed(composition.runtime.dep.native_launches,
                  composition.runtime.dep.native_launches.launch(command.launch_id), None, prior)
    return receipt


def reconcile_original(composition, command, principal):
    command = _operator(composition, command, principal, apply=False)
    receipt = _retained(composition, command)
    if receipt is None:
        raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "original close receipt is absent; do not change ID or resend",
                      request_id=command.request_id)
    return receipt


def close_original(composition, command, principal):
    """One server-owned Table transaction; no provider client or retry path."""
    command = _operator(composition, command, principal, apply=True)
    prior = _retained(composition, command)
    if prior is not None:
        return prior
    runtime, config = composition.runtime, composition.config
    runtime.refresh_bindings()
    store, workers, native = runtime.store, runtime.dep.workers, runtime.dep.native_launches
    control = runtime.work_policy
    require(control is not None and runtime.mode == "native", "standing native work is required")
    registration = control._job_record(command.job_id)
    require(registration is not None
            and digest(registration.model_dump(mode="json")) == command.registration_sha256,
            "original work registration differs")
    binding = registration.binding
    require(binding.owner == command.owner and binding.route.channel_id == "authenticated-pull",
            "original owner or route differs")
    inventory = store.read_inventory()
    from .behavior_recovery_history import _Inventory
    view = _Inventory(inventory, config)
    launch = view.row(native_key("launch", command.launch_id), _NativeLaunch)
    job = view.row(worker_key("job", command.job_id), JobRecord)
    target = view.row(native_key("target", launch.target.executor_id), _TargetReservation)
    sid = assistant_key("session-id", launch.start.binding.start_request_id)
    sessionkey, artifactkey = assistant_key("session", sid), assistant_key("artifact", binding.work.work_id)
    session, artifact = view.row(sessionkey, _Session), view.row(artifactkey, _ArtifactEffect)
    require(job.binding == launch.start.binding and job.owner == command.owner
            and digest(job.model_dump(mode="json")) == command.job_sha256
            and job.state == "capacity_uncertain" and job.cancel_requested
            and job.cancel_credit == job.stop_credit == 0 and job.completion_credit == 1
            and job.generation == 1 and not job.children and job.spec.parent_job_id is None
            and not job.result_forwarded and job.spec.job_id == binding.job_id
            and job.spec.work_id == binding.work.work_id and job.spec.obligation_id == binding.obligation_id
            and launch.phase == "claimed" and launch.api_binding_id == binding.binding_id
            and launch.definition == binding.work and launch.workload_version == "assistant-v1.2"
            and launch.launch_id == native_key("launch-id", job.binding.start_request_id)
            and launch.native_invocation_id == command.invocation_id
            and launch.target == command.stop.target and launch.target in config.active_targets
            and target.target == launch.target and target.active_launch_id == command.launch_id
            and target.generation == launch.process_generation,
            "original cancelled job/launch/target changed")
    require(command.stop.status_code == 204 and command.stop.error_code is None
            and command.stop.endpoint == stop_endpoint(launch.target)
            and command.stop.credential_client_id == config.identity.managed_identity_client_id
            and command.stop.observed_at <= runtime._now(),
            "original positive stop receipt differs")
    require(session.execution == job.binding and session.binding_id == binding.binding_id
            and session.state in {"running", "waiting"} and not session.assignment.resume
            and artifact.execution == job.binding and artifact.stage == "saved"
            and artifact.artifact_sha256 == hashlib.sha256(artifact.content).hexdigest()
            and assistant_key("result", binding.work.work_id) not in inventory.rows
            and assistant_key("finish", sid, "finish-task") not in inventory.receipts
            and all(assistant_key(kind, sid) not in inventory.rows for kind in ("archive", "restore")),
            "only original saved, unfinished, non-resumed work can close")
    modelkeys = sorted(key for key, row in inventory.rows.items()
        if isinstance(value := row.payload["data"].get("request_sha256"), str)
        and key == assistant_key("model-intent", sid, value))
    models = tuple(view.row(key, _ModelIntent) for key in modelkeys)
    require(1 <= len(models) == session.model_requests <= 6, "complete original model set is required")
    stopkey = worker_key("stop-intent", command.job_id, "1")
    stop_intent = view.row(stopkey, ExecutorStop, material=True)
    require(stop_intent.binding == job.binding, "original spent stop reservation differs")
    original_stop = view.receipt(stop_intent.request, (worker_key("job", command.job_id), stopkey),
                                 exact={stopkey: view.hash(stopkey)})
    require(original_stop.result["status"] == "capacity_uncertain", "original stop receipt differs")
    owner = view.row(control_key("owner", command.owner.owner_record_id), OwnerRecord)
    operations = tuple(control_key("operation", value) for value in owner.operation_ids)
    reads = tuple(dict.fromkeys((
        *native._keys(launch), *workers._owner_keys(command.owner),
        worker_key("attempt", command.job_id, "1"), worker_key("pool", workers.contract.workspace_id),
        stopkey, sessionkey, artifactkey, assistant_key("result", binding.work.work_id),
        *modelkeys, *operations, *control.start_keys(command.job_id),
        control_key("delivery-obligation", binding.obligation_id),
        control_key("delivery-work-state", binding.obligation_id), command.request_id,
    )))
    request = _request(command, reads)
    rows = {key: inventory.rows.get(key) for key in request.read_set}
    native._current(rows, launch)
    require(owner.epoch == command.owner.epoch, "original owner epoch changed")
    pool = workers._pool(rows)
    require(len(pool.slots) == 1 and pool.slots[0].binding == job.binding
            and pool.pending == (job.spec.job_id,) and not pool.completions,
            "only the original occupied C1 can close")
    known, unresolved = workers._operation_effects(rows, owner.operation_ids, command.owner)
    preserved = {key: view.hash(key) for key in (sessionkey, artifactkey, *modelkeys)}
    proof = OriginalStopClosed(
        command=command, request=request, operator_oid=principal.oid, binding=job.binding,
        registration_key=work_policy_key("registration", registration.admission_id),
        original_launch=launch, original_job=job, original_pool=pool, original_target=target, preserved=preserved,
        model_intents=tuple(ModelIntentState(request_sha256=model.request_sha256, consumed=model.consumed)
                            for model in models),
        known_effects=known, unresolved_effects=unresolved, recorded_at=runtime._now(),
    )
    evidence_id = _evidence_id(command.launch_id)
    evidence = ExecutorEvidence(evidence_id=evidence_id, binding=job.binding,
        sequence=max(launch.evidence_sequence, job.last_sequence) + 1,
        native_execution_id=command.invocation_id, state="stopped",
        entire_tree_idle=True, all_inflight_starts_fenced=True,
        result=_result(proof, config.delivery.verifier_version),
        proof=_material({"original_stop_close": command.request_id,
                         "proof_sha256": digest(proof.model_dump(mode="json"))}).data)
    saved = _NativeEvidence(launch_id=launch.launch_id, configuration_digest=workers.configuration_digest(),
        probe_id=command.request_id, evidence=evidence, observed_at=proof.recorded_at,
        expires_at=proof.recorded_at + timedelta(seconds=60), report_digest=digest(proof.model_dump(mode="json")))
    def commit(current):
        require(current == rows, "original close predicates changed")
        changes = workers._observe_changes(command.job_id, evidence, command.owner, current)
        require(changes.result["status"] == "cancelled", "original terminal completion was not selected")
        return ControlChanges(writes=(
            ControlWrite(row_id=command.request_id, mode="create", data=proof.model_dump(mode="json"),
                         material=_material(proof.model_dump(mode="json"))),
            native._write(native_key("evidence", evidence_id), saved, material=_material(saved.model_dump(mode="json"))),
            native._write(native_key("launch", command.launch_id),
                _update(launch, phase="sealed", sealed_evidence_id=evidence_id,
                        evidence_sequence=evidence.sequence, pending_control="cancel"), current),
            native._write(native_key("target", target.target.executor_id),
                          _update(target, active_launch_id=None), current),
            *changes.writes,
        ), result={"state": "cancelled", "job_id": command.job_id, "evidence_id": evidence_id,
                   "sdk_finish": False, "delivered": False, "budget_refunded": False})
    return store.transact(request, commit, expected_guard=inventory.guard)


def _check_history(view, proof, config, bindings):
    """Verify this one atomic producer's original receipt and unchanged material."""
    command, binding = proof.command, proof.binding
    configured = next((b for b in bindings if b.job_id == command.job_id), None)
    require(configured is not None and configured.owner == command.owner == binding.owner
            and configured.work == proof.original_launch.definition
            and configured.binding_id == proof.original_launch.api_binding_id
            and configured.work.work_id == binding.work_id
            and proof.operator_oid == config.identity.human_oid
            and command.configuration_sha256 == config.sha256
            and proof.request == _request(command, proof.request.read_set)
            and proof.original_launch.start.binding == proof.original_job.binding == binding
            and proof.original_job.generation == binding.generation == 1
            and proof.original_job.state == "capacity_uncertain"
            and proof.original_job.cancel_requested and proof.original_job.stop_credit == 0
            and proof.original_job.completion_credit == 1
            and command.job_sha256 == digest(proof.original_job.model_dump(mode="json")),
            "retained close binding or original job differs")
    registration = view.row(proof.registration_key, WorkRegistration, material=True)
    require(registration.binding == configured
            and digest(registration.model_dump(mode="json")) == command.registration_sha256,
            "retained close registration differs")
    original_launch = proof.original_launch
    require(original_launch.phase == "claimed" and original_launch.native_invocation_id == command.invocation_id
            and original_launch.launch_id == command.launch_id == native_key("launch-id", binding.start_request_id)
            and original_launch.target == command.stop.target and command.stop.target in config.active_targets
            and command.stop.endpoint == stop_endpoint(command.stop.target)
            and command.stop.status_code == 204 and command.stop.error_code is None
            and command.stop.credential_client_id == config.identity.managed_identity_client_id
            and command.stop.observed_at <= proof.recorded_at
            and proof.original_target.target == original_launch.target
            and proof.original_target.active_launch_id == command.launch_id
            and proof.original_target.generation == original_launch.process_generation
            and len(proof.original_pool.slots) == 1 and proof.original_pool.slots[0].binding == binding
            and proof.original_pool.pending == (binding.job_id,) and not proof.original_pool.completions,
            "retained original stop or capacity differs")
    sid = assistant_key("session-id", binding.start_request_id)
    sessionkey, artifactkey = assistant_key("session", sid), assistant_key("artifact", binding.work_id)
    session = view.row(sessionkey, _Session)
    artifact = view.row(artifactkey, _ArtifactEffect)
    expected_preserved = {sessionkey, artifactkey, *(assistant_key("model-intent", sid, item.request_sha256)
                                                  for item in proof.model_intents)}
    require(set(proof.preserved) == expected_preserved
            and all(view.hash(key) == expected for key, expected in proof.preserved.items())
            and len({item.request_sha256 for item in proof.model_intents}) == session.model_requests
            and session.execution == artifact.execution == binding and artifact.stage == "saved"
            and session.state in {"running", "waiting"}
            and assistant_key("result", binding.work_id) not in view.inventory.rows,
            "original partial result/model material changed")
    for item in proof.model_intents:
        model = view.row(assistant_key("model-intent", sid, item.request_sha256), _ModelIntent)
        require(model.request_sha256 == item.request_sha256 and model.consumed == item.consumed,
                "original model outcome changed")
    eid = _evidence_id(command.launch_id)
    savedkey = native_key("evidence", eid)
    saved = view.row(savedkey, _NativeEvidence, material=True)
    result = _result(proof, config.delivery.verifier_version)
    evidence = ExecutorEvidence(evidence_id=eid, binding=binding,
        sequence=max(original_launch.evidence_sequence, proof.original_job.last_sequence) + 1,
        native_execution_id=command.invocation_id, state="stopped",
        entire_tree_idle=True, all_inflight_starts_fenced=True, result=result,
        proof=_material({"original_stop_close": command.request_id,
                         "proof_sha256": digest(proof.model_dump(mode="json"))}).data)
    require(saved == _NativeEvidence(launch_id=command.launch_id,
        configuration_digest=original_launch.configuration_digest, probe_id=command.request_id,
        evidence=evidence, observed_at=proof.recorded_at, expires_at=proof.recorded_at + timedelta(seconds=60),
        report_digest=digest(proof.model_dump(mode="json"))), "original close native evidence differs")
    jobkey, poolkey = worker_key("job", binding.job_id), worker_key("pool", config.workers.workspace_id)
    launchkey, targetkey = native_key("launch", command.launch_id), native_key("target", original_launch.target.executor_id)
    expected_job = _update(proof.original_job, state="cancelled", completion_credit=0,
        native_execution_id=command.invocation_id, last_evidence_id=eid, last_sequence=evidence.sequence)
    expected_pool = _update(proof.original_pool,
        slots=tuple(_update(slot, binding=None, lease_until=None) for slot in proof.original_pool.slots),
        pending=(), completions=(binding.job_id,))
    expected_launch = _update(original_launch, phase="sealed", sealed_evidence_id=eid,
                             evidence_sequence=evidence.sequence, pending_control="cancel")
    immutable = {
        command.request_id: proof, savedkey: saved,
        worker_key("evidence", eid): evidence,
        worker_key("completion", binding.job_id, "1"): evidence,
        worker_key("work-result", result.evidence_id): result,
    }
    exact = {}
    for key, value in immutable.items():
        require(view.row(key, type(value), material=True) == value, "atomic close material differs")
        exact[key] = view.hash(key)
    for key, value in {
        jobkey: expected_job, poolkey: expected_pool, launchkey: expected_launch,
        targetkey: _update(proof.original_target, active_launch_id=None),
    }.items():
        exact[key] = view.image_hash(key, value)
    receipt = view.receipt(proof.request, exact, exact=exact, result={
        "state": "cancelled", "job_id": command.job_id, "evidence_id": eid,
        "sdk_finish": False, "delivered": False, "budget_refunded": False,
    })
    stopkey = worker_key("stop-intent", binding.job_id, "1")
    intent = view.row(stopkey, ExecutorStop, material=True)
    stop = view.receipt(intent.request, (jobkey, stopkey), exact={stopkey: view.hash(stopkey)})
    require(intent.binding == binding and stop.guard_revision < receipt.guard_revision,
            "original protocol stop receipt must precede closure")
    launch = view.row(launchkey, _NativeLaunch)
    job = view.row(jobkey, JobRecord)
    require(launch == _update(expected_launch, evidence_sequence=launch.evidence_sequence)
            and launch.evidence_sequence >= evidence.sequence
            and job == _update(expected_job, result_forwarded=job.result_forwarded,
                               last_evidence_id=job.last_evidence_id, last_sequence=job.last_sequence),
            "terminal original job or launch changed")
    return saved


def validate_close_history(inventory, config, bindings):
    candidates = {key: row for key, row in inventory.rows.items()
                  if row.payload["data"].get("kind") == "original_sdk_stop_closed"}
    if not candidates:
        return {}
    from .behavior_recovery_history import _Inventory
    view = _Inventory(inventory, config)
    for key in candidates:
        proof = view.row(key, OriginalStopClosed, material=True)
        require(key == proof.command.request_id, "original close proof key differs")
        _check_history(view, proof, config, bindings)
    return candidates


def verify_closed(native, launch, saved, proof_row):
    """Retained native verification; no effects and no renewed stop permission."""
    proof = _parse(proof_row.payload["data"], OriginalStopClosed)
    require(launch.launch_id == proof.command.launch_id
            and launch.sealed_evidence_id == _evidence_id(launch.launch_id), "original close proof differs")
    runtime = native.assistant.runtime
    runtime.refresh_bindings()
    from .behavior_recovery_history import _Inventory
    # Use only authority/config contracts; no runtime clock/credential authority is inferred.
    require(native.recovery_binding is not None, "installed recovery identity is missing")
    config = SimpleNamespace(
        sha256=native.recovery_binding.configuration_sha256,
        table=SimpleNamespace(scope=native.store._scope), workers=native.workers.contract,
        delivery=runtime.dep.delivery.contract, active_targets=native.configured_targets,
        identity=SimpleNamespace(human_oid=native.recovery_binding.operator_oid,
            managed_identity_client_id=native.recovery_binding.credential_client_id),
    )
    inventory = native.store.read_inventory()
    original = _check_history(_Inventory(inventory, config), proof, config, tuple(runtime.bindings.values()))
    if saved is not None:
        evidence = saved.evidence
        expected = _update(original.evidence, evidence_id=evidence.evidence_id, sequence=evidence.sequence)
        require(saved.launch_id == launch.launch_id and saved.probe_id == proof.command.request_id
                and saved.report_digest == original.report_digest and evidence == expected
                and (evidence.evidence_id == original.evidence.evidence_id
                     or evidence.evidence_id == native_key("terminal-verification-id", launch.launch_id, str(evidence.sequence))
                     and original.evidence.sequence < evidence.sequence <= launch.evidence_sequence),
                "revalidated close evidence differs")
