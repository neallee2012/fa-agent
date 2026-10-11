"""Immutable SDK checkpoints in the existing ArtifactStore; no new conversation engine."""
import hashlib
import json
from typing import Literal
from pydantic import Field

from .behavior_operations import _Model, _row, _write, _updated
from .behavior_store import ControlRequest, ControlChanges
from .behavior_wire import AssistantSdkStop, Digest, Identifier
from .behavior_sdk_state import (
    SdkState, StoredSdkArchive, SdkStatePart, SaveSdkState, CheckpointSource,
    DIAG_CODECS, DIAG_ARCHIVE_CODECS, ARCHIVE_CODECS,
)
from .errors import Problem
from .m3_models import MAX_ARTIFACT_BYTES
from .source_materials import digest
from .behavior_workers import worker_key


class SdkStateUpload(_Model):
    kind: Literal["assistant_sdk_state_upload"] = "assistant_sdk_state_upload"
    job_id: Identifier
    sdk_session_id: Identifier
    conversation_id: Identifier
    sha256: Digest
    byte_length: int
    sdk_stop: AssistantSdkStop
    prepare: ControlRequest
    reference: StoredSdkArchive | None = None
    source: CheckpointSource | None = Field(default=None, exclude_if=lambda v: v is None)


def checkpoint_key(job_id):
    from .behavior_assistant import key
    return key("sdk-state-upload", job_id)


def artifact_identity(tenant, team, agent, conversation, sdk_session):
    return digest(["sdk-state-v1", tenant, team, agent, conversation, sdk_session])


def part_version(job_id, state_sha, index):
    return digest(["sdk-state-part-v1", job_id, state_sha, index])[:32]


class SdkCheckpointStore:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def save(self, state, binding, *, conversation_id, source=None, checkpoint_profile=None, model_provider=None):
        state = SdkState.model_validate_json(state.model_dump_json())
        if state.model_provider != model_provider:
            raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint provider differs from the original task")
        _codec_source(state.codec, source, checkpoint_profile)
        if source is not None:
            from .behavior_sdk_compaction import events_from, validate_execution
            if (source.execution.sdk_session_id != state.sdk_session_id
                    or source.execution.conversation_id != conversation_id):
                raise Problem(403, "ASSISTANT_CHECKPOINT_SCOPE", "checkpoint source belongs to another SDK scope")
            validate_execution(events_from(state.files), source.execution)
        raw = state.encoded()
        state_sha = hashlib.sha256(raw).hexdigest()
        artifact = artifact_identity(binding.tenant_id, binding.team_id, binding.agent_oid,
                                     conversation_id, state.sdk_session_id)
        parts = []
        for index, offset in enumerate(range(0, len(raw), MAX_ARTIFACT_BYTES)):
            data = raw[offset:offset + MAX_ARTIFACT_BYTES]
            version = part_version(binding.job_id, state_sha, index)
            receipt = self.artifacts.put(binding.team_id, artifact, version, data, hashlib.sha256(data).hexdigest())
            # A write acknowledgment alone is not a complete, restorable checkpoint.
            if self.artifacts.read(binding.team_id, artifact, version, receipt) != data:
                raise Problem(409, "SDK_STATE_INCOMPLETE", "SDK state part readback differs")
            parts.append(SdkStatePart(version_id=version, receipt=receipt))
        return StoredSdkArchive(sdk_session_id=state.sdk_session_id, conversation_id=conversation_id,
            job_id=binding.job_id, tenant_id=binding.tenant_id, team_id=binding.team_id, agent_oid=binding.agent_oid,
            artifact_id=artifact, sha256=state_sha, byte_length=len(raw), parts=tuple(parts),
            codec=ARCHIVE_CODECS[state.codec], source=source,
            model_provider=state.model_provider)

    def load(self, reference, binding, *, conversation_id, sdk_session_id, job_id, checkpoint_profile=None, model_provider=None):
        reference = StoredSdkArchive.model_validate_json(reference.model_dump_json())
        reference.require_profile(checkpoint_profile, model_provider=model_provider)
        artifact = artifact_identity(binding.tenant_id, binding.team_id, binding.agent_oid,
                                     conversation_id, sdk_session_id)
        if ((reference.tenant_id, reference.team_id, reference.agent_oid, reference.conversation_id,
             reference.sdk_session_id, reference.job_id, reference.artifact_id) !=
                (binding.tenant_id, binding.team_id, binding.agent_oid, conversation_id, sdk_session_id, job_id, artifact)):
            raise Problem(403, "ASSISTANT_CHECKPOINT_SCOPE", "SDK state reference belongs to another scope")
        chunks = []
        for index, part in enumerate(reference.parts):
            if part.version_id != part_version(job_id, reference.sha256, index):
                raise Problem(409, "SDK_STATE_INCOMPLETE", "SDK state part identity differs")
            try:
                data = self.artifacts.read(binding.team_id, artifact, part.version_id, part.receipt)
            except Problem as error:
                if error.body["error"]["code"] not in {"ARTIFACT_BLOB_NOT_FOUND", "ARTIFACT_INTEGRITY_ERROR"}:
                    raise
                raise Problem(409, "SDK_STATE_INCOMPLETE", "required SDK state part is missing or differs") from None
            if len(data) != part.receipt.bytes or hashlib.sha256(data).hexdigest() != part.receipt.sha256:
                raise Problem(409, "SDK_STATE_INCOMPLETE", "SDK state part hash differs")
            chunks.append(data)
        raw = b"".join(chunks)
        if len(raw) != reference.byte_length or hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise Problem(409, "SDK_STATE_INCOMPLETE", "complete SDK state hash differs")
        try:
            state = SdkState.model_validate_json(raw)
            if (state.sdk_session_id != sdk_session_id or state.encoded() != raw
                    or state.model_provider != reference.model_provider
                    or ARCHIVE_CODECS[state.codec] != reference.codec):
                raise ValueError()
        except ValueError:
            raise Problem(409, "SDK_STATE_INCOMPLETE", "SDK state version, identity or content differs") from None
        if reference.source is not None:
            from .behavior_sdk_compaction import events_from, validate_execution
            validate_execution(events_from(state.files), reference.source.execution)
        return state


