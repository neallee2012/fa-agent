"""Explicit human fragments from saved turns; proposals are never effective claims."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_ingress import checked_human_text
from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_wire import Command, Digest, Identifier, WireModel
from .errors import Problem
from .memory_source import ProposalSource  # re-exported: existing importers keep this path
from .source_materials import canonical, digest


class NoteProposalCommand(Command):
    schema_version: Literal["fa-note-proposal-command-v1"]
    conversation_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    speaker: Literal["user", "assistant"]
    source_sha256: Digest
    start_byte: Annotated[int, Field(ge=0)]
    end_byte: Annotated[int, Field(ge=1)]
    share_team_id: Identifier
    share_work_id: Identifier
    share_confirmed: bool

    @model_validator(mode="after")
    def explicit_bounded_selection(self):
        if not self.share_confirmed or not 0 < self.end_byte - self.start_byte <= 4096:
            raise ValueError("explicit sharing and a nonempty selection of at most 4096 bytes are required")
        return self


def proposal_key(authority_domain, human_oid, request_id):
    return control_key("memory-note-proposal", authority_domain, human_oid, request_id)


def intent_digest(binding_id, human_oid, command):
    return digest({"binding_id": binding_id, "human_oid": human_oid, "command": command.model_dump(mode="json")})


class NoteProposalView(WireModel):
    schema_version: Literal["fa-note-proposal-view-v1"] = "fa-note-proposal-view-v1"
    proposal_id: Identifier | None = None
    request_id: Identifier
    binding_id: Identifier
    team_id: Identifier
    work_id: Identifier
    state: Literal["not_submitted", "pending_review"]
    effective: Literal[False] = False
    bridge_available: Literal[False] = False
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    source: ProposalSource

    @model_validator(mode="after")
    def exact_selected_text(self):
        raw = self.text.encode("utf-8")
        if (not 0 < len(raw) <= 4096 or len(raw) != self.source.end_byte - self.source.start_byte
                or self.source.end_byte > self.source.source_bytes
                or hashlib.sha256(raw).hexdigest() != self.source.selected_sha256
                or (self.state == "pending_review") != (self.proposal_id is not None)):
            raise ValueError("the proposal view is not an exact inactive selection")
        return self


def selected_view(binding_id, command, original_request_id, source_bytes, selected, proposal_id=None):
    return NoteProposalView(
        proposal_id=proposal_id, request_id=command.request_id, binding_id=binding_id,
        team_id=command.share_team_id, work_id=command.share_work_id,
        state="not_submitted" if proposal_id is None else "pending_review", text=selected,
        source=ProposalSource(
            conversation_id=command.conversation_id, turn_number=command.turn_number, speaker=command.speaker,
            original_request_id=original_request_id, source_sha256=command.source_sha256,
            source_bytes=source_bytes, start_byte=command.start_byte, end_byte=command.end_byte,
            selected_sha256=hashlib.sha256(selected.encode("utf-8")).hexdigest(),
        ),
    )


class NoteProposalRecord(WireModel):
    kind: Literal["memory_note_proposal"] = "memory_note_proposal"
    schema_version: Literal["fa-memory-note-proposal-v1"] = "fa-memory-note-proposal-v1"
    proposal_id: Identifier
    authority_domain: Identifier
    binding_id: Identifier
    human_oid: Identifier
    original_request_id: Identifier
    command: NoteProposalCommand
    source_bytes: Annotated[int, Field(ge=1)]
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    selected_sha256: Digest
    state: Literal["pending_review"] = "pending_review"
    effective: Literal[False] = False
    bridge_available: Literal[False] = False
    request: ControlRequest

    @model_validator(mode="after")
    def original_request_and_fragment(self):
        raw = self.text.encode("utf-8")
        if (self.proposal_id != proposal_key(self.authority_domain, self.human_oid, self.command.request_id)
                or self.request.request_id != control_key("memory-note-proposal-write", self.proposal_id)
                or self.request.request_digest != intent_digest(self.binding_id, self.human_oid, self.command)
                or self.proposal_id not in self.request.read_set
                or self.command.end_byte > self.source_bytes
                or len(raw) != self.command.end_byte - self.command.start_byte
                or hashlib.sha256(raw).hexdigest() != self.selected_sha256):
            raise ValueError("proposal identity or exact fragment differs")
        return self

    def public_view(self):
        return selected_view(self.binding_id, self.command, self.original_request_id,
                             self.source_bytes, self.text, self.proposal_id).model_dump(mode="json")


class NoteProposalService:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def _authorize(self, binding_id, principal):
        control = self.runtime.work_policy
        if control is None:
            raise Problem(403, "NOTE_PROPOSAL_DISABLED", "the original work policy is required")
        control._human(principal)
        binding, _, _ = self.runtime.authorize(binding_id, principal, "read")
        return control, binding

    def _identity(self, principal, request_id):
        return proposal_key(self.store._scope.authority_domain, principal.oid, request_id)

    def _stored(self, binding_id, principal, request_id):
        from .behavior_runtime import material

        key = self._identity(principal, request_id)
        row = self.store.read_snapshot((key,))[key]
        if row is None:
            return None
        try:
            record = NoteProposalRecord.model_validate_json(canonical(row.payload["data"]))
            if (record.proposal_id != key or record.binding_id != binding_id or record.human_oid != principal.oid
                    or record.authority_domain != self.store._scope.authority_domain
                    or row.payload["material"] != material(record.model_dump(mode="json")).model_dump(mode="json")):
                raise ValueError("original proposal differs")
        except (ValueError, TypeError, KeyError):
            raise Problem(409, "NOTE_PROPOSAL_INVALID", "the original proposal cannot be verified") from None
        return record

    def _origin(self, binding_id, command, principal):
        from .behavior_assistant import key as assistant_key
        from .behavior_work_policy import work_policy_key

        control, binding = self._authorize(binding_id, principal)
        record = control._instruction_record(principal, command.conversation_id, command.turn_number)
        if (record.binding.binding_id != binding_id or command.share_team_id != binding.team_id
                or command.share_work_id != binding.work.work_id):
            raise Problem(403, "NOTE_PROPOSAL_SCOPE_DENIED", "sharing must match the original turn, Team and work")
        reply = control.read_reply(principal, command.conversation_id, command.turn_number)
        text = record.profile.instruction.content if command.speaker == "user" else reply.text
        try:
            raw = text.encode("utf-8")
            if hashlib.sha256(raw).hexdigest() != command.source_sha256 or command.end_byte > len(raw):
                raise ValueError("source or range differs")
            selected = checked_human_text(raw[command.start_byte:command.end_byte])
            if not selected.strip():
                raise ValueError("an empty note selection is not supported")
        except (ValueError, TypeError):
            raise Problem(409, "NOTE_PROPOSAL_SOURCE_CHANGED", "the original source or exact UTF-8 range differs") from None
        keys = tuple(dict.fromkeys((
            *self.runtime._gate_keys(binding),
            work_policy_key("registration", record.admission_id),
            assistant_key("reply", binding.job_id),
            assistant_key("result", binding.work.work_id),
            self._identity(principal, command.request_id),
        )))
        return binding, record.request_id, raw, selected, keys, self.store.read_snapshot(keys)

    def preview(self, binding_id, command, principal):
        def read():
            _, original_request, raw, selected, _, _ = self._origin(binding_id, command, principal)
            return selected_view(binding_id, command, original_request, len(raw), selected).model_dump(mode="json")
        return self.store.consistent_read(read)

    def create(self, binding_id, command, principal):
        from .behavior_runtime import material

        self._authorize(binding_id, principal)
        existing = self._stored(binding_id, principal, command.request_id)
        if existing is not None:
            return self.reconcile(binding_id, command, principal)
        binding, original_request, raw, selected, keys, observed = self.store.consistent_read(
            lambda: self._origin(binding_id, command, principal))
        key = self._identity(principal, command.request_id)
        request = ControlRequest(request_id=control_key("memory-note-proposal-write", key),
                                 request_digest=intent_digest(binding_id, principal.oid, command), read_set=keys)
        proposal = NoteProposalRecord(
            proposal_id=key, authority_domain=self.store._scope.authority_domain, binding_id=binding_id,
            human_oid=principal.oid, original_request_id=original_request, command=command,
            source_bytes=len(raw), text=selected, selected_sha256=hashlib.sha256(selected.encode("utf-8")).hexdigest(),
            request=request,
        )

        def decide(rows):
            if any(rows[item] != observed[item] for item in keys):
                raise Problem(412, "NOTE_PROPOSAL_SOURCE_CHANGED", "the original source or authority changed before save")
            self.runtime._state(binding, rows, use="read")
            data = proposal.model_dump(mode="json")
            return ControlChanges(writes=(ControlWrite(
                row_id=key, mode="create", data=data, material=material(data),
            ),), result={"proposal_id": key, "request_id": command.request_id,
                         "state": "pending_review", "effective": False, "bridge_available": False})

        return self.store.transact(request, decide)

    def reconcile(self, binding_id, command, principal):
        def read():
            self._authorize(binding_id, principal)
            record = self._stored(binding_id, principal, command.request_id)
            if record is None:
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "no original proposal receipt is available; do not resubmit")
            if record.command != command:
                raise Problem(409, "NOTE_PROPOSAL_REQUEST_CONFLICT", "the original request has different content")
            return self.store.reconcile(record.request)
        return self.store.consistent_read(read)

    def read(self, binding_id, request_id, principal):
        def current():
            self._authorize(binding_id, principal)
            record = self._stored(binding_id, principal, request_id)
            if record is None:
                raise Problem(404, "NOTE_PROPOSAL_NOT_FOUND", "no original proposal is available")
            self.store.reconcile(record.request)
            return record.public_view()
        return self.store.consistent_read(current)
