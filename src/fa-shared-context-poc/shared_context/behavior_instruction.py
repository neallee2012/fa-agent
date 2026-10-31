"""Work indexes and original SDK archives; the SDK remains the conversation authority."""
from typing import Annotated, Literal
from datetime import datetime
import hashlib
import zlib

from pydantic import Field, field_validator, model_validator

from . import behavior_assistant_wire as aw
from .behavior_delivery import _Model, _parse, _material, _update, _utc, VerifiedWorkResult
from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_workers import JobRecord, worker_key
from .errors import Problem
from .models import Identifier
from .source_materials import digest
from .behavior_skill_prompt import SKILL_PROFILES, AssemblyRecord, assemble_prompt
from .behavior_capability_prompt import CapabilityAssemblyRecord, capability_record
from .m3_skills import ApprovedSkillCatalog
from .behavior_user_turn import UserTurnSources, ordered_user_sources
from .behavior_work_memory import WorkMemorySource
from .behavior_skill_copy import SopCopyOutcome, SopCopyReceipt
from .behavior_skill_draft import SkillDraftOutcome, SkillDraftReceipt, skill_draft_enabled
from .behavior_agent_worker import AgentVmBinding


def conversation_key(conversation_id):
    return control_key("assistant-conversation", conversation_id)


def conversation_id(policy, command):
    return command.conversation_id or control_key(
        "assistant-conversation-id", *policy.scope_ids, policy.template.human_oid, command.request_id)


def sdk_session_id(identifier):
    return control_key("assistant-sdk-conversation", identifier)


def hosted_session_id(identifier):
    return "fa-chat-" + control_key("assistant-hosted-conversation", identifier)[:48]


class Conversation(_Model):
    kind: Literal["assistant_sdk_conversation"] = "assistant_sdk_conversation"
    conversation_id: Identifier
    human_oid: Identifier
    team_id: Identifier
    agent_oid: Identifier
    sdk_session_id: Identifier
    jobs: tuple[Identifier, ...] = ()
    active_job_id: Identifier | None = None
    archive_id: Identifier | None = None
    hosted_session_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    hosted_agent_version: str | None = Field(default=None, exclude_if=lambda value: value is None)
    reset_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    archived_at: datetime | None = Field(default=None, exclude_if=lambda value: value is None)
    agent_vm: AgentVmBinding | None = Field(default=None, exclude_if=lambda value: value is None)

    _time = field_validator("archived_at")(lambda value: None if value is None else _utc(value))

    @model_validator(mode="after")
    def archive_pair(self):
        if (self.reset_request_id is None) != (self.archived_at is None):
            raise ValueError("an archived conversation requires its exact reset request and time")
        if self.agent_vm is not None and (
                self.agent_vm.team_id != self.team_id or self.agent_vm.hosted_session_id != self.hosted_session_id
                or self.agent_vm.hosted_agent_version != self.hosted_agent_version):
            raise ValueError("a conversation must retain its originally captured Agent VM")
        return self


class TurnArchive(_Model):
    kind: Literal["assistant_sdk_turn_archive"] = "assistant_sdk_turn_archive"
    conversation_id: Identifier
    job_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    archive: aw.SdkArchive | aw.ArchiveUnavailable | aw.StoredSdkArchive
    sdk_stop: aw.AssistantSdkStop
    context_read: bool = False
    failure: aw.InstructionFailure | None = None
    progress: aw.PublicProgress | None = Field(default=None,exclude_if=lambda v:v is None)
    prompt_assembly: AssemblyRecord | None = Field(default=None,exclude_if=lambda v:v is None)
    capability_assembly: CapabilityAssemblyRecord | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_catalog: ApprovedSkillCatalog | None = Field(default=None, exclude_if=lambda value: value is None)
    user_turn_sources: UserTurnSources = Field(default=(), exclude_if=lambda value: not value)
    work_memory_source: WorkMemorySource | None = Field(default=None, exclude_if=lambda value: value is None)
    sop_copy_receipt: SopCopyReceipt | None = Field(default=None, exclude_if=lambda value: value is None)
    sop_copy_outcome: SopCopyOutcome | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_draft_receipt: SkillDraftReceipt | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_draft_outcome: SkillDraftOutcome | None = Field(default=None, exclude_if=lambda value: value is None)

    _user_sources = field_validator("user_turn_sources")(ordered_user_sources)


