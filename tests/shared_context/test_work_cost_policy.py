from datetime import timedelta
from shared_context.behavior_work_policy import (
    SteadyWorkPolicy,CostObservation,CostLedger,cumulative_decision,WorkPolicyControl,
)
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.source_materials import digest
from test_behavior_assistant import assistant,runtime_path
from test_m3_auth import jwk_client,signing_key


def standing(assistant):
    h=assistant
    b=h.binding
    p=SteadyWorkPolicy(policy_id="same-poc-standing-work",revision=1,configuration_sha256="a"*64,
        evidence_mode="offline_contract",authority_domain=h.store._scope.authority_domain,control_binding=h.store._binding,
        active=True,read_enabled=True,template=b,profile=h.runtime.profiles[b.binding_id],legacy_template_policy=h.policy,
        cost_scope_sha256="c"*64,cost_observer_oid="original-cost-observer",work_estimate_microusd=2_000_000,
        minimum_observed_microusd=45_480_000,
        cost_max_age_seconds=300,queue_seconds=min(600,h.config.max_queue_seconds),max_model_requests=6)
    o=CostObservation(evidence_id="offline-cost-observation",evidence_mode="offline_contract",
        observer_oid=p.cost_observer_oid,authority_domain=p.authority_domain,control_binding=p.control_binding,
        cost_scope_sha256=p.cost_scope_sha256,observed_cumulative_microusd=45_480_000,
        legacy_unresolved_reserve_microusd=2_000_000,legacy_liability_sha256="d"*64,
        evidence_sha256="e"*64,observed_at=h.time)
    ledger=CostLedger(authority_domain=p.authority_domain,cost_scope_sha256=p.cost_scope_sha256,
        observed_high_water_microusd=o.observed_cumulative_microusd,
        legacy_unresolved_reserve_microusd=o.legacy_unresolved_reserve_microusd,
        legacy_liability_sha256=o.legacy_liability_sha256,latest_observed_at=o.observed_at,
        latest_observation_sha256=digest(o.model_dump(mode="json")),work_reservations_microusd=5_000_000)
    human=BehaviorPrincipal(b.tenant_id,b.human_oid,b.team_id,"human",None,
        frozenset({"read","submit","approve","acknowledge"}))
    return h,p,o,ledger,human


def test_budget_is_one_cumulative_usd_authority_not_abstract_root_units(assistant):
    h,p,o,ledger,_=standing(assistant)
    before=h.store.read_snapshot(())
    decision=cumulative_decision(p,ledger,o,h.time,read_id="one-read",mode=h.runtime.mode)
    assert decision.decision=="allow" and decision.unit=="USD-micro"
    assert decision.remaining_microusd==47_520_000 and decision.required_microusd==2_000_000
    assert not decision.authorization and not decision.provider_hard_cap
    assert h.store.read_snapshot(())==before
    exhausted=ledger.model_copy(update={"work_reservations_microusd":53_000_000})
    denied=cumulative_decision(p,exhausted,o,h.time,read_id="another-read",mode=h.runtime.mode)
    assert denied.decision=="deny" and denied.reason=="cumulative_budget_exhausted"


def test_only_new_paid_decision_uses_cost_freshness_not_a_policy_deadline(assistant):
    h,p,o,ledger,_=standing(assistant)
    h.time=p.legacy_template_policy.valid_until+timedelta(days=2)
    stale=cumulative_decision(p,ledger,o,h.time,read_id="old-cost",mode=h.runtime.mode)
    assert stale.reason=="cost_observation_stale"
    observed=o.model_copy(update={"observed_at":h.time})
    allowed=cumulative_decision(p,ledger,observed,h.time,read_id="new-cost",mode=h.runtime.mode)
    assert allowed.decision=="allow"
    assert p.legacy_template_policy==h.policy and "valid_until" not in SteadyWorkPolicy.model_fields
    revoked=cumulative_decision(p.model_copy(update={"active":False}),ledger,observed,h.time,read_id="revoked",mode=h.runtime.mode)
    assert revoked.reason=="policy_revoked"


def test_original_scope_and_spent_high_water_cannot_regress(assistant):
    h,p,o,ledger,_=standing(assistant)
    for changes,reason in (
        ({"observed_cumulative_microusd":0},"cost_observation_regressed"),
        ({"legacy_unresolved_reserve_microusd":0},"cost_observation_regressed"),
        ({"cost_scope_sha256":"f"*64},"cost_observation_scope"),
        ({"evidence_mode":"native"},"cost_observation_scope"),
    ):
        denied=cumulative_decision(p,ledger,o.model_copy(update=changes),h.time,read_id="same-authority",mode=h.runtime.mode)
        assert denied.decision=="deny" and denied.reason==reason


def test_delayed_native_usd_observation_is_rounded_up_not_reset():
    from shared_context.behavior_work_policy import microusd_from_decimal
    assert microusd_from_decimal("47.11268605947896027")==47_112_687
