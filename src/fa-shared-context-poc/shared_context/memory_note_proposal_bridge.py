"""Map one exact proposal through existing allowlists and human review/commit."""

import hashlib
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from . import behavior_wire as wire
from .behavior_ingress import TEXT_SCHEMA
from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest
from .errors import Problem
from .memory_governance import ClaimPlan, ClaimReference, SubjectState
from .memory_note_proposals import NoteProposalService
from .source_materials import SourcePlan, canonical, digest
from .source_reviews import HumanReviewPlan


class NoteBridgeCommand(wire.Command):
    schema_version: Literal["fa-note-bridge-v1"]
    source_id: wire.Identifier
    claim_id: wire.Identifier
    claim_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    supersedes: wire.Digest | None = None
    effective_from: datetime
    expires_at: datetime | None = None
    kind: wire.MemoryKind = Field(default="note", exclude_if=lambda value:value == "note")

    @field_validator("effective_from", "expires_at")
    @classmethod
    def utc_time(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("explicit UTC is required")
        return value

    @model_validator(mode="after")
    def version_and_time(self):
        if ((self.claim_version == 1) != (self.supersedes is None)
                or self.expires_at is not None and self.expires_at <= self.effective_from):
            raise ValueError("the exact claim predecessor and a valid interval are required")
        return self


class ReviewNoteBridge(wire.WireModel):
    mapping: NoteBridgeCommand
    candidate_sha256: wire.Digest
    evidence_sha256: wire.Digest
    policy_version: wire.Identifier
    reviewed_checks: tuple[Literal["necessity", "support", "contradiction", "sensitivity", "consent"], ...]

    @model_validator(mode="after")
    def complete_review(self):
        if len(self.reviewed_checks) != 5 or set(self.reviewed_checks) != {
                "necessity", "support", "contradiction", "sensitivity", "consent"}:
            raise ValueError("all five human judgments are required")
        return self


class ReconcileNoteBridge(wire.WireModel):
    mapping: NoteBridgeCommand
    stage: Literal["prepare", "review", "commit"]
    review: ReviewNoteBridge | None = None

    @model_validator(mode="after")
    def original_review(self):
        if (self.stage == "review") != (self.review is not None):
            raise ValueError("review lookup requires the complete original human review")
        if self.review is not None and self.review.mapping != self.mapping:
            raise ValueError("the review must retain the original mapping")
        return self


class NoteProposalBridge:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.proposals = NoteProposalService(runtime)

    def _context(self, binding_id, proposal_request_id, command, principal, *, approve=False):
        self.proposals._authorize(binding_id, principal)
        binding, policy, _ = self.runtime.authorize(binding_id, principal, "approve" if approve else "read")
        record = self.proposals._stored(binding_id, principal, proposal_request_id)
        if record is None:
            raise Problem(404, "NOTE_PROPOSAL_NOT_FOUND", "the original proposal is unavailable")
        self.store.reconcile(record.request)
        proposal = record.public_view()
        if (policy.source_visibility != "team" or not policy.human_text_enabled
                or command.source_id not in binding.source_ids or command.claim_id not in binding.claim_ids):
            raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "the original binding must already permit these text/claim slots")
        context = self.runtime._source_context(binding, policy, principal)
        scope = context.scope("team")
        bridge_id = control_key("memory-note-bridge", proposal["proposal_id"], command.request_id)
        source_head = control_key("source-head", scope.key, command.source_id)
        subject = control_key("memory-subject", scope.key, binding.subject)
        request = ControlRequest(
            request_id=control_key("memory-note-bridge-map", bridge_id),
            request_digest=digest([principal.oid, binding_id, proposal["proposal_id"], command.model_dump(mode="json")]),
            read_set=tuple(dict.fromkeys((*self.runtime._gate_keys(binding), proposal["proposal_id"], source_head, subject))),
        )
        return binding, policy, context, proposal, bridge_id, source_head, subject, request

    def _source_original(self, proposal):
        return proposal["text"].encode("utf-8")

    def _source_command(self, proposal, command):
        text = self._source_original(proposal)
        return wire.SourcePrepare(
            request_id=control_key("memory-note-bridge-source", proposal["proposal_id"], command.source_id),
            version=1, original=text, source_schema=TEXT_SCHEMA,
            retention_consent_sha256=hashlib.sha256(text).hexdigest(),
        )

    def _claim_target(self, rows, subject_key, command):
        row = rows[subject_key]
        state = None if row is None else SubjectState.model_validate_json(canonical(row.payload["data"]))
        head = None if state is None else next(
            (head for head in state.heads if head.reference.claim_id == command.claim_id), None)
        if head is None:
            valid = command.claim_version == 1 and command.supersedes is None
        else:
            valid = command.claim_version == head.reference.version + 1 and command.supersedes == head.reference.sha256
        if not valid:
            raise Problem(409, "MEMORY_VERSION_CONFLICT", "the original claim version and predecessor must still match")

    def _source_plan(self, binding, command, source_command, principal, proposal):
        key = self.runtime._plan_key(binding, "source-" + command.source_id, source_command.request_id)
        if self.store.read_snapshot((key,))[key] is None:
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "the original source plan is unavailable")
        plan = self.runtime._existing(binding, "source-" + command.source_id, principal, source_command)
        source = SourcePlan.model_validate_json(canonical(plan.data["source_plan"]))
        if source.original != self._source_original(proposal) or source.reference.source_id != command.source_id:
            raise Problem(409, "BEHAVIOR_SOURCE_DENIED", "the source plan differs from this exact proposal")
        return source

    def _prepared(self, binding, command, principal, proposal, context):
        source_command = self._source_command(proposal, command)
        source = self._source_plan(binding, command, source_command, principal, proposal)
        receipt = self.runtime.commit_source(binding.binding_id, command.source_id,
            wire.Command(request_id=source_command.request_id), principal, reconcile=True)
        exact = self.runtime.dep.sources.read_exact(context, source.reference, require_current=True)
        if exact.original != self._source_original(proposal):
            raise Problem(409, "BEHAVIOR_SOURCE_DENIED", "the retained source is not the proposal text")
        reference = wire.ExactSourceRef.model_validate(source.reference.model_dump(mode="python", exclude={"scope"}))
        return reference, receipt

    def _mapped(self, request):
        return self.store.reconcile(request)

    def prepare(self, binding_id, proposal_request_id, command, principal):
        binding, policy, context, proposal, bridge_id, source_head, subject, request = self._context(
            binding_id, proposal_request_id, command, principal, approve=True)
        if self.store._receipt(request) is not None:
            return self.reconcile(binding_id, proposal_request_id,
                ReconcileNoteBridge(mapping=command, stage="prepare"), principal)
        source_command = self._source_command(proposal, command)
        rows = self.store.read_snapshot(request.read_set)
        self._claim_target(rows, subject, command)
        if rows[source_head] is not None:
            source_key = self.runtime._plan_key(binding, "source-" + command.source_id, source_command.request_id)
            if self.store.read_snapshot((source_key,))[source_key] is None:
                raise Problem(409, "NOTE_BRIDGE_SOURCE_SLOT_UNAVAILABLE", "the allowed source slot is already in use")
            self._prepared(binding, command, principal, proposal, context)
        fresh = False

        def decide(current):
            nonlocal fresh
            self.runtime._state(binding, current, use="read")
            self._claim_target(current, subject, command)
            if current[source_head] != rows[source_head]:
                raise Problem(412, "BEHAVIOR_CONFLICT", "the original source slot changed")
            fresh = True
            return ControlChanges(result={
                "bridge_id": bridge_id, "proposal_id": proposal["proposal_id"],
                "mapping": command.model_dump(mode="json"), "state": "mapped", "effective": False,
            })

        self.store.transact(request, decide)
        if fresh:
            self.runtime.prepare_source(binding_id, command.source_id, source_command, principal)
            self.runtime.commit_source(binding_id, command.source_id,
                wire.Command(request_id=source_command.request_id), principal)
        return self.reconcile(binding_id, proposal_request_id,
            ReconcileNoteBridge(mapping=command, stage="prepare"), principal)

    def _draft(self, bridge_id, command, proposal, reference):
        return wire.ClaimDraft(
            request_id=control_key("memory-note-bridge-review", bridge_id),
            version=command.claim_version, text=proposal["text"], supporting=(reference,), kind=command.kind,
            effective_from=command.effective_from, expires_at=command.expires_at, supersedes=command.supersedes,
        )

    def preview(self, binding_id, proposal_request_id, command, principal):
        binding, _, context, proposal, bridge_id, _, subject, request = self._context(
            binding_id, proposal_request_id, command, principal, approve=True)
        self._mapped(request)
        self._claim_target(self.store.read_snapshot((subject,)), subject, command)
        reference, _ = self._prepared(binding, command, principal, proposal, context)
        draft = self._draft(bridge_id, command, proposal, reference)
        preview = self.runtime.preview_claim(binding_id, command.claim_id, draft, principal)
        claim = ClaimReference(scope=preview.scope, subject=binding.subject, claim_id=command.claim_id,
                               version=command.claim_version, sha256=preview.candidate_sha256)
        return {"bridge_id": bridge_id, "proposal_id": proposal["proposal_id"], "binding_id": binding_id,
                "mapping": command.model_dump(mode="json"),
                "text": proposal["text"], "source": reference.model_dump(mode="json"),
                "claim": claim.model_dump(mode="json"),
                "claim_id": command.claim_id, "claim_version": command.claim_version,
                "supersedes": command.supersedes, "review": preview.model_dump(mode="json"), "effective": False}

    def review(self, binding_id, proposal_request_id, command: ReviewNoteBridge, principal):
        mapping = command.mapping
        binding, _, _, proposal, bridge_id, _, _, request = self._context(
            binding_id, proposal_request_id, mapping, principal, approve=True)
        self._mapped(request)
        review_id = control_key("memory-note-bridge-review", bridge_id)
        key = self.runtime._plan_key(binding, "human-review-" + mapping.claim_id, review_id)
        if self.store.read_snapshot((key,))[key] is not None:
            return self.reconcile(binding_id, proposal_request_id,
                ReconcileNoteBridge(mapping=mapping, stage="review", review=command), principal)
        preview = self.preview(binding_id, proposal_request_id, mapping, principal)
        if any(getattr(command, name) != preview["review"][name] for name in
               ("candidate_sha256", "evidence_sha256", "policy_version")):
            raise Problem(409, "HUMAN_REVIEW_BINDING_CHANGED", "the human review does not match the current preview")
        reference = wire.ExactSourceRef.model_validate(preview["source"])
        draft = self._draft(bridge_id, mapping, proposal, reference)
        review = wire.ClaimReview(
            **draft.model_dump(), decision_id=review_id, reviewed_checks=command.reviewed_checks,
            candidate_sha256=command.candidate_sha256, evidence_sha256=command.evidence_sha256,
            policy_version=command.policy_version,
        )
        self.runtime.review_claim(binding_id, mapping.claim_id, review, principal)
        return self.reconcile(binding_id, proposal_request_id,
            ReconcileNoteBridge(mapping=mapping, stage="review", review=command), principal)

    def _claim_plan(self, binding, command, proposal, bridge_id, principal):
        review_id = control_key("memory-note-bridge-review", bridge_id)
        key = self.runtime._plan_key(binding, "claim-" + command.claim_id, review_id)
        if self.store.read_snapshot((key,))[key] is None:
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "the original reviewed claim plan is unavailable")
        record = self.runtime._load(binding, "claim-" + command.claim_id, review_id, principal)
        plan = ClaimPlan.model_validate_json(canonical(record.data["claim_plan"]))
        candidate = plan.candidate
        if (candidate.text != proposal["text"] or candidate.claim_id != command.claim_id
                or candidate.version != command.claim_version or candidate.supersedes != command.supersedes
                or candidate.effective_from != command.effective_from or candidate.expires_at != command.expires_at
                or len(candidate.supporting) != 1 or candidate.supporting[0].source_id != command.source_id):
            raise Problem(409, "BEHAVIOR_COMMAND_CONFLICT", "the reviewed plan differs from the original mapping")
        return plan

    def commit(self, binding_id, proposal_request_id, command, principal):
        from .behavior_runtime import runtime_key

        binding, _, _, proposal, bridge_id, _, _, request = self._context(
            binding_id, proposal_request_id, command, principal, approve=True)
        self._mapped(request)
        self._claim_plan(binding, command, proposal, bridge_id, principal)
        review_id = control_key("memory-note-bridge-review", bridge_id)
        consumed = runtime_key("consume", self.runtime._plan_key(binding, "claim-" + command.claim_id, review_id))
        if self.store.read_snapshot((consumed,))[consumed] is not None:
            return self.reconcile(binding_id, proposal_request_id,
                ReconcileNoteBridge(mapping=command, stage="commit"), principal)
        return self.runtime.commit_claim(binding_id, command.claim_id,
            wire.Command(request_id=review_id), principal)

    def reconcile(self, binding_id, proposal_request_id, command: ReconcileNoteBridge, principal):
        def read():
            mapping = command.mapping
            binding, _, context, proposal, bridge_id, _, _, request = self._context(
                binding_id, proposal_request_id, mapping, principal)
            self._mapped(request)
            if command.stage == "prepare":
                reference, receipt = self._prepared(binding, mapping, principal, proposal, context)
                return {"bridge_id": bridge_id, "source": reference.model_dump(mode="json"),
                        "source_receipt": receipt.payload["result"], "state": "source_ready", "effective": False}
            review_id = control_key("memory-note-bridge-review", bridge_id)
            if command.stage == "review":
                key = self.runtime._plan_key(binding, "human-review-" + mapping.claim_id, review_id)
                if self.store.read_snapshot((key,))[key] is None:
                    raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "the original human review plan is unavailable")
                record = self.runtime._load(binding, "human-review-" + mapping.claim_id, review_id, principal)
                plan = HumanReviewPlan.model_validate_json(canonical(record.data["human_review"]))
                human_command = plan.material.command
                if (plan.material.human_oid != principal.oid
                        or plan.material.human_source_actor != context.actor_id
                        or plan.material.scope != context.scope("team")
                        or human_command.review_id != review_id or human_command.decision != "approve"
                        or plan.request.request_id != control_key(
                            "source-human-review-request", plan.material.scope.key, review_id)
                        or plan.request.request_digest != digest(plan.material.model_dump(mode="json"))
                        or human_command.reviewed_checks != command.review.reviewed_checks
                        or any(getattr(human_command, name) != getattr(command.review, name) for name in
                               ("candidate_sha256", "evidence_sha256", "policy_version"))):
                    raise Problem(409, "BEHAVIOR_COMMAND_CONFLICT", "the original human review differs")
                claim = self._claim_plan(binding, mapping, proposal, bridge_id, principal)
                if human_command.candidate != claim.candidate:
                    raise Problem(409, "BEHAVIOR_COMMAND_CONFLICT", "the human review and claim plan differ")
                self.store.reconcile(plan.request)
                return {"bridge_id": bridge_id, "state": "reviewed", "effective": False}
            self._claim_plan(binding, mapping, proposal, bridge_id, principal)
            return self.runtime.commit_claim(binding_id, mapping.claim_id,
                wire.Command(request_id=review_id), principal, reconcile=True)
        return self.store.consistent_read(read)
