"""Read-only replay of the exact native clock mismatch, not an old-pin grant."""
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from shared_context.behavior_server_config import ServerConfigurationV2, read_document
from shared_context.behavior_work_policy_installation import StandingInstallPlan, StandingPolicyInstaller


def native_case():
    art=Path(os.environ["STANDING_CLOCK_PARENT_ART"])
    plan=read_document(art/"standing-native-0921"/"installer-phases"/"preview-response.json",StandingInstallPlan)
    cfg=read_document(art/"teams-window2-resumed-native-0920"/"window2"/"bundle"/"new-config.json",ServerConfigurationV2)
    return plan,cfg


def replay_service(plan,cfg):
    material=plan.source_review.material
    principal=SimpleNamespace(oid=cfg.identity.human_oid,source_actor_id=material.human_source_actor)
    service=object.__new__(StandingPolicyInstaller)
    service.policy,service.config=plan.policy,cfg
    service.clock=lambda: material.reviewed_at
    service.control=SimpleNamespace(_human=lambda *_:None)
    from shared_context.source_materials import AuthenticatedSourceContext
    context=AuthenticatedSourceContext(cfg.identity.tenant_id,cfg.identity.team_id,material.human_source_actor,
        plan.policy.template.ingress_id,plan.policy.legacy_template_policy.allowed_purposes)
    service.composition=SimpleNamespace(runtime=SimpleNamespace(
        _source_context=lambda *_:context,_reference=lambda *_:material.command.candidate.supporting[0]))
    return service,principal


def test_exact_native_review_clock_replay(monkeypatch):
    plan,cfg=native_case()
    material=plan.source_review.material
    assert (material.reviewed_at-material.command.candidate.effective_from).total_seconds()==0.33625
    service,principal=replay_service(plan,cfg)
    # Isolate the clock predicate at its originally approved code identity.
    # Separate regression below must reject this old plan on newly loaded code.
    monkeypatch.setattr("shared_context.behavior_work_policy_installation.m.code_sha256",lambda:plan.code_sha256)
    service._check(principal,plan,historical=True)


@pytest.fixture
def ticking_installation(monkeypatch):
    """Real source/review/claim producer; only unrelated old-history/ARM seams stubbed."""
    from datetime import datetime,timedelta,timezone
    from dataclasses import replace
    from shared_context import behavior_work_policy_installation as wi
    from shared_context.behavior_auth import BehaviorPrincipal
    from shared_context.behavior_store import BehaviorStore,GUARD_KEY
    from shared_context.source_materials import SourceMaterials,SourceInput,AuthenticatedSourceContext,digest
    from shared_context.source_reviews import DurableHumanReviewVerifier,HumanReviewPolicy
    from shared_context.memory_governance import MemoryGovernance
    from shared_context.behavior_wire import ExactSourceRef
    from shared_context.behavior_runtime import BehaviorRuntime
    from test_behavior_store import FakeTable,SCOPE
    old,cfg=native_case()
    class Clock:
        def __init__(self):self.value=datetime(2001,1,1,tzinfo=timezone.utc)
        def __call__(self):
            self.value+=timedelta(microseconds=336250)
            return self.value
    clock=Clock()
    table=FakeTable();store=BehaviorStore(table,SCOPE);store.initialize()
    sources=SourceMaterials(store,clock=clock)
    who=BehaviorPrincipal(cfg.identity.tenant_id,cfg.identity.human_oid,cfg.identity.team_id,"human",None,
        frozenset({"read","approve","submit"}))
    context=AuthenticatedSourceContext(who.tenant_id,who.team,who.source_actor_id,
        old.policy.template.ingress_id,old.policy.legacy_template_policy.allowed_purposes)
    source=sources.plan(context,SourceInput(source_id=old.policy.profile.source.source_id,version=1,
        purpose=old.policy.legacy_template_policy.purpose,schema_id="fa-artifact-metrics-v1",media_type="application/json"),
        b'{"schema_version":"fa-artifact-metrics-v1","item_count":12,"completed_count":5}')
    sources.commit(context,source)
    reference=ExactSourceRef(**{k:getattr(source.reference,k) for k in ("source_id","version","sha256","byte_length")})
    profile=old.policy.profile.model_copy(update={"source":reference,"claim_id":"OFFLINE-clock-review"})
    policy=old.policy.model_copy(update={"profile":profile})
    reviewer=DurableHumanReviewVerifier(sources,HumanReviewPolicy(policy_version=cfg.review_policy_version),clock=clock)
    memory=MemoryGovernance(sources,clock=clock,token_counter=len,review_verifier=reviewer)
    runtime=SimpleNamespace(store=store,dep=SimpleNamespace(claim_reviewer=reviewer,memory=memory))
    runtime._source_context=lambda *_:context
    runtime._reference=lambda *_:source.reference
    service=object.__new__(StandingPolicyInstaller)
    service.config,service.policy,service.store,service.clock=cfg,policy,store,clock
    service.composition=SimpleNamespace(runtime=runtime)
    service.evidence=None
    hashes={GUARD_KEY:"a"*64}
    inv=SimpleNamespace(rows={},entity_sha256=hashes,guard=SimpleNamespace(payload={"revision":1},etag="OFFLINE-etag"))
    store.read_inventory=lambda:inv
    cost=old.policy.model_dump(mode="json")
    cost_obj=SimpleNamespace(legacy_liability_sha256=digest(hashes),
        legacy_unresolved_reserve_microusd=1,observed_at=clock.value,model_dump=lambda **_:cost)
    service.cost_reader=lambda:cost_obj
    service.control=SimpleNamespace(_human=lambda principal,action:principal.require(who.team,action),
        initial_records=lambda *a:None,installed=lambda:SimpleNamespace(active=True))
    auth=SimpleNamespace(plan=SimpleNamespace(code_sha256="f"*64,scope=SimpleNamespace(handoff_id="OFFLINE-handoff")),
        model_dump=lambda **_:{"original":"retained"})
    view=SimpleNamespace(row=lambda *a,**k:SimpleNamespace(model_dump=lambda **_:{"original":"client_received"}))
    monkeypatch.setattr(wi,"fresh_protection",lambda *a:(SimpleNamespace(old_code_sha256="f"*64),{},clock.value+timedelta(hours=1)))
    monkeypatch.setattr(wi,"_snapshot_store",lambda *a:object())
    monkeypatch.setattr(wi.m,"installed_origin",lambda *a:cfg)
    monkeypatch.setattr(wi,"receipt_catalog",lambda *a:None)
    monkeypatch.setattr(wi,"_drained",lambda *a:None)
    monkeypatch.setattr(wi,"_Inventory",lambda *a:view)
    monkeypatch.setattr(wi,"validate_web_handoff",lambda *a:auth)
    monkeypatch.setattr(wi,"_counter_hash",lambda *a:"c"*64)
    # Keep genuine source producer binding intact after plan-only inventory probe.
    def validated(call,check):
        check()
        answer=call()
        check()
        return answer
    store.validated_writes=validated
    return SimpleNamespace(service=service,clock=clock,who=who,store=store,table=table,sources=sources,
        source=source,context=context,reviewer=reviewer,memory=memory)


