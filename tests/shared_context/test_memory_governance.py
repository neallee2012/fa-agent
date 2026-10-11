"""Offline memory lineage/authority tests; no native Memory or model calls."""

from __future__ import annotations

from datetime import timedelta

import pytest

from test_source_materials import (
    CONTEXT, NOW, OTHER, admitted, code, input_source, source_service,
)
from test_behavior_store import FakeTable, SCOPE
from shared_context.behavior_store import BehaviorStore
from shared_context.errors import Problem
from shared_context.memory_governance import (
    ClaimCandidate, ClaimPlan, ContextVersions, MemoryGovernance, ProjectionInput,
    ReviewAttestation, claim_digest,
)
from shared_context.source_materials import SourceMaterials, control_key, digest

CHECKS = ("necessity", "support", "contradiction", "sensitivity", "consent")
VERSIONS = ContextVersions(work_revision=1, release_version="release-v1", catalog_version="catalog-v1",
                           memory_schema_version="claims-v1")


class ReviewAuthority:
    """A test-only authenticated review ledger; existence is not an approval."""

    def __init__(self, reviewer_kind="human"):
        self.decisions = {}
        self.allow = True
        self.reviewer_kind = reviewer_kind

    def review(self, context, candidate, originals):
        if not self.allow or context.actor_id != CONTEXT.actor_id or not originals:
            raise Problem(403, "REVIEW_DENIED", "synthetic reviewer denied this claim")
        sha = claim_digest(candidate, context.scope(candidate.visibility))
        result = ReviewAttestation(
            decision_id=sha, reviewer_id="reviewer-one", reviewer_kind=self.reviewer_kind,
            policy_version="review-v1", candidate_sha256=sha, evidence_sha256="e" * 64,
            reviewed_checks=CHECKS, outcome="approved",
        )
        self.decisions[sha] = result
        return result

    def verify(self, context, candidate, attestation):
        if (not self.allow or context.actor_id != CONTEXT.actor_id
                or self.decisions.get(attestation.decision_id) != attestation):
            raise Problem(403, "REVIEW_UNVERIFIED", "no trusted decision for this exact candidate")

    def authorize_revocation(self, context, reference, reason):
        if not self.allow or context.actor_id != CONTEXT.actor_id:
            raise Problem(403, "REVOKE_DENIED", "synthetic revocation authority denied")
        return digest([reference.model_dump(mode="json"), reason])

    def predicate_keys(self, context, candidate, attestation):
        return ()

    def verify_snapshot(self, context, candidate, attestation, rows):
        self.verify(context, candidate, attestation)


def candidate(reference, **changes):
    return ClaimCandidate(**{
        "claim_id": "dashboard-color", "version": 1, "subject": "human-one",
        "purpose": "profile", "text": "Dashboard preferred color is cobalt.",
        "kind": "profile", "supporting": (reference,),
        "effective_from": NOW - timedelta(days=1), **changes,
    })


@pytest.fixture
def memory_service(source_service):
    sources, table = source_service
    review = ReviewAuthority()
    memory = MemoryGovernance(sources, clock=lambda: NOW, token_counter=lambda data: len(data),
                              review_verifier=review, projection_verifier=lambda *_: None)
    source, _ = admitted(sources, b"The dashboard preference is cobalt.\r\n")
    return memory, source, table, review


def reviewed(memory, source, **changes):
    plan = memory.plan_claim(CONTEXT, candidate(source.reference, **changes))
    receipt = memory.commit_claim(CONTEXT, plan)
    return plan, receipt


def assembled(memory, **changes):
    return memory.assemble(CONTEXT, **{
        "scope": CONTEXT.scope("private"), "subject": "human-one",
        "required_claim_ids": ("dashboard-color",), "versions": VERSIONS, **changes,
    })


def projection(memory, plan, **changes):
    return ProjectionInput(**{
        "projection_id": "summary-one", "subject": plan.candidate.subject, "covered_sequence": 1,
        "claims": (plan.reference,), "sources": plan.candidate.supporting,
        "projector_version": "projector-v1", "model_version": "offline-v1", "schema_id": "summary-v1",
        "text": "A dashboard color preference exists.", **changes,
    })


def test_reviewed_claim_is_immutable_scope_and_source_bound_with_exact_context(memory_service):
    memory, source, table, _ = memory_service
    plan, receipt = reviewed(memory, source)
    reconstructed = ClaimPlan.model_validate_json(plan.model_dump_json())
    before = len(table.submissions)
    assert memory.commit_claim(CONTEXT, reconstructed) == receipt
    assert len(table.submissions) == before
    current = memory.get_current(CONTEXT, scope=plan.reference.scope, subject="human-one", claim_id="dashboard-color")
    assert current.human_reviewed and current.candidate.text.endswith("cobalt.")
    context = assembled(memory)
    assert context.originals[source.reference.key] == source.original
    assert context.manifest.sources == (source.reference,)
    assert context.manifest.covered_sequence == 1 and context.manifest.versions == VERSIONS
    assert context.manifest.sha256 and context.manifest.total_tokens > 0
    assert context.manifest.exact_source_fallback