def _codec_source(codec, source, profile):
    if codec in DIAG_CODECS:
        if source is None or profile is None or source.profile != profile:
            raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "the trusted checkpoint profile is not enabled")
    elif source is not None:
        raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "ordinary checkpoints cannot carry diagnostic provenance")


def _source(authority, session, command):
    from .behavior_sdk_compaction import events_from, validate_execution, validate_inherited, unverified
    task = session.assignment.task.instruction
    profile = task.checkpoint_profile
    if command.state.codec not in DIAG_CODECS:
        if command.compaction is not None:
            unverified()
        if profile is not None and any(e["type"].startswith("session.compaction") for e in events_from(command.state.files)):
            unverified()
        if task.previous_archive_id is not None:
            # A restored checkpoint cannot quietly downgrade after compaction.
            from .behavior_instruction import previous_turn
            previous, _ = previous_turn(authority, session)
            if (profile is not None or isinstance(previous.archive, StoredSdkArchive)
                    and previous.archive.codec in DIAG_ARCHIVE_CODECS):
                unverified()
        return None, {}
    if profile is None or task.previous_archive_id is None:
        unverified()
    from .behavior_instruction import previous_turn
    previous, row = previous_turn(authority, session)
    reads = {task.previous_archive_id: row}
    if isinstance(previous.archive, StoredSdkArchive):
        prior = load_state(authority, session, previous.archive, job_id=previous.job_id)
        previous_key = checkpoint_key(previous.job_id)
        reads.update(authority.store.read_snapshot((previous_key,)))
        if _row(reads, previous_key, SdkStateUpload).reference != previous.archive:
            unverified()
    else:
        from .behavior_assistant_checkpoint import unpack
        prior = SdkState(sdk_session_id=previous.archive.sdk_session_id, files=unpack(previous.archive))
    events = events_from(command.state.files)
    validate_inherited(events, prior)
    if prior.codec in DIAG_CODECS:
        if command.compaction is not None or command.state.projected_event_ids != prior.projected_event_ids:
            unverified()
        execution = previous.archive.source.execution
    else:
        execution = command.compaction
        if (execution is None or execution.job_id != session.execution.job_id
                or execution.profile_digest != session.assignment.task.profile_digest
                or execution.runtime_pid != command.sdk_stop.pid or execution.base_state_sha256 != prior.sha256):
            unverified()
        from .behavior_assistant import _ModelIntent, key
        intent_key = key("model-intent", session.assignment.session_id, execution.model_request_sha256)
        reads.update(authority.store.read_snapshot((intent_key,)))
        if reads[intent_key] is None:
            unverified()
        intent = _row(reads, intent_key, _ModelIntent)
        if not intent.consumed or intent.request_sha256 != execution.model_request_sha256:
            unverified()
    if (execution.sdk_session_id != session.assignment.sdk_session_id
            or execution.conversation_id != task.conversation_id):
        unverified()
    validate_execution(events, execution)
    return CheckpointSource(profile=profile, execution=execution,
                            previous_job_id=previous.job_id, previous_sha256=previous.archive.sha256), reads


