"""Original accepted user instructions behind current conversation read authority."""

import hashlib
import json

from .behavior_delivery import VerifiedWorkResult, _parse as parse_record
from .behavior_instruction import Conversation, TurnArchive, conversation_key, reply_proof, turn_material
from .behavior_runtime import material
from .behavior_store import ControlChanges, _Receipt, _Row, _json as stored_json
from .behavior_user_turn import MAX_USER_TURN_SOURCES, UserTurnRead, UserTurnSource
from .behavior_user_file_prompt import FILE_READ_PROFILES, USER_INPUT_READ_PROFILES
from .behavior_history_prompt import REPLY_READ_PROFILES
from .behavior_workers import JobRecord, worker_key
from .errors import Problem
from .source_materials import digest


def _record(value, model):
    try:
        return parse_record(value, model)
    except Problem as error:
        if error.body["error"]["code"] != "DELIVERY_RECORD_INVALID":
            raise
        raise Problem(502, "USER_TURN_SOURCE_INVALID", "stored user-turn lineage has an unsupported or invalid shape") from None


def source_for(record, archive):
    task = record.profile.instruction
    raw = task.content.encode("utf-8")
    return UserTurnSource(
        schema_version="fa-user-turn-source-v1", conversation_id=task.conversation_id,
        request_id=record.request_id, job_id=record.binding.job_id, turn_number=task.turn_number,
        sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw), submit_sha256=record.submit_sha256,
        registration_sha256=digest(record.model_dump(mode="json")),
        turn_archive_sha256=turn_material(archive).sha256,
    )


def _original_write(store, request_id, originals, *, request_digest=None):
    saved = store._read("behavior:v1:receipt:" + request_id, _Receipt)
    if saved is None:
        raise Problem(409, "USER_TURN_SOURCE_INVALID", "original user source lacks its committed write receipt")
    receipt = saved[0]
    if (receipt.request.request_id != request_id
            or request_digest is not None and receipt.request.request_digest != request_digest
            or any(dict(receipt.writes).get(key) != hashlib.sha256(
                stored_json(_Row.model_validate_json(json.dumps(row.payload)))).hexdigest()
                for key, row in originals.items())):
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "original user source differs from its immutable write receipt")


def _scope(authority, session, principal):
    binding, _, _ = authority.runtime.authorize(session.binding_id, principal, "read")
    profile = authority.runtime.profiles[session.binding_id]
    task = session.assignment.task.instruction
    if (task is None or task.prompt_profile not in USER_INPUT_READ_PROFILES or profile.prompt_profile != task.prompt_profile
            or session.assignment.task.profile_digest != digest(profile.model_dump(mode="json"))
            or session.state != "running"):
        raise Problem(403, "USER_TURN_READ_DISABLED", "the original running server profile must enable user-original reads")
    authority._gate(session, authority.store.read_snapshot(authority._keys(session)))
    control = authority.runtime.work_policy
    if control is None:
        raise Problem(403, "USER_TURN_READ_DISABLED", "only registered original instruction turns are readable")
    ckey = conversation_key(task.conversation_id)
    row = authority.store.read_snapshot((ckey,))[ckey]
    if row is None:
        raise Problem(404, "USER_TURN_NOT_AVAILABLE", "original conversation is unavailable")
    conversation = _record(row.payload["data"], Conversation)
    if ((conversation.human_oid, conversation.team_id, conversation.agent_oid)
            != (binding.human_oid, binding.team_id, binding.agent_oid)
            or conversation.sdk_session_id != session.assignment.sdk_session_id
            or conversation.active_job_id != binding.job_id or conversation.reset_request_id is not None
            or len(conversation.jobs) != task.turn_number or conversation.jobs[-1] != binding.job_id):
        raise Problem(403, "USER_TURN_SCOPE_DENIED", "only this original active conversation may read its past user turns")
    return control, binding, task, conversation, {ckey: row}