def turn_material(value):
    metadata=value.model_dump(mode="json")
    metadata["archive"].pop("data",None)
    return _material(metadata)


class SavedReply(_Model):
    kind:Literal["assistant_text_reply"]="assistant_text_reply"
    job_id:Identifier
    data:Annotated[bytes,Field(max_length=32768)]
    sha256:Annotated[str,Field(pattern=r"^[a-f0-9]{64}$")]
    byte_length:Annotated[int,Field(ge=1,le=4*1024*1024)]


def unpack_reply(value):
    decoder=zlib.decompressobj()
    raw=decoder.decompress(value.data,4*1024*1024+1)
    if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or len(raw)!=value.byte_length
            or hashlib.sha256(raw).hexdigest()!=value.sha256):
        raise Problem(409,"INSTRUCTION_REPLY_INVALID","complete saved response bytes differ")
    return aw.TextReply.model_validate_json(raw)


def reply_reference(value):
    from .behavior_assistant import key
    return _material({"schema_version":"fa-assistant-text-reference-v1","reply_id":key("reply",value.job_id),
                      "sha256":value.sha256,"byte_length":value.byte_length}).data


def failure_notice(state, failure=None):
    notice = {"schema_version": "fa-assistant-notice-v1", "state": state, "sdk_reply_available": False}
    if failure == "model_call_limit_reached":
        notice["code"] = "MODEL_CALL_LIMIT_REACHED"
    return _material(notice).data