def _request(session, keys, phase, value):
    from .behavior_assistant import key
    return ControlRequest(request_id=key("sdk-state-" + phase, session.assignment.session_id),
        request_digest=digest(value), read_set=tuple(dict.fromkeys(keys)))


def _gate(authority, session, rows):
    """A cancelled turn still saves its gracefully stopped SDK state, under the checks of its cancelled reply.

    Cancellation stops new model calls and tool effects; it must not discard the conversation state the stopped
    SDK already has, or the conversation can never be continued.
    """
    from .behavior_assistant import _Session, key
    from .behavior_workers import JobRecord
    binding = authority._binding(session)
    job = _row(rows, worker_key("job", binding.job_id), JobRecord)
    if not job.cancel_requested:
        authority._gate(session, rows)
        return
    if session.vm_turn_ticket_id is not None:
        from .behavior_vm_session import vm_session_state
        _, context = vm_session_state(authority, session, rows, executing=False)
    else:
        _, context = authority.runtime._state(binding, rows, use="read")
    current = _row(rows, key("session", session.assignment.session_id), _Session)
    if (context != session.execution.owner or job.binding != session.execution
            or job.owner != session.execution.owner
            or job.state not in {"starting", "running", "capacity_uncertain"}
            or current.state not in {"running", "waiting"} or current.execution != session.execution):
        raise Problem(403, "ASSISTANT_EXECUTION_FENCED", "the original cancelled turn execution is required")


def save_state(authority, session, command):
    task = session.assignment.task.instruction
    if (task is None or command.request_id != "sdk-state"
            or command.state.sdk_session_id != session.assignment.sdk_session_id or not command.sdk_stop.graceful
            or command.sdk_stop.release is not None and command.sdk_stop.release != task.worker_profile):
        raise Problem(403, "ASSISTANT_CHECKPOINT_SCOPE", "only the stopped original instruction SDK may checkpoint")
    if command.state.model_provider != task.model_provider:
        raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint provider differs from the original task")
    binding = authority._binding(session)
    # Validate source and state before reserving an intent or issuing any PUT.
    command = SaveSdkState.model_validate_json(command.model_dump_json())
    source, prior_rows = _source(authority, session, command)
    key = checkpoint_key(binding.job_id)
    reads = (*authority._keys(session), key, *prior_rows)
    values = {"job_id": binding.job_id, "sdk_session_id": session.assignment.sdk_session_id,
              "conversation_id": task.conversation_id, "sha256": command.state.sha256,
              "byte_length": len(command.state.encoded()), "sdk_stop": command.sdk_stop.model_dump(mode="json")}
    if source is not None:
        values["source"] = source.model_dump(mode="json")
    request = _request(session, reads, "prepare", values)
    planned = SdkStateUpload(**values, prepare=request)
    fresh = False

    def prepare(rows):
        nonlocal fresh
        _gate(authority, session, rows)
        if any(rows[k] != value for k, value in prior_rows.items()):
            raise Problem(409, "SDK_STATE_SOURCE_UNVERIFIED", "prior published checkpoint changed")
        if rows[key] is not None:
            raise Problem(409, "SDK_STATE_UPLOAD_UNKNOWN", "original SDK state upload already exists")
        fresh = True
        return ControlChanges(writes=(_write(key, planned, "create"),), result={"upload_reserved": True})

    authority.store.transact(request, prepare)
    if not fresh:
        row = authority.store.read_snapshot((key,))[key]
        current = None if row is None else SdkStateUpload.model_validate_json(json.dumps(row.payload["data"]))
        if current is not None and current.prepare == request and current.reference is not None:
            load_state(authority, session, current.reference, job_id=binding.job_id)
            return current.reference
        raise Problem(409, "SDK_STATE_UPLOAD_UNKNOWN", "original upload may have occurred; do not repeat it")
    reference = SdkCheckpointStore(authority.artifacts.store).save(command.state, binding,
        conversation_id=task.conversation_id, source=source, checkpoint_profile=task.checkpoint_profile,
        model_provider=task.model_provider)
    committed = _updated(planned, reference=reference)
    complete = _request(session, reads, "complete", reference.model_dump(mode="json"))

    def finish(rows):
        _gate(authority, session, rows)
        if any(rows[k] != value for k, value in prior_rows.items()):
            raise Problem(409, "SDK_STATE_SOURCE_UNVERIFIED", "prior published checkpoint changed")
        if _row(rows, key, SdkStateUpload) != planned:
            raise Problem(409, "SDK_STATE_UPLOAD_UNKNOWN", "original SDK upload changed")
        return ControlChanges(writes=(_write(key, committed, "replace"),), result={"checkpoint_saved": True})

    authority.store.transact(complete, finish)
    return reference


