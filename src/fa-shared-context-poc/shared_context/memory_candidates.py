"""One inactive, exact user-text suggestion per original instruction turn."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_capabilities import BUILTIN_CAPABILITIES, runtime_grants
from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .memory_source import ProposalSource, quote_source
from .memory_candidate_wire import CandidateArguments, CandidateCommand, CandidateReason, CandidateReceipt
from .source_materials import canonical, digest

CANDIDATE_CAPABILITY = BUILTIN_CAPABILITIES["fa-memory-candidate-v1"]


def candidate_key(authority_domain, binding_id):
    return control_key("memory-turn-candidate", authority_domain, binding_id)


def _intent(binding_id, command):
    return digest({"binding_id": binding_id, "command": command.model_dump(mode="json")})


class MemoryCandidateRecord(WireModel):
    kind: Literal["memory_turn_candidate"] = "memory_turn_candidate"
    schema_version: Literal["fa-memory-turn-candidate-v1"] = "fa-memory-turn-candidate-v1"
    candidate_id: Identifier
    authority_domain: Identifier
    binding_id: Identifier
    admission_id: Identifier
    human_oid: Identifier
    agent_oid: Identifier
    original_request_id: Identifier
    conversation_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    content_sha256: Digest
    command: CandidateCommand
    source: ProposalSource | None
    effective: Literal[False] = False
    request: ControlRequest

    @model_validator(mode="after")
    def original_identity(self):
        from .behavior_work_policy import work_policy_key

        expected = candidate_key(self.authority_domain, self.binding_id)
        if (self.candidate_id != expected
                or self.request.request_id != control_key("memory-candidate-write", expected)
                or self.request.request_digest != _intent(self.binding_id, self.command)
                or expected not in self.request.read_set
                or work_policy_key("registration", self.admission_id) not in self.request.read_set
                or (self.source is not None) != (self.command.decision == "candidate")):
            raise ValueError("candidate identity differs from its original turn request")
        if self.source is not None:
            source = self.source
            raw = self.command.quote.encode("utf-8")
            if (source.speaker != "user" or source.conversation_id != self.conversation_id
                    or source.turn_number != self.turn_number
                    or source.original_request_id != self.original_request_id
                    or source.source_sha256 != self.content_sha256
                    or source.end_byte > source.source_bytes
                    or source.end_byte - source.start_byte != len(raw)
                    or source.selected_sha256 != hashlib.sha256(raw).hexdigest()):
                raise ValueError("candidate quote differs from its exact source range")
        return self


class CandidateView(WireModel):
    schema_version: Literal["fa-memory-candidate-view-v1"] = "fa-memory-candidate-view-v1"
    candidate_id: Identifier
    binding_id: Identifier
    team_id: Identifier
    work_id: Identifier
    conversation_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    state: Literal["not_recorded", "awaiting_saved_turn", "pending_review", "skipped", "ineligible"]
    effective: Literal[False] = False
    reason: CandidateReason | None = None
    text: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    source: ProposalSource | None = None

    @model_validator(mode="after")
    def saved_selection_only(self):
        if self.state == "pending_review":
            if self.text is None or self.source is None:
                raise ValueError("a reviewable suggestion requires its exact source")
            raw = self.text.encode("utf-8")
            if (self.source.speaker != "user" or self.source.conversation_id != self.conversation_id
                    or self.source.turn_number != self.turn_number
                    or self.source.end_byte - self.source.start_byte != len(raw)
                    or self.source.end_byte > self.source.source_bytes
                    or hashlib.sha256(raw).hexdigest() != self.source.selected_sha256):
                raise ValueError("the suggestion differs from its selected source bytes")
        elif self.text is not None or self.source is not None:
            raise ValueError("an unsaved or skipped suggestion must not expose a quote")
        return self


def _source(task, original_request, command):
    if command.decision == "no_candidate":
        return None
    return quote_source(content=task.content, conversation_id=task.conversation_id, turn_number=task.turn_number,
                        request_id=original_request, quote=command.quote)


def _original(runtime, binding_id):
    control = runtime.work_policy
    if control is None:
        raise Problem(403, "MEMORY_CANDIDATE_DISABLED", "the original instruction registration is required")
    control.refresh(binding_id)
    record = control.records.get(binding_id)
    if record is None or record.profile.instruction is None:
        raise Problem(403, "MEMORY_CANDIDATE_DISABLED", "the original instruction registration is required")
    return record


def _session_origin(authority, session, principal):
    runtime = authority.runtime
    binding, _, _ = runtime.authorize(session.binding_id, principal, "execute")
    record = _original(runtime, binding.binding_id)
    task = session.assignment.task.instruction
    grants = () if task is None or task.capabilities is None else task.capabilities.grants
    grant = next((item for item in grants if item.entry_id == CANDIDATE_CAPABILITY.entry_id), None)
    if (principal.kind != "agent" or principal.oid != binding.agent_oid or session.state != "running"
            or record.binding != binding or record.profile.instruction != task
            or grant is None or grant.kind != "fa-builtin"
            or grant.entry_sha256 != CANDIDATE_CAPABILITY.entry_sha256
            or grant.tools != CANDIDATE_CAPABILITY.tools or grant.vision
            or runtime_grants(runtime).get(grant.entry_id) != grant):
        raise Problem(403, "MEMORY_CANDIDATE_DISABLED", "this original turn has no current candidate capability")
    return record


class MemoryCandidates:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def _stored(self, original):
        from .behavior_runtime import material

        key = candidate_key(self.store._scope.authority_domain, original.binding.binding_id)
        row = self.store.read_snapshot((key,))[key]
        if row is None:
            return None
        try:
            value = MemoryCandidateRecord.model_validate_json(canonical(row.payload["data"]))
            task = original.profile.instruction
            if (value.candidate_id != key or value.binding_id != original.binding.binding_id
                    or value.admission_id != original.admission_id
                    or value.authority_domain != self.store._scope.authority_domain
                    or value.human_oid != original.binding.human_oid or value.agent_oid != original.binding.agent_oid
                    or value.original_request_id != original.request_id
                    or value.conversation_id != task.conversation_id or value.turn_number != task.turn_number
                    or value.content_sha256 != hashlib.sha256(task.content.encode("utf-8")).hexdigest()
                    or value.source != _source(task, original.request_id, value.command)
                    or row.payload["material"] != material(value.model_dump(mode="json")).model_dump(mode="json")):
                raise ValueError("the original candidate material differs")
        except (ValueError, TypeError, KeyError):
            raise Problem(409, "MEMORY_CANDIDATE_INVALID", "the original candidate cannot be verified") from None
        return value

    def _receipt(self, record):
        receipt = self.store.reconcile(record.request)
        return CandidateReceipt(
            **receipt.payload["result"], binding_id=record.binding_id, control_request_id=record.request.request_id,
            control_request_digest=record.request.request_digest,
        )

    def read(self, binding_id, principal):
        from .behavior_assistant import key as assistant_key
        from .behavior_delivery import VerifiedWorkResult
        from .behavior_runtime import material
        from .behavior_user_turn_source import _original_write
        from .behavior_workers import JobRecord, worker_key

        def current():
            control = self.runtime.work_policy
            if control is None:
                raise Problem(403, "MEMORY_CANDIDATE_DISABLED", "the original instruction policy is required")
            control._human(principal)
            self.runtime.authorize(binding_id, principal, "read")
            original = _original(self.runtime, binding_id)
            task, binding = original.profile.instruction, original.binding
            base = dict(candidate_id=candidate_key(self.store._scope.authority_domain, binding_id),
                        binding_id=binding_id, team_id=binding.team_id, work_id=binding.work.work_id,
                        conversation_id=task.conversation_id, turn_number=task.turn_number)
            record = self._stored(original)
            if record is None:
                return CandidateView(**base, state="not_recorded")
            self._receipt(record)
            result_key = assistant_key("result", binding.work.work_id)
            row = self.store.read_snapshot((result_key,))[result_key]
            if row is None:
                return CandidateView(**base, state="awaiting_saved_turn", reason=record.command.reason)
            result = VerifiedWorkResult.model_validate_json(canonical(row.payload["data"]))
            if (result.work_id != binding.work.work_id
                    or result.definition_digest != digest(binding.work.model_dump(mode="json"))
                    or row.payload["material"] != material(result.model_dump(mode="json")).model_dump(mode="json")):
                raise Problem(409, "MEMORY_CANDIDATE_SOURCE_INVALID", "the saved turn result differs")
            if result.state != "succeeded":
                return CandidateView(**base, state="ineligible", reason=record.command.reason)
            control.read_reply(principal, task.conversation_id, task.turn_number)
            job_key, reply_key = worker_key("job", binding.job_id), assistant_key("reply", binding.job_id)
            rows = self.store.read_snapshot((job_key, reply_key))
            if rows[job_key] is None or rows[reply_key] is None:
                raise Problem(409, "MEMORY_CANDIDATE_SOURCE_INVALID", "the original saved-turn receipt is unavailable")
            job = JobRecord.model_validate_json(canonical(rows[job_key].payload["data"]))
            if job.binding is None:
                raise Problem(409, "MEMORY_CANDIDATE_SOURCE_INVALID", "the original saved-turn receipt is unavailable")
            session_id = assistant_key("session-id", job.binding.start_request_id)
            _original_write(self.store, assistant_key("text-reply", session_id),
                            {result_key: row, reply_key: rows[reply_key]})
            if record.command.decision == "no_candidate":
                return CandidateView(**base, state="skipped", reason=record.command.reason)
            return CandidateView(**base, state="pending_review", reason=record.command.reason,
                                 text=record.command.quote, source=record.source)
        return self.store.consistent_read(current)


def stage_candidate(authority, session, command, principal):
    from .behavior_runtime import material
    from .behavior_work_policy import work_policy_key

    command = CandidateCommand.model_validate_json(command.model_dump_json())
    original = _session_origin(authority, session, principal)
    service = MemoryCandidates(authority.runtime)
    existing = service._stored(original)
    if existing is not None:
        if existing.command != command:
            raise Problem(409, "MEMORY_CANDIDATE_CONFLICT", "this turn already has its original suggestion decision")
        return service._receipt(existing)
    binding, task = original.binding, original.profile.instruction
    key = candidate_key(service.store._scope.authority_domain, binding.binding_id)
    registration = work_policy_key("registration", original.admission_id)
    request = ControlRequest(
        request_id=control_key("memory-candidate-write", key),
        request_digest=_intent(binding.binding_id, command),
        read_set=tuple(dict.fromkeys((*authority._keys(session), registration, key))),
    )
    record = MemoryCandidateRecord(
        candidate_id=key, authority_domain=service.store._scope.authority_domain,
        binding_id=binding.binding_id, admission_id=original.admission_id,
        human_oid=binding.human_oid, agent_oid=binding.agent_oid, original_request_id=original.request_id,
        conversation_id=task.conversation_id, turn_number=task.turn_number,
        content_sha256=hashlib.sha256(task.content.encode("utf-8")).hexdigest(),
        command=command, source=_source(task, original.request_id, command), request=request,
    )
    expected = service.store.read_snapshot((registration,))[registration]

    def decide(rows):
        authority._gate(session, rows)
        if rows[registration] != expected:
            raise Problem(409, "MEMORY_CANDIDATE_SOURCE_INVALID", "the original instruction changed before staging")
        data = record.model_dump(mode="json")
        return ControlChanges(writes=(ControlWrite(row_id=key, mode="create", data=data, material=material(data)),),
                              result={"candidate_id": key, "decision": command.decision, "state": "staged",
                                      "effective": False})

    service.store.transact(request, decide)
    return service._receipt(record)


def reconcile_candidate(authority, session, command, principal):
    original = _session_origin(authority, session, principal)
    service = MemoryCandidates(authority.runtime)
    record = service._stored(original)
    if record is None:
        raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "the original candidate receipt is not confirmed; do not resend")
    if record.command != command:
        raise Problem(409, "MEMORY_CANDIDATE_CONFLICT", "the lookup differs from this turn's original suggestion")
    return service._receipt(record)