def test_existing_source_is_not_a_review_and_rule_review_is_not_human_approval(memory_service):
    memory, source, _, review = memory_service
    disabled = MemoryGovernance(memory.sources, clock=lambda: NOW, token_counter=len)
    with pytest.raises(Problem) as no_review:
        disabled.plan_claim(CONTEXT, candidate(source.reference))
    assert code(no_review) == "MEMORY_REVIEW_UNAVAILABLE"
    review.allow = False
    with pytest.raises(Problem) as denied:
        memory.plan_claim(CONTEXT, candidate(source.reference))
    assert code(denied) == "REVIEW_DENIED"
    review.allow, review.reviewer_kind = True, "rule"
    plan, _ = reviewed(memory, source)
    assert not memory.get_current(CONTEXT, scope=plan.reference.scope, subject="human-one",
                                  claim_id="dashboard-color").human_reviewed


def test_fabricated_review_and_incomplete_checks_cannot_be_committed(memory_service):
    memory, source, table, _ = memory_service
    plan = memory.plan_claim(CONTEXT, candidate(source.reference))
    before = len(table.submissions)
    forged = plan.model_copy(update={"review": plan.review.model_copy(update={"decision_id": "invented"})})
    with pytest.raises(Problem) as no_approval:
        memory.commit_claim(CONTEXT, forged)
    assert code(no_approval) == "REVIEW_UNVERIFIED"
    omitted = plan.model_copy(update={"review": plan.review.model_copy(update={"reviewed_checks": ("support",)})})
    with pytest.raises(Problem) as checks:
        memory.commit_claim(CONTEXT, omitted)
    assert code(checks) == "MEMORY_REVIEW_REJECTED"
    assert len(table.submissions) == before


def test_fake_citation_and_private_scope_leak_fail_before_review(memory_service):
    memory, source, _, review = memory_service
    invalid = source.reference.model_copy(update={"sha256": "f" * 64})
    with pytest.raises(Problem) as fabricated:
        memory.plan_claim(CONTEXT, candidate(invalid))
    assert code(fabricated) == "SOURCE_REFERENCE_MISMATCH"
    assert review.decisions == {}
    private, _ = admitted(memory.sources, b"private color preference", source_id="private", visibility="private")
    with pytest.raises(Problem) as leak:
        memory.plan_claim(CONTEXT, candidate(private.reference, visibility="team"))
    assert code(leak) == "MEMORY_SCOPE_DENIED"


def test_source_correction_rejects_stale_claim_and_stale_summary(memory_service):
    memory, source, _, _ = memory_service
    plan, _ = reviewed(memory, source)
    memory.record_projection(CONTEXT, projection(memory, plan))
    admitted(memory.sources, b"Now the preference is amber.", version=2)
    for operation in (
        lambda: assembled(memory, projection_id="summary-one"),
        lambda: memory.record_projection(CONTEXT, projection(memory, plan, projection_id="late")),
        lambda: memory.get_current(CONTEXT, scope=plan.reference.scope, subject="human-one", claim_id="dashboard-color"),
    ):
        with pytest.raises(Problem) as stale:
            operation()
        assert code(stale) == "SOURCE_STALE"


def test_supersession_preserves_history_and_only_new_version_is_current(memory_service):
    memory, source, _, _ = memory_service
    first, _ = reviewed(memory, source)
    second, _ = reviewed(memory, source, version=2, supersedes=first.reference.sha256,
                         text="The explicitly reviewed appearance preference is cobalt.")
    context = assembled(memory)
    assert context.manifest.claims == (second.reference,)
    historical = memory.read_snapshot((first.reference.key,))[first.reference.key]
    assert historical.payload["data"]["candidate"]["text"] == first.candidate.text
    with pytest.raises(Problem) as old:
        memory.revoke(CONTEXT, first.reference, reason="old revision")
    assert code(old) == "MEMORY_CLAIM_STALE"


def test_revocation_blocks_context_and_late_projection_cannot_reinstate(memory_service):
    memory, source, table, _ = memory_service
    plan, _ = reviewed(memory, source)
    old = projection(memory, plan)
    memory.record_projection(CONTEXT, old)
    receipt = memory.revoke(CONTEXT, plan.reference, reason="consent withdrawn")
    count = len(table.submissions)
    assert memory.revoke(CONTEXT, plan.reference, reason="consent withdrawn") == receipt
    assert len(table.submissions) == count
    with pytest.raises(Problem) as revoked:
        assembled(memory, projection_id="summary-one")
    assert code(revoked) == "MEMORY_CLAIM_REVOKED"
    with pytest.raises(Problem) as late:
        memory.record_projection(CONTEXT, old.model_copy(update={"projection_id": "late"}))
    assert code(late) == "MEMORY_PROJECTION_STALE"
    assert memory.sources.read_exact(CONTEXT, source.reference).original == source.original