def published_record(authority, reference, *, job_id):
    """Table-only check that a stored checkpoint was completely published; no Blob bytes are read."""
    key = checkpoint_key(job_id)
    record = _row(authority.store.read_snapshot((key,)), key, SdkStateUpload)
    if (record.reference != reference or record.source != reference.source or record.job_id != job_id
            or record.sha256 != reference.sha256 or record.byte_length != reference.byte_length
            or not record.sdk_stop.graceful):
        raise Problem(409, "SDK_STATE_INCOMPLETE", "SDK checkpoint was not completely published")
    if record.source is not None:
        source = record.source
        if (source.execution.sdk_session_id != reference.sdk_session_id
                or source.execution.conversation_id != reference.conversation_id
                or source.execution.job_id == job_id and source.execution.runtime_pid != record.sdk_stop.pid):
            raise Problem(409, "SDK_STATE_SOURCE_UNVERIFIED", "checkpoint execution binding differs")
    return record


def load_state(authority, session, reference, *, job_id):
    published_record(authority, reference, job_id=job_id)
    return SdkCheckpointStore(authority.artifacts.store).load(reference, authority._binding(session),
        conversation_id=session.assignment.task.instruction.conversation_id,
        sdk_session_id=session.assignment.sdk_session_id, job_id=job_id,
        checkpoint_profile=session.assignment.task.instruction.checkpoint_profile,
        model_provider=session.assignment.task.instruction.model_provider)


