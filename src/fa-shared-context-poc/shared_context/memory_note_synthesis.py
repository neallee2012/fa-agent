"""Human-selected saved summaries supported by two exact original user fragments."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_delivery import VerifiedWorkResult
from .behavior_operations import control_key
from .behavior_runtime import material
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_wire import Command, Digest, Identifier, WireModel
from .errors import Problem
from .memory_note_proposals import NoteProposalCommand, NoteProposalService, ProposalSource
from .memory_note_proposal_bridge import NoteProposalBridge
from .source_materials import canonical, digest


class SynthesisFragmentSelection(WireModel):
    turn_number: Annotated[int, Field(ge=1)]
    source_sha256: Digest
    start_byte: Annotated[int, Field(ge=0)]
    end_byte: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def bounded_selection(self):
        if not 0 < self.end_byte - self.start_byte <= 4096:
            raise ValueError("one exact fragment of at most 4096 UTF-8 bytes is required")
        return self


class NoteSynthesisCommand(Command):
    schema_version: Literal["fa-note-synthesis-command-v1"]
    conversation_id: Identifier
    summary: SynthesisFragmentSelection
    supporting: Annotated[tuple[SynthesisFragmentSelection, ...], Field(min_length=2, max_length=2)]
    share_team_id: Identifier
    share_work_id: Identifier
    share_confirmed: bool

    @model_validator(mode="after")
    def original_ordered_turns(self):
        numbers = [source.turn_number for source in self.supporting]
        if (not self.share_confirmed or numbers != sorted(set(numbers))
                or numbers[-1] > self.summary.turn_number):
            raise ValueError("explicit sharing of two distinct prior/current user turns is required")
        return self


class SynthesisEvidenceFragment(WireModel):
    source: ProposalSource
    text: Annotated[str, Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def exact_user_bytes(self):
        raw = self.text.encode("utf-8")
        if (self.source.speaker != "user" or not 0 < len(raw) <= 4096
                or self.source.end_byte > self.source.source_bytes
                or self.source.end_byte - self.source.start_byte != len(raw)
                or hashlib.sha256(raw).hexdigest() != self.source.selected_sha256):
            raise ValueError("the supporting original differs from its exact user fragment")
        return self


class SynthesisEvidence(WireModel):
    schema_version: Literal["fa-note-synthesis-evidence-v1"] = "fa-note-synthesis-evidence-v1"
    usage: Literal["knowledge-data-not-permission"] = "knowledge-data-not-permission"
    fragments: Annotated[tuple[SynthesisEvidenceFragment, ...], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def bounded_original_bundle(self):
        sources = tuple(fragment.source for fragment in self.fragments)
        if (sources[0].conversation_id != sources[1].conversation_id
                or sources[0].turn_number >= sources[1].turn_number
                or len(canonical(self.model_dump(mode="json"))) > 8192):
            raise ValueError("a complete ordered original bundle within the source byte limit is required")
        return self

    def original(self):
        return canonical(self.model_dump(mode="json"))


class NoteSynthesisView(WireModel):
    schema_version: Literal["fa-note-synthesis-view-v1"] = "fa-note-synthesis-view-v1"
    proposal_id: Identifier | None = None
    request_id: Identifier
    binding_id: Identifier
    team_id: Identifier
    work_id: Identifier
    state: Literal["not_submitted", "pending_review"]
    effective: Literal[False] = False
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    summary_source: ProposalSource
    evidence: SynthesisEvidence

    @model_validator(mode="after")
    def summary_is_not_its_own_evidence(self):
        raw = self.text.encode("utf-8")
        source = self.summary_source
        if (source.speaker != "assistant" or not 0 < len(raw) <= 4096
                or source.end_byte > source.source_bytes
                or source.end_byte - source.start_byte != len(raw)
                or hashlib.sha256(raw).hexdigest() != source.selected_sha256
                or (self.state == "pending_review") != (self.proposal_id is not None)
                or any(fragment.source.conversation_id != source.conversation_id
                       or fragment.source.turn_number > source.turn_number for fragment in self.evidence.fragments)):
            raise ValueError("the saved summary and its supporting originals differ")
        return self


def synthesis_key(domain, human_oid, request_id):
    return control_key("memory-note-synthesis", domain, human_oid, request_id)


def synthesis_intent(binding_id, human_oid, command):
    return digest({"binding_id": binding_id, "human_oid": human_oid, "command": command.model_dump(mode="json")})


class NoteSynthesisRecord(WireModel):
    kind: Literal["memory_note_synthesis"] = "memory_note_synthesis"
    schema_version: Literal["fa-memory-note-synthesis-v1"] = "fa-memory-note-synthesis-v1"
    proposal_id: Identifier
    authority_domain: Identifier
    binding_id: Identifier
    human_oid: Identifier
    command: NoteSynthesisCommand
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    summary_source: ProposalSource
    evidence: SynthesisEvidence
    state: Literal["pending_review"] = "pending_review"
    effective: Literal[False] = False
    request: ControlRequest

    @model_validator(mode="after")
    def original_request(self):
        if (self.proposal_id != synthesis_key(self.authority_domain, self.human_oid, self.command.request_id)
                or self.request.request_id != control_key("memory-note-synthesis-write", self.proposal_id)
                or self.request.request_digest != synthesis_intent(self.binding_id, self.human_oid, self.command)
                or self.proposal_id not in self.request.read_set):
            raise ValueError("the original synthesis request identity differs")
        sources = (self.summary_source, *(item.source for item in self.evidence.fragments))
        selections = (self.command.summary, *self.command.supporting)
        for source, selection in zip(sources, selections, strict=True):
            if (source.conversation_id != self.command.conversation_id
                    or any(getattr(source, field) != getattr(selection, field)
                           for field in ("turn_number", "source_sha256", "start_byte", "end_byte"))):
                raise ValueError("the retained synthesis sources differ from the explicit human selection")
        self.public_view()
        return self

    def public_view(self):
        return NoteSynthesisView(
            proposal_id=self.proposal_id, request_id=self.command.request_id, binding_id=self.binding_id,
            team_id=self.command.share_team_id, work_id=self.command.share_work_id,
            state="pending_review", text=self.text, summary_source=self.summary_source, evidence=self.evidence,
        ).model_dump(mode="json")


class NoteSynthesisService:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.quotes = NoteProposalService(runtime)

    def _authorize(self, binding_id, principal):
        return self.quotes._authorize(binding_id, principal)

    def _identity(self, principal, request_id):
        return synthesis_key(self.store._scope.authority_domain, principal.oid, request_id)

    def _stored(self, binding_id, principal, request_id):
        key = self._identity(principal, request_id)
        row = self.store.read_snapshot((key,))[key]
        if row is None:
            return None
        try:
            record = NoteSynthesisRecord.model_validate_json(canonical(row.payload["data"]))
            if (record.proposal_id != key or record.binding_id != binding_id or record.human_oid != principal.oid
                    or record.authority_domain != self.store._scope.authority_domain
                    or row.payload["material"] != material(record.model_dump(mode="json")).model_dump(mode="json")):
                raise ValueError("the original synthesis differs")
        except (ValueError, TypeError, KeyError):
            raise Problem(409, "NOTE_SYNTHESIS_INVALID", "the original synthesis cannot be verified") from None
        return record

    def _fragment(self, control, target, command, selection, speaker, principal):
        from .behavior_assistant import key as assistant_key

        origin = control._instruction_record(principal, command.conversation_id, selection.turn_number)
        binding = origin.binding
        if (origin.agent_scope != target.agent_scope
                or any(getattr(binding, field) != getattr(target.binding, field)
                       for field in ("tenant_id", "team_id", "human_oid", "agent_oid", "subject"))
                or origin.runtime_policy.purpose != target.runtime_policy.purpose):
            raise Problem(403, "NOTE_SYNTHESIS_SCOPE_DENIED", "supporting turns must keep the original conversation scope")
        request_id = control_key("memory-synthesis-input", command.request_id, str(selection.turn_number), speaker)
        quote = NoteProposalCommand(
            schema_version="fa-note-proposal-command-v1", request_id=request_id,
            conversation_id=command.conversation_id, speaker=speaker, **selection.model_dump(),
            share_team_id=binding.team_id, share_work_id=binding.work.work_id, share_confirmed=True,
        )
        _, original_request, raw, text, _, observed = self.quotes._origin(binding.binding_id, quote, principal)
        result_key = assistant_key("result", binding.work.work_id)
        result = VerifiedWorkResult.model_validate_json(canonical(observed[result_key].payload["data"]))
        if result.state != "succeeded":
            raise Problem(409, "NOTE_SYNTHESIS_SOURCE_UNAVAILABLE", "only successful complete saved turns may support synthesis")
        source = ProposalSource(
            conversation_id=command.conversation_id, speaker=speaker, original_request_id=original_request,
            source_bytes=len(raw), selected_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            **selection.model_dump(),
        )
        observed.pop(self.quotes._identity(principal, request_id))
        return binding, source, text, observed

    def _origin(self, binding_id, command, principal):
        control, binding = self._authorize(binding_id, principal)
        target = control._instruction_record(principal, command.conversation_id, command.summary.turn_number)
        if (target.binding != binding or command.share_team_id != binding.team_id
                or command.share_work_id != binding.work.work_id):
            raise Problem(403, "NOTE_SYNTHESIS_SCOPE_DENIED", "share into the summary turn's original Team and work")
        origin, summary, text, observed = self._fragment(
            control, target, command, command.summary, "assistant", principal)
        bindings, fragments = [origin], []
        for selection in command.supporting:
            source_binding, source, original, snapshot = self._fragment(
                control, target, command, selection, "user", principal)
            bindings.append(source_binding)
            fragments.append(SynthesisEvidenceFragment(source=source, text=original))
            observed.update(snapshot)
        key = self._identity(principal, command.request_id)
        observed.update(self.store.read_snapshot((key,)))
        try:
            evidence = SynthesisEvidence(fragments=tuple(fragments))
            view = NoteSynthesisView(
                request_id=command.request_id, binding_id=binding_id,
                team_id=binding.team_id, work_id=binding.work.work_id,
                state="not_submitted", text=text, summary_source=summary, evidence=evidence,
            )
        except ValueError:
            raise Problem(413, "NOTE_SYNTHESIS_SOURCE_LIMIT", "the complete original evidence bundle is too large") from None
        return view, tuple(bindings), observed

    def preview(self, binding_id, command, principal):
        return self.store.consistent_read(
            lambda: self._origin(binding_id, command, principal)[0].model_dump(mode="json"))

    def create(self, binding_id, command, principal):
        self._authorize(binding_id, principal)
        if self._stored(binding_id, principal, command.request_id) is not None:
            return self.reconcile(binding_id, command, principal)
        view, bindings, observed = self.store.consistent_read(lambda: self._origin(binding_id, command, principal))
        key = self._identity(principal, command.request_id)
        request = ControlRequest(
            request_id=control_key("memory-note-synthesis-write", key),
            request_digest=synthesis_intent(binding_id, principal.oid, command),
            read_set=tuple(observed),
        )
        record = NoteSynthesisRecord(
            proposal_id=key, authority_domain=self.store._scope.authority_domain,
            binding_id=binding_id, human_oid=principal.oid, command=command, text=view.text,
            summary_source=view.summary_source, evidence=view.evidence, request=request,
        )

        def decide(rows):
            if any(rows[item] != observed[item] for item in observed):
                raise Problem(412, "NOTE_SYNTHESIS_SOURCE_CHANGED", "the original turns or read authority changed before save")
            for binding in bindings:
                self.runtime._state(binding, rows, use="read")
            data = record.model_dump(mode="json")
            return ControlChanges(
                writes=(ControlWrite(row_id=key, mode="create", data=data, material=material(data)),),
                result={"proposal_id": key, "request_id": command.request_id,
                        "state": "pending_review", "effective": False},
            )

        return self.store.transact(request, decide)

    def reconcile(self, binding_id, command, principal):
        def read():
            self._authorize(binding_id, principal)
            record = self._stored(binding_id, principal, command.request_id)
            if record is None:
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "the original synthesis receipt is unavailable; do not resend")
            if record.command != command:
                raise Problem(409, "NOTE_SYNTHESIS_CONFLICT", "the original request has different selections")
            return self.store.reconcile(record.request)
        return self.store.consistent_read(read)

    def read(self, binding_id, request_id, principal):
        def current():
            self._authorize(binding_id, principal)
            record = self._stored(binding_id, principal, request_id)
            if record is None:
                raise Problem(404, "NOTE_SYNTHESIS_NOT_FOUND", "the original synthesis is unavailable")
            self.store.reconcile(record.request)
            view, _, _ = self._origin(binding_id, record.command, principal)
            if (view.text != record.text or view.summary_source != record.summary_source
                    or view.evidence != record.evidence):
                raise Problem(409, "NOTE_SYNTHESIS_SOURCE_CHANGED", "the original saved synthesis sources differ")
            return record.public_view()
        return self.store.consistent_read(current)


class NoteSynthesisBridge(NoteProposalBridge):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.proposals = NoteSynthesisService(runtime)

    def _context(self, binding_id, proposal_request_id, command, principal, *, approve=False):
        from .memory_bridge_settings import memory_slot_ids

        context = super()._context(binding_id, proposal_request_id, command, principal, approve=approve)
        record = self.proposals._stored(binding_id, principal, proposal_request_id)
        original = self.runtime.work_policy._instruction_record(
            principal, record.command.conversation_id, record.command.summary.turn_number)
        if ((command.source_id, command.claim_id) != memory_slot_ids(original.admission_id)
                or command.claim_version != 1 or command.supersedes is not None):
            raise Problem(403, "NOTE_SYNTHESIS_TARGET_DENIED",
                          "synthesis may only create the summary work's own new memory note")
        view, _, _ = self.proposals._origin(binding_id, record.command, principal)
        if (view.text != record.text or view.summary_source != record.summary_source
                or view.evidence != record.evidence):
            raise Problem(409, "NOTE_SYNTHESIS_SOURCE_CHANGED", "the supporting originals are no longer confirmed")
        return context

    def _source_original(self, proposal):
        return SynthesisEvidence.model_validate_json(canonical(proposal["evidence"])).original()

    def preview(self, binding_id, proposal_request_id, command, principal):
        result = super().preview(binding_id, proposal_request_id, command, principal)
        record = self.proposals._stored(binding_id, principal, proposal_request_id)
        return {
            **result, "summary_source": record.summary_source.model_dump(mode="json"),
            "evidence": record.evidence.model_dump(mode="json"),
        }