@pytest.mark.parametrize("delta,expected", [(timedelta(seconds=1), "MEMORY_CLAIM_EXPIRED"),
                                            (timedelta(days=-2), "MEMORY_CLAIM_NOT_EFFECTIVE")])
def test_effective_time_and_expiry_fail_closed(memory_service, delta, expected):
    memory, source, _, _ = memory_service
    reviewed(memory, source, expires_at=NOW + timedelta(seconds=1))
    memory._clock = lambda: NOW + delta
    with pytest.raises(Problem) as expired:
        assembled(memory)
    assert code(expired) == expected


def test_expired_claim_can_still_be_immutably_revoked(memory_service):
    memory, source, _, _ = memory_service
    plan, _ = reviewed(memory, source, expires_at=NOW + timedelta(seconds=1))
    memory._clock = lambda: NOW + timedelta(seconds=2)
    assert memory.revoke(CONTEXT, plan.reference, reason="withdraw expired preference").payload["result"]["state"] == "revoked"
    with pytest.raises(Problem) as revoked:
        assembled(memory)
    assert code(revoked) == "MEMORY_CLAIM_REVOKED"


def test_compaction_losing_cobalt_never_loses_original_claim_or_exact_fallback(memory_service):
    memory, source, _, _ = memory_service
    plan, _ = reviewed(memory, source)
    memory.record_projection(CONTEXT, projection(memory, plan))
    compacted = assembled(memory, projection_id="summary-one")
    assert "cobalt" not in compacted.summary
    assert "cobalt" in compacted.claims[0].candidate.text
    assert b"cobalt" in compacted.originals[source.reference.key]
    missing = assembled(memory, projection_id="projection-was-lost")
    assert missing.summary is None and missing.manifest.exact_source_fallback
    assert missing.manifest.omissions and b"cobalt" in missing.originals[source.reference.key]


@pytest.mark.parametrize("mode", ["fake-source", "fake-claim", "missing-source"])
def test_summary_rejects_fabricated_or_incomplete_source_claim_coverage(memory_service, mode):
    memory, source, _, _ = memory_service
    plan, _ = reviewed(memory, source)
    changes = {}
    if mode == "fake-source":
        changes["sources"] = (source.reference.model_copy(update={"sha256": "f" * 64}),)
    elif mode == "fake-claim":
        changes["claims"] = (plan.reference.model_copy(update={"sha256": "f" * 64}),)
    else:
        missing = source.reference.model_copy(update={"source_id": "does-not-exist"})
        changes["sources"] = (missing,)
    with pytest.raises(Problem) as invalid:
        memory.record_projection(CONTEXT, projection(memory, plan, **changes))
    assert code(invalid) == "MEMORY_PROJECTION_LINEAGE"


def test_required_fact_absence_wrong_actor_and_unknown_token_count_are_not_empty_success(memory_service):
    memory, source, _, _ = memory_service
    plan, _ = reviewed(memory, source)
    for claims in ((), ("fabricated-fact",)):
        with pytest.raises(Problem) as missing:
            assembled(memory, required_claim_ids=claims)
        assert code(missing) == "MEMORY_REQUIRED_FACT_MISSING"
    with pytest.raises(Problem) as wrong_actor:
        memory.assemble(OTHER, scope=plan.reference.scope, subject="human-one",
                        required_claim_ids=("dashboard-color",), versions=VERSIONS)
    assert code(wrong_actor) == "SOURCE_SCOPE_DENIED"
    for count in (None, 0, -1, True):
        memory._count = lambda _, count=count: count
        with pytest.raises(Problem) as unknown:
            assembled(memory)
        assert code(unknown) == "MEMORY_TOKEN_COUNT_UNKNOWN"
        with pytest.raises(Problem) as summary:
            memory.record_projection(CONTEXT, projection(memory, plan))
        assert code(summary) == "MEMORY_TOKEN_COUNT_UNKNOWN"


def test_unknown_claim_commit_only_reconciles_same_original_identity(memory_service):
    memory, source, table, _ = memory_service
    plan = memory.plan_claim(CONTEXT, candidate(source.reference))
    table.failure = "after"
    with pytest.raises(Problem) as unknown:
        memory.commit_claim(CONTEXT, plan)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    count = len(table.submissions)
    reconstructed = ClaimPlan.model_validate_json(plan.model_dump_json())
    assert memory.reconcile_claim(CONTEXT, reconstructed).payload["result"]["claim"]["sha256"] == plan.reference.sha256
    assert len(table.submissions) == count