def test_advancing_clock_real_plan_authorize_source_retains_both_times(ticking_installation,monkeypatch):
    from datetime import timedelta
    from shared_context.behavior_work_policy_installation import StandingInstallPlan
    c=ticking_installation;s=c.service
    plan=s.plan(c.who,installation_id="OFFLINE-clock-install")
    first=plan.source_review.material.command.candidate.effective_from
    reviewed=plan.source_review.material.reviewed_at
    assert first<reviewed
    plan=StandingInstallPlan.model_validate_json(plan.model_dump_json())
    approval=s.authorize(c.who,plan,decision_id="OFFLINE-approve",expires_at=c.clock.value+timedelta(minutes=1))
    # Applying old full-history cutover is outside this clock seam. Source calls
    # both original producers; no mocked review/claim writer and no native I/O.
    monkeypatch.setattr(s,"reconcile",lambda *a:object())
    before=len(c.table.submissions)
    receipt=s.source(c.who,plan,approval)
    assert len(c.table.submissions)==before+2
    c.reviewer.reconcile_decision(c.who,c.context,plan.source_review)
    claim=c.memory.get_current(c.context,scope=c.context.scope("team"),subject=plan.policy.template.subject,
        claim_id=plan.policy.profile.claim_id)
    assert claim.candidate.effective_from==first and claim.candidate.expires_at is None
    assert plan.source_review.material.reviewed_at==reviewed
    assert receipt.payload["request"]["request_id"]==claim.request.request_id


def test_new_loaded_code_still_rejects_original_native_plan_pin():
    from shared_context.errors import Problem
    from shared_context.behavior_migration import code_sha256
    plan,cfg=native_case()
    assert plan.code_sha256=="3996484c7333d2a0679da2b225b53b7f68c122f0b84aca2f75d36a264e1c7da8"
    assert code_sha256()!=plan.code_sha256
    service,principal=replay_service(plan,cfg)
    with pytest.raises(Problem,match="exact standing-policy code/scope required"):
        service._check(principal,plan,historical=True)


