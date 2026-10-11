"""An original failed instruction with no SDK turn or fabricated checkpoint."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_code_identity import code_sha256
from .behavior_delivery import _Model, _material, _parse, _update, VerifiedWorkResult
from .behavior_instruction import Conversation, conversation_key
from .behavior_operations import control_key
from .behavior_sdk_checkpoint import checkpoint_key
from .behavior_store import ControlRequest
from .behavior_wire import Identifier, SdkNoStart, SdkStartBinding, sdk_start_version
from .behavior_work_memory_prompt import WORK_MEMORY_PROFILE
from .behavior_workers import JobRecord, worker_key
from .errors import Problem
from .source_materials import digest


class NoSdkTurn(_Model):
    kind: Literal["assistant_no_sdk_turn"] = "assistant_no_sdk_turn"
    schema_version: Literal["fa-no-sdk-turn-v1", "fa-no-sdk-turn-v2"]
    conversation_id: Identifier
    job_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    previous_archive_id: Identifier | None
    disposition: SdkNoStart
    closure: ControlRequest

    @model_validator(mode="after")
    def disposition_version(self):
        if self.schema_version != "fa-no-sdk-turn-" + self.disposition.gate.binding.version:
            raise ValueError("No-SDK turn must retain its exact disposition version")
        return self


def keys(authority, session):
    from .behavior_assistant import key
    binding = authority._binding(session)
    task = session.assignment.task.instruction
    return tuple(dict.fromkeys((
        *authority._keys(session), conversation_key(task.conversation_id),
        key("no-sdk-turn", binding.job_id), key("result", binding.work.work_id),
        key("text-archive", binding.job_id), key("reply", binding.job_id),
        checkpoint_key(binding.job_id), key("artifact", binding.work.work_id),
        key("progress", session.assignment.session_id), control_key("operation", binding.operation_id),
    )))


def result_for(session, disposition, verifier_version):
    from .behavior_assistant import key
    return VerifiedWorkResult(
        evidence_id=key("result", session.execution.work_id), verifier_version=verifier_version,
        work_id=session.execution.work_id, definition_digest=session.assignment.task.definition_digest,
        state="failed", passed_checks=(),
        content=_material({"schema_version": "fa-assistant-notice-v1", "state": "failed",
                           "sdk_reply_available": False}).data,
        proof=_material({"schema_version": "fa-sdk-no-start-result-" + disposition.gate.binding.version,
                         "session_id": session.assignment.session_id,
                         "disposition": disposition.model_dump(mode="json")}).data,
    )


def changes(authority, session, disposition, request, rows):
    from .behavior_assistant import _Session, _write, key
    disposition = SdkNoStart.model_validate_json(disposition.model_dump_json())
    binding = authority._binding(session)
    task = session.assignment.task.instruction
    profile = authority.runtime.profiles[session.binding_id]
    scope = session.assignment.sdk_start_binding
    if (task is None or task.sdk_start_profile is None
            or profile.sdk_start_profile != task.sdk_start_profile or scope is None
            or (task.sdk_start_profile == "fa-sdk-start-gate-v2"
                and (task.prompt_profile != WORK_MEMORY_PROFILE or profile.prompt_profile != WORK_MEMORY_PROFILE))
            or disposition.gate.binding != scope
            or scope != SdkStartBinding(
                schema_version="fa-sdk-start-binding-" + sdk_start_version(task.sdk_start_profile),
                profile=task.sdk_start_profile,
                session_id=session.assignment.session_id, sdk_session_id=session.assignment.sdk_session_id,
                job_id=binding.job_id, task_digest=digest(session.assignment.task.model_dump(mode="json")),
                profile_digest=session.assignment.task.profile_digest,
                execution_digest=digest(session.execution.model_dump(mode="json")),
                worker_configuration_digest=authority.runtime.dep.workers.configuration_digest(),
                code_sha256=code_sha256(),
            )):
        raise Problem(403, "SDK_NO_START_SCOPE", "the current server-enabled original code, task and execution are required")
    if disposition.gate.owner.kind == "local-offline" and authority.runtime.mode != "offline_contract":
        raise Problem(403, "SDK_NO_START_SCOPE", "local observation is not native execution evidence")
    authority._gate(session, rows)
    current = _parse(rows[key("session", session.assignment.session_id)].payload["data"], _Session)
    job = _parse(rows[worker_key("job", binding.job_id)].payload["data"], JobRecord)
    ckey = conversation_key(task.conversation_id)
    conversation = _parse(rows[ckey].payload["data"], Conversation)
    excluded = (key("no-sdk-turn", binding.job_id), key("result", binding.work.work_id),
                key("text-archive", binding.job_id), key("reply", binding.job_id),
                checkpoint_key(binding.job_id), key("artifact", binding.work.work_id),
                key("progress", session.assignment.session_id))
    known, unknown = authority.runtime.dep.workers._operation_effects(
        rows, (binding.operation_id,), session.execution.owner)
    if (current != session or current.state != "running" or current.model_requests or current.model_intent_ids
            or job.children or job.cancel_requested or job.generation != 1 or job.checkpoint is not None or known or unknown
            or any(rows[row] is not None for row in excluded)
            or conversation.active_job_id != binding.job_id or conversation.jobs[-1] != binding.job_id
            or len(conversation.jobs) != task.turn_number
            or conversation.sdk_session_id != session.assignment.sdk_session_id):
        raise Problem(409, "SDK_NO_START_UNRESOLVED", "original work has activity or unresolved state; no no-SDK closure")
    saved = NoSdkTurn(schema_version="fa-no-sdk-turn-" + scope.version,
                      conversation_id=task.conversation_id, job_id=binding.job_id, turn_number=task.turn_number,
                      previous_archive_id=task.previous_archive_id, disposition=disposition, closure=request)
    result = result_for(session, disposition, authority.runtime.dep.delivery.contract.verifier_version)
    writes = (
        _write(key("result", binding.work.work_id), result, "create", _material(result.model_dump(mode="json"))),
        _write(key("no-sdk-turn", binding.job_id), saved, "create", _material(saved.model_dump(mode="json"))),
        _write(key("session", session.assignment.session_id), _update(current, state="ready_to_finalize"), "replace"),
        _write(ckey, _update(conversation, active_job_id=None, archive_id=None), "replace"),
    )
    return result, writes


def finish_local(authority, session, disposition):
    from .behavior_assistant import key
    from .behavior_store import ControlChanges
    if authority.runtime.mode != "offline_contract" or disposition.gate.owner.kind != "local-offline":
        raise Problem(403, "SDK_NO_START_SCOPE", "only the original offline executor can use local closure")
    request = ControlRequest(request_id=key("no-sdk-finish", session.assignment.session_id),
        request_digest=digest(disposition.model_dump(mode="json")), read_set=keys(authority, session))

    def decide(rows):
        result, writes = changes(authority, session, disposition, request, rows)
        return ControlChanges(writes=writes, result={"state": "ready_to_finalize",
            "result_digest": digest(result.model_dump(mode="json")), "work_outcome": "failed"})

    return authority.store.transact(request, decide).payload["result"]


def validate_history(view, record, image):
    from .behavior_assistant import _Session, key
    from .behavior_runtime_rotation_history import require
    binding, task = record.binding, record.profile.instruction
    nkey = key("no-sdk-turn", binding.job_id)
    saved = view.row(nkey, NoSdkTurn, material=True)
    sid = saved.disposition.gate.binding.session_id
    skey, rkey, ckey = key("session", sid), key("result", binding.work.work_id), conversation_key(task.conversation_id)
    session = view.row(skey, _Session)
    result = view.row(rkey, VerifiedWorkResult, material=True)
    require(task.sdk_start_profile == record.profile.sdk_start_profile == saved.disposition.gate.binding.profile
            and (task.sdk_start_profile != "fa-sdk-start-gate-v2" or task.prompt_profile == WORK_MEMORY_PROFILE)
            and saved.conversation_id == task.conversation_id and saved.job_id == binding.job_id
            and saved.turn_number == task.turn_number and saved.previous_archive_id == task.previous_archive_id
            and session.assignment.task.instruction == task and session.assignment.sdk_session_id == image.sdk_session_id
            and session.assignment.sdk_start_binding == saved.disposition.gate.binding
            and session.execution.job_id == binding.job_id and session.execution.owner == binding.owner
            and session.state == "ready_to_finalize" and session.model_requests == 0 and not session.model_intent_ids
            and result == result_for(session, saved.disposition, result.verifier_version),
            "original no-SDK failure changed identity, outcome or preserved material")
    require(all(key(kind, binding.job_id) not in view.inventory.rows for kind in ("text-archive", "reply"))
            and checkpoint_key(binding.job_id) not in view.inventory.rows,
            "no-SDK failure cannot manufacture an SDK archive")
    expected = image.model_copy(update={"active_job_id": None, "archive_id": None})
    write_keys = [nkey, skey, rkey, ckey]
    owner = saved.disposition.gate.owner
    if owner.kind == "hosted":
        from .behavior_foundry import native_key, _NativeLaunch
        from .behavior_wire import HostedReportBody
        receipt = view.receipts.get(saved.closure.request_id)
        require(receipt is not None and receipt.result.get("state") == "failed", "no-SDK native receipt missing")
        probe_id = receipt.result.get("probe_id")
        eid = native_key("evidence-id", owner.launch_id, probe_id)
        report_key = native_key("report", eid)
        report = view.row(report_key, HostedReportBody, material=True)
        launch = view.row(native_key("launch", owner.launch_id), _NativeLaunch)
        require(report.sdk_no_start == saved.disposition and report.phase == "sdk_not_started"
                and report.process_id == owner.supervisor_id == launch.process_id
                and report.claim_id == owner.claim_id == launch.claim_id
                and saved.closure.request_id == native_key("report", owner.launch_id, probe_id),
                "no-SDK failure lacks its original process-bound report")
        write_keys.extend((native_key("evidence", eid), report_key, native_key("launch", owner.launch_id),
                           native_key("target", launch.target.executor_id, binding.job_id)))
    else:
        require(saved.closure.request_id == key("no-sdk-finish", sid)
                and saved.closure.request_digest == digest(saved.disposition.model_dump(mode="json")),
                "offline no-SDK failure lacks its original closure request")
    receipt = view.receipt(saved.closure, write_keys, exact={
        nkey: view.hash(nkey), skey: view.hash(skey), rkey: view.hash(rkey), ckey: view.image_hash(ckey, expected)})
    return expected, {nkey}, receipt