def instruction_state(control, command, *, registry_rows=None):
    cid = conversation_id(control.policy, command)
    key = conversation_key(cid)
    row = control.store.read_snapshot((key,))[key]
    b = control.policy.template
    current = (Conversation(conversation_id=cid, human_oid=b.human_oid, team_id=b.team_id,
                            agent_oid=b.agent_oid, sdk_session_id=sdk_session_id(cid),
                            hosted_session_id=hosted_session_id(cid)) if row is None
               else _parse(row.payload["data"], Conversation))
    if ((command.conversation_id is not None and row is None)
            or (current.human_oid, current.team_id, current.agent_oid) != (b.human_oid, b.team_id, b.agent_oid)):
        raise Problem(403, "CONVERSATION_SCOPE_DENIED", "conversation must belong to this original human and agent")
    if current.reset_request_id is not None:
        raise Problem(409, "CONVERSATION_ARCHIVED", "this conversation was explicitly reset; start a new session")
    if current.hosted_session_id is None:
        raise Problem(409, "CONVERSATION_SESSION_RESET_REQUIRED", "legacy per-work sessions require an explicit reset or new session")
    if current.jobs:
        from .behavior_foundry import session_version_failure
        from .behavior_workers import JobRecord, worker_key
        previous = control._job_record(current.jobs[-1])
        if previous is None:
            control.refresh()
            previous = control._job_record(current.jobs[-1])
        if previous is None or not control.listed(previous.binding.binding_id, registry_rows):
            raise Problem(403, "BEHAVIOR_BINDING_DENIED", "the prior turn is not in the current admission registry")
        job_key = worker_key("job", current.jobs[-1])
        job_row = control.store.read_snapshot((job_key,))[job_key]
        job = None if job_row is None else _parse(job_row.payload["data"], JobRecord)
        failure = session_version_failure(control.store, job)
        from .behavior_conversation_transfer import transferred_failure
        if failure is not None and not transferred_failure(control, current, job):
            raise Problem(409, failure, "the original session changed version; explicitly start a new conversation")
        from .behavior_foundry import lost_session_failure
        if failure is None and lost_session_failure(control.store, job) is not None:
            raise Problem(409, "ASSISTANT_SESSION_LOST",
                          "the original Hosted environment stopped during the last turn; its outcome is unknown "
                          "and it is not resent; explicitly reset or start a new conversation")
    if current.active_job_id is not None:
        raise Problem(409, "CONVERSATION_TURN_UNRESOLVED", "the original conversation turn is still pending or unknown")
    if current.jobs:
        prior = control.runtime.dep.workers.inspect(current.jobs[-1])
        if prior.state in {"succeeded", "failed", "cancelled"} and prior.result_forwarded and current.archive_id is None:
            from .behavior_assistant import key as assistant_key
            nkey = assistant_key("no-sdk-turn", current.jobs[-1])
            if control.store.read_snapshot((nkey,))[nkey] is not None:
                raise Problem(409, "SDK_NOT_STARTED_RESET_REQUIRED",
                              "original task failed before SDK entry; explicitly reset without discarding its history")
        if prior.state not in {"succeeded","failed","cancelled"} or not prior.result_forwarded or current.archive_id is None:
            raise Problem(409, "CONVERSATION_TURN_UNRESOLVED", "previous SDK turn must be saved and execution complete")
        saved=control.store.read_snapshot((current.archive_id,))[current.archive_id]
        archive=None if saved is None else _parse(saved.payload["data"],TurnArchive)
        if archive is None or not isinstance(archive.archive,(aw.SdkArchive,aw.StoredSdkArchive)):
            raise Problem(409,"CONVERSATION_CONTEXT_MISSING","previous reply is saved but its SDK context is unavailable")
        if archive.sop_copy_outcome is not None and archive.sop_copy_outcome.unresolved_operations:
            raise Problem(409, "SOP_COPY_OUTCOME_UNKNOWN", "the original copy remains unresolved; a new turn is not a retry")
        if archive.skill_draft_outcome is not None and archive.skill_draft_outcome.unresolved_operations:
            raise Problem(409, "SKILL_DRAFT_OUTCOME_UNKNOWN", "the original skill draft remains unresolved; a new turn is not a retry")
        if isinstance(archive.archive, aw.StoredSdkArchive):
            from .behavior_profile_selection import choice_for_conversation, profile_from_template
            choice = choice_for_conversation(control, current)
            consumer = control.policy.profile if choice is None else profile_from_template(control.policy, choice.command.template)
            archive.archive.require_profile(consumer.checkpoint_profile, model_provider=consumer.model_provider)
    return current, row


def registration_write(conversation, job_id):
    next_value = conversation.model_copy(update={"jobs": (*conversation.jobs, job_id), "active_job_id": job_id})
    return ControlWrite(row_id=conversation_key(conversation.conversation_id),
        mode="replace" if conversation.jobs else "create", data=next_value.model_dump(mode="json"))


def reset_request(control, conversation, request_id):
    last=conversation.jobs[-1] if conversation.jobs else None
    return ControlRequest(request_id=control_key("conversation-reset",conversation.conversation_id,request_id),
        request_digest=digest([conversation.conversation_id,request_id,conversation.human_oid]),
        read_set=(control.policy_key,control.revocation_key,conversation_key(conversation.conversation_id),
                  *((worker_key("job",last),) if last is not None else ())))


def reset_result(conversation):
    return {"conversation_id":conversation.conversation_id,"archived":True,
        "reset_request_id":conversation.reset_request_id,"hosted_session_id":conversation.hosted_session_id,
        "job_id":conversation.jobs[-1] if conversation.jobs else None}