def test_clock_and_bound_material_tampering_rejected_before_approval_or_source(ticking_installation):
    from datetime import timedelta
    from shared_context.errors import Problem
    c=ticking_installation;s=c.service
    plan=s.plan(c.who,installation_id="OFFLINE-clock-negatives")
    m=plan.source_review.material;command=m.command;candidate=command.candidate
    cases={
        "effective_after_review":("candidate",{"effective_from":m.reviewed_at+timedelta(seconds=1)}),
        "future_review":("material",{"reviewed_at":c.clock.value+timedelta(days=1)}),
        "candidate_hash":("command",{"candidate_sha256":"0"*64}),
        "evidence_hash":("command",{"evidence_sha256":"0"*64}),
        "review_id":("command",{"review_id":"different-review"}),
        "review_policy":("command",{"policy_version":"different-policy"}),
        "wrong_source":("candidate",{"supporting":(candidate.supporting[0].model_copy(update={"sha256":"0"*64}),)}),
        "wrong_claim_id":("candidate",{"claim_id":"different-claim"}),
        "expires_added":("candidate",{"expires_at":m.reviewed_at+timedelta(minutes=1)}),
        "wrong_human":("material",{"human_oid":"different-human"}),
        "wrong_actor":("material",{"human_source_actor":"human-other"}),
        "request_id":("request",{"request_id":"different-request"}),
        "request_digest":("request",{"request_digest":"0"*64}),
        "request_reads":("request",{"read_set":()}),
    }
    before=len(c.table.submissions)
    for label,(kind,changes) in cases.items():
        review=plan.source_review
        if kind=="candidate":
            updated=command.model_copy(update={"candidate":candidate.model_copy(update=changes)})
            review=review.model_copy(update={"material":m.model_copy(update={"command":updated})})
        elif kind=="command":
            review=review.model_copy(update={"material":m.model_copy(update={"command":command.model_copy(update=changes)})})
        elif kind=="material":
            review=review.model_copy(update={"material":m.model_copy(update=changes)})
        else:
            review=review.model_copy(update={"request":review.request.model_copy(update=changes)})
        changed=plan.model_copy(update={"source_review":review})
        with pytest.raises(Problem):
            s.authorize(c.who,changed,decision_id="OFFLINE-invalid",expires_at=c.clock.value+timedelta(seconds=20))
        assert len(c.table.submissions)==before,label
    print("rejected_exact_clock_and_review_binding="+",".join(cases))


def test_rehashed_bad_evidence_and_readset_still_use_real_source_verifier(ticking_installation):
    from datetime import timedelta
    from shared_context.source_materials import digest
    from shared_context.errors import Problem
    c=ticking_installation;s=c.service
    plan=s.plan(c.who,installation_id="OFFLINE-rehashed")
    review=plan.source_review
    bad_material=review.material.model_copy(update={"command":review.material.command.model_copy(update={"evidence_sha256":"f"*64})})
    rebuilt=review.model_copy(update={"material":bad_material,"request":review.request.model_copy(update={
        "request_digest":digest(bad_material.model_dump(mode="json"))})})
    before=len(c.table.submissions)
    with pytest.raises(Problem,match="explicit review must match current candidate, evidence and policy"):
        s.authorize(c.who,plan.model_copy(update={"source_review":rebuilt}),
                    decision_id="OFFLINE-invalid",expires_at=c.clock.value+timedelta(seconds=20))
    with pytest.raises(Problem,match="original exact source review read set"):
        s.authorize(c.who,plan.model_copy(update={"source_review":review.model_copy(update={
            "request":review.request.model_copy(update={"read_set":()})})}),
            decision_id="OFFLINE-invalid",expires_at=c.clock.value+timedelta(seconds=20))
    assert len(c.table.submissions)==before


def test_expiry_after_last_source_validation_never_authorizes(ticking_installation,monkeypatch):
    from datetime import timedelta
    from shared_context.errors import Problem
    c=ticking_installation;s=c.service
    plan=s.plan(c.who,installation_id="OFFLINE-expiry")
    read=c.reviewer._originals
    def delayed(*args):
        value=read(*args)
        c.clock.value=plan.change_valid_until
        return value
    monkeypatch.setattr(c.reviewer,"_originals",delayed)
    before=len(c.table.submissions)
    with pytest.raises(Problem,match="expired after source validation"):
        s.authorize(c.who,plan,decision_id="OFFLINE-expired",expires_at=plan.change_valid_until)
    assert len(c.table.submissions)==before


def test_source_unknown_reconciles_original_producer_receipt_without_retime(ticking_installation,monkeypatch):
    from datetime import timedelta
    from shared_context.errors import Problem
    c=ticking_installation;s=c.service
    plan=s.plan(c.who,installation_id="OFFLINE-ACKloss")
    approved=s.authorize(c.who,plan,decision_id="OFFLINE-approved",expires_at=c.clock.value+timedelta(minutes=1))
    monkeypatch.setattr(s,"reconcile",lambda *a:object())
    c.table.failure="after"
    with pytest.raises(Problem):
        s.source(c.who,plan,approved)
    # Only the original review was committed. A historical read is not a resend.
    before=len(c.table.submissions)
    receipt=c.reviewer.reconcile_decision(c.who,c.context,plan.source_review)
    assert receipt.payload["request"]==plan.source_review.request.model_dump(mode="json")
    assert len(c.table.submissions)==before
    assert plan.source_review.material.command.candidate.effective_from<plan.source_review.material.reviewed_at