def validate_state_history(view, record, binding):
    from types import SimpleNamespace
    from .behavior_runtime_rotation_history import require
    from .behavior_assistant import key, _Session, AssistantAuthority
    from .behavior_runtime import BehaviorRuntime
    from .behavior_work_policy import WorkPolicyControl, WorkRegistration
    rowkey = checkpoint_key(binding.job_id)
    sid = key("session-id", worker_key("start", binding.job_id, "1"))
    session = view.row(key("session", sid), _Session)
    registration = next(view.row(k, WorkRegistration) for k, row in view.inventory.rows.items()
                        if row.payload["data"].get("kind") == "steady_work_registration"
                        and row.payload["data"].get("binding", {}).get("job_id") == binding.job_id)
    control = object.__new__(WorkPolicyControl)
    control.policy = SimpleNamespace(authority_domain=view.config.table.scope.authority_domain)
    control.records = {binding.binding_id: registration}
    control.peer_roots = {}
    if registration.profile.instruction.peer_work is not None:
        from .behavior_agent_message_api import AgentCollaborationRoot
        root_id = registration.profile.instruction.peer_work.root_id
        control.peer_roots[root_id] = view.row(root_id, AgentCollaborationRoot)
    runtime = SimpleNamespace(work_policy=control, teams_metrics=None, bindings={binding.binding_id: binding},
        dep=SimpleNamespace(workers=SimpleNamespace(pool_key=lambda job:worker_key("pool",view.config.workers.workspace_id,job))),
        refresh_bindings=lambda _: None)
    runtime._gate_keys = lambda b: BehaviorRuntime._gate_keys(runtime,b)
    authority = object.__new__(AssistantAuthority)
    authority.runtime = runtime
    prior_keys = ()
    source = record.source
    task = session.assignment.task.instruction
    require(record.reference is None or record.reference.model_provider == task.model_provider,
            "checkpoint provider differs from its original instruction")
    if source is not None:
        from .behavior_instruction import TurnArchive, turn_material
        from .behavior_assistant_wire import SdkArchive
        prior = view.row(task.previous_archive_id, TurnArchive)
        from .behavior_conversation_transfer import EnvironmentTransfer, matches_resume_source, history_ready
        transfer = (None if registration.environment_transfer_id is None else
                    view.row(registration.environment_transfer_id, EnvironmentTransfer, material=True))
        gap_allowed = (transfer is not None and history_ready(view, transfer)
                       and matches_resume_source(transfer, registration, prior))
        require(view.inventory.rows[task.previous_archive_id].payload["material"] == turn_material(prior).model_dump(mode="json")
            and prior.conversation_id == record.conversation_id
            and (prior.turn_number == task.turn_number - 1 or gap_allowed)
            and task.previous_archive_id == key("text-archive", prior.job_id)
            and isinstance(prior.archive, (StoredSdkArchive, SdkArchive))
            and source.previous_job_id == prior.job_id and source.previous_sha256 == prior.archive.sha256
            and source.profile == task.checkpoint_profile == registration.profile.checkpoint_profile
            and source.execution.sdk_session_id == record.sdk_session_id
            and source.execution.conversation_id == record.conversation_id,
            "SDK compaction source escaped its original profile or prior checkpoint")
        prior_keys = (task.previous_archive_id,)
        if isinstance(prior.archive, StoredSdkArchive):
            prior_key = checkpoint_key(prior.job_id)
            previous_upload = view.row(prior_key, SdkStateUpload)
            require(previous_upload.reference == prior.archive, "prior SDK checkpoint was not published")
            prior_keys = (*prior_keys, prior_key)
        if isinstance(prior.archive, StoredSdkArchive) and prior.archive.source is not None:
            require(source.execution == prior.archive.source.execution, "inherited compaction observation changed")
        else:
            require(source.execution.job_id == binding.job_id
                and source.execution.profile_digest == session.assignment.task.profile_digest
                and source.execution.runtime_pid == record.sdk_stop.pid,
                "new compact observation is not bound to this original stopped job")
            from .behavior_assistant import _ModelIntent
            intent_key = key("model-intent", session.assignment.session_id, source.execution.model_request_sha256)
            intent = view.row(intent_key, _ModelIntent)
            require(intent.consumed and intent.request_sha256 == source.execution.model_request_sha256,
                    "compact did not use the original authorized model dispatch")
            prior_keys = (*prior_keys, intent_key)
    expected_reads = tuple(dict.fromkeys((*authority._keys(session), rowkey, *prior_keys)))
    planned = record.model_copy(update={"reference": None})
    values = record.model_dump(mode="json", exclude={"kind", "prepare", "reference"})
    require(record.job_id == binding.job_id and record.prepare.request_digest == digest(values)
        and record.prepare.request_id == key("sdk-state-prepare", key("session-id", worker_key("start", binding.job_id, "1")))
        and record.prepare.read_set == expected_reads, "SDK state upload escaped its original job")
    prepared = view.receipt(record.prepare, (rowkey,), exact={rowkey: view.image_hash(rowkey, planned)},
                 result={"upload_reserved": True})
    if record.reference is not None:
        ref = record.reference
        require(ref.job_id == binding.job_id and ref.conversation_id == record.conversation_id
            and ref.sdk_session_id == record.sdk_session_id and ref.sha256 == record.sha256
            and ref.byte_length == record.byte_length and ref.tenant_id == binding.tenant_id
            and ref.team_id == binding.team_id and ref.agent_oid == binding.agent_oid and ref.source == record.source,
            "SDK state published reference differs")
        complete = ControlRequest(request_id=key("sdk-state-complete", key("session-id", worker_key("start", binding.job_id, "1"))),
            request_digest=digest(ref.model_dump(mode="json")), read_set=record.prepare.read_set)
        committed = view.receipt(complete, (rowkey,), exact={rowkey: view.hash(rowkey)}, result={"checkpoint_saved": True})
        require(committed.guard_revision > prepared.guard_revision,"SDK state publication preceded its upload intent")
