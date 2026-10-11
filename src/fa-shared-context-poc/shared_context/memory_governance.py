"""Reviewed claims are authority; summaries/Memory/Search are disposable views.

Initialize with the SAME SourceMaterials/BehaviorStore/snapshot reader, a trusted
clock, tokenizer, and ReviewVerifier backed by server-authenticated review
records. Neither an HTTP reviewer_id nor finding an existing source is approval.
ReviewVerifier.verify must look up the exact prior decision from its trusted
review authority; do not implement it as a model-supplied boolean check.

Rows use control_key("memory-subject", scope.key, subject), "memory-claim",
"memory-revocation" and "memory-projection". Subject state is a bounded (24 claim)
current-head directory, not semantic search. Claim versions/revocations and
projection coverage are immutable and share the BehaviorStore guard. Revoke
advances subject sequence immediately; old projections cannot reactivate a head.
Source current versions are checked again inside each mutation's EGT.

No Foundry Memory calls, native extraction receipts, deployment or policy
publication occur here. An external projection consumer must preserve this
lineage and separately verify native receipts. Missing required facts, stale
sources, unknown token counts and incomplete necessary graphs fail closed.
ContextResult includes real original bytes, never just a hash-only manifest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Literal, Protocol

from pydantic import Field, model_validator

from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest, Digest
from .errors import Problem
from .models import Identifier
from .records import Record
from .source_materials import (
    AuthenticatedSourceContext, ExactSource,
    SourceMaterials, SourceModel, SourceReference, SourceScope,
    authorize, canonical, checked, control_row, control_write, digest, utc,
)

MAX_SUBJECT_CLAIMS = 24

__all__ = [
    "ClaimCandidate", "ClaimPlan", "ClaimReference", "ClaimRevocation", "ContextManifest",
    "ContextResult", "ContextSection", "ContextVersions", "MemoryGovernance",
    "ProjectionInput", "ProjectionRecord", "ProjectionVerifier", "ReviewAttestation",
    "ReviewedClaim", "ReviewVerifier", "SubjectState", "claim_digest",
]


class ClaimCandidate(SourceModel):
    claim_id: Identifier
    version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    subject: Identifier
    visibility: Literal["team", "private"] = "private"
    purpose: Identifier
    kind: Literal["profile", "episode", "note"]
    text: Annotated[str, Field(min_length=1, max_length=8192)]
    supporting: Annotated[tuple[SourceReference, ...], Field(min_length=1, max_length=12)]
    effective_from: datetime
    expires_at: datetime | None = None
    supersedes: Digest | None = None

    @model_validator(mode="after")
    def effective_interval(self):
        utc(self.effective_from)
        if self.expires_at is not None and utc(self.expires_at) <= utc(self.effective_from):
            raise ValueError("claim expires before becoming effective")
        if len({item.key for item in self.supporting}) != len(self.supporting):
            raise ValueError("duplicate supporting source")
        if (self.version == 1) != (self.supersedes is None):
            raise ValueError("every correction must name the previous claim digest")
        return self


class ClaimReference(SourceModel):
    scope: SourceScope
    subject: Identifier
    claim_id: Identifier
    version: Annotated[int, Field(ge=1)]
    sha256: Digest

    @property
    def key(self) -> str:
        return control_key("memory-claim", self.scope.key, self.subject, self.claim_id, str(self.version))


class ReviewAttestation(SourceModel):
    decision_id: Identifier
    reviewer_id: Identifier
    reviewer_kind: Literal["human", "rule", "independent-model"]
    policy_version: Identifier
    candidate_sha256: Digest
    evidence_sha256: Digest
    reviewed_checks: tuple[Literal["necessity", "support", "contradiction", "sensitivity", "consent"], ...]
    outcome: Literal["approved"]
    authority_record_sha256: Digest | None = None


class ReviewVerifier(Protocol):
    def review(self, context: AuthenticatedSourceContext, candidate: ClaimCandidate,
               originals: Mapping[str, ExactSource]) -> ReviewAttestation: ...

    def verify(self, context: AuthenticatedSourceContext, candidate: ClaimCandidate,
               attestation: ReviewAttestation) -> None: ...

    def predicate_keys(self, context: AuthenticatedSourceContext, candidate: ClaimCandidate,
                       attestation: ReviewAttestation) -> tuple[str, ...]: ...

    def verify_snapshot(self, context: AuthenticatedSourceContext, candidate: ClaimCandidate,
                        attestation: ReviewAttestation, rows: Mapping[str, Record | None]) -> None:
        """Pure decision over the same guarded snapshot, including revocations."""
        ...

    def authorize_revocation(self, context: AuthenticatedSourceContext, reference: ClaimReference,
                             reason: str) -> str:
        """Return an exact trusted authorization receipt ID, or raise Problem."""
        ...


class ClaimPlan(SourceModel):
    candidate: ClaimCandidate
    reference: ClaimReference
    review: ReviewAttestation
    request: ControlRequest


class ReviewedClaim(SourceModel):
    schema_version: Literal[1] = 1
    reference: ClaimReference
    candidate: ClaimCandidate
    review: ReviewAttestation
    sequence: Annotated[int, Field(ge=1)]
    request: ControlRequest

    @property
    def human_reviewed(self) -> bool:
        return self.review.reviewer_kind == "human"


class ClaimHead(SourceModel):
    reference: ClaimReference
    status: Literal["active", "revoked"]
    sequence: Annotated[int, Field(ge=1)]


class SubjectState(SourceModel):
    scope: SourceScope
    subject: Identifier
    sequence: Annotated[int, Field(ge=0)]
    heads: Annotated[tuple[ClaimHead, ...], Field(max_length=MAX_SUBJECT_CLAIMS)] = ()

    @model_validator(mode="after")
    def unique_heads(self):
        if len({head.reference.claim_id for head in self.heads}) != len(self.heads):
            raise ValueError("duplicate current claim")
        if any(head.reference.scope != self.scope or head.reference.subject != self.subject
               or head.sequence > self.sequence for head in self.heads):
            raise ValueError("head scope/sequence differs")
        return self

    @property
    def key(self):
        return control_key("memory-subject", self.scope.key, self.subject)


class ClaimRevocation(SourceModel):
    reference: ClaimReference
    reason: Annotated[str, Field(min_length=1, max_length=2048)]
    authorization_receipt_id: Identifier
    revoked_by: Identifier
    sequence: Annotated[int, Field(ge=1)]
    revoked_at: datetime


class ProjectionInput(SourceModel):
    projection_id: Identifier
    subject: Identifier
    visibility: Literal["team", "private"] = "private"
    covered_sequence: Annotated[int, Field(ge=1)]
    claims: Annotated[tuple[ClaimReference, ...], Field(min_length=1, max_length=MAX_SUBJECT_CLAIMS)]
    sources: Annotated[tuple[SourceReference, ...], Field(min_length=1, max_length=24)]
    projector_version: Identifier
    model_version: Identifier
    schema_id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=16384)]


class ProjectionRecord(SourceModel):
    schema_version: Literal[1] = 1
    scope: SourceScope
    projection: ProjectionInput
    source_fingerprint: Digest
    token_count: Annotated[int, Field(ge=1)]
    byte_length: Annotated[int, Field(ge=1)]
    request: ControlRequest


class ContextVersions(SourceModel):
    work_revision: Annotated[int, Field(ge=1)]
    release_version: Identifier
    catalog_version: Identifier
    memory_schema_version: Identifier


class ContextSection(SourceModel):
    kind: Literal["claim", "original", "projection"]
    key: Identifier
    sha256: Digest
    byte_length: Annotated[int, Field(ge=0)]
    token_count: Annotated[int, Field(ge=0)]


class ContextManifest(SourceModel):
    schema_version: Literal[1] = 1
    scope: SourceScope
    subject: Identifier
    covered_sequence: Annotated[int, Field(ge=1)]
    versions: ContextVersions
    claims: tuple[ClaimReference, ...]
    sources: tuple[SourceReference, ...]
    source_covered_sequences: dict[str, int]
    source_access_revisions: dict[str, int]
    sections: tuple[ContextSection, ...]
    total_tokens: Annotated[int, Field(ge=1)]
    total_bytes: Annotated[int, Field(ge=1)]
    omissions: tuple[str, ...]
    exact_source_fallback: bool

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


@dataclass(frozen=True)
class ContextResult:
    manifest: ContextManifest
    claims: tuple[ReviewedClaim, ...]
    originals: Mapping[str, bytes]
    summary: str | None


ProjectionVerifier = Callable[[ProjectionInput, tuple[ReviewedClaim, ...], Mapping[str, ExactSource]], None]


def claim_digest(candidate: ClaimCandidate, scope: SourceScope) -> str:
    return digest({"scope": scope.model_dump(mode="json"), "candidate": candidate.model_dump(mode="json")})


class MemoryGovernance:
    def __init__(self, sources: SourceMaterials, *, clock: Callable[[], datetime],
                 token_counter: Callable[[bytes], int | None], review_verifier: ReviewVerifier | None = None,
                 projection_verifier: ProjectionVerifier | None = None,
                 max_context_tokens: int = 8192, max_context_bytes: int = 1024 * 1024):
        if (type(max_context_tokens) is not int or max_context_tokens < 1
                or type(max_context_bytes) is not int or max_context_bytes < 1):
            raise Problem(422, "MEMORY_INVALID_BUDGET", "positive explicit context bounds are required")
        self.sources, self.store, self.read_snapshot = sources, sources.store, sources.read_snapshot
        self._clock, self._count = clock, token_counter
        self._review, self._projection_review = review_verifier, projection_verifier
        self._max_tokens, self._max_bytes = max_context_tokens, max_context_bytes

    def _state(self, context, scope, subject, rows=None) -> SubjectState:
        authorize(context, scope)
        key = control_key("memory-subject", scope.key, subject)
        if rows is None:
            rows = self.read_snapshot((key,))
        state = control_row(rows, key, SubjectState)
        if state.scope != scope or state.subject != subject:
            raise Problem(403, "MEMORY_SCOPE_DENIED", "subject authority scope differs")
        return state

    def _source_set(self, context, references, scope) -> dict[str, ExactSource]:
        found, pending = {}, list(references)
        while pending:
            reference = pending.pop()
            if reference.key in found:
                if found[reference.key].event.reference != reference:
                    raise Problem(409, "MEMORY_FABRICATED_REFERENCE", "conflicting source citation")
                continue
            if len(found) >= 24:
                raise Problem(413, "MEMORY_SOURCE_LIMIT", "necessary source graph exceeds its bound")
            if reference.scope.visibility == "private" and reference.scope != scope:
                raise Problem(403, "MEMORY_SCOPE_DENIED", "claim scope would disclose a private original")
            exact = self.sources.read_exact(context, reference, require_current=True)
            found[reference.key] = exact
            pending.extend(exact.event.necessary)
        return found

    def _source_predicates(self, rows, originals):
        self.sources.guard_sources(rows, originals, require_current=True)

    def _source_keys(self, originals):
        return list(self.sources.source_read_keys(originals))

    def _tokens(self, data: bytes) -> int:
        count = self._count(data)
        if type(count) is not int or count < 0 or (data and count == 0):
            raise Problem(503, "MEMORY_TOKEN_COUNT_UNKNOWN", "necessary token count is unavailable; no zero fallback")
        return count

    def _review_checks(self, context, candidate, scope, review):
        if self._review is None:
            raise Problem(503, "MEMORY_REVIEW_UNAVAILABLE", "trusted review authority is not configured")
        review = checked(review, ReviewAttestation)
        if (review.candidate_sha256 != claim_digest(candidate, scope)
                or set(review.reviewed_checks) != {"necessity", "support", "contradiction", "sensitivity", "consent"}
                or len(review.reviewed_checks) != 5):
            raise Problem(403, "MEMORY_REVIEW_REJECTED", "review does not cover this exact claim and required checks")
        self._review.verify(context, candidate, review)

    def _review_keys(self, context, candidate, review):
        if self._review is None or not callable(getattr(self._review, "predicate_keys", None)):
            raise Problem(503, "MEMORY_REVIEW_UNAVAILABLE", "same-authority review predicates are required")
        return list(self._review.predicate_keys(context, candidate, review))

    def _review_predicates(self, context, rows, candidate, review):
        if self._review is None or not callable(getattr(self._review, "verify_snapshot", None)):
            raise Problem(503, "MEMORY_REVIEW_UNAVAILABLE", "same-authority review validation is required")
        self._review.verify_snapshot(context, candidate, review, rows)

    def plan_claim(self, context: AuthenticatedSourceContext, candidate: ClaimCandidate) -> ClaimPlan:
        candidate = checked(candidate, ClaimCandidate)
        scope = context.scope(candidate.visibility)
        authorize(context, scope, candidate.purpose)
        if self._review is None:
            raise Problem(503, "MEMORY_REVIEW_UNAVAILABLE", "source existence is not a review or human approval")
        originals = self._source_set(context, candidate.supporting, scope)
        review = self._review.review(context, candidate, MappingProxyType(originals))
        self._review_checks(context, candidate, scope, review)
        reference = ClaimReference(scope=scope, subject=candidate.subject, claim_id=candidate.claim_id,
                                   version=candidate.version, sha256=claim_digest(candidate, scope))
        state_key = control_key("memory-subject", scope.key, candidate.subject)
        request = ControlRequest(
            request_id=control_key("memory-review", reference.key),
            request_digest=digest([reference.model_dump(mode="json"), review.model_dump(mode="json")]),
            read_set=tuple(dict.fromkeys([state_key, reference.key] + self._source_keys(originals)
                                        + self._review_keys(context, candidate, review))),
        )
        return ClaimPlan(candidate=candidate, reference=reference, review=review, request=request)

    def commit_claim(self, context: AuthenticatedSourceContext, plan: ClaimPlan) -> Record:
        plan = checked(plan, ClaimPlan)
        candidate, reference = plan.candidate, plan.reference
        scope = context.scope(candidate.visibility)
        authorize(context, scope, candidate.purpose)
        if (reference.scope != scope or reference.subject != candidate.subject
                or reference.claim_id != candidate.claim_id or reference.version != candidate.version
                or reference.sha256 != claim_digest(candidate, scope)):
            raise Problem(403, "MEMORY_SCOPE_DENIED", "claim plan identity differs")
        self._review_checks(context, candidate, scope, plan.review)
        originals = self._source_set(context, candidate.supporting, scope)
        state_key = control_key("memory-subject", scope.key, candidate.subject)
        expected = ControlRequest(
            request_id=control_key("memory-review", reference.key),
            request_digest=digest([reference.model_dump(mode="json"), plan.review.model_dump(mode="json")]),
            read_set=tuple(dict.fromkeys([state_key, reference.key] + self._source_keys(originals)
                                        + self._review_keys(context, candidate, plan.review))),
        )
        if plan.request != expected:
            raise Problem(409, "MEMORY_REQUEST_MISMATCH", "retain the original reviewed claim request")
        if candidate.expires_at is not None and utc(self._clock()) >= utc(candidate.expires_at):
            raise Problem(409, "MEMORY_CLAIM_EXPIRED", "expired claims cannot become current")

        def decide(rows):
            self._source_predicates(rows, originals)
            self._review_predicates(context, rows, candidate, plan.review)
            state = (SubjectState(scope=scope, subject=candidate.subject, sequence=0)
                     if rows[state_key] is None else self._state(context, scope, candidate.subject, rows))
            prior = next((head for head in state.heads if head.reference.claim_id == candidate.claim_id), None)
            if prior is None:
                if candidate.version != 1 or candidate.supersedes is not None:
                    raise Problem(409, "MEMORY_VERSION_CONFLICT", "first claim version must be one")
                if len(state.heads) == MAX_SUBJECT_CLAIMS:
                    raise Problem(413, "MEMORY_SUBJECT_LIMIT", "subject claim directory is full")
            elif (candidate.version != prior.reference.version + 1
                  or candidate.supersedes != prior.reference.sha256):
                raise Problem(409, "MEMORY_VERSION_CONFLICT", "correction must supersede the exact prior claim")
            sequence = state.sequence + 1
            claim = ReviewedClaim(reference=reference, candidate=candidate, review=plan.review,
                                  sequence=sequence, request=plan.request)
            head = ClaimHead(reference=reference, status="active", sequence=sequence)
            heads = tuple(item for item in state.heads if item.reference.claim_id != candidate.claim_id) + (head,)
            updated = SubjectState(scope=scope, subject=candidate.subject, sequence=sequence, heads=heads)
            return ControlChanges(writes=(
                control_write(reference.key, claim),
                control_write(state_key, updated, "create" if rows[state_key] is None else "replace"),
            ), result={"claim": reference.model_dump(mode="json"), "sequence": sequence})

        return self.store.transact(plan.request, decide)

    def reconcile_claim(self, context: AuthenticatedSourceContext, plan: ClaimPlan) -> Record:
        plan = checked(plan, ClaimPlan)
        authorize(context, plan.reference.scope, plan.candidate.purpose)
        return self.store.reconcile(plan.request)

    def _active(self, state, claim_id) -> ClaimHead:
        head = next((item for item in state.heads if item.reference.claim_id == claim_id), None)
        if head is None:
            raise Problem(409, "MEMORY_REQUIRED_FACT_MISSING", "a required reviewed fact is absent", claim_id=claim_id)
        if head.status != "active":
            raise Problem(409, "MEMORY_CLAIM_REVOKED", "a required reviewed fact has been revoked", claim_id=claim_id)
        return head

    def _effective(self, candidate):
        now = utc(self._clock())
        if now < utc(candidate.effective_from):
            raise Problem(409, "MEMORY_CLAIM_NOT_EFFECTIVE", "required claim is not yet effective")
        if candidate.expires_at is not None and now >= utc(candidate.expires_at):
            raise Problem(409, "MEMORY_CLAIM_EXPIRED", "required claim has expired")

    def _claim(self, context, rows, head, *, require_effective=True):
        claim = control_row(rows, head.reference.key, ReviewedClaim)
        if claim.reference != head.reference or claim.sequence != head.sequence:
            raise Problem(409, "MEMORY_CLAIM_STALE", "claim is not the authoritative current version")
        authorize(context, claim.reference.scope, claim.candidate.purpose)
        if claim.reference.sha256 != claim_digest(claim.candidate, claim.reference.scope):
            raise Problem(502, "MEMORY_CLAIM_CORRUPT", "reviewed claim content digest differs")
        self.sources.verify_receipt(rows[head.reference.key], claim.request, head.reference.key)
        if require_effective:
            self._effective(claim.candidate)
            self._review_checks(context, claim.candidate, claim.reference.scope, claim.review)
        return claim

    def get_current(self, context: AuthenticatedSourceContext, *, scope: SourceScope,
                    subject: str, claim_id: str) -> ReviewedClaim:
        state = self._state(context, scope, subject)
        head = self._active(state, claim_id)
        rows = self.read_snapshot((state.key, head.reference.key))
        if self._state(context, scope, subject, rows) != state:
            raise Problem(409, "MEMORY_CLAIM_STALE", "subject changed during exact lookup")
        claim = self._claim(context, rows, head)
        originals = self._source_set(context, claim.candidate.supporting, scope)
        final = self.read_snapshot(tuple(dict.fromkeys([state.key] + self._source_keys(originals)
                                  + self._review_keys(context, claim.candidate, claim.review))))
        if self._state(context, scope, subject, final) != state:
            raise Problem(409, "MEMORY_CLAIM_STALE", "claim was invalidated during source verification")
        self._source_predicates(final, originals)
        self._review_predicates(context, final, claim.candidate, claim.review)
        self._effective(claim.candidate)
        return claim

    def revoke(self, context: AuthenticatedSourceContext, reference: ClaimReference, *, reason: str) -> Record:
        reference = checked(reference, ClaimReference)
        authorize(context, reference.scope)
        if self._review is None:
            raise Problem(503, "MEMORY_REVIEW_UNAVAILABLE", "trusted revocation authorization is unavailable")
        authorization = self._review.authorize_revocation(context, reference, reason)
        state_key = control_key("memory-subject", reference.scope.key, reference.subject)
        key = control_key("memory-revocation", reference.key)
        request = ControlRequest(request_id=control_key("memory-revoke", reference.key),
                                 request_digest=digest([reference.model_dump(mode="json"), reason, authorization]),
                                 read_set=(state_key, reference.key, key))

        def decide(rows):
            state = self._state(context, reference.scope, reference.subject, rows)
            head = self._active(state, reference.claim_id)
            if head.reference != reference:
                raise Problem(409, "MEMORY_CLAIM_STALE", "revocation must name the current exact claim")
            self._claim(context, rows, head, require_effective=False)
            sequence = state.sequence + 1
            revoked = ClaimRevocation(reference=reference, reason=reason, authorization_receipt_id=authorization,
                                      revoked_by=context.actor_id, sequence=sequence, revoked_at=utc(self._clock()))
            heads = tuple(ClaimHead(reference=item.reference, status="revoked", sequence=sequence)
                          if item.reference == reference else item for item in state.heads)
            return ControlChanges(writes=(
                control_write(key, revoked),
                control_write(state_key, SubjectState(scope=state.scope, subject=state.subject,
                                                     sequence=sequence, heads=heads), "replace"),
            ), result={"claim": reference.model_dump(mode="json"), "state": "revoked", "sequence": sequence})

        return self.store.transact(request, decide)

    def _projection_sources(self, originals):
        return tuple(item.event.reference for _, item in sorted(originals.items()))

    def record_projection(self, context: AuthenticatedSourceContext, projection: ProjectionInput) -> Record:
        projection = checked(projection, ProjectionInput)
        scope = context.scope(projection.visibility)
        state = self._state(context, scope, projection.subject)
        if state.sequence != projection.covered_sequence:
            raise Problem(409, "MEMORY_PROJECTION_STALE", "late projection cannot replace newer authority")
        active = tuple(head.reference for head in state.heads if head.status == "active")
        if (len(set(item.key for item in projection.claims)) != len(projection.claims)
                or sorted(item.key for item in active) != sorted(item.key for item in projection.claims)):
            raise Problem(409, "MEMORY_PROJECTION_LINEAGE", "projection must cover the current active claim set")
        rows = self.read_snapshot(tuple([state.key] + [item.key for item in active]))
        if self._state(context, scope, projection.subject, rows) != state:
            raise Problem(409, "MEMORY_PROJECTION_STALE", "projection authority changed")
        claims = tuple(self._claim(context, rows, self._active(state, item.claim_id)) for item in active)
        if sorted(item.model_dump_json() for item in projection.claims) != sorted(
                item.reference.model_dump_json() for item in claims):
            raise Problem(409, "MEMORY_PROJECTION_LINEAGE", "fabricated or superseded claim citation")
        originals = self._source_set(context, tuple(ref for claim in claims for ref in claim.candidate.supporting), scope)
        expected_sources = self._projection_sources(originals)
        if sorted(item.model_dump_json() for item in projection.sources) != sorted(
                item.model_dump_json() for item in expected_sources):
            raise Problem(409, "MEMORY_PROJECTION_LINEAGE", "projection source versions are incomplete or fabricated")
        if self._projection_review is None:
            raise Problem(503, "MEMORY_PROJECTION_REVIEW_UNAVAILABLE", "trusted summary support validation is required")
        self._projection_review(projection, claims, MappingProxyType(originals))
        text = projection.text.encode("utf-8")
        tokens = self._tokens(text)
        key = control_key("memory-projection", scope.key, projection.subject, projection.projection_id)
        review_keys = [key for claim in claims for key in self._review_keys(context, claim.candidate, claim.review)]
        request = ControlRequest(
            request_id=control_key("memory-project", key), request_digest=digest(projection.model_dump(mode="json")),
            read_set=tuple(dict.fromkeys([state.key, key] + self._source_keys(originals) + review_keys)),
        )
        record = ProjectionRecord(scope=scope, projection=projection,
                                  source_fingerprint=self.sources.evidence_digest(originals),
                                  token_count=tokens, byte_length=len(text), request=request)

        def decide(snapshot):
            if self._state(context, scope, projection.subject, snapshot) != state:
                raise Problem(409, "MEMORY_PROJECTION_STALE", "claim revocation/correction won the projection race")
            self._source_predicates(snapshot, originals)
            for claim in claims:
                self._review_predicates(context, snapshot, claim.candidate, claim.review)
            return ControlChanges(writes=(control_write(key, record),),
                                  result={"projection_key": key, "covered_sequence": state.sequence})

        return self.store.transact(request, decide)

    def assemble(self, context: AuthenticatedSourceContext, *, scope: SourceScope, subject: str,
                 required_claim_ids: tuple[str, ...], versions: ContextVersions,
                 projection_id: str | None = None) -> ContextResult:
        versions = checked(versions, ContextVersions)
        if (not required_claim_ids or len(required_claim_ids) > MAX_SUBJECT_CLAIMS
                or len(set(required_claim_ids)) != len(required_claim_ids)):
            raise Problem(409, "MEMORY_REQUIRED_FACT_MISSING", "explicit bounded necessary fact IDs are required")
        state = self._state(context, scope, subject)
        heads = tuple(self._active(state, key) for key in required_claim_ids)
        rows = self.read_snapshot(tuple([state.key] + [head.reference.key for head in heads]))
        if self._state(context, scope, subject, rows) != state:
            raise Problem(409, "MEMORY_CLAIM_STALE", "context authority changed")
        claims = tuple(self._claim(context, rows, head) for head in heads)
        originals = self._source_set(context, tuple(ref for claim in claims for ref in claim.candidate.supporting), scope)
        sections, omissions, summary = [], [], None
        for claim in claims:
            data = canonical(claim.candidate.model_dump(mode="json"))
            sections.append(ContextSection(kind="claim", key=claim.reference.key, sha256=digest(
                claim.candidate.model_dump(mode="json")), byte_length=len(data), token_count=self._tokens(data)))
        for key, exact in sorted(originals.items()):
            sections.append(ContextSection(kind="original", key=key, sha256=exact.event.reference.sha256,
                                           byte_length=len(exact.original), token_count=self._tokens(exact.original)))
        if projection_id is not None:
            key = control_key("memory-projection", scope.key, subject, projection_id)
            projection_rows = self.read_snapshot((key,))
            if projection_rows[key] is None:
                omissions.append("projection-missing: using exact reviewed claims and original sources")
            else:
                projection = control_row(projection_rows, key, ProjectionRecord)
                refs = self._projection_sources(originals)
                if (projection.scope != scope or projection.projection.subject != subject):
                    raise Problem(403, "MEMORY_SCOPE_DENIED", "projection scope differs")
                if projection.projection.covered_sequence != state.sequence:
                    omissions.append("projection-stale: using exact reviewed claims and original sources")
                elif (sorted(item.model_dump_json() for item in projection.projection.claims)
                      != sorted(head.reference.model_dump_json() for head in heads)
                      or projection.source_fingerprint != self.sources.evidence_digest(originals)
                      or sorted(item.model_dump_json() for item in projection.projection.sources)
                      != sorted(item.model_dump_json() for item in refs)):
                    omissions.append("projection-coverage-mismatch: using exact sources")
                else:
                    self.sources.verify_receipt(projection_rows[key], projection.request, key)
                    summary = projection.projection.text
                    data = summary.encode("utf-8")
                    tokens = self._tokens(data)
                    if tokens != projection.token_count or len(data) != projection.byte_length:
                        raise Problem(409, "MEMORY_PROJECTION_STALE", "projection tokenization or bytes changed")
                    sections.append(ContextSection(kind="projection", key=key, sha256=hashlib.sha256(data).hexdigest(),
                                                   byte_length=len(data), token_count=tokens))
        total_tokens = sum(section.token_count for section in sections)
        total_bytes = sum(section.byte_length for section in sections)
        if total_tokens > self._max_tokens or total_bytes > self._max_bytes:
            # A projection is the only optional section in this bounded slice.
            if summary is not None:
                optional = sections.pop()
                total_tokens -= optional.token_count
                total_bytes -= optional.byte_length
                summary = None
                omissions.append("projection-budget: preserved necessary originals and claims")
            if total_tokens > self._max_tokens or total_bytes > self._max_bytes:
                raise Problem(413, "MEMORY_REQUIRED_CONTEXT_TOO_LARGE", "necessary facts do not fit; no truncation")
        review_keys = [key for claim in claims for key in self._review_keys(context, claim.candidate, claim.review)]
        final = self.read_snapshot(tuple(dict.fromkeys([state.key] + self._source_keys(originals) + review_keys)))
        if self._state(context, scope, subject, final) != state:
            raise Problem(409, "MEMORY_CLAIM_STALE", "context was invalidated during assembly")
        self._source_predicates(final, originals)
        for claim in claims:
            self._review_predicates(context, final, claim.candidate, claim.review)
            self._effective(claim.candidate)
        manifest = ContextManifest(
            scope=scope, subject=subject, covered_sequence=state.sequence, versions=versions,
            claims=tuple(claim.reference for claim in claims), sources=self._projection_sources(originals),
            source_covered_sequences={item.event.reference.scope.key: max(
                other.event.sequence for other in originals.values()
                if other.event.reference.scope == item.event.reference.scope) for item in originals.values()},
            source_access_revisions={key: item.access_revision for key, item in originals.items()},
            sections=tuple(sections), total_tokens=total_tokens, total_bytes=total_bytes,
            omissions=tuple(omissions), exact_source_fallback=summary is None,
        )
        return ContextResult(manifest, claims, MappingProxyType({key: item.original for key, item in originals.items()}),
                             summary)
