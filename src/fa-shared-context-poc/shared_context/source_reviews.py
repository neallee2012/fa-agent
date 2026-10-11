"""Durable human review authority, composed with the existing BehaviorStore.

Initialization: initialize BehaviorStore; construct SourceMaterials(store);
construct DurableHumanReviewVerifier(sources, HumanReviewPolicy(...), clock=...);
inject it into MemoryGovernance. No review/grant row is created at construction.
Only explicit plan_decision/commit_decision calls accept a BehaviorPrincipal.
Require kind=human plus require(team, "approve") and "read", and the parent's
source_actor_id mapping. Never map a human to an agent's private-source context.

Endpoint order: preview -> explicit HumanReviewCommand -> plan_decision ->
durably retain HumanReviewPlan -> commit_decision. After any unknown commit use
reconcile_decision with that SAME plan/ID. The accepted material, decision row and
receipt share one EGT. Review material is capped at 16 KiB; existing entity/batch
limits still apply. A rejected candidate cannot silently become approved.

review()/verify() only READ the existing decision; neither mints an approval.
Source evidence covers the complete necessary graph, exact events/versions,
scope, purpose and current source-head access revisions. verify_snapshot() is
pure and rechecks review/revocation plus all source predicates in the caller's
same-guard transaction or final read. A human revocation marker blocks use
immediately, even before MemoryGovernance.revoke updates the subject directory.

Keys: control_key("source-human-review", scope.key, candidate_digest, policy);
"source-human-revocation" binds scope/candidate independently of policy;
"source-human-review-request" binds scope/review_id. Review IDs are server-owned
request identities, not approval flags. No Entra roles, consent or Azure
configuration are created here; these are offline-verifiable app contracts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .behavior_auth import BehaviorPrincipal
from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest, Digest, InlineMaterial
from .errors import Problem
from .memory_governance import (
    ClaimCandidate, ClaimReference, ReviewAttestation, ReviewedClaim, SubjectState, claim_digest,
)
from .models import Identifier
from .records import Record
from .source_materials import (
    AuthenticatedSourceContext, ExactSource, SourceMaterials, SourceModel, SourceReference,
    SourceScope, authorize, canonical, checked, control_row, control_write, digest,
    require_human_context, utc,
)

ReviewCheck = Literal["necessity", "support", "contradiction", "sensitivity", "consent"]
REQUIRED_CHECKS = frozenset({"necessity", "support", "contradiction", "sensitivity", "consent"})

__all__ = [
    "DurableHumanReviewVerifier", "HumanReviewPolicy", "HumanReviewCommand", "HumanReviewPlan",
    "HumanReviewPreview",
]


class HumanReviewPolicy(SourceModel):
    policy_version: Identifier
    max_material_bytes: Annotated[int, Field(ge=1, le=16 * 1024)] = 16 * 1024


class HumanReviewPreview(SourceModel):
    scope: SourceScope
    candidate_sha256: Digest
    evidence_sha256: Digest
    policy_version: Identifier
    sources: tuple[SourceReference, ...]
    source_access_revisions: dict[str, int]


class HumanReviewCommand(SourceModel):
    review_id: Identifier
    candidate: ClaimCandidate
    candidate_sha256: Digest
    evidence_sha256: Digest
    policy_version: Identifier
    decision: Literal["approve", "reject", "revoke"]
    reviewed_checks: tuple[ReviewCheck, ...]
    reason: Annotated[str, Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def complete_checks(self):
        if len(self.reviewed_checks) != 5 or set(self.reviewed_checks) != REQUIRED_CHECKS:
            raise ValueError("explicit review must cover all required checks")
        return self


class _DecisionMaterial(SourceModel):
    schema_version: Literal[1] = 1
    command: HumanReviewCommand
    scope: SourceScope
    human_oid: Identifier
    human_source_actor: Identifier
    reviewed_at: datetime


class HumanReviewPlan(SourceModel):
    material: _DecisionMaterial
    request: ControlRequest


class _DecisionRecord(SourceModel):
    schema_version: Literal[1] = 1
    decision_key: Identifier
    scope: SourceScope
    candidate_sha256: Digest
    evidence_sha256: Digest
    policy_version: Identifier
    decision: Literal["approve", "reject", "revoke"]
    human_oid: Identifier
    material_sha256: Digest
    request: ControlRequest


class DurableHumanReviewVerifier:
    def __init__(self, sources: SourceMaterials, policy: HumanReviewPolicy, *, clock: Callable[[], datetime]):
        self.sources, self.store = sources, sources.store
        self.policy, self._clock = checked(policy, HumanReviewPolicy), clock

    def _scope(self, context, candidate):
        candidate = checked(candidate, ClaimCandidate)
        if not isinstance(context, AuthenticatedSourceContext):
            raise Problem(403, "SOURCE_SCOPE_DENIED", "authenticated source context is required")
        scope = context.scope(candidate.visibility)
        authorize(context, scope, candidate.purpose)
        return scope

    def _reference(self, candidate, scope):
        return ClaimReference(scope=scope, subject=candidate.subject, claim_id=candidate.claim_id,
                              version=candidate.version, sha256=claim_digest(candidate, scope))

    def _keys(self, scope, candidate_sha256):
        return (
            control_key("source-human-review", scope.key, candidate_sha256, self.policy.policy_version),
            control_key("source-human-revocation", scope.key, candidate_sha256),
        )

    def _originals(self, context, candidate, scope):
        originals = self.sources.read_graph(context, candidate.supporting, require_current=True)
        if any(item.event.reference.scope.visibility == "private" and item.event.reference.scope != scope
               for item in originals.values()):
            raise Problem(403, "SOURCE_SCOPE_DENIED", "review cannot disclose a private source through a shared claim")
        return originals

    def preview(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext,
                candidate: ClaimCandidate) -> HumanReviewPreview:
        scope = self._scope(context, candidate)
        require_human_context(principal, context, scope)
        originals = self._originals(context, candidate, scope)
        return HumanReviewPreview(
            scope=scope, candidate_sha256=claim_digest(candidate, scope),
            evidence_sha256=self.sources.evidence_digest(originals), policy_version=self.policy.policy_version,
            sources=tuple(item.event.reference for _, item in sorted(originals.items())),
            source_access_revisions={key: item.access_revision for key, item in originals.items()},
        )

    def _command(self, context, command, scope, originals):
        if (command.candidate_sha256 != claim_digest(command.candidate, scope)
                or command.evidence_sha256 != self.sources.evidence_digest(originals)
                or command.policy_version != self.policy.policy_version):
            raise Problem(409, "HUMAN_REVIEW_BINDING_CHANGED", "explicit review must match current candidate, evidence and policy")

    def _request(self, material, originals):
        command, scope = material.command, material.scope
        review_key, revocation_key = self._keys(scope, command.candidate_sha256)
        reads = [review_key, revocation_key] + list(self.sources.source_read_keys(originals))
        if command.decision == "revoke":
            reference = self._reference(command.candidate, scope)
            reads += [reference.key, control_key("memory-subject", scope.key, reference.subject)]
        return ControlRequest(
            request_id=control_key("source-human-review-request", scope.key, command.review_id),
            request_digest=digest(material.model_dump(mode="json")), read_set=tuple(dict.fromkeys(reads)),
        )

    def _material(self, material):
        raw = canonical(material.model_dump(mode="json"))
        if len(raw) > self.policy.max_material_bytes:
            raise Problem(413, "HUMAN_REVIEW_TOO_LARGE", "review exceeds the atomic inline material bound")
        return InlineMaterial(data=raw, sha256=hashlib.sha256(raw).hexdigest())

    def _revocation_target(self, rows, candidate, scope):
        reference = self._reference(candidate, scope)
        claim = control_row(rows, reference.key, ReviewedClaim)
        state = control_row(rows, control_key("memory-subject", scope.key, reference.subject), SubjectState)
        head = next((item for item in state.heads if item.reference.claim_id == reference.claim_id), None)
        if (claim.reference != reference or claim.candidate != candidate
                or state.scope != scope or state.subject != candidate.subject
                or head is None or head.reference != reference or head.status != "active"):
            raise Problem(409, "HUMAN_REVIEW_TARGET_STALE", "revocation must name the exact current reviewed claim")
        return claim

    def plan_decision(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext,
                      command: HumanReviewCommand) -> HumanReviewPlan:
        command = checked(command, HumanReviewCommand)
        scope = self._scope(context, command.candidate)
        require_human_context(principal, context, scope)
        originals = self._originals(context, command.candidate, scope)
        self._command(context, command, scope, originals)
        material = _DecisionMaterial(command=command, scope=scope, human_oid=principal.oid,
                                     human_source_actor=context.actor_id, reviewed_at=utc(self._clock()))
        self._material(material)
        request = self._request(material, originals)
        if command.decision == "revoke":
            rows = self.store.read_snapshot(request.read_set)
            claim = self._revocation_target(rows, command.candidate, scope)
            self.sources.verify_receipt(rows[claim.reference.key], claim.request, claim.reference.key)
        return HumanReviewPlan(material=material, request=request)

    def _principal(self, principal, context, plan):
        plan = checked(plan, HumanReviewPlan)
        material = plan.material
        require_human_context(principal, context, material.scope)
        if (material.human_oid != principal.oid or material.human_source_actor != context.actor_id
                or self._scope(context, material.command.candidate) != material.scope):
            raise Problem(403, "HUMAN_REVIEW_IDENTITY_MISMATCH", "only the exact authenticated human may submit this decision")
        return plan

    def commit_decision(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext,
                        plan: HumanReviewPlan) -> Record:
        plan = self._principal(principal, context, plan)
        material, command = plan.material, plan.material.command
        originals = self._originals(context, command.candidate, material.scope)
        self._command(context, command, material.scope, originals)
        if self._request(material, originals) != plan.request or utc(material.reviewed_at) > utc(self._clock()):
            raise Problem(409, "HUMAN_REVIEW_REQUEST_MISMATCH", "retain the original exact review request")
        inline = self._material(material)
        review_key, revocation_key = self._keys(material.scope, command.candidate_sha256)
        key = revocation_key if command.decision == "revoke" else review_key
        record = _DecisionRecord(
            decision_key=key, scope=material.scope, candidate_sha256=command.candidate_sha256,
            evidence_sha256=command.evidence_sha256, policy_version=command.policy_version,
            decision=command.decision, human_oid=principal.oid, material_sha256=inline.sha256, request=plan.request,
        )

        def decide(rows):
            self.sources.guard_sources(rows, originals, require_current=True)
            if rows[revocation_key] is not None:
                raise Problem(409, "HUMAN_REVIEW_REVOKED", "this candidate has already been revoked")
            if command.decision == "revoke":
                self._revocation_target(rows, command.candidate, material.scope)
                approval, _, _ = self._decode(rows, review_key, verify_receipt=False)
                if approval.decision != "approve":
                    raise Problem(409, "HUMAN_REVIEW_NOT_APPROVED", "there is no approved decision to revoke")
            return ControlChanges(writes=(control_write(key, record, material=inline),),
                                  result={"decision_key": key, "decision": command.decision,
                                          "candidate_sha256": command.candidate_sha256})

        return self.store.transact(plan.request, decide)

    def reconcile_decision(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext,
                           plan: HumanReviewPlan) -> Record:
        """Historical receipt only, not a new grant or material read."""
        plan = self._principal(principal, context, plan)
        if (plan.request.request_digest != digest(plan.material.model_dump(mode="json"))
                or plan.request.request_id != control_key("source-human-review-request",
                                                          plan.material.scope.key, plan.material.command.review_id)):
            raise Problem(409, "HUMAN_REVIEW_REQUEST_MISMATCH", "reconcile the original human decision")
        return self.store.reconcile(plan.request)

    def _decode(self, rows, key, *, verify_receipt):
        if rows.get(key) is None:
            raise Problem(409, "HUMAN_REVIEW_MISSING", "no persisted human decision exists for this exact candidate")
        record = control_row(rows, key, _DecisionRecord)
        row = rows[key]
        try:
            inline = InlineMaterial.model_validate_json(canonical(row.payload["material"]))
            material = _DecisionMaterial.model_validate_json(inline.data)
        except (KeyError, ValueError, TypeError, ValidationError):
            raise Problem(502, "HUMAN_REVIEW_CORRUPT", "immutable review material is missing or invalid") from None
        command = material.command
        if (record.decision_key != key or record.scope != material.scope
                or record.candidate_sha256 != claim_digest(command.candidate, material.scope)
                or record.candidate_sha256 != command.candidate_sha256
                or record.evidence_sha256 != command.evidence_sha256 or record.decision != command.decision
                or record.policy_version != command.policy_version or record.human_oid != material.human_oid
                or material.human_source_actor != "human-" + material.human_oid
                or record.material_sha256 != inline.sha256
                or record.request.request_digest != digest(material.model_dump(mode="json"))
                or record.request.request_id != control_key("source-human-review-request",
                                                             material.scope.key, command.review_id)):
            raise Problem(502, "HUMAN_REVIEW_CORRUPT", "review metadata differs from its immutable material")
        if verify_receipt:
            self.sources.verify_receipt(row, record.request, key)
        return record, material, row

    def _attestation(self, record, material, row):
        if record.decision != "approve":
            raise Problem(403, "HUMAN_REVIEW_NOT_APPROVED", "explicit human decision did not approve this candidate")
        return ReviewAttestation(
            decision_id=record.decision_key, reviewer_id=record.human_oid, reviewer_kind="human",
            policy_version=record.policy_version, candidate_sha256=record.candidate_sha256,
            evidence_sha256=record.evidence_sha256, reviewed_checks=material.command.reviewed_checks,
            outcome="approved", authority_record_sha256=digest(row.payload),
        )

    def predicate_keys(self, context, candidate, attestation):
        scope = self._scope(context, candidate)
        return self._keys(scope, claim_digest(candidate, scope))

    def verify_snapshot(self, context, candidate, attestation, rows):
        attestation = checked(attestation, ReviewAttestation)
        scope = self._scope(context, candidate)
        review_key, revocation_key = self.predicate_keys(context, candidate, attestation)
        record, material, row = self._decode(rows, review_key, verify_receipt=False)
        if rows.get(revocation_key) is not None:
            raise Problem(403, "HUMAN_REVIEW_REVOKED", "human revocation invalidated this reviewed candidate")
        if (record.policy_version != self.policy.policy_version or material.command.candidate != candidate
                or material.scope != scope or self._attestation(record, material, row) != attestation):
            raise Problem(403, "HUMAN_REVIEW_BINDING_CHANGED", "human decision is not valid for this exact context")
        evidence = self.sources.snapshot_evidence(context, candidate.supporting, rows)
        if any(event.reference.scope.visibility == "private" and event.reference.scope != scope
               for event, _ in evidence.values()):
            raise Problem(403, "SOURCE_SCOPE_DENIED", "review scope cannot expose private evidence")
        if self.sources.snapshot_evidence_digest(evidence) != attestation.evidence_sha256:
            raise Problem(409, "HUMAN_REVIEW_EVIDENCE_CHANGED", "source versions, graph or access revisions changed")

    def review(self, context: AuthenticatedSourceContext, candidate: ClaimCandidate,
               originals: Mapping[str, ExactSource]) -> ReviewAttestation:
        scope = self._scope(context, candidate)
        current = self._originals(context, candidate, scope)
        if (self.sources.evidence_digest(originals) != self.sources.evidence_digest(current)
                or set(originals) != set(current)
                or any(originals[key].original != item.original or originals[key].receipt != item.receipt
                       for key, item in current.items())):
            raise Problem(409, "HUMAN_REVIEW_EVIDENCE_CHANGED", "caller evidence is not the exact current graph")
        review_key, revocation_key = self._keys(scope, claim_digest(candidate, scope))
        keys = (review_key, revocation_key) + self.sources.source_read_keys(current)
        rows = self.store.read_snapshot(keys)
        record, material, row = self._decode(rows, review_key, verify_receipt=True)
        result = self._attestation(record, material, row)
        self.verify_snapshot(context, candidate, result, rows)
        # Receipt verification is another read; close its invalidation window.
        self.verify_snapshot(context, candidate, result, self.store.read_snapshot(keys))
        return result

    def verify(self, context, candidate, attestation):
        scope = self._scope(context, candidate)
        actual = self.review(context, candidate, self._originals(context, candidate, scope))
        if actual != checked(attestation, ReviewAttestation):
            raise Problem(403, "HUMAN_REVIEW_BINDING_CHANGED", "attestation is not the persisted human decision")

    def authorize_revocation(self, context, reference, reason):
        reference = checked(reference, ClaimReference)
        authorize(context, reference.scope)
        key = self._keys(reference.scope, reference.sha256)[1]
        record, material, _ = self._decode(self.store.read_snapshot((key,)), key, verify_receipt=True)
        candidate = material.command.candidate
        authorize(context, reference.scope, candidate.purpose)
        if (record.decision != "revoke" or self._reference(candidate, reference.scope) != reference
                or record.scope != reference.scope or material.command.reason != reason
                or record.policy_version != self.policy.policy_version):
            raise Problem(403, "HUMAN_REVOCATION_BINDING_CHANGED", "no explicit human revocation for this exact claim and reason")
        return record.request.request_id
