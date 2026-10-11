"""Human decision/withdrawal composition through the real offline control core."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from conftest import TENANT
from test_behavior_store import FakeTable, SCOPE, wire_store
from test_memory_governance import CHECKS, VERSIONS, candidate
from test_source_materials import CONTEXT, NOW, admitted, code, input_source, source_service
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import BehaviorStore
from shared_context.errors import Problem
from shared_context.memory_governance import MemoryGovernance, ProjectionInput
from shared_context.source_materials import (
    AuthenticatedSourceContext, SourceMaterials, SourceWithdrawalInput, canonical,
)
from shared_context.source_reviews import (
    DurableHumanReviewVerifier, HumanReviewCommand, HumanReviewPlan, HumanReviewPolicy,
)

HUMAN = BehaviorPrincipal(TENANT, "dddddddd-dddd-dddd-dddd-dddddddddddd", "team-one", "human",
                          None, frozenset({"read", "approve", "submit"}))
HUMAN_CONTEXT = AuthenticatedSourceContext(TENANT, HUMAN.team, HUMAN.source_actor_id, "human-ingress",
                                            ("work", "profile"))
AGENT = BehaviorPrincipal(TENANT, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "team-one", "agent",
                          "agent-a", frozenset({"read", "submit", "execute"}))
POLICY = HumanReviewPolicy(policy_version="human-policy-v1")


@pytest.fixture
def reviews(source_service):
    sources, table = source_service
    source, _ = admitted(sources, b"The dashboard preference is cobalt.\r\n")
    verifier = DurableHumanReviewVerifier(sources, POLICY, clock=lambda: NOW)
    memory = MemoryGovernance(sources, clock=lambda: NOW, token_counter=len, review_verifier=verifier,
                              projection_verifier=lambda *_: None)
    proposed = candidate(source.reference, visibility="team")
    return sources, table, source, verifier, memory, proposed


def decision(verifier, proposed, *, decision="approve", review_id="human-review-one", reason="Reviewed explicitly"):
    preview = verifier.preview(HUMAN, HUMAN_CONTEXT, proposed)
    return HumanReviewCommand(
        review_id=review_id, candidate=proposed, candidate_sha256=preview.candidate_sha256,
        evidence_sha256=preview.evidence_sha256, policy_version=preview.policy_version,
        decision=decision, reviewed_checks=CHECKS, reason=reason,
    )


def approved(verifier, proposed):
    plan = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, decision(
        verifier, proposed, review_id=control_key("test-review", proposed.model_dump_json())))
    return plan, verifier.commit_decision(HUMAN, HUMAN_CONTEXT, plan)


def current(memory, proposed):
    return memory.get_current(CONTEXT, scope=CONTEXT.scope("team"), subject=proposed.subject,
                              claim_id=proposed.claim_id)


def assembled(memory, proposed, projection_id=None):
    return memory.assemble(CONTEXT, scope=CONTEXT.scope("team"), subject=proposed.subject,
                           required_claim_ids=(proposed.claim_id,), versions=VERSIONS, projection_id=projection_id)


def claimed(verifier, memory, proposed):
    approved(verifier, proposed)
    plan = memory.plan_claim(CONTEXT, proposed)
    memory.commit_claim(CONTEXT, plan)
    return plan


def projected(plan):
    return ProjectionInput(
        projection_id="projection-one", subject=plan.candidate.subject, visibility="team", covered_sequence=1,
        claims=(plan.reference,), sources=plan.candidate.supporting, projector_version="projector-v1",
        model_version="offline", schema_id="schema-v1", text="Dashboard preference is cobalt.",
    )


def withdrawal(source, *, withdrawal_id="withdraw-one"):
    return SourceWithdrawalInput(withdrawal_id=withdrawal_id, reference=source.reference,
                                 expected_access_revision=1, reason="Withdraw source access")


def test_no_constructor_review_or_source_existence_can_mint_human_decisions(reviews):
    sources, table, source, verifier, memory, proposed = reviews
    before = len(table.submissions)
    with pytest.raises(Problem) as missing:
        memory.plan_claim(CONTEXT, proposed)
    assert code(missing) == "HUMAN_REVIEW_MISSING"
    assert len(table.submissions) == before
    preview = verifier.preview(HUMAN, HUMAN_CONTEXT, proposed)
    assert preview.sources == (source.reference,)
    assert preview.source_access_revisions == {source.reference.key: 1}
    assert len(table.submissions) == before
    assert sources.read_exact(CONTEXT, source.reference).original == source.original


def test_explicit_human_decision_is_durable_inline_and_read_by_agent_without_impersonation(reviews):
    sources, table, source, verifier, memory, proposed = reviews
    command = decision(verifier, proposed, reason="人工核對\r\ncobalt，沒有改寫原件。")
    plan = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, command)
    receipt = verifier.commit_decision(HUMAN, HUMAN_CONTEXT, plan)
    assert len(table.submissions[-1]) == 3  # guard + receipt + decision/material
    key = receipt.payload["result"]["decision_key"]
    row = sources.store.read_snapshot((key,))[key]
    assert row.payload["material"]["sha256"] == hashlib.sha256(canonical(plan.material.model_dump(mode="json"))).hexdigest()
    assert row.payload["data"]["human_oid"] == HUMAN.oid
    assert row.payload["data"]["scope"]["owner_actor"] is None
    count = len(table.submissions)
    graph = sources.read_graph(CONTEXT, proposed.supporting, require_current=True)
    attestation = verifier.review(CONTEXT, proposed, graph)
    verifier.verify(CONTEXT, proposed, attestation)
    assert attestation.reviewer_kind == "human" and attestation.reviewer_id == HUMAN.oid
    assert attestation.authority_record_sha256
    assert len(table.submissions) == count
    claim = memory.plan_claim(CONTEXT, proposed)
    memory.commit_claim(CONTEXT, claim)
    assert current(memory, proposed).review == attestation
    context = assembled(memory, proposed)
    assert context.originals[source.reference.key] == source.original
    assert context.manifest.source_access_revisions == {source.reference.key: 1}


@pytest.mark.parametrize("principal,context", [
    (AGENT, CONTEXT),
    (replace(AGENT, actions=frozenset({"read", "approve"})), CONTEXT),
    (replace(HUMAN, actions=frozenset({"read"})), HUMAN_CONTEXT),
    (replace(HUMAN, team="other-team"), HUMAN_CONTEXT),
    (HUMAN, CONTEXT),
    (HUMAN, replace(HUMAN_CONTEXT, tenant_id="other-tenant")),
], ids=["agent", "agent-with-approve", "human-no-approve", "wrong-team", "impersonation", "wrong-tenant"])
def test_only_exact_human_principal_with_approve_and_matching_context_can_decide(reviews, principal, context):
    _, table, _, verifier, _, proposed = reviews
    before = len(table.submissions)
    with pytest.raises(Problem) as denied:
        verifier.preview(principal, context, proposed)
    assert denied.value.status == 403
    assert len(table.submissions) == before


def test_model_boolean_and_incomplete_review_checks_are_invalid(reviews):
    _, _, _, verifier, _, proposed = reviews
    command = decision(verifier, proposed)
    for updates in ({"decision": True}, {"approved": True}, {"reviewed_checks": ("support",)}):
        with pytest.raises(ValidationError):
            HumanReviewCommand.model_validate({**command.model_dump(), **updates})


def test_human_cannot_review_an_agents_private_source_by_changing_candidate_visibility(reviews):
    sources, _, _, verifier, _, _ = reviews
    private, _ = admitted(sources, b"agent-private cobalt", source_id="private", visibility="private")
    for visibility in ("team", "private"):
        proposed = candidate(private.reference, visibility=visibility)
        with pytest.raises(Problem) as denied:
            verifier.preview(HUMAN, HUMAN_CONTEXT, proposed)
        assert code(denied) == "SOURCE_SCOPE_DENIED"
    with pytest.raises(Problem) as denied:
        sources.withdraw_source(HUMAN, HUMAN_CONTEXT, withdrawal(private))
    assert code(denied) == "SOURCE_SCOPE_DENIED"


def test_human_own_private_source_is_not_broadened_to_agent_access(reviews):
    sources, _, _, verifier, _, _ = reviews
    plan = sources.plan(HUMAN_CONTEXT, input_source(source_id="human-private", visibility="private"), b"cobalt")
    sources.commit(HUMAN_CONTEXT, plan)
    proposed = candidate(plan.reference, visibility="private")
    approved(verifier, proposed)
    graph = sources.read_graph(HUMAN_CONTEXT, proposed.supporting, require_current=True)
    assert verifier.review(HUMAN_CONTEXT, proposed, graph).reviewer_id == HUMAN.oid
    with pytest.raises(Problem):
        verifier.verify(CONTEXT, proposed, verifier.review(HUMAN_CONTEXT, proposed, graph))


@pytest.mark.parametrize("failure", ["before", "after"])
def test_unknown_review_request_survives_reconstruction_and_only_reconciles(reviews, failure):
    sources, table, _, verifier, _, proposed = reviews
    plan = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, decision(verifier, proposed))
    table.failure = failure
    with pytest.raises(Problem) as unknown:
        verifier.commit_decision(HUMAN, HUMAN_CONTEXT, plan)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert unknown.value.body["error"]["request_id"] == plan.request.request_id
    count = len(table.submissions)
    rebuilt_sources = SourceMaterials(BehaviorStore(FakeTable(table.backing), SCOPE))
    rebuilt = DurableHumanReviewVerifier(rebuilt_sources, POLICY, clock=lambda: NOW)
    retained = HumanReviewPlan.model_validate_json(plan.model_dump_json())
    if failure == "after":
        assert rebuilt.reconcile_decision(HUMAN, HUMAN_CONTEXT, retained).payload["result"]["decision"] == "approve"
    else:
        with pytest.raises(Problem) as absent:
            rebuilt.reconcile_decision(HUMAN, HUMAN_CONTEXT, retained)
        assert code(absent) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert len(table.submissions) == count


def test_stable_id_mismatch_and_rejection_never_become_implicit_approval(reviews):
    _, table, _, verifier, memory, proposed = reviews
    command = decision(verifier, proposed, decision="reject", reason="Contradictory evidence")
    plan = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, command)
    original = verifier.commit_decision(HUMAN, HUMAN_CONTEXT, plan)
    count = len(table.submissions)
    assert verifier.commit_decision(HUMAN, HUMAN_CONTEXT, plan) == original
    assert len(table.submissions) == count
    changed = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, decision(verifier, proposed))
    with pytest.raises(Problem) as collision:
        verifier.commit_decision(HUMAN, HUMAN_CONTEXT, changed)
    assert code(collision) == "BEHAVIOR_REQUEST_CONFLICT"
    with pytest.raises(Problem) as rejected:
        memory.plan_claim(CONTEXT, proposed)
    assert code(rejected) == "HUMAN_REVIEW_NOT_APPROVED"


def test_review_binds_policy_candidate_bytes_complete_graph_and_actual_evidence(reviews):
    sources, _, source, verifier, _, proposed = reviews
    command = decision(verifier, proposed)
    for changes in ({"candidate_sha256": "f" * 64}, {"evidence_sha256": "f" * 64},
                    {"policy_version": "unapproved-policy"}):
        with pytest.raises(Problem) as mismatch:
            verifier.plan_decision(HUMAN, HUMAN_CONTEXT, command.model_copy(update=changes))
        assert code(mismatch) == "HUMAN_REVIEW_BINDING_CHANGED"
    approved(verifier, proposed)
    originals = sources.read_graph(CONTEXT, proposed.supporting, require_current=True)
    fabricated = {source.reference.key: replace(originals[source.reference.key], original=b"fabricated")}
    with pytest.raises(Problem) as fake_bytes:
        verifier.review(CONTEXT, proposed, fabricated)
    assert code(fake_bytes) == "HUMAN_REVIEW_EVIDENCE_CHANGED"
    dependency, _ = admitted(sources, b"necessary supporting fact", source_id="dependency")
    root, _ = admitted(sources, b"cobalt", source_id="root", necessary=(dependency.reference,))
    with_graph = candidate(root.reference, visibility="team", claim_id="graph-claim")
    approved(verifier, with_graph)
    graph = sources.read_graph(CONTEXT, with_graph.supporting, require_current=True)
    assert set(graph) == {root.reference.key, dependency.reference.key}
    with pytest.raises(Problem) as incomplete:
        verifier.review(CONTEXT, with_graph, {root.reference.key: graph[root.reference.key]})
    assert code(incomplete) == "HUMAN_REVIEW_EVIDENCE_CHANGED"


def test_policy_change_and_new_source_version_invalidate_current_reviewed_claims(reviews):
    sources, _, source, verifier, memory, proposed = reviews
    claimed(verifier, memory, proposed)
    newer_policy = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version="v2"), clock=lambda: NOW)
    with pytest.raises(Problem):
        newer_policy.verify(CONTEXT, proposed, current(memory, proposed).review)
    admitted(sources, b"preference changed to amber", version=2)
    with pytest.raises(Problem) as stale:
        current(memory, proposed)
    assert code(stale) == "SOURCE_STALE"


def test_human_revocation_is_durable_and_blocks_claims_before_head_projection_catches_up(reviews):
    sources, table, _, verifier, memory, proposed = reviews
    plan = claimed(verifier, memory, proposed)
    memory.record_projection(CONTEXT, projected(plan))
    revoke = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, decision(
        verifier, proposed, decision="revoke", review_id="human-revoke", reason="Consent withdrawn"))
    receipt = verifier.commit_decision(HUMAN, HUMAN_CONTEXT, revoke)
    count = len(table.submissions)
    assert verifier.commit_decision(HUMAN, HUMAN_CONTEXT, revoke) == receipt
    assert len(table.submissions) == count
    state_key = control_key("memory-subject", plan.reference.scope.key, proposed.subject)
    assert sources.store.read_snapshot((state_key,))[state_key].payload["data"]["heads"][0]["status"] == "active"
    for action in (lambda: current(memory, proposed), lambda: assembled(memory, proposed, "projection-one"),
                   lambda: memory.record_projection(CONTEXT, projected(plan))):
        with pytest.raises(Problem) as denied:
            action()
        assert code(denied) == "HUMAN_REVIEW_REVOKED"
    with pytest.raises(Problem) as wrong_reason:
        memory.revoke(CONTEXT, plan.reference, reason="model invented reason")
    assert code(wrong_reason) == "HUMAN_REVOCATION_BINDING_CHANGED"
    result = memory.revoke(CONTEXT, plan.reference, reason="Consent withdrawn")
    assert result.payload["result"]["state"] == "revoked"


def test_missing_human_revocation_cannot_be_minted_by_memory_consumer(reviews):
    _, table, _, verifier, memory, proposed = reviews
    plan = claimed(verifier, memory, proposed)
    before = len(table.submissions)
    with pytest.raises(Problem) as absent:
        memory.revoke(CONTEXT, plan.reference, reason="model says revoke")
    assert code(absent) == "HUMAN_REVIEW_MISSING"
    assert len(table.submissions) == before
    assert current(memory, proposed).reference == plan.reference


def test_unknown_human_revocation_blocks_use_and_reconciles_without_resending(reviews):
    _, table, _, verifier, memory, proposed = reviews
    claimed(verifier, memory, proposed)
    revoke = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, decision(
        verifier, proposed, decision="revoke", review_id="unknown-revoke"))
    table.failure = "after"
    with pytest.raises(Problem) as unknown:
        verifier.commit_decision(HUMAN, HUMAN_CONTEXT, revoke)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    count = len(table.submissions)
    with pytest.raises(Problem) as revoked:
        current(memory, proposed)
    assert code(revoked) == "HUMAN_REVIEW_REVOKED"
    retained = HumanReviewPlan.model_validate_json(revoke.model_dump_json())
    assert verifier.reconcile_decision(HUMAN, HUMAN_CONTEXT, retained).payload["result"]["decision"] == "revoke"
    assert len(table.submissions) == count


def test_revocation_before_projection_transaction_is_rechecked_in_shared_snapshot(reviews):
    sources, _, _, verifier, memory, proposed = reviews
    plan = claimed(verifier, memory, proposed)
    revoke = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, decision(
        verifier, proposed, decision="revoke", review_id="revoke-race"))
    done = False
    def count(data):
        nonlocal done
        if not done:
            done = True
            verifier.commit_decision(HUMAN, HUMAN_CONTEXT, revoke)
        return len(data)
    memory._count = count
    with pytest.raises(Problem) as denied:
        memory.record_projection(CONTEXT, projected(plan))
    assert code(denied) == "HUMAN_REVIEW_REVOKED"
    key = control_key("memory-projection", plan.reference.scope.key, proposed.subject, "projection-one")
    assert sources.store.read_snapshot((key,))[key] is None


def test_revocation_during_context_assembly_does_not_return_stale_facts(reviews):
    _, _, _, verifier, memory, proposed = reviews
    claimed(verifier, memory, proposed)
    revoke = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, decision(
        verifier, proposed, decision="revoke", review_id="revoke-context"))
    done = False
    def count(data):
        nonlocal done
        if not done:
            done = True
            verifier.commit_decision(HUMAN, HUMAN_CONTEXT, revoke)
        return len(data)
    memory._count = count
    with pytest.raises(Problem) as denied:
        assembled(memory, proposed)
    assert code(denied) == "HUMAN_REVIEW_REVOKED"


def test_guard_race_between_review_and_source_withdrawal_has_no_partial_approval(reviews):
    sources, table, source, verifier, _, proposed = reviews
    plan = verifier.plan_decision(HUMAN, HUMAN_CONTEXT, decision(verifier, proposed))
    other = SourceMaterials(BehaviorStore(FakeTable(table.backing), SCOPE))
    table.before_submit = lambda: other.withdraw_source(HUMAN, HUMAN_CONTEXT, withdrawal(source))
    with pytest.raises(Problem) as race:
        verifier.commit_decision(HUMAN, HUMAN_CONTEXT, plan)
    assert code(race) == "BEHAVIOR_CONFLICT"
    key = verifier.predicate_keys(CONTEXT, proposed, None)[0]
    assert sources.store.read_snapshot((key,))[key] is None


def test_withdrawal_invalidates_old_versions_claims_projections_and_snapshot_restore(reviews):
    sources, table, source, verifier, memory, proposed = reviews
    plan = claimed(verifier, memory, proposed)
    memory.record_projection(CONTEXT, projected(plan))
    snapshot = sources.capture_snapshot(CONTEXT, snapshot_id="snapshot-one", scope=source.reference.scope,
                                        conversation_id="conversation-one", runtime_version="runtime-one",
                                        roots=(source.reference,))
    command = withdrawal(source)
    result = sources.withdraw_source(HUMAN, HUMAN_CONTEXT, command)
    assert result.payload["result"]["access_revision"] == 2
    assert result.payload["result"]["access_state"] == "withdrawn"
    count = len(table.submissions)
    assert sources.withdraw_source(HUMAN, HUMAN_CONTEXT, command) == result
    assert len(table.submissions) == count
    for action in (
        lambda: sources.read_exact(CONTEXT, source.reference),
        lambda: sources.read_exact(CONTEXT, source.reference, require_current=True),
        lambda: current(memory, proposed),
        lambda: assembled(memory, proposed, "projection-one"),
        lambda: memory.record_projection(CONTEXT, projected(plan)),
        lambda: sources.restore_snapshot(CONTEXT, scope=source.reference.scope,
                                          snapshot_id="snapshot-one", receipt=snapshot),
        lambda: admitted(sources, b"attempted reactivation", version=2),
    ):
        with pytest.raises(Problem) as withdrawn:
            action()
        assert code(withdrawn) == "SOURCE_WITHDRAWN"
    assert sources.store.read_snapshot((source.reference.key,))[source.reference.key].payload["material"] is not None


def test_withdrawal_covers_historical_source_versions_and_transitive_required_graph(reviews):
    sources, _, first, _, _, _ = reviews
    second, _ = admitted(sources, b"cobalt version two", version=2)
    parent, _ = admitted(sources, b"parent event", source_id="parent", necessary=(first.reference,))
    sources.withdraw_source(HUMAN, HUMAN_CONTEXT, withdrawal(second))
    for reference in (first.reference, second.reference):
        with pytest.raises(Problem) as withdrawn:
            sources.read_exact(CONTEXT, reference)
        assert code(withdrawn) == "SOURCE_WITHDRAWN"
    with pytest.raises(Problem) as required:
        sources.read_graph(CONTEXT, (parent.reference,))
    assert code(required) == "SOURCE_WITHDRAWN"


def test_transitive_withdrawal_is_checked_in_composed_source_attachment(reviews):
    sources, table, leaf, _, _, _ = reviews
    root, _ = admitted(sources, b"root", source_id="root", necessary=(leaf.reference,))
    plan = sources.plan(CONTEXT, input_source(source_id="outer", necessary=(root.reference,)), b"outer")
    assert {item.key for item in plan.dependency_versions} == {root.reference.key, leaf.reference.key}
    sources.validate_attachment(CONTEXT, plan)
    sources.withdraw_source(HUMAN, HUMAN_CONTEXT, withdrawal(leaf))
    before = len(table.submissions)
    with pytest.raises(Problem) as withdrawn:
        sources.store.transact(plan.request, lambda rows: sources.attachment(CONTEXT, plan, rows))
    assert code(withdrawn) == "SOURCE_WITHDRAWN"
    assert len(table.submissions) == before
    assert sources.store.read_snapshot((plan.reference.key,))[plan.reference.key] is None


def test_withdrawal_requires_human_approval_and_exact_access_revision(reviews):
    sources, table, source, _, _, _ = reviews
    command = withdrawal(source)
    before = len(table.submissions)
    with pytest.raises(Problem) as agent:
        sources.withdraw_source(replace(AGENT, actions=frozenset({"read", "approve"})), CONTEXT, command)
    assert code(agent) == "SOURCE_HUMAN_REQUIRED"
    with pytest.raises(Problem) as stale:
        sources.withdraw_source(HUMAN, HUMAN_CONTEXT, command.model_copy(update={"expected_access_revision": 9}))
    assert code(stale) == "SOURCE_ACCESS_CHANGED"
    assert len(table.submissions) == before
    assert sources.read_exact(CONTEXT, source.reference).original == source.original


def test_withdrawal_after_initial_read_is_rechecked_before_returning_bytes(reviews, monkeypatch):
    sources, _, source, _, _, _ = reviews
    original = sources.verify_receipt
    def verify_then_withdraw(row, request, key):
        result = original(row, request, key)
        sources.withdraw_source(HUMAN, HUMAN_CONTEXT, withdrawal(source))
        return result
    monkeypatch.setattr(sources, "verify_receipt", verify_then_withdraw)
    with pytest.raises(Problem) as withdrawn:
        sources.read_exact(CONTEXT, source.reference)
    assert code(withdrawn) == "SOURCE_WITHDRAWN"


def test_unknown_withdrawal_keeps_original_request_and_retained_material(reviews):
    sources, table, source, _, _, _ = reviews
    command = withdrawal(source)
    request = sources.withdrawal_request(HUMAN, HUMAN_CONTEXT, command)
    table.failure = "after"
    with pytest.raises(Problem) as unknown:
        sources.withdraw_source(HUMAN, HUMAN_CONTEXT, command)
    assert unknown.value.body["error"]["request_id"] == request.request_id
    count = len(table.submissions)
    rebuilt = SourceMaterials(BehaviorStore(FakeTable(table.backing), SCOPE))
    retained = SourceWithdrawalInput.model_validate_json(command.model_dump_json())
    assert rebuilt.reconcile_withdrawal(HUMAN, HUMAN_CONTEXT, retained).payload["result"]["access_state"] == "withdrawn"
    assert len(table.submissions) == count
    assert rebuilt.store.read_snapshot((source.reference.key,))[source.reference.key].payload["material"] is not None


def test_review_bounds_and_corrupt_material_fail_closed(reviews):
    sources, table, _, verifier, memory, proposed = reviews
    tiny = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version=POLICY.policy_version,
                                                                max_material_bytes=10), clock=lambda: NOW)
    before = len(table.submissions)
    with pytest.raises(Problem) as too_large:
        tiny.plan_decision(HUMAN, HUMAN_CONTEXT, decision(verifier, proposed))
    assert code(too_large) == "HUMAN_REVIEW_TOO_LARGE"
    assert len(table.submissions) == before
    _, receipt = approved(verifier, proposed)
    key = receipt.payload["result"]["decision_key"]
    entity = table.backing.rows[("Behavior", SCOPE.partition_key, "behavior:v1:row:" + key)]
    payload = json.loads(entity["Payload"])
    payload["material"] = None
    entity["Payload"] = json.dumps(payload).encode()
    with pytest.raises(Problem):
        memory.plan_claim(CONTEXT, proposed)


def test_real_table_sdk_serializes_and_reads_immutable_human_review_material():
    store, transport = wire_store()
    store.initialize()
    sources = SourceMaterials(store)
    source, _ = admitted(sources)
    verifier = DurableHumanReviewVerifier(sources, POLICY, clock=lambda: NOW)
    proposed = candidate(source.reference, visibility="team")
    plan, receipt = approved(verifier, proposed)
    assert len(transport.parts[-1]) == 3
    assert transport.parts[-1][0][2]["If-Match"]
    graph = sources.read_graph(CONTEXT, proposed.supporting, require_current=True)
    attestation = verifier.review(CONTEXT, proposed, graph)
    assert attestation.decision_id == receipt.payload["result"]["decision_key"]
    assert attestation.evidence_sha256 == plan.material.command.evidence_sha256