def archive_conversation(control,principal,identifier,command):
    control._human(principal,"submit")
    control.refresh()
    key=conversation_key(identifier)
    row=control.store.read_snapshot((key,))[key]
    if row is None:raise Problem(404,"CONVERSATION_NOT_FOUND","original conversation is unavailable")
    original=_parse(row.payload["data"],Conversation)
    if (original.human_oid,original.team_id,original.agent_oid)!=(principal.oid,principal.team,control.policy.template.agent_oid):
        raise Problem(403,"CONVERSATION_SCOPE_DENIED","only this conversation's original human may reset it")
    request=reset_request(control,original,command.request_id)
    def decide(rows):
        control.installed(rows)
        current=_parse(rows[key].payload["data"],Conversation)
        if current.reset_request_id is not None:
            raise Problem(409,"CONVERSATION_ARCHIVED","this conversation already has an original reset")
        if current!=original or current.active_job_id not in {None,current.jobs[-1] if current.jobs else None}:
            raise Problem(409,"CONVERSATION_TURN_UNRESOLVED","finish or reconcile the original work before reset")
        if current.jobs:
            job=_parse(rows[worker_key("job",current.jobs[-1])].payload["data"],JobRecord)
            if job.state not in {"succeeded","failed","cancelled"} or not job.result_forwarded:
                raise Problem(409,"CONVERSATION_TURN_UNRESOLVED","reset does not discard running or unknown work")
        archived=_update(current,active_job_id=None,reset_request_id=command.request_id,archived_at=control.runtime._now())
        return ControlChanges(writes=(ControlWrite(row_id=key,mode="replace",data=archived.model_dump(mode="json")),),
                              result=reset_result(archived))
    return control.store.transact(request,decide)


