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
from .behavior_work_memory_prompt import WORK_MEMORY_PROFILE
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


def registration_image(record, records, *, environment_transfers=None):
    task=record.profile.instruction
    prior=sorted((r for r in records if r.profile.instruction is not None
        and r.profile.instruction.conversation_id==task.conversation_id
        and r.profile.instruction.turn_number<=task.turn_number),key=lambda r:r.profile.instruction.turn_number)
    if [r.profile.instruction.turn_number for r in prior]!=list(range(1,task.turn_number+1)):
        raise Problem(409,"CONVERSATION_HISTORY_INVALID","original conversation turn sequence differs")
    b=record.binding
    if any(r.agent_scope!=record.agent_scope
           or (r.binding.tenant_id,r.binding.team_id,r.binding.human_oid,r.binding.agent_oid)
           != (b.tenant_id,b.team_id,b.human_oid,b.agent_oid) for r in prior):
        raise Problem(409,"CONVERSATION_HISTORY_INVALID","a conversation must retain its original agent scope")
    if any(r.profile.prompt_profile!=record.profile.prompt_profile for r in prior):
        raise Problem(409,"CONVERSATION_HISTORY_INVALID","a conversation cannot silently switch prompt profiles")
    if any(r.profile.text_attachment_profile!=record.profile.text_attachment_profile for r in prior):
        raise Problem(409,"CONVERSATION_HISTORY_INVALID","a conversation cannot silently switch attachment profiles")
    if any(r.profile.user_text_file_profile!=record.profile.user_text_file_profile for r in prior):
        raise Problem(409,"CONVERSATION_HISTORY_INVALID","a conversation cannot silently switch literal-file profiles")
    if any(r.profile_selection_id!=record.profile_selection_id for r in prior):
        raise Problem(409,"CONVERSATION_HISTORY_INVALID","a conversation must retain its original approved profile selection")
    if any(r.profile.memory_scope!=record.profile.memory_scope for r in prior):
        raise Problem(409,"CONVERSATION_HISTORY_INVALID","a conversation must retain its original memory capture")
    if record.environment_transfer_id is None:
        if record.hosted_session_id is not None and any(
            r.hosted_session_id!=record.hosted_session_id or r.environment_transfer_id is not None
            or r.profile.instruction.hosted_agent_version!=task.hosted_agent_version
            or r.agent_vm!=record.agent_vm for r in prior):
            raise Problem(409,"CONVERSATION_HISTORY_INVALID","a conversation cannot silently switch Hosted session/version")
    else:
        from .behavior_conversation_transfer import matches_registration
        transfer = (environment_transfers or {}).get(record.environment_transfer_id)
        if (transfer is None or not matches_registration(transfer, record)
                or tuple(r.binding.job_id for r in prior[:len(transfer.source.jobs)]) != transfer.source.jobs
                or any(not matches_registration(transfer, r) for r in prior[len(transfer.source.jobs):])):
            raise Problem(409, "CONVERSATION_HISTORY_INVALID", "Hosted environment transition lacks its exact original scope")
    return Conversation(conversation_id=task.conversation_id,human_oid=b.human_oid,team_id=b.team_id,
        agent_oid=b.agent_oid,sdk_session_id=sdk_session_id(task.conversation_id),
        jobs=tuple(r.binding.job_id for r in prior),active_job_id=b.job_id,archive_id=task.previous_archive_id,
        hosted_session_id=record.hosted_session_id,agent_vm=record.agent_vm,
        hosted_agent_version=task.hosted_agent_version if record.hosted_session_id is not None else None)


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


