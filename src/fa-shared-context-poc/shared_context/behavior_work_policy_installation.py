"""One finite operator transaction installs a standing policy, not a usage window."""
import hashlib
import json
from datetime import datetime,timedelta
from types import MappingProxyType,SimpleNamespace
from typing import Literal
from pydantic import field_validator

from . import behavior_migration as m
from .behavior_channels_migration import CatalogEntry,CatalogChunk,_drained
from .behavior_runtime_rotation import _snapshot_store,_measure_layout,RotationEvidenceFiles,RotationRetirement
from .behavior_runtime_rotation_history import binding_scopes,shared_keys,receipt_catalog,validate_additions,require
from .behavior_runtime_transition import _counter_hash,_payload_hash
from .behavior_server_config import ProtectionObservation
from .behavior_store import ControlRequest,ControlChanges,ControlWrite,GUARD_KEY
from .behavior_work_policy import (
    SteadyWorkPolicy,StandingPolicyInstallation,CostLedger,WorkRegistry,WorkPolicyControl,
    WorkRegistration,WorkCostReservation,WorkDispatchPermission,PerWorkRuntimePolicy,WorkPolicyRevocation,CostObservationUpdate,work_policy_key,
)
from .behavior_web_handoff import validate_web_handoff,WebHandoffFulfillment,handoff_key
from .behavior_recovery_history import _Inventory
from .behavior_inbox import CurrentWorkInboxAccess
from .source_materials import SourceEvent,digest,utc
from .memory_governance import ClaimCandidate,claim_digest
from .source_reviews import HumanReviewCommand,HumanReviewPlan
from .records import Record
from .models import Identifier


def standing_source_cursors(config):
    return {m.control_key("source-cursor", scope.key)
            for binding in config.bindings for scope in binding_scopes(config, binding)}


def standing_shared_keys(config):
    return shared_keys(config) | standing_source_cursors(config)


def _legacy_cursor_row(entry, current, original_rows):
    """Old catalogs pinned the cursor but retained the source events that determine its preimage."""
    sequence = 0
    for row in original_rows.values():
        data = row.payload["data"]
        if {"reference", "source_actor", "ingress_id", "schema_id"} <= data.keys():
            event = m._parsed(data, SourceEvent)
            if m.control_key("source-cursor", event.reference.scope.key) == entry.row_id:
                sequence = max(sequence, event.sequence)
    payload = {**current.payload, "data": {"sequence": sequence}}
    require(_payload_hash(payload) == entry.sha256,
            "original source cursor cannot be reconstructed from its retained sources")
    return Record(payload, entry.etag)


def read_standing_catalog(inv,config,history):
    """Rebuild the immutable predecessor, including cursors predating cursor preimages."""
    mutable=standing_shared_keys(config)
    cursor_keys=standing_source_cursors(config)
    legacy_cursors=[]
    rows,receipts,hashes={},{},{GUARD_KEY:history.physical_guard_sha256}
    metadata,expected=set(),{}
    for i,(key,pin) in enumerate(history.catalog):
        chunk=m._parsed(m._row(inv,key).payload["data"],CatalogChunk)
        require(chunk.index==i and chunk.transition_id==history.plan.installation_id
            and key==work_policy_key("catalog",history.plan.installation_id+"-"+str(i))
            and digest(chunk.model_dump(mode="json"))==pin,"retained complete catalog differs")
        metadata.add(key);expected[key]=_payload_hash(inv.rows[key].payload)
        for entry in chunk.entries:
            target=rows if entry.kind=="row" else receipts
            source=(inv.rows if entry.kind=="row" else inv.receipts).get(entry.row_id)
            require(source is not None and entry.row_id not in target,"original row/receipt missing or repeated")
            legacy_cursor=entry.kind=="row" and entry.row_id in cursor_keys and entry.preimage is None
            if legacy_cursor:
                saved=source
                legacy_cursors.append(entry)
            elif entry.kind=="row" and entry.row_id in mutable:
                require(entry.preimage is not None,"exact shared preimage is required")
                saved=Record(json.loads(entry.preimage),entry.etag)
            else:
                require(entry.preimage is None,"immutable history cannot be substituted")
                saved=source
            if not legacy_cursor:
                require(_payload_hash(saved.payload)==entry.sha256,"original immutable bytes changed")
            target[entry.row_id]=saved
            hashes["behavior:v1:"+entry.kind+":"+entry.row_id]=entry.sha256
    for entry in legacy_cursors:
        rows[entry.row_id]=_legacy_cursor_row(entry,rows[entry.row_id],rows)
    return rows,receipts,hashes,metadata,expected