def save_reply(authority, session, command):
    from .behavior_assistant import _Session, _write, key, _ledger_conflicts
    from .behavior_assistant_checkpoint import unpack
    task = session.assignment.task.instruction
    if task is None or command.request_id != "save-text-reply":
        raise Problem(403, "INSTRUCTION_RESULT_DENIED", "original free instruction reply required")
    if isinstance(command.archive,aw.SdkArchive):
        if task.model_provider is not None:
            raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "Responses requires its provider-bound checkpoint codec")
        unpack(command.archive)
    if isinstance(command.archive,aw.StoredSdkArchive):
        from .behavior_sdk_checkpoint import load_state
        load_state(authority, session, command.archive, job_id=session.execution.job_id)
    reply, archive = command.reply, command.archive
    control = authority.runtime.work_policy
    registration = None if control is None else control.records.get(session.binding_id)
    agent_turn = task.peer_work is not None or registration is not None and registration.agent_origin is not None
    if (reply is None and command.failure is None
            or command.failure == "model_call_limit_reached" and (reply is not None or not agent_turn)
            or reply is not None and (reply.conversation_id != task.conversation_id or reply.turn_number != task.turn_number
                                      or reply.sdk_session_id != session.assignment.sdk_session_id)
            or isinstance(archive,(aw.SdkArchive,aw.StoredSdkArchive)) and archive.sdk_session_id != session.assignment.sdk_session_id
            or not command.sdk_stop.graceful
            or command.sdk_stop.release is not None and command.sdk_stop.release != task.worker_profile):
        raise Problem(403, "INSTRUCTION_RESULT_DENIED", "reply/archive must bind the original stopped SDK session")
    runtime, store = authority.runtime, authority.store
    binding = authority._binding(session)
    sid = session.assignment.session_id
    ckey, akey, rkey = conversation_key(task.conversation_id), key("text-archive", binding.job_id), key("result", binding.work.work_id)
    reply_key=key("reply",binding.job_id)
    operation_key=control_key("operation",binding.operation_id)
    from .behavior_instruction_progress import progress_record,ProgressRecord
    final_progress=None if command.progress is None else progress_record(session,command.progress)
    pkey=key("progress",sid)
    from .behavior_sdk_checkpoint import checkpoint_key,SdkStateUpload
    checkpoint_reads=() if not isinstance(archive,aw.StoredSdkArchive) else (checkpoint_key(binding.job_id),)
    draft_reads = () if task.sop_copy is None and not skill_draft_enabled(task.prompt_profile, task.capabilities) else (
        key("artifact", binding.work.work_id),)
    reads = tuple(dict.fromkeys((*authority._keys(session), ckey, akey, rkey,reply_key,operation_key,*checkpoint_reads,
                                *draft_reads,*((pkey,) if final_progress is not None else ()))))
    request = ControlRequest(request_id=key("text-reply", sid), request_digest=digest(command.model_dump(mode="json")), read_set=reads)
    packed=None
    if reply is not None:
        raw=reply.model_dump_json().encode("utf-8")
        compressed=zlib.compress(raw)
        if len(raw)>4*1024*1024 or len(compressed)>32768:
            raise Problem(413,"INSTRUCTION_REPLY_STORAGE_LIMIT","complete reply exceeds the protected Table envelope; nothing is truncated")
        packed=SavedReply(job_id=binding.job_id,data=compressed,sha256=hashlib.sha256(raw).hexdigest(),byte_length=len(raw))
    def decide(rows):
        job = _parse(rows[worker_key("job", binding.job_id)].payload["data"], JobRecord)
        def read_state():
            if session.vm_turn_ticket_id is not None:
                from .behavior_vm_session import vm_session_state
                return vm_session_state(authority,session,rows,executing=False)
            return runtime._state(binding,rows,use="read")
        if task.sop_copy is not None:
            read_state()
        if command.failure is None and not job.cancel_requested:authority._gate(session,rows)
        else:read_state()
        current = _parse(rows[key("session", sid)].payload["data"], _Session)
        conversation = _parse(rows[ckey].payload["data"], Conversation)
        if (job.binding != session.execution or job.owner != session.execution.owner
                or command.failure=="cancelled" and not job.cancel_requested
                or job.state not in {"starting", "running", "capacity_uncertain"}
                or current.state not in {"running","waiting"} or current.execution != session.execution
                or conversation.active_job_id != binding.job_id or conversation.jobs[-1] != binding.job_id
                or conversation.sdk_session_id != session.assignment.sdk_session_id or len(conversation.jobs) != task.turn_number):
            raise Problem(409, "INSTRUCTION_RESULT_FENCED", "original turn execution or conversation changed")
        failure="cancelled" if job.cancel_requested else command.failure
        if checkpoint_reads and _parse(rows[checkpoint_reads[0]].payload["data"],SdkStateUpload).reference!=archive:
            raise Problem(409,"SDK_STATE_INCOMPLETE","saved SDK state reference changed before reply commit")
        progress_writes=()
        if final_progress is not None:
            previous=rows[pkey]
            if previous is not None:
                old=_parse(previous.payload["data"],ProgressRecord)
                if old.progress.events[-1].seq>final_progress.progress.events[-1].seq:
                    raise Problem(409,"INSTRUCTION_PROGRESS_INVALID","final progress cannot replace newer events")
            progress_writes=(_write(pkey,final_progress,"create" if previous is None else "replace"),)
        copy_receipt, copy_outcome = None, None
        if task.sop_copy is not None:
            from .behavior_skill_copy_source import saved_copy
            copy_receipt, copy_outcome = saved_copy(session, rows, allow_unresolved=failure is not None)
        skill_draft_receipt, skill_draft_outcome = None, None
        if skill_draft_enabled(task.prompt_profile, task.capabilities):
            from .behavior_skill_draft_source import saved_draft
            skill_draft_receipt, skill_draft_outcome = saved_draft(session, rows, allow_unresolved=failure is not None)
        saved = TurnArchive(conversation_id=task.conversation_id, job_id=binding.job_id,
                            turn_number=task.turn_number, archive=archive, sdk_stop=command.sdk_stop,
                            context_read=current.context_read,failure=command.failure,progress=command.progress,
                            prompt_assembly=assemble_prompt(task.prompt_profile).record,
                            capability_assembly=capability_record(task.prompt_profile, task.capabilities),
                            skill_catalog=current.skill_catalog, user_turn_sources=current.user_turn_sources,
                            work_memory_source=current.work_memory_source, sop_copy_receipt=copy_receipt,
                            sop_copy_outcome=copy_outcome, skill_draft_receipt=skill_draft_receipt,
                            skill_draft_outcome=skill_draft_outcome)
        known,unresolved=runtime.dep.workers._operation_effects(rows,(binding.operation_id,),session.execution.owner)
        if copy_outcome is not None:
            known = (*known, *copy_outcome.known_effects)
            unresolved = (*unresolved, *copy_outcome.unresolved_operations)
        if skill_draft_outcome is not None:
            known = (*known, *skill_draft_outcome.known_effects)
            unresolved = (*unresolved, *skill_draft_outcome.unresolved_operations)
        state="cancelled" if failure=="cancelled" else "failed" if failure or unresolved else "succeeded"
        content=reply_reference(packed) if packed is not None else failure_notice(state, failure)
        result=VerifiedWorkResult(evidence_id=rkey,verifier_version=runtime.dep.delivery.contract.verifier_version,
            work_id=binding.work.work_id,definition_digest=digest(binding.work.model_dump(mode="json")),
            state=state,passed_checks=aw.TEXT_CHECKS if state=="succeeded" else (),
            content=content,known_effects=known,unresolved_operations=unresolved,
            proof=reply_proof(session.assignment.sdk_session_id,archive,command.sdk_stop,failure))
        return ControlChanges(writes=(
            _write(rkey, result, "create", _material(result.model_dump(mode="json"))),
            *(() if packed is None else (_write(reply_key,packed,"create"),)),
            _write(akey, saved, "create", turn_material(saved)),
            _write(key("session", sid), _update(current, state="ready_to_finalize"), "replace"),
            _write(ckey, _update(conversation, active_job_id=None, archive_id=akey), "replace"),
            *progress_writes,
        ), result={"state": "ready_to_finalize", "result_digest": digest(result.model_dump(mode="json")),"work_outcome":state})
    record = (_ledger_conflicts(store,request,decide) if runtime.mode=="native"
              else store.transact(request,decide))
    return aw.ToolReceipt(**record.payload["result"])