def _read(authority, session, principal, turn_number):
    from .behavior_assistant import _Session, key
    from .behavior_assistant_wire import SdkArchive, StoredSdkArchive
    from .behavior_assistant_checkpoint import unpack
    from .behavior_sdk_checkpoint import SdkStateUpload, checkpoint_key
    from .behavior_skill_prompt import assemble_prompt
    from .behavior_work_policy import (
        WorkRegistration, registered_instruction, registration_request_digest, work_policy_key,
    )

    control, current_binding, current_task, conversation, snapshot = _scope(authority, session, principal)
    if not 1 <= turn_number < current_task.turn_number:
        raise Problem(403, "USER_TURN_SCOPE_DENIED", "only a completed past turn number in this conversation may be read")
    job_id = conversation.jobs[turn_number - 1]
    candidates = [record for record in control.records.values() if record.binding.job_id == job_id]
    if len(candidates) != 1:
        raise Problem(404, "USER_TURN_NOT_AVAILABLE", "the original registered user turn is unavailable")
    registered = candidates[0]
    if registered.agent_origin is not None:
        raise Problem(404, "USER_TURN_NOT_AVAILABLE", "an Agent message is not an original human instruction")
    binding = registered.binding
    authority.runtime.authorize(binding.binding_id, principal, "read")
    task = registered.profile.instruction
    if (task is None or task.conversation_id != current_task.conversation_id or task.turn_number != turn_number
            or registered.principal_oid != current_binding.human_oid
            or (binding.tenant_id, binding.team_id, binding.human_oid, binding.agent_oid)
            != (current_binding.tenant_id, current_binding.team_id, current_binding.human_oid, current_binding.agent_oid)):
        raise Problem(403, "USER_TURN_SCOPE_DENIED", "the original user turn belongs to another conversation or identity")
    rkey = work_policy_key("registration", registered.admission_id)
    jkey, akey, result_key = worker_key("job", job_id), key("text-archive", job_id), key("result", binding.work.work_id)
    rows = authority.store.read_snapshot(tuple(dict.fromkeys((
        *authority.runtime._gate_keys(binding), rkey, jkey, akey, result_key,
    ))))
    if any(rows[name] is None for name in (rkey, jkey, akey, result_key)):
        raise Problem(404, "USER_TURN_NOT_AVAILABLE", "a saved and forwarded original SDK turn is required")
    record = _record(rows[rkey].payload["data"], WorkRegistration)
    archive = _record(rows[akey].payload["data"], TurnArchive)
    job = _record(rows[jkey].payload["data"], JobRecord)
    result = _record(rows[result_key].payload["data"], VerifiedWorkResult)
    if (job.state not in {"succeeded", "failed", "cancelled"} or not job.result_forwarded
            or job.binding is None or not isinstance(archive.archive, (SdkArchive, StoredSdkArchive))):
        raise Problem(403, "USER_TURN_NOT_AVAILABLE", "unfinished or unavailable history is not an original-user source")
    sid = key("session-id", job.binding.start_request_id)
    skey = key("session", sid)
    state = authority.store.read_snapshot((skey,))[skey]
    if state is None:
        raise Problem(409, "USER_TURN_SOURCE_INVALID", "original saved SDK session is missing")
    original = _record(state.payload["data"], _Session)
    submitted = registered_instruction(record)
    if (record != registered or rows[rkey].payload["material"] != material(record.model_dump(mode="json")).model_dump(mode="json")
            or record.submit_sha256 != hashlib.sha256(submitted.model_dump_json().encode("utf-8")).hexdigest()
            or job.spec.input != original.assignment.task.model_dump_json().encode("utf-8")
            or original.assignment.task.instruction != task
            or original.assignment.task.profile_digest != digest(record.profile.model_dump(mode="json"))
            or original.execution != job.binding or original.state != "ready_to_finalize"
            or original.assignment.sdk_session_id != conversation.sdk_session_id
            or archive.conversation_id != conversation.conversation_id or archive.job_id != job_id
            or archive.turn_number != turn_number or archive.archive.sdk_session_id != conversation.sdk_session_id
            or archive.prompt_assembly != assemble_prompt(task.prompt_profile).record
            or archive.context_read != original.context_read or archive.skill_catalog != original.skill_catalog
            or archive.user_turn_sources != original.user_turn_sources
            or archive.work_memory_source != original.work_memory_source
            or rows[akey].payload["material"] != turn_material(archive).model_dump(mode="json")
            or rows[result_key].payload["material"] != material(result.model_dump(mode="json")).model_dump(mode="json")
            or result.state != job.state or result.work_id != binding.work.work_id
            or result.definition_digest != digest(binding.work.model_dump(mode="json"))
            or result.proof != reply_proof(conversation.sdk_session_id, archive.archive, archive.sdk_stop,
                                          "cancelled" if result.state == "cancelled" else archive.failure)):
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "saved user instruction or its original turn lineage differs")
    _original_write(authority.store, rkey, {rkey: rows[rkey]},
        request_digest=registration_request_digest(record))
    _original_write(authority.store, key("text-reply", sid),
                    {akey: rows[akey], result_key: rows[result_key], skey: state})
    if isinstance(archive.archive, SdkArchive):
        unpack(archive.archive)
    else:
        ckey = checkpoint_key(job_id)
        uploaded = authority.store.read_snapshot((ckey,))[ckey]
        if uploaded is None:
            raise Problem(409, "USER_TURN_SOURCE_INVALID", "original checkpoint publication is missing")
        checkpoint = _record(uploaded.payload["data"], SdkStateUpload)
        archive.archive.require_profile(current_task.checkpoint_profile, model_provider=current_task.model_provider)
        if (checkpoint.reference != archive.archive or checkpoint.job_id != job_id
                or checkpoint.sdk_stop != archive.sdk_stop or checkpoint.conversation_id != conversation.conversation_id):
            raise Problem(409, "USER_TURN_SOURCE_CHANGED", "original checkpoint publication differs")
        snapshot[ckey] = uploaded
    snapshot.update(rows)
    snapshot[skey] = state
    if record.routine is not None:
        raise Problem(404, "USER_TURN_NOT_AVAILABLE", "a routine occurrence is not an original human instruction")
    try:
        response = UserTurnRead(schema_version="fa-user-turn-read-v1", source=source_for(record, archive), text=task.content)
    except ValueError:
        raise Problem(413, "USER_TURN_SOURCE_LIMIT", "exact original text or complete response exceeds its supported bound") from None
    return response, snapshot