class StandingInstallPlan(m._Model):
    schema_version:Literal["fa-standing-policy-install-plan-v1"]="fa-standing-policy-install-plan-v1"
    installation_id:Identifier
    configuration_sha256:m.Digest
    code_sha256:m.Digest
    policy:SteadyWorkPolicy
    inventory_sha256:m.Digest
    guard:m.GuardAnchor
    counter_sha256:m.Digest
    original_handoff_sha256:m.Digest
    original_fulfillment_sha256:m.Digest
    observations_sha256:dict[str,m.Digest]
    source_review:HumanReviewPlan
    change_valid_until:datetime
    _time=field_validator("change_valid_until")(utc)

    @property
    def sha256(self):return digest(self.model_dump(mode="json"))


class StandingInstallApproval(m._Model):
    schema_version:Literal["fa-standing-policy-install-approval-v1"]="fa-standing-policy-install-approval-v1"
    decision_id:Identifier
    operator_oid:Identifier
    plan_sha256:m.Digest
    purpose:Literal["install_standing_same_source_work_policy"]="install_standing_same_source_work_policy"
    not_before:datetime
    expires_at:datetime
    _times=field_validator("not_before","expires_at")(utc)


class StandingInstallHistory(m._Model):
    schema_version:Literal["fa-standing-policy-install-history-v1"]="fa-standing-policy-install-history-v1"
    plan:StandingInstallPlan
    approval:StandingInstallApproval
    installation:StandingPolicyInstallation
    initial_cost_ledger:CostLedger
    before_guard:dict
    physical_guard_sha256:m.Digest
    catalog:tuple[tuple[Identifier,m.Digest],...]
    request:ControlRequest
    applied_at:datetime
    _time=field_validator("applied_at")(utc)


def fresh_protection(config,evidence,clock):
    from .source_materials import InventoryProtectionVerifier
    raw=evidence.read()
    retirement=RotationRetirement.model_validate_json(raw["retirement"])
    now=utc(clock())
    require(retirement.observer_oid==config.evidence.observer_oid
        and retirement.operator_oid==config.identity.human_oid
        and retirement.authority_domain==config.table.scope.authority_domain
        and retirement.old_configuration_sha256==retirement.new_configuration_sha256==config.sha256
        and retirement.controller_resource_id==config.controller_resource_id
        and retirement.api_resource_id==config.api_resource_id
        and tuple(v.target for v in retirement.targets)==config.active_targets
        and tuple(v.target for v in retirement.current_targets)==config.active_targets
        and retirement.observed_at<=now<retirement.valid_until
        and retirement.valid_until-retirement.observed_at<=timedelta(minutes=20),
        "fresh exact retirement is only the one installation prerequisite")
    for target in retirement.current_targets:
        require(target.credential_client_id==config.identity.managed_identity_client_id
            and target.credential_tenant_id==config.identity.tenant_id
            and target.session_owner_oid==target.credential_principal_oid
            and target.credential_principal_oid not in {config.identity.human_oid,*(a.oid for a in config.identity.agents)},
            "current original target must retain API MI ownership")
    until=[retirement.valid_until]
    for lane in ("sources","backups"):
        o=ProtectionObservation.model_validate_json(raw[lane])
        expected=getattr(config,lane)
        require(o.configuration==expected.protection and o.inventory_coverage is not None
            and set(o.inventory_coverage)==set(config.evidence.inventory_scopes)
            and len(o.inventory_coverage)==len(config.evidence.inventory_scopes)
            and o.inventory_observed_at is not None and o.inventory_valid_until is not None
            and o.blob_service.account_resource_id==expected.account_resource_id
            and o.management_policy.account_resource_id==expected.account_resource_id,
            "exact original retained source/backup protection required")
        InventoryProtectionVerifier(o.state,clock=clock).verify(expected.protection)
        for start,end in ((o.observed_at,o.valid_until),(o.inventory_observed_at,o.inventory_valid_until),
                          (o.blob_service.observed_at,o.blob_service.valid_until),
                          (o.management_policy.observed_at,o.management_policy.valid_until)):
            require(start<=now<end and now-start<=timedelta(seconds=config.evidence.max_age_seconds),
                    "fresh source/backup observation required for installation")
            until.append(end)
    return retirement,{k:hashlib.sha256(v).hexdigest() for k,v in raw.items()},min(until)