def reply_proof(sdk_session_id,archive,stop,failure=None):
    return _material({"sdk_session_id":sdk_session_id,"failure":failure,
        "archive_sha256":archive.sha256 if isinstance(archive,(aw.SdkArchive,aw.StoredSdkArchive)) else None,
        "archive_error":archive.error if isinstance(archive,aw.ArchiveUnavailable) else None,
        "sdk_stop":stop.model_dump(mode="json")}).data


def previous_turn(authority, session):
    from .behavior_assistant import key
    task = session.assignment.task.instruction
    if task is None or task.previous_archive_id is None:
        raise Problem(409, "CONVERSATION_CONTEXT_MISSING", "original prior SDK archive is required")
    row = authority.store.read_snapshot((task.previous_archive_id,))[task.previous_archive_id]
    if row is None:
        raise Problem(409, "CONVERSATION_CONTEXT_MISSING", "original SDK archive is unavailable; do not start empty")
    saved = _parse(row.payload["data"], TurnArchive)
    if not isinstance(saved.archive,(aw.SdkArchive,aw.StoredSdkArchive)):
        raise Problem(409,"CONVERSATION_CONTEXT_MISSING","saved SDK context is unavailable; do not start empty")
    from .behavior_conversation_transfer import permits_archive_gap
    control = authority.runtime.work_policy
    record = None if control is None else control._job_record(session.assignment.task.job_id)
    gap_allowed = control is not None and permits_archive_gap(control, record, saved)
    if (saved.conversation_id != task.conversation_id or saved.turn_number != task.turn_number - 1 and not gap_allowed
            or task.previous_archive_id != key("text-archive", saved.job_id)
            or saved.archive.sdk_session_id != session.assignment.sdk_session_id
            or row.payload["material"] != turn_material(saved).model_dump(mode="json")):
        raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "original SDK context differs")
    return saved, row