def load_original(authority, session, principal, turn_number):
    return _consistent_read(authority, lambda: _read(authority, session, principal, turn_number))


def _consistent_read(authority, read):
    try:
        return authority.store.consistent_read(read)
    except Problem as error:
        if error.body["error"]["code"] != "BEHAVIOR_READ_SCOPE_LIMIT":
            raise
        raise Problem(413, "USER_TURN_SOURCE_LIMIT", "complete original-source and receipt checks exceed their read bound") from None


def _track_original(authority, session, command, principal, result, snapshot):
    from .behavior_assistant import _Session, _updated, _write, key
    references = {source.turn_number: source for source in session.user_turn_sources}
    if result.source.turn_number in references and references[result.source.turn_number] != result.source:
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "previously read user-original provenance changed")
    references[result.source.turn_number] = result.source
    if len(references) > MAX_USER_TURN_SOURCES:
        raise Problem(413, "USER_TURN_SOURCE_LIMIT", "retained user-original source checks are at their bound")
    candidate = _updated(session, user_turn_sources=tuple(references[number] for number in sorted(references)))
    snapshot.update(authority.model_sources(candidate, principal))
    if result.source in session.user_turn_sources:
        again, current_snapshot = load_original(authority, session, principal, command.turn_number)
        if again != result:
            raise Problem(409, "USER_TURN_SOURCE_CHANGED", "original user text changed during its read-only lookup")
        return result, current_snapshot
    if len(set((*authority._keys(session), *snapshot))) > 98:
        raise Problem(413, "USER_TURN_SOURCE_LIMIT", "original source checks exceed the supported atomic read set")
    sid = session.assignment.session_id

    def decide(rows):
        authority._current_sources(rows, snapshot)
        current = _record(rows[key("session", sid)].payload["data"], _Session)
        if current.user_turn_sources != session.user_turn_sources:
            raise Problem(409, "USER_TURN_SOURCE_CHANGED", "active user-source checks changed during this read")
        next_value = _updated(current, user_turn_sources=candidate.user_turn_sources)
        return ControlChanges(writes=(_write(key("session", sid), next_value, "replace"),), result={"state": "accepted"})

    authority._command(session, "user-turn-" + str(command.turn_number), command, decide, extra=tuple(snapshot))
    again, current_snapshot = load_original(authority, session, principal, command.turn_number)
    if again != result:
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "the original read changed before returning its exact text")
    return result, current_snapshot


def read_user_turn(authority, session, command, principal):
    result, snapshot = load_original(authority, session, principal, command.turn_number)
    return _track_original(authority, session, command, principal, result, snapshot)[0]


def _file_in_original(authority, source, snapshot, name):
    from .behavior_text_files import UserFileRead, file_reference
    from .behavior_work_policy import WorkRegistration, work_policy_key
    policy = authority.runtime.work_policy.policy
    admission = work_policy_key("admission", policy.authority_domain, policy.template.human_oid, source.request_id)
    row = snapshot[work_policy_key("registration", admission)]
    record = _record(row.payload["data"], WorkRegistration)
    if record.binding.job_id != source.job_id or digest(record.model_dump(mode="json")) != source.registration_sha256:
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "original file input does not match its registered user source")
    for file in record.profile.instruction.files:
        if file.name == name:
            return UserFileRead(turn_source=source, file=file_reference(file), text=file.text)
    raise Problem(404, "USER_FILE_NOT_FOUND", "the original completed turn contains no file with this name")


