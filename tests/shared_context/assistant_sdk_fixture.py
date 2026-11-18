"""The designated human source review an offline assistant configuration needs."""
from datetime import timedelta

from shared_context.behavior_auth import BehaviorPrincipal
from test_behavior_server import NOW

TENANT = "00000000-0000-0000-0000-000000000013"
HUMAN = "00000000-0000-0000-0000-00000000001b"


def review_source(store, config, *, review_id="offline-reviewed-pins", claim_id=None,
                  text="The fixed metrics source is reviewed."):
    from shared_context.source_materials import SourceMaterials, SourceInput, AuthenticatedSourceContext
    from shared_context.source_reviews import DurableHumanReviewVerifier, HumanReviewPolicy, HumanReviewCommand
    from shared_context.memory_governance import MemoryGovernance, ClaimCandidate
    from test_behavior_api import METRICS
    b, policy, profile = config.bindings[0], config.policies[0].policy, config.assistant.profiles[0]
    identity = config.identity
    principal = BehaviorPrincipal(identity.tenant_id, identity.human_oid, identity.team_id, "human", None,
                                  frozenset({"read", "approve"}))
    context = AuthenticatedSourceContext(b.tenant_id, b.team_id, principal.source_actor_id, b.ingress_id, policy.allowed_purposes)
    sources = SourceMaterials(store, clock=lambda: NOW)
    plan = sources.plan(context, SourceInput(source_id=profile.source.source_id, version=profile.source.version,
        visibility=policy.source_visibility, purpose=policy.purpose, schema_id="fa-artifact-metrics-v1",
        media_type="application/json"), METRICS)
    assert plan.reference.sha256 == profile.source.sha256
    sources.commit(context, plan)
    reviewer = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version=config.review_policy_version),
                                         clock=lambda: NOW)
    candidate = ClaimCandidate(claim_id=claim_id or profile.claim_id, version=1, subject=b.subject,
        visibility=policy.source_visibility, purpose=policy.purpose, kind="note",
        text=text, supporting=(plan.reference,),
        effective_from=NOW, expires_at=NOW + timedelta(minutes=2))
    preview = reviewer.preview(principal, context, candidate)
    command = HumanReviewCommand(review_id=review_id, candidate=candidate,
        candidate_sha256=preview.candidate_sha256, evidence_sha256=preview.evidence_sha256,
        policy_version=preview.policy_version, decision="approve",
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        reason="Offline explicit designated human review.")
    reviewer.commit_decision(principal, context, reviewer.plan_decision(principal, context, command))
    memory = MemoryGovernance(sources, clock=lambda: NOW, token_counter=len, review_verifier=reviewer)
    memory.commit_claim(context, memory.plan_claim(context, candidate))