def validate_turn_skills(authority, session, saved):
    """A completed turn's catalog is history; an unfinished job's catalog is not."""
    if saved.skill_catalog is not None:
        task = session.assignment.task.instruction
        if session.skill_catalog is None:
            # A captured fa-skills-v1 grant may be narrowed away by the next turn; the old catalog stays history.
            if task is not None and task.prompt_profile not in SKILL_PROFILES:
                return
            raise Problem(403, "SKILL_SOURCE_CHANGED", "retained skills require the same enabled skill profile")
        _, team, _ = authority._skill_scope(session)
        if any(entry.team != team for entry in saved.skill_catalog.entries):
            raise Problem(403, "SKILL_IDENTITY_DENIED", "historical guidance belongs to another Team")
        if saved.skill_catalog != session.skill_catalog:
            prior = authority.runtime.dep.workers.inspect(saved.job_id)
            if (task is None
                    or saved.prompt_assembly != assemble_prompt(task.prompt_profile).record
                    or prior.state not in {"succeeded", "failed", "cancelled"} or not prior.result_forwarded
                    or not saved.sdk_stop.graceful):
                raise Problem(403, "SKILL_SOURCE_CHANGED",
                              "catalog changes require a terminal saved turn under the same skill profile")


def restore_turn(authority, session, principal, *, resident=None):
    """Revalidate the prior turn before its history reaches an SDK.

    ``resident`` (ADR-0005 PoC) runs the same checks, then confirms the still-running SDK released exactly
    the published prior turn instead of transferring the archive bytes again.
    """
    from .behavior_assistant_checkpoint import unpack
    if session.assignment.task.instruction.text_attachment_profile is not None:
        from .behavior_attachment_source import current_sources
        current_sources(authority, session, principal)
    saved, _ = previous_turn(authority, session)
    validate_turn_skills(authority, session, saved)
    if saved.work_memory_source != session.work_memory_source:
        raise Problem(403, "WORK_MEMORY_SOURCE_CHANGED", "restored work knowledge differs from its original archive")
    if session.work_memory_source is not None:
        from .behavior_work_memory_source import current_sources
        current_sources(authority, session, principal)
    if session.user_turn_sources:
        from .behavior_user_turn_source import current_sources
        current_sources(authority, session, principal)
    if session.skill_catalog is not None:
        authority.current_skills(session)
    if isinstance(saved.archive,aw.SdkArchive):
        unpack(saved.archive)
    if saved.context_read:
        authority._reviewed(session, principal)
    if resident is not None:
        from .behavior_sdk_checkpoint import published_record
        stop = saved.sdk_stop
        if (not isinstance(saved.archive, aw.StoredSdkArchive) or stop.release is None
                or stop.release != session.assignment.task.instruction.worker_profile
                or stop.pid != resident.pid or saved.archive.sha256 != resident.archive_sha256
                or published_record(authority, saved.archive, job_id=saved.job_id).sdk_stop != stop):
            raise Problem(409, "RESIDENT_WORKER_CHANGED", "the resident SDK does not hold the original released turn")
        return aw.ToolReceipt(state="running")
    if isinstance(saved.archive,aw.StoredSdkArchive):
        from .behavior_sdk_checkpoint import load_state
        return load_state(authority,session,saved.archive,job_id=saved.job_id)
    return saved.archive


# The four predicates below are one conjunction ("saved SDK reply changed or claimed unsupported effects"),
# split at its own term boundaries; the caller joins them with `and` in the original order.
