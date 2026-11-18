"""從 test_work_cost_policy.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from shared_context.behavior_work_policy import SteadyWorkPolicy, CostObservation, CostLedger, work_policy_key
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_operations import _write
from shared_context.behavior_runtime import material
from shared_context.behavior_store import ControlChanges, ControlRequest
from shared_context.errors import Problem
from shared_context.source_materials import digest


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


def install_offline(control,principal,decision_id,change_expires_at):
    if control.runtime.mode!="offline_contract":
        raise Problem(403,"WORK_POLICY_NATIVE_INSTALLER_REQUIRED","native requires its exact historical cutover")
    values=control.initial_records(principal,decision_id,change_expires_at)
    keys=(control.policy_key,control.cost_key,control.registry_key)
    request=ControlRequest(request_id=work_policy_key("install",control.policy.authority_domain),
        request_digest=digest([v.model_dump(mode="json") for v in values]),read_set=keys)
    def decide(rows):
        if any(rows[k] is not None for k in keys):
            raise Problem(409,"WORK_POLICY_ALREADY_INSTALLED","standing scope and cumulative cost cannot reset")
        if control.runtime._now()>=change_expires_at:
            raise Problem(403,"WORK_POLICY_CHANGE_EXPIRED","initial control transaction expired")
        return ControlChanges(writes=tuple(_write(k,v,"create",material(v.model_dump(mode="json")) if k==control.policy_key else None)
            for k,v in zip(keys,values)),result={"policy_id":control.policy.policy_id,"installed":True,"business_deadline":None})
    result=control.store.transact(request,decide)
    control.runtime.work_policy=control
    control.runtime.historical_binding_ids=frozenset((*control.runtime.historical_binding_ids,*control.base_bindings))
    control.runtime.dep.inbox.register_work_policy(control)
    control.runtime.dep.delivery.work_policy=control
    control.runtime.dep.workers.work_policy=control
    return result