def test_revocation_wins_shared_guard_projection_race_without_partial_projection(memory_service):
    memory, source, table, review = memory_service
    plan, _ = reviewed(memory, source)
    other_sources = SourceMaterials(BehaviorStore(FakeTable(table.backing), SCOPE))
    other = MemoryGovernance(other_sources, clock=lambda: NOW, token_counter=len, review_verifier=review)
    table.before_submit = lambda: other.revoke(CONTEXT, plan.reference, reason="raced revocation")
    with pytest.raises(Problem) as race:
        memory.record_projection(CONTEXT, projection(memory, plan))
    assert code(race) == "BEHAVIOR_CONFLICT"
    key = control_key("memory-projection", plan.reference.scope.key, "human-one", "summary-one")
    assert memory.read_snapshot((key,))[key] is None
    with pytest.raises(Problem) as revoked:
        assembled(memory)
    assert code(revoked) == "MEMORY_CLAIM_REVOKED"


def test_source_changes_during_claim_commit_are_guard_fenced(memory_service):
    memory, source, table, _ = memory_service
    plan = memory.plan_claim(CONTEXT, candidate(source.reference))
    other_sources = SourceMaterials(BehaviorStore(FakeTable(table.backing), SCOPE))
    table.before_submit = lambda: admitted(other_sources, b"new version", version=2)
    with pytest.raises(Problem) as race:
        memory.commit_claim(CONTEXT, plan)
    assert code(race) == "BEHAVIOR_CONFLICT"
    assert memory.read_snapshot((plan.reference.key,))[plan.reference.key] is None


def test_context_does_not_truncate_required_facts_for_budget(memory_service):
    memory, source, _, _ = memory_service
    plan, _ = reviewed(memory, source)
    memory.record_projection(CONTEXT, projection(memory, plan))
    exact = assembled(memory)
    memory._max_tokens = exact.manifest.total_tokens
    without_optional = assembled(memory, projection_id="summary-one")
    assert without_optional.summary is None
    assert any("projection-budget" in item for item in without_optional.manifest.omissions)
    memory._max_tokens = 1
    with pytest.raises(Problem) as too_large:
        assembled(memory)
    assert code(too_large) == "MEMORY_REQUIRED_CONTEXT_TOO_LARGE"


def test_context_does_not_return_a_claim_that_expires_while_counting_its_required_material(memory_service):
    memory, source, _, review = memory_service
    deadline = NOW + timedelta(seconds=1)
    reviewed(memory, source, expires_at=deadline)
    clock = [NOW]

    def count(data):
        clock[0] = deadline
        return len(data)

    reader = MemoryGovernance(memory.sources, clock=lambda: clock[0], token_counter=count, review_verifier=review)
    with pytest.raises(Problem) as expired:
        assembled(reader)
    assert code(expired) == "MEMORY_CLAIM_EXPIRED"


def test_context_detects_missing_transitive_necessary_original(memory_service):
    memory, source, table, _ = memory_service
    dependency, _ = admitted(memory.sources, b"necessary evidence", source_id="dependency")
    root, _ = admitted(memory.sources, b"cobalt", source_id="graph-root", necessary=(dependency.reference,))
    reviewed(memory, root)
    del table.backing.rows[("Behavior", SCOPE.partition_key, "behavior:v1:row:" + dependency.reference.key)]
    with pytest.raises(Problem) as missing:
        assembled(memory)
    assert code(missing) == "SOURCE_REQUIRED_MISSING"


def test_required_memory_is_unchanged_by_long_unrelated_history_and_optional_summary_omission(memory_service):
    memory, source, _, _ = memory_service
    selected, _ = reviewed(memory, source)
    short = assembled(memory)
    previous = None
    for version in range(1, 9):
        extra, _ = admitted(memory.sources, f"Unrelated episode {version}.".encode(), source_id="other-source", version=version)
        previous, _ = reviewed(memory, extra, claim_id="unrelated-episode", kind="episode",
            text=f"Unrelated episode {version}.", version=version,
            supersedes=None if previous is None else previous.reference.sha256,
            expires_at=NOW + timedelta(seconds=1))
    memory._clock = lambda: NOW + timedelta(seconds=1)
    long = assembled(memory, projection_id="not-recorded")
    assert long.claims == short.claims and long.manifest.claims == (selected.reference,)
    assert dict(long.originals) == dict(short.originals)
    assert long.manifest.total_tokens == short.manifest.total_tokens
    assert long.manifest.total_bytes == short.manifest.total_bytes
    assert long.manifest.exact_source_fallback
    assert any("projection-missing" in reason for reason in long.manifest.omissions)
    assert all(b"Unrelated" not in raw for raw in long.originals.values())