def validate_instruction_history(inventory,config,bindings):
    from types import SimpleNamespace
    from .behavior_recovery_history import _Inventory
    from .behavior_work_policy import WorkRegistration,WorkPolicyControl,work_policy_key,registered_instruction
    from .behavior_assistant import AssistantAuthority,_Session,key
    from .behavior_runtime import BehaviorRuntime
    from .behavior_assistant_checkpoint import unpack
    from .behavior_runtime_rotation_history import require
    view=_Inventory(inventory,config)
    records=[view.row(k,WorkRegistration,material=True) for k,row in inventory.rows.items()
             if row.payload["data"].get("kind")=="steady_work_registration"
             and row.payload["data"].get("profile",{}).get("instruction") is not None]
    checked=set()
    copy_effects = {}
    skill_draft_effects = {}
    allowed={b.binding_id:b for b in bindings}
    groups={}
    for record in records:
        require(allowed.get(record.binding.binding_id)==record.binding,"conversation work scope differs")
        peer_work=record.profile.instruction.peer_work
        if peer_work is None:
            require(record.submit_sha256==hashlib.sha256(
                registered_instruction(record).model_dump_json().encode()).hexdigest(),"original instruction bytes differ")
        else:
            from .behavior_agent_messages import AgentMessage, MessageEnvelope
            from .behavior_store import InlineMaterial
            require(record.submit_sha256==peer_work.message_sha256
                    and digest(record.profile.instruction.content)==peer_work.content_sha256,
                    "original peer brief bytes differ")
            if peer_work.message_key in inventory.rows:
                message=view.row(peer_work.message_key,MessageEnvelope)
                payload=_parse(inventory.rows[peer_work.message_key].payload["material"],InlineMaterial)
                command=AgentMessage.model_validate_json(payload.data)
                require(message.payload_sha256==peer_work.message_sha256
                        and message.to_work_id==record.binding.work.work_id
                        and message.recipient_agent_id==peer_work.recipient_agent_id
                        and message.sender_agent_id==peer_work.sender_agent_id
                        and command.kind in {"request","handoff"} and command.content==record.profile.instruction.content,
                        "receiver instruction escaped the original accepted handoff")
            else:
                require(worker_key("job",record.binding.job_id) not in inventory.rows,
                        "a receiver cannot run before its original message was accepted")
        groups.setdefault(record.profile.instruction.conversation_id,[]).append(record)
        from .behavior_sdk_checkpoint import checkpoint_key,SdkStateUpload,validate_state_history
        if record.profile.instruction.sop_copy is not None:
            from .behavior_skill_copy_source import validate_history
            copy_effects[record.binding.job_id], retained = validate_history(view, record)
            checked.update(retained)
        if skill_draft_enabled(record.profile.instruction.prompt_profile, record.profile.instruction.capabilities):
            from .behavior_skill_draft_source import validate_history
            skill_draft_effects[record.binding.job_id], retained = validate_history(view, record)
            checked.update(retained)
        upload_key=checkpoint_key(record.binding.job_id)
        if upload_key in inventory.rows:
            uploaded=view.row(upload_key,SdkStateUpload)
            require(uploaded.conversation_id==record.profile.instruction.conversation_id
                    and uploaded.sdk_session_id==sdk_session_id(uploaded.conversation_id),
                    "SDK state upload crossed a conversation")
            validate_state_history(view,uploaded,record.binding)
            checked.add(upload_key)
    from .behavior_conversation_transfer import (
        validate_history as validate_environments, history_ready, ready_conversation, head_key,
    )
    environments = validate_environments(view, records)
    checked.update(environments)
    checked.update(head_key(value.source.conversation_id) for value in environments.values()
                   if history_ready(view, value))
    for cid,turns in groups.items():
        turns.sort(key=lambda r:r.profile.instruction.turn_number)
        ckey=conversation_key(cid)
        current=view.row(ckey,Conversation)
        previous_archive=None
        for record in turns:
            task,b=record.profile.instruction,record.binding
            require(task.previous_archive_id==previous_archive,"follow-up skipped original SDK context")
            image=registration_image(record,records,environment_transfers=environments)
            from .behavior_work_policy import registration_receipt_id
            registration=view.receipts[registration_receipt_id(record)]
            require(dict(registration.writes).get(ckey)==view.image_hash(ckey,image)
                and ckey in registration.request.read_set,"conversation admission has no exact CAS write")
            akey=key("text-archive",b.job_id)
            if akey not in inventory.rows:
                transfer = next((value for value in environments.values()
                                 if value.command.failed_job_id == b.job_id and history_ready(view, value)), None)
                if transfer is not None:
                    require(transfer.source == image, "environment preparation changed the original failed conversation")
                    expected = ready_conversation(transfer)
                    previous_archive = expected.archive_id
                    continue
                if key("no-sdk-turn", b.job_id) in inventory.rows:
                    from .behavior_instruction_no_sdk import validate_history
                    require(record is turns[-1], "no-SDK failure cannot silently skip a user turn")
                    expected, retained, closure = validate_history(view, record, image)
                    require(closure.guard_revision > registration.guard_revision, "no-SDK failure precedes admission")
                    checked.update(retained)
                    break
                require(record is turns[-1],"a later turn bypassed an unfinished original turn")
                expected=image
                break
            saved=view.row(akey,TurnArchive)
            copy_effect = copy_effects.get(b.job_id)
            copy_receipt = None if copy_effect is None else copy_effect.receipt
            copy_outcome = None if copy_effect is None else copy_effect.outcome()
            skill_draft_effect = skill_draft_effects.get(b.job_id)
            skill_draft_receipt = None if skill_draft_effect is None else skill_draft_effect.receipt
            skill_draft_outcome = None if skill_draft_effect is None else skill_draft_effect.outcome()
            require(saved.sop_copy_receipt == copy_receipt, "saved copy receipt differs from its original effect")
            require(saved.sop_copy_outcome == copy_outcome, "saved copy outcome differs from its retained original stage")
            require(saved.skill_draft_receipt == skill_draft_receipt,
                    "saved skill draft receipt differs from its original effect")
            require(saved.skill_draft_outcome == skill_draft_outcome,
                    "saved skill draft outcome differs from its retained original stage")
            require(inventory.rows[akey].payload["material"]==turn_material(saved).model_dump(mode="json"),
                    "original SDK archive metadata differs")
            require(saved.prompt_assembly==assemble_prompt(task.prompt_profile).record
                    and saved.capability_assembly==capability_record(task.prompt_profile,task.capabilities),
                    "saved instruction assembly differs from the original server profile")
            if isinstance(saved.archive,aw.SdkArchive):unpack(saved.archive)
            require(saved.conversation_id==cid and saved.job_id==b.job_id
                and saved.turn_number==task.turn_number
                and (not isinstance(saved.archive,(aw.SdkArchive,aw.StoredSdkArchive)) or saved.archive.sdk_session_id==image.sdk_session_id)
                and saved.sdk_stop.graceful,"SDK archive identity or owned stop differs")
            sid=key("session-id",worker_key("start",b.job_id,"1"))
            skey,rkey=key("session",sid),key("result",b.work.work_id)
            session=view.row(skey,_Session)
            require(saved.skill_catalog==session.skill_catalog, "saved skill catalog differs from its original session")
            require(saved.user_turn_sources==session.user_turn_sources,
                    "saved user-original references differ from their original session")
            require(saved.work_memory_source==session.work_memory_source,
                    "saved reviewed-memory provenance differs from its original session")
            if saved.work_memory_source is not None:
                require(task.prompt_profile == WORK_MEMORY_PROFILE
                        and saved.work_memory_source.claim_id == record.profile.claim_id
                        and saved.work_memory_source.team_id == b.team_id
                        and saved.work_memory_source.subject == b.subject,
                        "reviewed-memory provenance differs from its original scope")
            if saved.user_turn_sources:
                from .behavior_user_turn_source import source_for
                for source in saved.user_turn_sources:
                    require(1 <= source.turn_number < task.turn_number, "user-original source is not a past turn")
                    prior_record = turns[source.turn_number - 1]
                    prior_archive = view.row(key("text-archive", prior_record.binding.job_id), TurnArchive)
                    require(source == source_for(prior_record, prior_archive),
                            "user-original source no longer matches its retained registration and archive")
            result=view.row(rkey,VerifiedWorkResult,material=True)
            reply_key=key("reply",b.job_id)
            packed=None if reply_key not in inventory.rows else view.row(reply_key,SavedReply)
            reply=None if packed is None else unpack_reply(packed)
            if packed is not None:
                require(packed.job_id==b.job_id and result.content==reply_reference(packed),"saved reply reference differs")
                checked.add(reply_key)
            command=aw.SaveTextReply(request_id="save-text-reply",reply=reply,archive=saved.archive,
                                    sdk_stop=saved.sdk_stop,failure=saved.failure,progress=saved.progress)
            failure="cancelled" if result.state=="cancelled" else saved.failure
            require(session.state=="ready_to_finalize" and session.assignment.task.instruction==task
                and saved.context_read==session.context_read
                and session.assignment.sdk_session_id==image.sdk_session_id
                and (reply is None or reply.sdk_session_id==image.sdk_session_id
                     and reply.conversation_id==cid and reply.turn_number==task.turn_number)
                and (saved.failure != "model_call_limit_reached"
                     or (task.peer_work is not None or record.agent_origin is not None) and reply is None
                     and result.content == failure_notice(result.state, failure))
                and result.work_id==b.work.work_id and result.definition_digest==digest(b.work.model_dump(mode="json"))
                and result.passed_checks==(aw.TEXT_CHECKS if result.state=="succeeded" else ())
                and (result.state!="succeeded" or reply is not None and saved.failure is None and not result.unresolved_operations)
                and not result.artifacts
                and set((*result.known_effects,*result.unresolved_operations)) <= {
                    b.operation_id, *((copy_outcome.receipt_id,) if copy_outcome is not None else ()),
                    *((skill_draft_outcome.receipt_id,) if skill_draft_outcome is not None else ())}
                and result.proof==reply_proof(image.sdk_session_id,saved.archive,saved.sdk_stop,failure),
                "saved SDK reply changed or claimed unsupported effects")
            if copy_outcome is not None:
                require((copy_outcome.receipt_id in result.known_effects) == bool(copy_outcome.known_effects)
                        and (copy_outcome.receipt_id in result.unresolved_operations) == bool(copy_outcome.unresolved_operations)
                        and (not copy_outcome.unresolved_operations or failure is not None
                             and result.state in {"failed", "cancelled"}),
                        "original failed copy lost its known or unresolved effects")
            if skill_draft_outcome is not None:
                require((skill_draft_outcome.receipt_id in result.known_effects) == bool(skill_draft_outcome.known_effects)
                        and (skill_draft_outcome.receipt_id in result.unresolved_operations)
                        == bool(skill_draft_outcome.unresolved_operations)
                        and (not skill_draft_outcome.unresolved_operations or failure is not None
                             and result.state in {"failed", "cancelled"}),
                        "original failed skill draft lost its known or unresolved effects")
            control=object.__new__(WorkPolicyControl)
            control.policy=SimpleNamespace(authority_domain=config.table.scope.authority_domain)
            control.records={b.binding_id:record}
            control.peer_roots={}
            if task.peer_work is not None:
                from .behavior_agent_message_api import AgentCollaborationRoot
                control.peer_roots[task.peer_work.root_id]=view.row(task.peer_work.root_id,AgentCollaborationRoot)
            runtime=SimpleNamespace(teams_metrics=None,work_policy=control,bindings={b.binding_id:b},
                refresh_bindings=lambda binding_id=None:None,dep=SimpleNamespace(workers=SimpleNamespace(contract=config.workers,
                    pool_key=lambda job:worker_key("pool",config.workers.workspace_id,job))))
            runtime._gate_keys=lambda binding:BehaviorRuntime._gate_keys(runtime,binding)
            authority=object.__new__(AssistantAuthority)
            authority.runtime=runtime
            pkeys=() if saved.progress is None else (key("progress",sid),)
            checkpoint_reads=() if not isinstance(saved.archive,aw.StoredSdkArchive) else (checkpoint_key(b.job_id),)
            if checkpoint_reads:
                require(view.row(checkpoint_reads[0],SdkStateUpload).reference==saved.archive,
                        "saved reply has no completed SDK state upload")
            reads=tuple(dict.fromkeys((*authority._keys(session),ckey,akey,rkey,reply_key,
                                      control_key("operation",b.operation_id),*checkpoint_reads,
                                      *((key("artifact",b.work.work_id),)
                                        if task.sop_copy is not None or skill_draft_enabled(task.prompt_profile, task.capabilities) else ()),
                                      *pkeys)))
            request=ControlRequest(request_id=key("text-reply",sid),
                request_digest=digest(command.model_dump(mode="json")),read_set=reads)
            expected=image.model_copy(update={"active_job_id":None,"archive_id":akey})
            extra=(*(() if packed is None else (reply_key,)),*pkeys)
            if pkeys:
                from .behavior_instruction_progress import progress_record,ProgressRecord
                require(view.row(pkeys[0],ProgressRecord)==progress_record(session,saved.progress),
                        "final progress differs from the saved original turn")
            receipt=view.receipt(request,(rkey,akey,skey,ckey,*extra),exact={
                rkey:view.hash(rkey),akey:view.hash(akey),skey:view.hash(skey),ckey:view.image_hash(ckey,expected)},
                result={"state":"ready_to_finalize","result_digest":digest(result.model_dump(mode="json")),
                        "work_outcome":result.state})
            require(receipt.guard_revision>registration.guard_revision,"reply precedes its original admission")
            if record is not turns[-1]:
                require(copy_outcome is None or not copy_outcome.unresolved_operations,
                        "a new turn cannot bypass an original unresolved copy")
                require(skill_draft_outcome is None or not skill_draft_outcome.unresolved_operations,
                        "a new turn cannot bypass an original unresolved skill draft")
                require(isinstance(saved.archive,(aw.SdkArchive,aw.StoredSdkArchive)),"follow-up skipped unavailable original SDK context")
                following=turns[task.turn_number]
                next_receipt=view.receipts[registration_receipt_id(following)]
                require(receipt.guard_revision<next_receipt.guard_revision,"follow-up precedes saved SDK context")
            checked.add(akey)
            previous_archive=akey
        if current.reset_request_id is not None:
            require(current.active_job_id is None,"an archived conversation cannot retain an active writer")
            archived=_update(expected,active_job_id=None,reset_request_id=current.reset_request_id,archived_at=current.archived_at)
            from .behavior_work_policy import work_policy_key
            reset_control=SimpleNamespace(
                policy_key=work_policy_key("policy",config.table.scope.authority_domain),
                revocation_key=work_policy_key("revocation",config.table.scope.authority_domain))
            request=reset_request(reset_control,expected,current.reset_request_id)
            reset=view.receipt(request,(ckey,),exact={ckey:view.image_hash(ckey,archived)},result=reset_result(archived))
            job_key=worker_key("job",current.jobs[-1])
            job=view.row(job_key,JobRecord)
            require(job.state in {"succeeded","failed","cancelled"} and job.result_forwarded
                    and any(dict(value.writes).get(job_key)==view.hash(job_key)
                            and value.guard_revision<reset.guard_revision for value in view.receipts.values()),
                    "reset preceded verified original work completion")
            expected=archived
        require(current==expected,"current SDK conversation pointer differs")
        checked.add(ckey)
    declared={k for k,row in inventory.rows.items() if row.payload["data"].get("kind") in {
        "assistant_sdk_conversation","assistant_sdk_turn_archive","assistant_text_reply","assistant_sdk_state_upload",
        "assistant_no_sdk_turn", "assistant_sop_skill_copy", "assistant_skill_draft",
        "conversation_environment_transfer", "conversation_environment_head"}}
    require(declared==checked,"orphan or missing SDK conversation history")
    return checked
