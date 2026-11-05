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


def test_every_observation_scope_field_and_mode_must_match(assistant):
    h,p,o,ledger,_=standing(assistant)
    decide=lambda observation,mode=h.runtime.mode:cumulative_decision(p,ledger,observation,h.time,read_id="scope",mode=mode)
    for changes in ({"observer_oid":"other-observer"},{"authority_domain":"other-domain"},
                    {"control_binding":"f"*64},{"legacy_liability_sha256":"f"*64}):
        assert decide(o.model_copy(update=changes)).reason=="cost_observation_scope"
    assert decide(None).reason=="cost_observation_scope"
    assert decide(o,"native").reason=="cost_observation_scope"


def test_freshness_and_regression_edges(assistant):
    h,p,o,ledger,_=standing(assistant)
    decide=lambda observation,now=h.time,book=ledger:cumulative_decision(p,book,observation,now,read_id="edge",
                                                                         mode=h.runtime.mode)
    assert decide(o.model_copy(update={"observed_at":h.time+timedelta(seconds=1)})).reason=="cost_observation_stale"
    assert decide(o,h.time+timedelta(seconds=299)).decision=="allow"
    assert decide(o,h.time+timedelta(seconds=300)).reason=="cost_observation_stale"
    later=ledger.model_copy(update={"latest_observed_at":h.time+timedelta(seconds=1)})
    assert decide(o,h.time+timedelta(seconds=2),later).reason=="cost_observation_regressed"
    low=ledger.model_copy(update={"observed_high_water_microusd":0})
    assert decide(o.model_copy(update={"observed_cumulative_microusd":45_479_999}),book=low).reason==(
        "cost_observation_regressed")


def test_revocation_precedence_and_ledger_authority(assistant):
    import pytest
    from shared_context.errors import Problem
    h,p,o,ledger,_=standing(assistant)
    decide=lambda policy,observation=o,book=ledger:cumulative_decision(policy,book,observation,h.time,read_id="order",
                                                                       mode=h.runtime.mode)
    assert decide(p.model_copy(update={"read_enabled":False})).reason=="result_access_revoked"
    assert decide(p.model_copy(update={"active":False,"read_enabled":False})).reason=="policy_revoked"
    assert decide(p.model_copy(update={"read_enabled":False}),None).reason=="result_access_revoked"
    spent=ledger.model_copy(update={"work_reservations_microusd":53_000_000})
    assert decide(p,None,spent).reason=="cost_observation_scope"
    for changes in ({"authority_domain":"other-domain"},{"cost_scope_sha256":"f"*64}):
        with pytest.raises(Problem) as denied:
            decide(p.model_copy(update={"active":False}),book=ledger.model_copy(update=changes))
        assert (denied.value.status,denied.value.body["error"]["code"])==(409,"WORK_POLICY_BUDGET_SCOPE")


def test_amounts_use_only_a_matched_observation_and_every_held_amount(assistant):
    from shared_context.behavior_work_policy import work_policy_key
    h,p,o,ledger,_=standing(assistant)
    grown=o.model_copy(update={"observed_cumulative_microusd":50_000_000,"legacy_unresolved_reserve_microusd":3_000_000})
    held=ledger.model_copy(update={"model_pending_microusd":1_000_000,"model_metered_microusd":500_000})
    decide=lambda book,observation=o,now=h.time:cumulative_decision(p,book,observation,now,read_id="amounts",
                                                                    mode=h.runtime.mode)
    allowed=decide(held,grown)
    assert (allowed.observed_high_water_microusd,allowed.reserved_microusd,allowed.remaining_microusd)==(
        50_000_000,9_500_000,40_500_000)
    assert (allowed.budget_id,allowed.policy_id,allowed.policy_revision,allowed.read_id,allowed.observed_at)==(
        work_policy_key("cost",p.authority_domain),p.policy_id,1,"amounts",h.time)
    assert (allowed.safety_reserve_microusd,allowed.cumulative_ceiling_microusd,allowed.budget_enforced)==(
        10_000_000,110_000_000,True)
    stale=decide(held,grown,h.time+timedelta(seconds=300))
    assert (stale.reason,stale.observed_high_water_microusd,stale.reserved_microusd,stale.remaining_microusd)==(
        "cost_observation_stale",45_480_000,8_500_000,46_020_000)
    exact=ledger.model_copy(update={"work_reservations_microusd":5_000_000+47_520_000-2_000_000})
    assert (decide(exact).decision,decide(exact).remaining_microusd)==("allow",2_000_000)
    short=exact.model_copy(update={"work_reservations_microusd":exact.work_reservations_microusd+1})
    assert decide(short).reason=="cumulative_budget_exhausted"


def test_an_instruction_needs_only_some_budget_and_no_fresh_cost(assistant):
    h,p,o,ledger,_=standing(assistant)
    decide=lambda book,now=h.time:cumulative_decision(p,book,o,now,read_id="turn",mode=h.runtime.mode,instruction=True)
    late=decide(ledger,h.time+timedelta(days=1))
    assert (late.decision,late.required_microusd,late.remaining_microusd)==("allow",0,47_520_000)
    nearly=decide(ledger.model_copy(update={"work_reservations_microusd":5_000_000+47_520_000-1}))
    assert (nearly.decision,nearly.remaining_microusd)==("allow",1)
    spent=decide(ledger.model_copy(update={"work_reservations_microusd":53_000_000}))
    assert (spent.reason,spent.remaining_microusd)==("cumulative_budget_exhausted",0)
    assert decide(ledger,h.time-timedelta(seconds=1)).reason=="cost_observation_stale"


def test_accounting_only_reports_no_budget_numbers_and_never_exhausts(assistant):
    h,p,o,ledger,_=standing(assistant)
    off=ledger.model_copy(update={"budget_enforced":False,"work_reservations_microusd":200_000_000})
    late=cumulative_decision(p,off,o,h.time+timedelta(days=1),read_id="off",mode=h.runtime.mode)
    assert (late.decision,late.remaining_microusd,late.safety_reserve_microusd,late.cumulative_ceiling_microusd,
            late.budget_enforced,late.required_microusd)==("allow",None,None,None,False,2_000_000)
    assert late.model_dump(mode="json")["budget_enforced"] is False