def read_user_file(authority, session, command, principal):
    task = session.assignment.task.instruction
    profile = authority.runtime.profiles[session.binding_id]
    if (task is None or task.prompt_profile not in FILE_READ_PROFILES or profile.prompt_profile != task.prompt_profile):
        raise Problem(403, "USER_FILE_READ_DISABLED", "the original server profile must explicitly enable historical file reads")
    original, snapshot = load_original(authority, session, principal, command.turn_number)
    result = _file_in_original(authority, original.source, snapshot, command.name)
    current, current_snapshot = _track_original(authority, session, command, principal, original, snapshot)
    again = _file_in_original(authority, current.source, current_snapshot, command.name)
    if again != result:
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "the original file changed before its data was returned")
    return result


def _reply_in_original(authority, session, source, snapshot):
    from .behavior_assistant import key
    from .behavior_assistant_reply import AssistantReply, AssistantReplyRead, reply_turn
    from .behavior_instruction import SavedReply, reply_reference, unpack_reply
    from .behavior_work_policy import WorkRegistration, work_policy_key
    policy = authority.runtime.work_policy.policy
    admission = work_policy_key("admission", policy.authority_domain, policy.template.human_oid, source.request_id)
    record = _record(snapshot[work_policy_key("registration", admission)].payload["data"], WorkRegistration)
    if record.binding.job_id != source.job_id or digest(record.model_dump(mode="json")) != source.registration_sha256:
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "the saved reply does not match its registered original turn")
    job = _record(snapshot[worker_key("job", source.job_id)].payload["data"], JobRecord)
    result = _record(snapshot[key("result", record.binding.work.work_id)].payload["data"], VerifiedWorkResult)
    reply_key = key("reply", source.job_id)
    row = snapshot.get(reply_key)
    if row is None:
        raise Problem(404, "ASSISTANT_REPLY_NOT_AVAILABLE", "the original completed turn saved no assistant reply")
    saved = _record(row.payload["data"], SavedReply)
    try:
        reply = unpack_reply(saved)
    except Problem:
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "the saved assistant reply bytes differ from their record") from None
    if (saved.job_id != source.job_id or result.content != reply_reference(saved)
            or reply.conversation_id != source.conversation_id or reply.turn_number != source.turn_number
            or reply.sdk_session_id != session.assignment.sdk_session_id):
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "the saved assistant reply belongs to another original turn")
    _original_write(authority.store, key("text-reply", key("session-id", job.binding.start_request_id)), {reply_key: row})
    raw = reply.text.encode("utf-8")
    try:
        return AssistantReplyRead(turn=reply_turn(source), reply=AssistantReply(
            outcome=result.state, saved_sha256=saved.sha256,
            sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw), text=reply.text))
    except ValueError:
        raise Problem(413, "USER_TURN_SOURCE_LIMIT", "the exact saved reply exceeds its supported read bound") from None


def load_reply(authority, session, principal, turn_number):
    from .behavior_assistant import key

    def read():
        original, snapshot = _read(authority, session, principal, turn_number)
        reply_key = key("reply", original.source.job_id)
        row = authority.store.read_snapshot((reply_key,))[reply_key]
        if row is not None:
            snapshot[reply_key] = row
        return original, snapshot, _reply_in_original(authority, session, original.source, snapshot)
    return _consistent_read(authority, read)


def read_assistant_reply(authority, session, command, principal):
    task = session.assignment.task.instruction
    profile = authority.runtime.profiles[session.binding_id]
    if (task is None or task.prompt_profile not in REPLY_READ_PROFILES
            or profile.prompt_profile != task.prompt_profile):
        raise Problem(403, "ASSISTANT_REPLY_READ_DISABLED", "the original server profile must enable past-reply reads")
    original, snapshot, result = load_reply(authority, session, principal, command.turn_number)
    _track_original(authority, session, command, principal, original, snapshot)
    if load_reply(authority, session, principal, command.turn_number)[2] != result:
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "the saved reply changed before its data was returned")
    return result


def current_sources(authority, session, principal):
    def read():
        _scope(authority, session, principal)
        snapshot = {}
        for expected in session.user_turn_sources:
            current, rows = _read(authority, session, principal, expected.turn_number)
            if current.source != expected:
                raise Problem(403, "USER_TURN_SOURCE_CHANGED", "historical user text is no longer its original authorized source")
            snapshot.update(rows)
        if len(set((*authority._keys(session), *snapshot))) > 96:
            raise Problem(413, "USER_TURN_SOURCE_LIMIT", "original sources exceed the bounded model-effect read set")
        return snapshot
    return _consistent_read(authority, read)
