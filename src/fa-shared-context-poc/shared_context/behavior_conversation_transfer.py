"""Explicit new-environment preparation; never submits a message or runs the SDK."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import Field, field_validator

from . import behavior_assistant_wire as aw, behavior_wire as wire
from .behavior_assistant_checkpoint import unpack
from .behavior_delivery import _Model, _material, _parse, _utc
from .behavior_foundry import FoundryTarget, SESSION_VERSION_CHANGED, native_key, session_version_failure
from .behavior_instruction import Conversation, TurnArchive, conversation_key, turn_material
from .behavior_operations import control_key
from .behavior_sdk_checkpoint import SdkCheckpointStore, SdkStateUpload, checkpoint_key
from .behavior_sdk_state import SdkState, StoredSdkArchive
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_workers import JobRecord, worker_key
from .errors import Problem
from .source_materials import digest

if TYPE_CHECKING:
    from .behavior_work_policy import WorkRegistration


class TransferCommand(wire.Command):
    failed_job_id: wire.Identifier
    expected_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]


class TransferView(wire.WireModel):
    schema_version: Literal["fa-conversation-environment-v1"] = "fa-conversation-environment-v1"
    conversation_id: wire.Identifier
    failed_job_id: wire.Identifier
    state: Literal["available", "pending", "ready"]
    archive_turn_number: Annotated[int, Field(ge=1)]
    destination_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    transfer_id: wire.Identifier | None = None
    hosted_session_id: wire.Identifier | None = None
    sdk_restored: Literal[False] = False
    failed_message_resent: Literal[False] = False
    permissions_changed: Literal[False] = False


class EnvironmentTransfer(_Model):
    kind: Literal["conversation_environment_transfer"] = "conversation_environment_transfer"
    transfer_id: wire.Identifier
    command: TransferCommand
    source: Conversation
    previous_transfer_id: wire.Identifier | None
    target: FoundryTarget
    archive_sha256: wire.Digest
    archive_turn_number: Annotated[int, Field(ge=1)]
    profile_digest: wire.Digest
    policy_sha256: wire.Digest
    requested_by: wire.Identifier
    prepared_at: datetime
    request: ControlRequest

    _time = field_validator("prepared_at")(_utc)


class EnvironmentHead(_Model):
    kind: Literal["conversation_environment_head"] = "conversation_environment_head"
    transfer_id: wire.Identifier


def transfer_key(identifier, failed_job_id):
    return control_key("conversation-environment-transfer", identifier, failed_job_id)


def head_key(identifier):
    return control_key("conversation-environment-head", identifier)


@dataclass(frozen=True)
class _Source:
    conversation: Conversation
    job: JobRecord
    record: WorkRegistration
    archive: TurnArchive


def _source(control, authority, principal, identifier):
    control._human(principal, "read")
    policy = control.installed()
    if not policy.active or not policy.read_enabled:
        raise Problem(403, "WORK_POLICY_REVOKED", "current conversation access is required")
    if authority is None or authority.runtime is not control.runtime:
        raise Problem(503, "CONVERSATION_TRANSFER_UNAVAILABLE", "the original checkpoint reader is unavailable")
    ckey = conversation_key(identifier)
    row = control.store.read_snapshot((ckey,))[ckey]
    if row is None:
        raise Problem(404, "CONVERSATION_NOT_FOUND", "original conversation is unavailable")
    conversation = _parse(row.payload["data"], Conversation)
    control._refresh_jobs(conversation.jobs)
    if ((conversation.human_oid, conversation.team_id, conversation.agent_oid)
            != (principal.oid, policy.template.team_id, policy.template.agent_oid)):
        raise Problem(409, "CONVERSATION_TRANSFER_SCOPE", "the original human, Team and agent must match")
    if conversation.reset_request_id is not None or not conversation.jobs:
        raise Problem(409, "CONVERSATION_TRANSFER_NOT_READY", "an unarchived failed original turn is required")
    job = control.runtime.dep.workers.inspect(conversation.jobs[-1])
    if (job.state != "failed" or not job.result_forwarded
            or session_version_failure(control.store, job) != SESSION_VERSION_CHANGED
            or conversation.active_job_id not in {None, job.spec.job_id}):
        raise Problem(409, "CONVERSATION_TRANSFER_NOT_READY", "the original version failure must be fully forwarded")
    record = control._job_record(job.spec.job_id)
    task = None if record is None else record.profile.instruction
    if (task is None or task.conversation_id != identifier or task.turn_number != len(conversation.jobs)
            or record.hosted_session_id != conversation.hosted_session_id
            or task.hosted_agent_version != conversation.hosted_agent_version):
        raise Problem(409, "CONVERSATION_TRANSFER_SCOPE", "the saved original assignment differs")
    control.runtime.authorize(record.binding.binding_id, principal, "read")
    archive = _checkpoint(control, authority, conversation, record)
    return _Source(conversation, job, record, archive)


def _checkpoint(control, authority, conversation, record):
    task = record.profile.instruction
    identifier = conversation.conversation_id
    from .behavior_profile_selection import choice_for_conversation, profile_from_template
    choice = choice_for_conversation(control, conversation)
    consumer = control.policy.profile if choice is None else profile_from_template(control.policy, choice.command.template)
    if consumer.checkpoint_profile != task.checkpoint_profile or consumer.model_provider != task.model_provider:
        raise Problem(409, "CONVERSATION_TRANSFER_ARCHIVE_INVALID", "the current checkpoint consumer profile differs")
    if conversation.archive_id is None or conversation.archive_id != task.previous_archive_id:
        raise Problem(409, "CONVERSATION_TRANSFER_ARCHIVE_INVALID", "the last original SDK checkpoint is required")
    akey = conversation.archive_id
    archived = control.store.read_snapshot((akey,))[akey]
    if archived is None:
        raise Problem(409, "CONVERSATION_TRANSFER_ARCHIVE_INVALID", "the original checkpoint is missing")
    try:
        archive = _parse(archived.payload["data"], TurnArchive)
        from .behavior_assistant import key
        if (archive.conversation_id != identifier or archive.turn_number >= task.turn_number
                or akey != key("text-archive", archive.job_id) or not archive.sdk_stop.graceful
                or archived.payload["material"] != turn_material(archive).model_dump(mode="json")
                or not isinstance(archive.archive, (aw.SdkArchive, StoredSdkArchive))
                or archive.archive.sdk_session_id != conversation.sdk_session_id):
            raise ValueError("original archive differs")
        if isinstance(archive.archive, StoredSdkArchive):
            upload_key = checkpoint_key(archive.job_id)
            uploaded = control.store.read_snapshot((upload_key,))[upload_key]
            if uploaded is None:
                raise ValueError("original checkpoint publication is missing")
            upload = _parse(uploaded.payload["data"], SdkStateUpload)
            if (upload.reference != archive.archive or upload.source != archive.archive.source
                    or upload.job_id != archive.job_id or upload.sha256 != archive.archive.sha256
                    or upload.byte_length != archive.archive.byte_length or upload.sdk_stop != archive.sdk_stop):
                raise ValueError("original checkpoint publication differs")
            SdkCheckpointStore(authority.artifacts.store).load(
                archive.archive, record.binding, conversation_id=identifier,
                sdk_session_id=conversation.sdk_session_id, job_id=archive.job_id,
                checkpoint_profile=task.checkpoint_profile,
                model_provider=task.model_provider,
            )
        else:
            SdkState(sdk_session_id=conversation.sdk_session_id, files=unpack(archive.archive))
    except (ValueError, TypeError, KeyError):
        raise Problem(409, "CONVERSATION_TRANSFER_ARCHIVE_INVALID", "a complete compatible SDK archive is required") from None
    except Problem as error:
        if error.status in {403, 404, 409}:
            raise Problem(409, "CONVERSATION_TRANSFER_ARCHIVE_INVALID",
                          "the original checkpoint cannot be verified under its current scope and profile") from None
        raise
    return archive


def _destination(control, source):
    executor = control._session_executor()
    targets = executor.authority.targets
    if len(targets) != 1 or targets[0].agent_oid != source.record.binding.agent_oid:
        raise Problem(409, "CONVERSATION_TRANSFER_SCOPE", "the destination must retain the original agent identity")
    from .behavior_agent_versions import current_hosted_version
    version = current_hosted_version(control.runtime, targets[0])
    try:
        return FoundryTarget.model_validate_json(
            targets[0].model_copy(update={"agent_version": version}).model_dump_json())
    except ValueError:
        raise Problem(503, "INSTRUCTION_HOSTED_CONFIGURATION", "one current concrete Hosted version is required") from None


def _head(store, identifier):
    key = head_key(identifier)
    row = store.read_snapshot((key,))[key]
    return None if row is None else _parse(row.payload["data"], EnvironmentHead).transfer_id


def _keys(control, source, transfer_id):
    from .behavior_work_policy import work_policy_key
    launch_id = native_key("launch-id", source.job.binding.start_request_id)
    return tuple(sorted({
        control.policy_key, control.revocation_key, control.cost_key,
        transfer_id, head_key(source.conversation.conversation_id),
        conversation_key(source.conversation.conversation_id),
        worker_key("job", source.job.spec.job_id),
        work_policy_key("registration", source.record.admission_id),
        native_key("launch", launch_id), native_key("unclaimed-fence", launch_id),
        native_key("evidence", native_key("unclaimed-fence-evidence", launch_id)),
        source.conversation.archive_id,
        *(() if not isinstance(source.archive.archive, StoredSdkArchive) else (checkpoint_key(source.archive.job_id),)),
    }))


def _apply_request(value):
    return ControlRequest(
        request_id=control_key("conversation-environment-ready", value.transfer_id),
        request_digest=digest(value.model_dump(mode="json")), read_set=value.request.read_set,
    )


def _result(value, state):
    return TransferView(
        conversation_id=value.source.conversation_id, failed_job_id=value.command.failed_job_id,
        state=state, archive_turn_number=value.archive_turn_number,
        destination_agent_version=value.target.agent_version, transfer_id=value.transfer_id,
        hosted_session_id=value.target.session_id,
    )


def read_transfer(control, transfer_id):
    row = control.store.read_snapshot((transfer_id,))[transfer_id]
    if row is None:
        raise Problem(409, "CONVERSATION_TRANSFER_UNKNOWN", "the original environment transfer is not confirmed")
    value = _parse(row.payload["data"], EnvironmentTransfer)
    expected = transfer_key(value.source.conversation_id, value.command.failed_job_id)
    if (expected != value.transfer_id or expected != transfer_id
            or row.payload["material"] != _material(value.model_dump(mode="json")).model_dump(mode="json")):
        raise Problem(409, "CONVERSATION_TRANSFER_INVALID", "the immutable environment transfer differs")
    receipt = control.store.reconcile(value.request)
    from .source_materials import _payload_hash
    if dict(receipt.payload["writes"]).get(transfer_id) != _payload_hash(row.payload):
        raise Problem(409, "CONVERSATION_TRANSFER_INVALID", "the original transfer reservation differs")
    return value


def _access(control, principal, value):
    control._human(principal, "read")
    policy = control.installed()
    if (not policy.active or not policy.read_enabled or policy.sha256 != value.policy_sha256
            or value.requested_by != principal.oid or value.source.human_oid != principal.oid
            or (value.source.team_id, value.source.agent_oid) !=
               (policy.template.team_id, policy.template.agent_oid)):
        raise Problem(409, "CONVERSATION_TRANSFER_SCOPE", "current access to the original conversation is required")


def _completed(control, value):
    from .behavior_store import _Receipt, _Row
    from .source_materials import _payload_hash
    request = _apply_request(value)
    stored = control.store._read("behavior:v1:receipt:" + request.request_id, _Receipt)
    if stored is None:
        return False
    receipt = control.store.reconcile(request)
    ckey, hkey = conversation_key(value.source.conversation_id), head_key(value.source.conversation_id)
    expected = {
        ckey: _payload_hash(_Row(binding=control.store._binding, row_id=ckey,
                                data=ready_conversation(value).model_dump(mode="json")).model_dump(mode="json")),
        hkey: _payload_hash(_Row(binding=control.store._binding, row_id=hkey,
                                data=EnvironmentHead(transfer_id=value.transfer_id).model_dump(mode="json")).model_dump(mode="json")),
    }
    if (receipt.payload["result"] != _result(value, "ready").model_dump(mode="json")
            or dict(receipt.payload["writes"]) != expected):
        raise Problem(409, "CONVERSATION_TRANSFER_INVALID", "the original environment readiness receipt differs")
    return True


def current_transfer(control, conversation):
    identifier = _head(control.store, conversation.conversation_id)
    if identifier is None:
        return None
    value = read_transfer(control, identifier)
    if (not _completed(control, value) or value.source.conversation_id != conversation.conversation_id
            or value.source.sdk_session_id != conversation.sdk_session_id
            or (value.source.human_oid, value.source.team_id, value.source.agent_oid) !=
               (conversation.human_oid, conversation.team_id, conversation.agent_oid)
            or (conversation.hosted_session_id, conversation.hosted_agent_version) !=
               (value.target.session_id, value.target.agent_version)
            or conversation.jobs[:len(value.source.jobs)] != value.source.jobs):
        raise Problem(409, "CONVERSATION_TRANSFER_INVALID", "the current environment lacks its original transfer receipt")
    return value


def _archive_matches(value, archive):
    from .behavior_assistant import key
    return (
        archive.conversation_id == value.source.conversation_id
        and archive.turn_number == value.archive_turn_number
        and key("text-archive", archive.job_id) == value.source.archive_id
        and isinstance(archive.archive, (aw.SdkArchive, StoredSdkArchive))
        and archive.archive.sha256 == value.archive_sha256
        and archive.archive.sdk_session_id == value.source.sdk_session_id
    )


def matches_resume_source(value, record, archive):
    task = record.profile.instruction
    return (value is not None and task is not None and record.environment_transfer_id == value.transfer_id
            and task.conversation_id == value.source.conversation_id
            and task.turn_number == len(value.source.jobs) + 1
            and task.previous_archive_id == value.source.archive_id
            and record.hosted_session_id == value.target.session_id
            and task.hosted_agent_version == value.target.agent_version and _archive_matches(value, archive))


def matches_registration(value, record):
    task = record.profile.instruction
    return (task is not None and record.environment_transfer_id == value.transfer_id
            and task.conversation_id == value.source.conversation_id
            and task.turn_number > len(value.source.jobs)
            and (record.binding.human_oid, record.binding.team_id, record.binding.agent_oid) ==
               (value.source.human_oid, value.source.team_id, value.source.agent_oid)
            and record.principal_oid == value.requested_by and record.policy_sha256 == value.policy_sha256
            and record.hosted_session_id == value.target.session_id
            and task.hosted_agent_version == value.target.agent_version)


def registration_transfer(control, record):
    if record.environment_transfer_id is None:
        return None
    value = read_transfer(control, record.environment_transfer_id)
    if not _completed(control, value) or not matches_registration(value, record):
        raise Problem(409, "CONVERSATION_TRANSFER_INVALID", "registered work escaped its original environment transfer")
    return value


def permits_archive_gap(control, record, archive):
    if record is None or record.environment_transfer_id is None:
        return False
    value = registration_transfer(control, record)
    return matches_resume_source(value, record, archive)


def transferred_failure(control, conversation, job):
    value = current_transfer(control, conversation)
    return (value is not None and job is not None and job.spec.job_id == value.command.failed_job_id
            and job.state == "failed" and job.result_forwarded
            and conversation.archive_id == value.source.archive_id
            and session_version_failure(control.store, job) == SESSION_VERSION_CHANGED)


def ready_conversation(value):
    return value.source.model_copy(update={
        "hosted_session_id": value.target.session_id, "hosted_agent_version": value.target.agent_version,
        "active_job_id": None,
    })


def preview(control, authority, principal, identifier):
    control._human(principal, "read")
    ckey = conversation_key(identifier)
    current = control.store.read_snapshot((ckey,))[ckey]
    conversation = None if current is None else _parse(current.payload["data"], Conversation)
    control._refresh_jobs(() if conversation is None else conversation.jobs)
    if conversation is not None:
        if conversation.jobs:
            key = transfer_key(identifier, conversation.jobs[-1])
            if control.store.read_snapshot((key,))[key] is not None:
                value = read_transfer(control, key)
                _access(control, principal, value)
                return _result(value, "ready" if _completed(control, value) else "pending")
    source = _source(control, authority, principal, identifier)
    target = _destination(control, source)
    return TransferView(
        conversation_id=identifier, failed_job_id=source.job.spec.job_id, state="available",
        archive_turn_number=source.archive.turn_number, destination_agent_version=target.agent_version,
    )


def _check_original(control, principal, value, rows):
    control._human(principal, "submit")
    control._human(principal, "read")
    policy = control.installed(rows)
    ckey, hkey = conversation_key(value.source.conversation_id), head_key(value.source.conversation_id)
    current = None if rows[ckey] is None else _parse(rows[ckey].payload["data"], Conversation)
    head = None if rows[hkey] is None else _parse(rows[hkey].payload["data"], EnvironmentHead).transfer_id
    job = None if rows[worker_key("job", value.command.failed_job_id)] is None else _parse(
        rows[worker_key("job", value.command.failed_job_id)].payload["data"], JobRecord)
    if (not policy.active or not policy.read_enabled or policy.sha256 != value.policy_sha256
            or current != value.source or head != value.previous_transfer_id
            or job is None or job.state != "failed" or not job.result_forwarded):
        raise Problem(409, "CONVERSATION_TRANSFER_CHANGED", "the original failed conversation changed")


def _budget(control, request_id):
    from .behavior_work_policy import CostLedger, cumulative_decision
    key = control.cost_key
    ledger = _parse(control.store.read_snapshot((key,))[key].payload["data"], CostLedger)
    decision = cumulative_decision(
        control.installed(), ledger, control.cost_reader(), control.runtime._now(),
        read_id=request_id, mode=control.runtime.mode, instruction=True,
    )
    if decision.decision != "allow":
        raise Problem(403, "CONVERSATION_TRANSFER_BUDGET", "current work allowance does not permit environment preparation")


def _finish(control, authority, principal, value):
    _access(control, principal, value)
    if _completed(control, value):
        return _result(value, "ready")
    source = _source(control, authority, principal, value.source.conversation_id)
    if (source.conversation != value.source or source.archive.archive.sha256 != value.archive_sha256
            or source.archive.turn_number != value.archive_turn_number
            or digest(source.record.profile.model_dump(mode="json")) != value.profile_digest):
        raise Problem(409, "CONVERSATION_TRANSFER_CHANGED", "the original restore source changed")
    request = _apply_request(value)
    def ready(rows):
        _check_original(control, principal, value, rows)
        if _parse(rows[value.transfer_id].payload["data"], EnvironmentTransfer) != value:
            raise Problem(409, "CONVERSATION_TRANSFER_INVALID", "the original transfer reservation changed")
        conversation = ready_conversation(value)
        hkey = head_key(value.source.conversation_id)
        head = EnvironmentHead(transfer_id=value.transfer_id)
        return ControlChanges(writes=(
            ControlWrite(row_id=conversation_key(conversation.conversation_id), mode="replace",
                         data=conversation.model_dump(mode="json")),
            ControlWrite(row_id=hkey, mode="create" if rows[hkey] is None else "replace",
                         data=head.model_dump(mode="json")),
        ), result=_result(value, "ready").model_dump(mode="json"))
    control.store.transact(request, ready)
    return _result(value, "ready")


def _new_transfer(control, authority, principal, identifier, command, key):
    source = _source(control, authority, principal, identifier)
    if source.job.spec.job_id != command.failed_job_id:
        raise Problem(409, "CONVERSATION_TRANSFER_CHANGED", "the displayed failed turn is no longer current")
    _budget(control, command.request_id)
    selected = _destination(control, source)
    if selected.agent_version != command.expected_agent_version:
        raise Problem(409, "CONVERSATION_TRANSFER_CHANGED", "the displayed destination version changed")
    target = selected.model_copy(update={
        "session_id": "fa-move-" + control_key(
            "conversation-environment-session", identifier, command.failed_job_id, command.request_id)[:48],
    })
    request = ControlRequest(
        request_id=key, request_digest=digest([identifier, command.model_dump(mode="json")]),
        read_set=_keys(control, source, key),
    )
    return EnvironmentTransfer(
        transfer_id=key, command=command, source=source.conversation,
        previous_transfer_id=_head(control.store, identifier), target=target,
        archive_sha256=source.archive.archive.sha256, archive_turn_number=source.archive.turn_number,
        profile_digest=digest(source.record.profile.model_dump(mode="json")),
        policy_sha256=control.policy.sha256, requested_by=principal.oid, prepared_at=control.runtime._now(),
        request=request,
    )


def prepare(control, authority, principal, identifier, command, *, reconcile=False):
    control._human(principal, "submit")
    control._human(principal, "read")
    command = TransferCommand.model_validate_json(command.model_dump_json())
    key = transfer_key(identifier, command.failed_job_id)
    if control.store.read_snapshot((key,))[key] is not None:
        value = read_transfer(control, key)
        _access(control, principal, value)
        if value.command != command:
            raise Problem(409, "CONVERSATION_TRANSFER_CONFLICT", "keep the original transfer request and environment")
        if _completed(control, value):
            return _result(value, "ready")
        fresh = False
    else:
        if reconcile:
            raise Problem(409, "CONVERSATION_TRANSFER_UNKNOWN", "the original transfer is not confirmed; do not recreate")
        try:
            value = _new_transfer(control, authority, principal, identifier, command, key)
        except Problem as error:
            details = error.body["error"]
            if 400 <= error.status < 500 and details["code"].startswith("CONVERSATION_TRANSFER_"):
                raise Problem(error.status, details["code"], details["message"],
                              transfer_refusal={"stage": "before-reservation", "reserved": False}) from None
            raise
        fresh = False
        def reserve(rows):
            nonlocal fresh
            _check_original(control, principal, value, rows)
            if rows[key] is not None:
                raise Problem(409, "CONVERSATION_TRANSFER_CONFLICT", "the failed turn already has an original transfer")
            fresh = True
            return ControlChanges(writes=(
                ControlWrite(row_id=key, mode="create", data=value.model_dump(mode="json"),
                             material=_material(value.model_dump(mode="json"))),
            ), result={"transfer_reserved": True})
        control.store.transact(value.request, reserve)
        if not fresh:
            value = read_transfer(control, key)
    executor = control._session_executor()
    original = executor.clients.get(value.target.executor_id)
    if original is None:
        raise Problem(409, "CONVERSATION_TRANSFER_UNAVAILABLE", "the original destination transport is unavailable")
    client = original.for_target(value.target)
    def before_send():
        _access(control, principal, value)
        _check_original(control, principal, value, control.store.read_snapshot(value.request.read_set))
        if fresh:
            _budget(control, command.request_id)
    try:
        if fresh:
            client.create_session(before_send=before_send)
        client.verify_session(before_send=before_send)
    except Problem as error:
        if error.body["error"]["code"] in {
            "CONVERSATION_TRANSFER_SCOPE", "CONVERSATION_TRANSFER_CHANGED", "CONVERSATION_TRANSFER_BUDGET",
            "WORK_POLICY_REVOKED",
        }:
            raise
        raise Problem(409, "CONVERSATION_TRANSFER_UNKNOWN",
                      "the original new environment remains unconfirmed; only read that same session") from None
    return _finish(control, authority, principal, value)