class StandingPolicyInstaller:
    def __init__(self,composition,policy,evidence,cost_reader,*,allow_apply=False):
        require(type(evidence) is RotationEvidenceFiles and type(policy) is SteadyWorkPolicy
                and policy.configuration_sha256==composition.config.sha256
                and policy.evidence_mode=="native" and composition.runtime.mode=="native",
                "one exact native standing-policy change is required")
        self.composition,self.config,self.policy,self.evidence=composition,composition.config,policy,evidence
        self.store=m.MigrationStore(composition.clients[0],composition.config.table.scope)
        self.control=WorkPolicyControl(composition.runtime,policy,cost_reader)
        self.cost_reader,self.allow_apply=cost_reader,allow_apply
        self.clock=composition.runtime.clock
        require(policy.cost_observer_oid==self.config.evidence.observer_oid
                and policy.profile.claim_id!=self.config.assistant.profiles[0].claim_id,
                "one new explicitly reviewed source claim, not renewal of claim4, is required")

    def _review(self,principal,at):
        b,p=self.policy.template,self.policy.legacy_template_policy
        context=self.composition.runtime._source_context(b,p,principal)
        source=self.composition.runtime._reference(self.policy.profile.source,b,p,principal)
        candidate=ClaimCandidate(claim_id=self.policy.profile.claim_id,version=1,subject=b.subject,
            visibility=p.source_visibility,purpose=p.purpose,kind="note",supporting=(source,),
            text="The accepted metrics source has been reviewed.",effective_from=at)
        reviewer=self.composition.runtime.dep.claim_reviewer
        preview=reviewer.preview(principal,context,candidate)
        plan=reviewer.plan_decision(principal,context,HumanReviewCommand(
            review_id=work_policy_key("source-review",self.policy.policy_id),candidate=candidate,
            candidate_sha256=preview.candidate_sha256,evidence_sha256=preview.evidence_sha256,
            policy_version=preview.policy_version,decision="approve",
            reviewed_checks=("necessity","support","contradiction","sensitivity","consent"),
            reason="Explicit standing fixed-source work policy approval."))
        return plan

    def plan(self,principal,*,installation_id):
        self.control._human(principal,"approve")
        retired,observations,until=fresh_protection(self.config,self.evidence,self.clock)
        inv=self.store.read_inventory()
        m.installed_origin(_snapshot_store(self.store,inv,self.config),self.config)
        require(work_policy_key("current",self.policy.authority_domain) not in inv.rows,"standing policy is already installed")
        receipt_catalog(inv)
        _drained(inv)
        auth=validate_web_handoff(_Inventory(inv,self.config),self.config)
        require(auth is not None and retired.old_code_sha256==auth.plan.code_sha256,"exact predecessor handoff/code is required once")
        final=_Inventory(inv,self.config).row(handoff_key("fulfillment",auth.plan.scope.handoff_id),WebHandoffFulfillment,material=True)
        observation=self.cost_reader()
        require(observation.legacy_liability_sha256==digest(dict(inv.entity_sha256)),
                "initial monetary observation must bind the complete original liability inventory")
        if any("request_sha256" in row.payload["data"] and "consumed" in row.payload["data"] for row in inv.rows.values()):
            require(observation.legacy_unresolved_reserve_microusd>0,"retained model exposure must not silently receive zero reserve")
        now=utc(self.clock())
        self.control.initial_records(principal,"plan-only",min(until,now+timedelta(minutes=15)))
        review=self._review(principal,now)
        return StandingInstallPlan(installation_id=installation_id,configuration_sha256=self.config.sha256,
            code_sha256=m.code_sha256(),policy=self.policy,inventory_sha256=digest(dict(inv.entity_sha256)),
            guard=m.GuardAnchor(binding=self.store._binding,revision=inv.guard.payload["revision"],etag=inv.guard.etag),
            counter_sha256=_counter_hash(inv.rows),original_handoff_sha256=digest(auth.model_dump(mode="json")),
            original_fulfillment_sha256=digest(final.model_dump(mode="json")),
            observations_sha256={**observations,"cost":digest(observation.model_dump(mode="json"))},source_review=review,
            change_valid_until=min(until,observation.observed_at+timedelta(seconds=self.policy.cost_max_age_seconds)))

    def authorize(self,principal,plan,*,decision_id,expires_at):
        self.control._human(principal,"approve")
        self._check(principal,plan)
        require(digest(dict(self.store.read_inventory().entity_sha256))==plan.inventory_sha256,"original installation preimage changed")
        now=utc(self.clock())
        require(now<expires_at<=min(plan.change_valid_until,now+timedelta(minutes=15)),"one short operator transaction only")
        return StandingInstallApproval(decision_id=decision_id,operator_oid=principal.oid,plan_sha256=plan.sha256,
            not_before=now,expires_at=expires_at)

    def _check(self,principal,plan,approval=None,*,historical=False):
        self.control._human(principal,"approve")
        require(plan.policy==self.policy and plan.configuration_sha256==self.config.sha256
            and plan.code_sha256==m.code_sha256(),"exact standing-policy code/scope required")
        if approval is not None:
            require(approval.operator_oid==principal.oid and approval.plan_sha256==plan.sha256,
                    "one exact operator decision is required")
        if not historical:
            _,observations,until=fresh_protection(self.config,self.evidence,self.clock)
            require({**observations,"cost":digest(self.cost_reader().model_dump(mode="json"))}==plan.observations_sha256,
                    "installation evidence changed")
            now=utc(self.clock())
            require(now<min(until,plan.change_valid_until,plan.change_valid_until if approval is None else approval.expires_at)
                    and (approval is None or approval.not_before<=now),"operator transaction permission expired")
        b,p=self.policy.template,self.policy.legacy_template_policy
        context=self.composition.runtime._source_context(b,p,principal)
        source=self.composition.runtime._reference(self.policy.profile.source,b,p,principal)
        material=plan.source_review.material
        command=material.command
        # Candidate effectiveness is captured before the reviewer reads sources;
        # reviewed_at records that later decision, not a replacement effective time.
        expected=ClaimCandidate(claim_id=self.policy.profile.claim_id,version=1,subject=b.subject,
            visibility=p.source_visibility,purpose=p.purpose,kind="note",supporting=(source,),
            text="The accepted metrics source has been reviewed.",effective_from=command.candidate.effective_from)
        require(command.candidate==expected and command.decision=="approve"
            and command.review_id==work_policy_key("source-review",self.policy.policy_id)
            and command.reviewed_checks==("necessity","support","contradiction","sensitivity","consent")
            and material.human_oid==principal.oid and material.human_source_actor==principal.source_actor_id
            and material.scope==context.scope(p.source_visibility)
            and utc(expected.effective_from)<=utc(material.reviewed_at)<=utc(self.clock())
            and utc(material.reviewed_at)<plan.change_valid_until
            and command.candidate_sha256==claim_digest(expected,material.scope)
            and command.policy_version==self.config.review_policy_version
            and plan.source_review.request.request_id==m.control_key(
                "source-human-review-request",material.scope.key,command.review_id)
            and plan.source_review.request.request_digest==digest(material.model_dump(mode="json")),
            "only the exact initial fixed-source human review is part of this policy")
        if not historical:
            reviewer=self.composition.runtime.dep.claim_reviewer
            reviewer._principal(principal,context,plan.source_review)
            originals=reviewer._originals(context,expected,material.scope)
            reviewer._command(context,command,material.scope,originals)
            require(reviewer._request(material,originals)==plan.source_review.request,
                    "retain the original exact source review read set")
            now=utc(self.clock())
            require(now<min(until,plan.change_valid_until,plan.change_valid_until if approval is None else approval.expires_at)
                    and (approval is None or approval.not_before<=now),
                    "operator transaction permission expired after source validation")

    def request(self,plan,approval):
        return ControlRequest(request_id=work_policy_key("install",plan.installation_id),
            request_digest=digest([plan.sha256,approval.model_dump(mode="json")]))

    def _layout(self,inv,plan,approval,values,at):
        mutable=standing_shared_keys(self.config)
        entries=tuple(CatalogEntry(kind=kind,row_id=k,sha256=_payload_hash(v.payload),etag=v.etag,
            preimage=json.dumps(v.payload,ensure_ascii=False,separators=(",",":")) if kind=="row" and k in mutable else None)
            for kind,records in (("row",inv.rows),("receipt",inv.receipts)) for k,v in sorted(records.items()))
        chunks=tuple(CatalogChunk(transition_id=plan.installation_id,index=i//48,entries=entries[i:i+48])
                     for i in range(0,len(entries),48))
        catalog=tuple((work_policy_key("catalog",plan.installation_id+"-"+str(c.index)),c) for c in chunks)
        installation,ledger,registry=values
        history=StandingInstallHistory(plan=plan,approval=approval,installation=installation,initial_cost_ledger=ledger,
            before_guard={**inv.guard.payload,"etag":inv.guard.etag},physical_guard_sha256=inv.entity_sha256[GUARD_KEY],
            catalog=tuple((k,digest(v.model_dump(mode="json"))) for k,v in catalog),
            request=self.request(plan,approval),applied_at=at)
        hkey=work_policy_key("history",plan.installation_id)
        writes=[ControlWrite(row_id=k,mode="create",data=v.model_dump(mode="json")) for k,v in (*catalog,(hkey,history))]
        for k,v in zip((self.control.policy_key,self.control.cost_key,self.control.registry_key),values):
            from .behavior_runtime import material
            writes.append(ControlWrite(row_id=k,mode="create",data=v.model_dump(mode="json"),
                material=material(v.model_dump(mode="json")) if k==self.control.policy_key else None))
        writes.append(ControlWrite(row_id=work_policy_key("current",self.policy.authority_domain),mode="create",data={
            "schema_version":"fa-standing-policy-current-v1","history_key":hkey,"history_sha256":digest(history.model_dump(mode="json"))}))
        return ControlChanges(writes=tuple(writes),result={"standing_policy_id":self.policy.policy_id,"business_deadline":None,
            "configuration_sha256":self.config.sha256,"source_review_required":True,"activation_performed":False})

    def apply(self,principal,plan,approval):
        require(self.allow_apply,"standing-policy apply disabled")
        self._check(principal,plan,approval)
        inv=self.store.read_inventory()
        require(digest(dict(inv.entity_sha256))==plan.inventory_sha256,"original full inventory changed")
        m.installed_origin(_snapshot_store(self.store,inv,self.config),self.config)
        _drained(inv)
        require(_counter_hash(inv.rows)==plan.counter_sha256
            and plan.guard.revision==inv.guard.payload["revision"] and plan.guard.etag==inv.guard.etag
            and plan.guard.binding==self.store._binding,"original counter/guard snapshot differs")
        values=self.control.initial_records(principal,approval.decision_id,approval.expires_at)
        at=utc(self.clock());changes=self._layout(inv,plan,approval,values,at)
        _measure_layout(self.store,inv,self.config,self.config,self.request(plan,approval),changes)
        def last():
            self._check(principal,plan,approval)
            require(digest(dict(self.store.read_inventory().entity_sha256))==plan.inventory_sha256,"late original effect changed")
            self._check(principal,plan,approval)
        return self.store.transact(self.request(plan,approval),lambda _:changes,expected_guard=inv.guard,before_send=last)

    def source(self,principal,plan,approval):
        self._check(principal,plan,approval)
        self.reconcile(principal,plan,approval)
        require(self.control.installed().active,"revoked standing policy cannot publish another source review")
        context=self.composition.runtime._source_context(self.policy.template,self.policy.legacy_template_policy,principal)
        def publish():
            self.composition.runtime.dep.claim_reviewer.commit_decision(principal,context,plan.source_review)
            claimed=self.composition.runtime.dep.memory.plan_claim(context,plan.source_review.material.command.candidate)
            return self.composition.runtime.dep.memory.commit_claim(context,claimed)
        return self.composition.runtime.store.validated_writes(publish,lambda:self._check(principal,plan,approval))

    def reconcile(self,principal,plan,approval):
        self._check(principal,plan,approval,historical=True)
        return self.store.reconcile(self.request(plan,approval))


def installed_work_policy_origin(store,config):
    inv=store.read_inventory(max_entities=None,log_capacity=True);receipt_catalog(inv)
    ckey=work_policy_key("current",config.table.scope.authority_domain)
    pointer=m._row(inv,ckey).payload["data"]
    require(set(pointer)=={"schema_version","history_key","history_sha256"}
        and pointer["schema_version"]=="fa-standing-policy-current-v1","standing-policy pointer differs")
    history=m._parsed(m._row(inv,pointer["history_key"]).payload["data"],StandingInstallHistory)
    plan,approval=history.plan,history.approval
    require(plan.configuration_sha256==config.sha256==inv.installation_configuration_sha256
        and plan.policy.configuration_sha256==config.sha256 and plan.policy.template==config.bindings[0]
        and plan.policy.control_binding==store._binding and plan.policy.authority_domain==config.table.scope.authority_domain
        and approval.plan_sha256==plan.sha256 and approval.operator_oid==config.identity.human_oid
        and approval.not_before<=history.applied_at<approval.expires_at<=plan.change_valid_until
        and pointer["history_key"]==work_policy_key("history",plan.installation_id)
        and digest(history.model_dump(mode="json"))==pointer["history_sha256"],
        "standing policy is not exact original approved history")
    mutable=standing_shared_keys(config)
    rows,receipts,hashes,catalog_metadata,catalog_expected=read_standing_catalog(inv,config,history)
    metadata={ckey,pointer["history_key"]}
    expected={k:_payload_hash(inv.rows[k].payload) for k in metadata}
    metadata.update(catalog_metadata);expected.update(catalog_expected)
    guard={k:v for k,v in history.before_guard.items() if k!="etag"}
    physical=m._MigratedGuard(binding=guard["binding"],revision=guard["revision"],configuration_sha256=config.sha256)
    require(digest(hashes)==plan.inventory_sha256 and _payload_hash(physical.model_dump(mode="json"))==history.physical_guard_sha256
        and guard["revision"]==plan.guard.revision and plan.guard.etag==history.before_guard["etag"]
        and _counter_hash(rows)==plan.counter_sha256,"original complete inventory/counters differ")
    before=m.AuthorityInventory(store._scope,Record(guard,history.before_guard["etag"]),
        MappingProxyType(rows),MappingProxyType(receipts),MappingProxyType(hashes),config.sha256)
    original=m.installed_origin(_snapshot_store(store,before,config),config)
    _drained(before)
    auth=validate_web_handoff(_Inventory(before,config),config)
    final=_Inventory(before,config).row(handoff_key("fulfillment",auth.plan.scope.handoff_id),WebHandoffFulfillment,material=True)
    require(digest(auth.model_dump(mode="json"))==plan.original_handoff_sha256
        and digest(final.model_dump(mode="json"))==plan.original_fulfillment_sha256,"original bootstrap handoff differs")
    from .behavior_store import _Row
    from .behavior_runtime import material
    values=(history.installation,history.initial_cost_ledger,WorkRegistry(policy_id=plan.policy.policy_id))
    pkey=work_policy_key("policy",plan.policy.authority_domain)
    for key,value in zip((pkey,work_policy_key("cost",plan.policy.authority_domain),work_policy_key("registry",plan.policy.authority_domain)),values):
        expected[key]=_payload_hash(_Row(binding=store._binding,row_id=key,data=value.model_dump(mode="json"),
            material=material(value.model_dump(mode="json")) if key==pkey else None).model_dump(mode="json"))
    request=ControlRequest(request_id=work_policy_key("install",plan.installation_id),
        request_digest=digest([plan.sha256,approval.model_dump(mode="json")]))
    receipt=store.reconcile(request)
    require(history.request==request and dict(receipt.payload["writes"])==expected
            and receipt.payload["guard_revision"]==plan.guard.revision+1,"one exact installation EGT required")
    from .behavior_work_policy_history import validate_work_policy_history
    from .behavior_work_budget import BudgetEnforcementChange
    from .behavior_profile_selection import ConversationProfileSelection, ConversationProfileHead
    bindings=validate_work_policy_history(inv,config,history)
    validate_additions(inv,config,config,set(rows),metadata,bindings_override=bindings,
        max_inventory_entities=None,
        extra_models=(StandingPolicyInstallation,CostLedger,WorkRegistry,WorkRegistration,WorkCostReservation,
                      WorkDispatchPermission,PerWorkRuntimePolicy,WorkPolicyRevocation,CostObservationUpdate,
                      CurrentWorkInboxAccess,BudgetEnforcementChange,ConversationProfileSelection,ConversationProfileHead))
    store.bind_installation(m.installation_record(config)[0],config.sha256,
        protected_rows=tuple(metadata | (set(rows)-mutable)))
    store._verified_migration_epoch=config.sha256
    from .behavior_turn_ticket import restore_turn_tickets
    restore_turn_tickets(store,inv)
    return original
