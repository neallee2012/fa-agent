"""Explicit original-responsibility channel handoff; never a Teams sender."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from . import behavior_delivery as d, behavior_inbox as inbox
from .behavior_auth import BehaviorPrincipal
from .behavior_recovery import ExpiredTeamsCleanupScope, ExpiredTeamsCleanupAuthorization, ExpiredTeamsCleanupHistory, recovery_key, _cancelled_result
from .behavior_recovery_history import _Inventory, _multi_anchor
from .behavior_teams_recovery import original_teams_state
from .behavior_teams_recovery_history import failure_payload
from .behavior_operations import control_key, BudgetRecord, OwnerRecord, PolicyRecord
from .behavior_workers import worker_key, JobRecord, _Pool
from .behavior_foundry import native_key, _NativeLaunch
from .behavior_migration import code_sha256
from .behavior_store import ControlRequest, ControlWrite, ControlChanges, Digest, InlineMaterial, GUARD_KEY
from .models import Identifier
from .source_materials import digest
from .errors import Problem
from .teams_identity import TEAMS_VERSION


def handoff_key(kind, handoff_id):
    return control_key("original-web-handoff-"+kind, handoff_id)


def require(ok, message):
    if not ok:
        raise Problem(409, "WEB_HANDOFF_INVALID", message)


class WebHandoffScope(d._Model):
    schema_version: Literal["fa-original-web-handoff-scope-v1"] = "fa-original-web-handoff-scope-v1"
    purpose: Literal["handoff_original_failure_to_same_human_web"]
    handoff_id: Identifier
    configuration_sha256: Digest
    original_recovery_id: Identifier
    original_authorization_sha256: Digest
    original_scope_sha256: Digest
    event_id: Digest
    job_id: Identifier
    launch_id: Identifier
    operator_oid: Identifier
    not_before: datetime
    expires_at: datetime
    _times = field_validator("not_before", "expires_at")(d._utc)

    @model_validator(mode="after")
    def bounded(self):
        if not self.not_before < self.expires_at <= self.not_before+timedelta(hours=1):
            raise ValueError("independent web handoff requires at most one hour")
        return self


class WebHandoffObservation(d._Model):
    schema_version: Literal["fa-original-web-handoff-observation-v1"] = "fa-original-web-handoff-observation-v1"
    configuration_sha256: Digest
    code_sha256: Digest
    observer_oid: Identifier
    scope_sha256: Digest
    inventory_sha256: Digest
    guard_revision: Annotated[int, Field(ge=0)]
    original_result_sha256: Digest
    receiver_origin: str
    protection_sha256: Digest
    deployment_sha256: Digest
    observed_at: datetime
    expires_at: datetime
    _times = field_validator("observed_at", "expires_at")(d._utc)


class WebHandoffPlan(d._Model):
    schema_version: Literal["fa-original-web-handoff-plan-v1"] = "fa-original-web-handoff-plan-v1"
    scope: WebHandoffScope
    original_scope: ExpiredTeamsCleanupScope
    code_sha256: Digest
    observation: WebHandoffObservation
    original_route: d.Route
    original_obligation: d._Obligation
    original_index: d.PendingIndexRecord
    web_delivery: d.DeliveryBinding
    payload: InlineMaterial
    original_rows: Annotated[dict[Identifier, Digest], Field(max_length=48)]
    original_receipts: Annotated[dict[Identifier, Digest], Field(max_length=32)]
    inventory_sha256: Digest
    inventory_guard_revision: Annotated[int, Field(ge=0)]
    inventory_guard_sha256: Digest
    inventory_guard_etag: str
    original_job_sha256: Digest

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class WebHandoffAuthorization(d._Model):
    schema_version: Literal["fa-original-web-handoff-authorization-v1"] = "fa-original-web-handoff-authorization-v1"
    plan: WebHandoffPlan
    decision_id: Identifier
    principal_id: Identifier
    approved_at: datetime
    expires_at: datetime
    _times = field_validator("approved_at", "expires_at")(d._utc)


class WebHandoffFulfillment(d._Model):
    schema_version: Literal["fa-original-web-handoff-fulfillment-v1"] = "fa-original-web-handoff-fulfillment-v1"
    handoff_id: Identifier
    authorization_sha256: Digest
    original_obligation_id: Identifier
    original_route: d.Route
    received_binding: d.DeliveryBinding
    receipt: d.VerifiedDeliveryReceipt
    fulfillment: Literal["fulfilled"] = "fulfilled"
    confirmation_level: Literal["client_received"] = "client_received"


@dataclass(frozen=True)
class _WebHandoffAccess:
    service: "OriginalFailureWebHandoff"
    principal: BehaviorPrincipal
    authorization: WebHandoffAuthorization
    access: inbox.VerifiedInboxAccess
    request_id: str
    read_set: tuple[str,...]


def original_failure(view, scope, original_scope):
    config = view.config
    authkey = recovery_key("authorization", scope.original_recovery_id)
    old = view.row(authkey, ExpiredTeamsCleanupAuthorization, material=True)
    require(digest(old.model_dump(mode="json")) == scope.original_authorization_sha256
            and type(original_scope) is ExpiredTeamsCleanupScope
            and digest(original_scope.model_dump(mode="json"))==scope.original_scope_sha256
            and original_scope.recovery_id==scope.original_recovery_id
            and original_scope.not_before==old.plan.not_before and original_scope.expires_at==old.plan.cleanup_expires_at
            and original_scope.observation_sha256==digest(old.plan.observation.model_dump(mode="json"))
            and original_scope.configuration_sha256==scope.configuration_sha256
            and original_scope.operator_oid==scope.operator_oid and original_scope.launch_id==scope.launch_id
            and original_scope.job_id==scope.job_id and original_scope.event_id==scope.event_id
            and old.plan.configuration_sha256 == scope.configuration_sha256 == config.sha256
            and old.plan.event_id == scope.event_id and old.plan.launch_id == scope.launch_id
            and old.plan.binding.job_id == scope.job_id and scope.operator_oid == config.identity.human_oid,
            "original execution authorization does not bind this handoff")
    launch = view.row(native_key("launch", scope.launch_id), _NativeLaunch)
    registered, _, refs, receipts = original_teams_state(view.inventory, config, launch)
    proofkey = native_key("platform-stop", scope.launch_id)
    proof = view.row(proofkey, ExpiredTeamsCleanupHistory, material=True)
    jobkey = worker_key("job", scope.job_id)
    job = view.row(jobkey, JobRecord)
    pool = view.row(worker_key("pool", config.workers.workspace_id), _Pool)
    result = _cancelled_result(SimpleNamespace(verifier_version=config.delivery.verifier_version), launch, proof)
    b = registered.binding
    workkey = control_key("delivery-work-state", b.obligation_id)
    work = view.row(workkey, d._Work, material=result.model_dump(mode="json"))
    require(proof.intent.authorization == old and launch.phase == "sealed" and job.state == "cancelled"
            and job.result_forwarded and not job.completion_credit and not pool.pending and not pool.completions
            and all(slot.binding is None and slot.lease_until is None for slot in pool.slots)
            and result == work.result and result.state == "cancelled" and not result.artifacts
            and not result.known_effects and not result.unresolved_operations,
            "only original positively stopped forwarded zero-model failure is eligible")
    for rid in (authkey, recovery_key("stop-intent", scope.original_recovery_id),
                recovery_key("stop-observation", scope.original_recovery_id), proofkey,
                native_key("evidence", launch.sealed_evidence_id),
                worker_key("completion", scope.job_id, str(job.generation)),
                worker_key("result-forward", scope.job_id), worker_key("work-result", result.evidence_id), workkey,
                native_key("launch",scope.launch_id),jobkey,worker_key("pool",config.workers.workspace_id),
                control_key("owner",b.owner.owner_record_id),control_key("teams-route",config.channels.route_id),
                control_key("teams-route-access",config.channels.route_id)):
        refs[rid] = view.hash(rid)
    for rid, receipt in view.receipts.items():
        if old.plan.inventory_guard_revision < receipt.guard_revision and any(k in refs for k, _ in receipt.writes):
            receipts[rid] = digest(view.inventory.receipts[rid].payload)
    return old, b, result, refs, receipts


class OriginalFailureWebHandoff:
    """One independent original-result receiver, not generic channel override."""
    def __init__(self, composition, scope, observation, current_observation, *, original_scope):
        require(type(scope) is WebHandoffScope and type(observation) is WebHandoffObservation
                and callable(current_observation), "explicit trusted web handoff registration required")
        self.composition, self.runtime, self.config = composition, composition.runtime, composition.config
        self.scope, self.observation, self.current_observation = scope, observation, current_observation
        self.original_scope=original_scope
        self.store = self.runtime.store
        self.registration = scope
        self.inbox = inbox.PullInbox(self.store, replace(self.runtime.dep.inbox.registration,
            verify_access=lambda _: (_ for _ in ()).throw(Problem(403,"WEB_HANDOFF_ACCESS_DENIED","explicit handoff only"))))
        self.core = d.DeliveryCore(self.store, clock=self.runtime.clock,
            registration=replace(self.runtime.dep.delivery.registration,channel_adapter=self.inbox))
        self.inbox.attach(self.core)
        self.inbox.register_retained_results(self)

    def _human(self, principal, *, approve=False, current=True):
        if (not isinstance(principal,BehaviorPrincipal) or principal.kind!="human"
                or principal.oid != self.scope.operator_oid or principal.oid != self.config.identity.human_oid
                or principal.tenant_id != self.config.identity.tenant_id or principal.actor_id is not None):
            raise Problem(403,"WEB_HANDOFF_HUMAN_REQUIRED","same original designated human required")
        principal.require(self.config.identity.team_id,"read")
        if approve:principal.require(self.config.identity.team_id,"approve")
        if current:
            if code_sha256()!=self.observation.code_sha256 or self.current_observation()!=self.observation:
                raise Problem(403,"WEB_HANDOFF_OBSERVATION_CHANGED","current exact receiver observation required")
            self._time()

    def _time(self, auth=None):
        now=self.runtime._now()
        start=max(self.scope.not_before,self.observation.observed_at,self.scope.not_before if auth is None else auth.approved_at)
        until=min(self.scope.expires_at,self.observation.expires_at,self.scope.expires_at if auth is None else auth.expires_at)
        if not start<=now<until:raise Problem(403,"WEB_HANDOFF_EXPIRED","independent original-result window expired")

    def preview(self, principal):
        self._human(principal,approve=True)
        self.composition.installation()
        inv=self.store.read_inventory();view=_Inventory(inv,self.config)
        old,b,result,refs,receipts=original_failure(view,self.scope,self.original_scope)
        from .behavior_teams_continuation import _no_send,continuation_key
        did=_no_send(inv,b)
        require(continuation_key(did) not in inv.rows and handoff_key("authorization",self.scope.handoff_id) not in inv.rows,
                "existing send/continuation/handoff cannot acquire another delivery grant")
        payload=d._material(failure_payload(b,result).model_dump(mode="json"))
        route=d.Route(tenant_id=b.tenant_id,account_id=self.config.delivery.entry_id,recipient_id=b.human_oid,
                      channel_id=inbox.INBOX_CHANNEL_ID,thread_id=b.owner.conversation_id)
        delivery=d.DeliveryBinding(obligation_id=b.obligation_id,delivery_id=did,version=1,payload_digest=payload.sha256,
                                  route=route,channel_version=inbox.INBOX_VERSION,artifacts=())
        o=self.observation
        require(o.configuration_sha256==self.config.sha256 and o.observer_oid==self.config.evidence.observer_oid
                and o.scope_sha256==digest(self.scope.model_dump(mode="json"))
                and o.inventory_sha256==digest(dict(inv.entity_sha256)) and o.guard_revision==view.guard.revision
                and o.original_result_sha256==d._material(result.model_dump(mode="json")).sha256
                and self.runtime._now()-o.observed_at<=timedelta(minutes=5),
                "independent receiver/retention observation differs")
        from urllib.parse import urlsplit
        origin=urlsplit(o.receiver_origin)
        require(origin.scheme=="https" and origin.netloc and not origin.username and not origin.query and not origin.fragment
                and origin.path in ("","/"),"fixed HTTPS receiver origin required")
        plan=WebHandoffPlan(scope=self.scope,original_scope=self.original_scope,code_sha256=code_sha256(),observation=o,
            original_route=b.route,original_obligation=view.row(control_key("delivery-obligation",b.obligation_id),d._Obligation),
            original_index=view.row(control_key("delivery-index",b.owner.root_budget_id),d.PendingIndexRecord),
            web_delivery=delivery,payload=payload,original_rows=refs,original_receipts=receipts,
            inventory_sha256=digest(dict(inv.entity_sha256)),inventory_guard_revision=view.guard.revision,
            inventory_guard_sha256=inv.entity_sha256[GUARD_KEY],inventory_guard_etag=inv.guard.etag,original_job_sha256=view.hash(worker_key("job",b.job_id)))
        require(self.store.read_inventory().entity_sha256==inv.entity_sha256,"handoff preview changed")
        self._time()
        return plan

    def authorize(self, principal, plan, decision_id):
        self._human(principal,approve=True)
        authkey=handoff_key("authorization",self.scope.handoff_id)
        prior=self.store.read_snapshot((authkey,))[authkey]
        if prior:
            auth=d._parse(prior.payload["data"],WebHandoffAuthorization)
            require(auth.plan==plan and auth.decision_id==decision_id,"original handoff decision differs")
            self._authorized(principal,auth)
            return auth
        require(plan==self.preview(principal),"exact readonly handoff plan required")
        auth=WebHandoffAuthorization(plan=plan,decision_id=decision_id,principal_id=principal.oid,
            approved_at=self.runtime._now(),expires_at=min(self.scope.expires_at,self.observation.expires_at))
        inv=self.store.read_inventory()
        obkey=control_key("delivery-obligation",plan.web_delivery.obligation_id)
        outkey=control_key("delivery-outbox",plan.web_delivery.delivery_id)
        ob=d._parse(inv.rows[obkey].payload["data"],d._Obligation)
        require(ob.final_delivery_id is None and ob.fulfillment=="pending","original responsibility already prepared")
        out=d._Outbox(schema_version=1,runtime_version=d.RUNTIME_VERSION,evidence_mode="native",
            configuration_digest=self.core.configuration_digest(),binding=plan.web_delivery,message_kind="failure")
        request=ControlRequest(request_id=authkey,request_digest=digest(auth.model_dump(mode="json")),read_set=(obkey,outkey))
        def decide(rows):
            require(rows[obkey]==inv.rows[obkey] and rows[outkey] is None,"original handoff boundary changed")
            return ControlChanges(writes=(
                ControlWrite(row_id=authkey,mode="create",data=auth.model_dump(mode="json"),material=d._material(auth.model_dump(mode="json"))),
                self.core._write(outkey,out,"create",plan.payload),
                self.core._write(obkey,d._update(ob,final_delivery_id=plan.web_delivery.delivery_id,final_version=1,last_version=1)),
            ),result={"handoff_authorization":authkey,"channel_handoff":"msteams-to-authenticated-pull","fulfilled":False})
        def gate():
            self._human(principal,approve=True)
            require(self.store.read_inventory().entity_sha256==inv.entity_sha256,"handoff authority changed before commit")
            self._time(auth)
        self.store.transact(request,decide,expected_guard=inv.guard,before_send=gate)
        return auth

    def _authorized(self, principal, auth):
        self._human(principal)
        require(auth.plan.scope==self.scope and auth.principal_id==principal.oid,"original web grant differs")
        self.composition.installation()
        inv=self.store.read_inventory()
        view=_Inventory(inv,self.config)
        require(validate_web_handoff(view,self.config)==auth,"stored exact handoff grant required")
        self._time(auth)

    def _auth(self):
        rid=handoff_key("authorization",self.scope.handoff_id)
        row=self.store.read_snapshot((rid,))[rid]
        require(row is not None,"original web handoff authorization is not installed")
        return d._parse(row.payload["data"],WebHandoffAuthorization)

    def _keys(self, auth):
        p=auth.plan;_,b,_,_,_=original_failure(_Inventory(self.store.read_inventory(),self.config),self.scope,self.original_scope)
        return tuple(dict.fromkeys((handoff_key("authorization",self.scope.handoff_id),
            control_key("owner",b.owner.owner_record_id),control_key("delivery-conversation",b.owner.conversation_id),
            control_key("delivery-obligation",b.obligation_id),control_key("delivery-outbox",p.web_delivery.delivery_id),
            control_key("policy",b.owner.policy_id))))

    def _access(self, principal, request_id, action):
        auth=self._auth();self._authorized(principal,auth)
        _,b,_,_,_=original_failure(_Inventory(self.store.read_inventory(),self.config),self.scope,self.original_scope)
        access=inbox.VerifiedInboxAccess(evidence_id=handoff_key(action,self.scope.handoff_id),principal_id=principal.oid,
            authentication_version=self.config.authentication_version,binding=auth.plan.web_delivery,action=action,
            policy_revision=self.core._row(self.store.read_snapshot((control_key("policy",b.owner.policy_id),)),
                                          control_key("policy",b.owner.policy_id),PolicyRecord).revision,
            observed_at=auth.approved_at,expires_at=auth.expires_at,
            proof=d._material({"web_handoff_authorization":handoff_key("authorization",self.scope.handoff_id),"authorization_sha256":digest(auth.model_dump(mode="json"))}).data)
        return _WebHandoffAccess(self,principal,auth,access,request_id,self._keys(auth)),b.owner

    def _check_permit(self,permit,command,context,action):
        self._owned(permit)
        self._authorized(permit.principal,permit.authorization)
        require(permit.service is self and permit.access.action==action and command.access_evidence_id==permit.access.evidence_id
                and command.obligation_id==permit.access.binding.obligation_id and command.delivery_id==permit.access.binding.delivery_id,
                "exact opaque handoff access required")
        return permit.access

    def _owned(self,permit):
        from .behavior_migration import _WRITE_VALIDATION, _execution_owner
        state=_WRITE_VALIDATION.get()
        require(type(permit) is _WebHandoffAccess and permit.service is self
                and state is not None and state.active and state.store is self.store
                and state.owner==_execution_owner(),"handoff permit requires its active synchronous store/thread/task")

    def _gate(self,rows,permit):
        self._authorized(permit.principal,permit.authorization)
        require(all(self.store.read_snapshot(tuple(rows))[k]==v for k,v in rows.items()),"handoff current predicates changed")
        self._time(permit.authorization)

    def _bound(self,rows,command,context,access):
        self.core._owner(rows,context)
        ob=self.core._row(rows,control_key("delivery-obligation",command.obligation_id),d._Obligation)
        auth=self._auth();box=self.core._row(rows,control_key("delivery-outbox",command.delivery_id),d._Outbox)
        material=d._parse(rows[control_key("delivery-outbox",command.delivery_id)].payload["material"],InlineMaterial)
        require(ob.route==auth.plan.original_route and ob.channel_version==TEAMS_VERSION
                and ob.required_confirmation_level=="provider_accepted" and box.binding==auth.plan.web_delivery==access.binding
                and material==auth.plan.payload and ob.final_delivery_id==box.binding.delivery_id,
                "formal original/web handoff route contract differs")
        return ob,box,material

    def fetch(self,principal,command):
        permit,owner=self._access(principal,command.request_id,"fetch")
        p=permit.authorization.plan
        return self.store.validated_writes(lambda:self.inbox.fetch_retained(inbox.InboxFetch(obligation_id=p.web_delivery.obligation_id,
            delivery_id=p.web_delivery.delivery_id,access_evidence_id=permit.access.evidence_id),owner,permit),
            lambda:self._authorized(principal,permit.authorization))

    def acknowledge(self,principal,command):
        principal.require(self.config.identity.team_id,"acknowledge")
        permit,owner=self._access(principal,command.request_id,"acknowledge")
        p=permit.authorization.plan
        return self.store.validated_writes(lambda:self.inbox.acknowledge_retained(inbox.InboxAcknowledgment(
            obligation_id=p.web_delivery.obligation_id,delivery_id=p.web_delivery.delivery_id,access_evidence_id=permit.access.evidence_id,
            exchange_id=command.exchange_id,acknowledgment_token=command.acknowledgment_token,
            binding=d.DeliveryBinding.model_validate_json(command.binding.model_dump_json())),owner,permit),
            lambda:self._authorized(principal,permit.authorization))

    def _complete(self,delivery_id,context,permit):
        self._owned(permit)
        auth=self._auth();self._authorized(permit.principal,auth)
        require(permit.service is self and permit.authorization==auth and permit.access.action=="acknowledge",
                "original authenticated ACK permit required")
        proof=self.inbox.verify_receipt(control_key("inbox-ack-evidence",delivery_id))
        p=auth.plan
        require(proof.binding==p.web_delivery and proof.confirmed_levels==("client_received",),"original web ACK required")
        obkey=control_key("delivery-obligation",p.web_delivery.obligation_id)
        fkey=handoff_key("fulfillment",self.scope.handoff_id)
        quota=self.core._quota_keys(context);index=control_key("delivery-index",context.root_budget_id)
        outkey=control_key("delivery-outbox",delivery_id)
        reads=tuple(dict.fromkeys((obkey,index,*quota,outkey,control_key("owner",context.owner_record_id),
            control_key("delivery-conversation",context.conversation_id),fkey,*self._keys(auth))))
        request=ControlRequest(request_id=fkey,request_digest=digest(proof.model_dump(mode="json")),read_set=reads)
        def decide(rows):
            self._time(auth);self.core._owner(rows,context)
            ob=self.core._row(rows,obkey,d._Obligation)
            require(ob.route==p.original_route and ob.fulfillment=="pending" and ob.fulfillment_credit==1
                    and ob.final_delivery_id==delivery_id,"original responsibility not available for transfer fulfillment")
            updated=d._update(ob,fulfillment="fulfilled",delivery_state="fulfilled",fulfillment_credit=0,
                fulfillment_receipt_id=proof.evidence_id,confirmed_levels=("client_received",),due_pending=False,automation="stopped")
            q=self.core._quotas(rows,context)
            require(all(v.pending>0 for v in q) and q[-1].pending==len(self.core._index(rows,context).entries),
                    "original pending reservations missing")
            final=WebHandoffFulfillment(handoff_id=self.scope.handoff_id,authorization_sha256=digest(auth.model_dump(mode="json")),
                original_obligation_id=ob.obligation_id,original_route=p.original_route,received_binding=p.web_delivery,receipt=proof)
            return ControlChanges(writes=(
                *(self.core._write(k,d._update(v,pending=v.pending-1)) for k,v in zip(quota,q)),
                *self.core._index_writes(rows,context,updated),self.core._write(obkey,updated),
                self.core._write(outkey,d._update(self.core._row(rows,outkey,d._Outbox),send_outcome="emitted",
                    confirmed_levels=("client_received",),receipt_count=1),material=p.payload),
                self.core._write(fkey,final,"create",d._material(final.model_dump(mode="json")))),
                result={"fulfillment":"fulfilled","confirmation_level":"client_received","confirmed_levels":["client_received"],
                        "fulfillment_receipt_id":proof.evidence_id,"channel_handoff_id":self.scope.handoff_id})
        return self.store.transact(request,decide,before_send=lambda:self._authorized(permit.principal,auth))

    def status(self,principal):
        self._human(principal,current=False);auth=self._auth()
        self.composition.installation()
        fkey=handoff_key("fulfillment",self.scope.handoff_id)
        fulfilled=self.store.read_snapshot((fkey,))[fkey] is not None
        return {"schema_version":"fa-original-web-handoff-status-v1","handoff_id":self.scope.handoff_id,
                "job_id":self.scope.job_id,"obligation_id":auth.plan.web_delivery.obligation_id,
                "delivery_id":auth.plan.web_delivery.delivery_id,"payload_sha256":auth.plan.payload.sha256,
                "work_state":"cancelled","fulfillment":"fulfilled" if fulfilled else "pending","fulfilled":fulfilled,
                "confirmation_level":"client_received" if fulfilled else None,
                "confirmed_levels":["client_received"] if fulfilled else [],"supported_confirmation_level":"client_received",
                "expired":self.runtime._now()>=auth.expires_at,"dispatch_performed":False}

    def reconcile(self,principal,command):
        principal.require(self.config.identity.team_id,"acknowledge")
        permit,owner=self._access(principal,command.request_id,"acknowledge")
        return self.store.validated_writes(
            lambda:self._complete(permit.access.binding.delivery_id,owner,permit),
            lambda:self._authorized(principal,permit.authorization))


def validate_web_handoff(view,config):
    candidates=[rid for rid,row in view.inventory.rows.items() if row.payload["data"].get("schema_version")=="fa-original-web-handoff-authorization-v1"]
    if not candidates:return None
    require(len(candidates)==1,"one original web handoff only")
    auth=view.row(candidates[0],WebHandoffAuthorization,material=True);p=auth.plan;s=p.scope
    old,b,result,refs,receipts=original_failure(view,s,p.original_scope)
    require(candidates[0]==handoff_key("authorization",s.handoff_id)
            and refs==p.original_rows and {k:v for k,v in receipts.items() if view.receipts[k].guard_revision<=p.inventory_guard_revision}==p.original_receipts
            and p.original_route==b.route and auth.principal_id==b.human_oid and s.not_before<=auth.approved_at<auth.expires_at<=s.expires_at
            and p.payload==d._material(failure_payload(b,result).model_dump(mode="json")),"original web handoff snapshot/identity differs")
    obkey=control_key("delivery-obligation",b.obligation_id);outkey=control_key("delivery-outbox",p.web_delivery.delivery_id)
    request=ControlRequest(request_id=candidates[0],request_digest=digest(auth.model_dump(mode="json")),read_set=(obkey,outkey))
    original_ob=p.original_obligation
    prior_writes=[v for v in view.receipts.values() if v.guard_revision<=p.inventory_guard_revision and obkey in dict(v.writes)]
    require(prior_writes and dict(max(prior_writes,key=lambda v:v.guard_revision).writes)[obkey]==view.image_hash(obkey,original_ob)
            and original_ob.route==b.route and original_ob.obligation_id==b.obligation_id
            and original_ob.final_delivery_id is None and original_ob.fulfillment=="pending",
            "original pending obligation must match its exact snapshot receipt")
    indexkey=control_key("delivery-index",b.owner.root_budget_id)
    index_writers=[v for v in view.receipts.values() if v.guard_revision<=p.inventory_guard_revision and indexkey in dict(v.writes)]
    require(index_writers and dict(max(index_writers,key=lambda v:v.guard_revision).writes)[indexkey]==view.image_hash(indexkey,p.original_index)
            and any(e.obligation_id==b.obligation_id for e in p.original_index.entries),
            "original pending index must be the exact retained snapshot")
    expected_ob=d._update(original_ob,final_delivery_id=p.web_delivery.delivery_id,final_version=1,last_version=1)
    expected_box=d._Outbox(configuration_digest=original_ob.configuration_digest,evidence_mode="native",
                          binding=p.web_delivery,message_kind="failure")
    receipt=view.receipt(request,(candidates[0],outkey,obkey),exact={candidates[0]:view.hash(candidates[0]),
        obkey:view.image_hash(obkey,expected_ob),outkey:view.image_hash(outkey,expected_box)})
    require(receipt.guard_revision==p.inventory_guard_revision+1,"handoff authorization snapshot moved")
    anchor=SimpleNamespace(**{k:getattr(p,k) for k in ("inventory_guard_revision","inventory_guard_sha256","inventory_sha256","original_job_sha256")},
                           configuration_sha256=config.sha256,binding=old.plan.binding)
    _multi_anchor(view,SimpleNamespace(plan=anchor))
    box=view.row(outkey,d._Outbox);ob=view.row(obkey,d._Obligation)
    expected_route=d.Route(tenant_id=b.tenant_id,account_id=config.delivery.entry_id,recipient_id=b.human_oid,
                          channel_id=inbox.INBOX_CHANNEL_ID,thread_id=b.owner.conversation_id)
    o=p.observation
    require(box.binding==p.web_delivery and view.inventory.rows[outkey].payload["material"]==p.payload.model_dump(mode="json")
            and ob.route==p.original_route and ob.required_confirmation_level=="provider_accepted"
            and p.web_delivery.route==expected_route and p.web_delivery.channel_version==inbox.INBOX_VERSION
            and p.web_delivery.delivery_id==control_key("delivery-final-id",b.obligation_id)
            and p.web_delivery.obligation_id==b.obligation_id and p.web_delivery.version==1 and not p.web_delivery.artifacts
            and p.web_delivery.payload_digest==p.payload.sha256
            and o.configuration_sha256==config.sha256 and o.code_sha256==p.code_sha256
            and o.observer_oid==config.evidence.observer_oid and o.scope_sha256==digest(s.model_dump(mode="json"))
            and o.inventory_sha256==p.inventory_sha256 and o.guard_revision==p.inventory_guard_revision
            and o.original_result_sha256==d._material(result.model_dump(mode="json")).sha256
            and o.observed_at<=auth.approved_at<auth.expires_at<=o.expires_at
            and auth.approved_at-o.observed_at<=timedelta(minutes=5),
            "original route/receiver/retention must match explicit handoff")
    exchange_key=control_key("inbox-exchange",p.web_delivery.delivery_id)
    owner=b.owner
    permitkeys=(candidates[0],control_key("owner",owner.owner_record_id),control_key("delivery-conversation",owner.conversation_id),
                obkey,outkey,control_key("policy",owner.policy_id))
    basekeys=(*permitkeys[1:],exchange_key)
    def access(action):
        return inbox.VerifiedInboxAccess(evidence_id=handoff_key(action,s.handoff_id),principal_id=b.human_oid,
            authentication_version=config.authentication_version,binding=p.web_delivery,action=action,
            policy_revision=view.row(control_key("policy",owner.policy_id),PolicyRecord).revision,
            observed_at=auth.approved_at,expires_at=auth.expires_at,
            proof=d._material({"web_handoff_authorization":candidates[0],"authorization_sha256":digest(auth.model_dump(mode="json"))}).data)
    if exchange_key in view.inventory.rows:
        ex=view.row(exchange_key,inbox._Exchange,material=None)
        fetchaccess=access("fetch")
        fetchcommand=inbox.InboxFetch(obligation_id=b.obligation_id,delivery_id=p.web_delivery.delivery_id,access_evidence_id=fetchaccess.evidence_id)
        fetchrequest=ControlRequest(request_id=control_key("inbox-fetch",p.web_delivery.delivery_id,fetchaccess.evidence_id),
            request_digest=digest([fetchcommand.model_dump(mode="json"),owner.model_dump(mode="json"),fetchaccess.model_dump(mode="json")]),
            read_set=tuple(dict.fromkeys((*basekeys,*permitkeys))))
        require(ex.binding==p.web_delivery and ex.principal_id==b.human_oid and ex.fetch_count==1
                and ex.authorization_evidence==fetchaccess and ex.fetch_request==fetchrequest
                and view.inventory.rows[exchange_key].payload["material"]==d._material(fetchaccess.model_dump(mode="json")).model_dump(mode="json"),
                "handoff exchange differs")
        expected_ob=d._update(expected_ob,delivery_state="delivery_unknown")
        expected_box=d._update(expected_box,send_outcome="unknown")
        initial_ex=d._update(ex,acknowledged=False,receipt_evidence_id=None)
        view.receipt(fetchrequest,(outkey,obkey,exchange_key),exact={outkey:view.image_hash(outkey,expected_box),
            obkey:view.image_hash(obkey,expected_ob),exchange_key:view.image_hash(exchange_key,initial_ex)},result={"exchange_id":ex.exchange_id})
        if ex.acknowledged:
            ackkey=control_key("inbox-ack-access",ex.receipt_evidence_id);proofkey=control_key("inbox-receipt-evidence",ex.receipt_evidence_id)
            ack=view.row(ackkey,inbox._Acknowledged,material=True);proof=view.row(proofkey,d.VerifiedDeliveryReceipt,material=True)
            ackrequest=ControlRequest(request_id=control_key("inbox-acknowledge",p.web_delivery.delivery_id),
                request_digest=digest([p.web_delivery.delivery_id,ex.exchange_id,ex.acknowledgment_token,p.web_delivery.model_dump(mode="json"),b.human_oid]),
                read_set=tuple(dict.fromkeys((*basekeys,proofkey,*permitkeys))))
            require(proof.binding==p.web_delivery and proof.proof==inbox._ack_proof(ex,ack) and proof.confirmed_levels==("client_received",)
                    and proof.send_outcome=="emitted" and proof.verifier_version==config.delivery.verifier_version
                    and proof.dispatch_authorization_ref==fetchrequest.request_id
                    and ex.receipt_evidence_id==control_key("inbox-ack-evidence",p.web_delivery.delivery_id)==proof.evidence_id
                    and ack.access==access("acknowledge") and ack.request==ackrequest and ack.exchange_id==ex.exchange_id,
                    "exact authenticated web ACK missing")
            view.receipt(ackrequest,(exchange_key,proofkey,ackkey),exact={k:view.hash(k) for k in (exchange_key,proofkey,ackkey)})
    finalkey=handoff_key("fulfillment",s.handoff_id)
    if finalkey in view.inventory.rows:
        final=view.row(finalkey,WebHandoffFulfillment,material=True)
        require(final.authorization_sha256==digest(auth.model_dump(mode="json")) and final.original_route==b.route
                and final.received_binding==p.web_delivery and ob.fulfillment=="fulfilled"
                and ob.fulfillment_receipt_id==final.receipt.evidence_id and ob.confirmed_levels==("client_received",)
                and exchange_key in view.inventory.rows and ex.acknowledged,"handoff cannot fulfill without original client ACK")
        require(final.receipt==proof and final.original_obligation_id==b.obligation_id and final.handoff_id==s.handoff_id,
                "fulfillment must refer to this same acknowledged original obligation")
        expected_ob=d._update(expected_ob,fulfillment="fulfilled",delivery_state="fulfilled",fulfillment_credit=0,
            fulfillment_receipt_id=proof.evidence_id,confirmed_levels=("client_received",),due_pending=False,automation="stopped")
        expected_box=d._update(expected_box,send_outcome="emitted",confirmed_levels=("client_received",),receipt_count=1)
        quotas=tuple(control_key("delivery-quota",kind,value) for kind,value in (
            ("team",config.identity.team_id),("entry",config.delivery.entry_id),("root",owner.root_budget_id)))
        index=control_key("delivery-index",owner.root_budget_id)
        reads=tuple(dict.fromkeys((obkey,index,*quotas,outkey,control_key("owner",owner.owner_record_id),
            control_key("delivery-conversation",owner.conversation_id),finalkey,*permitkeys)))
        finalrequest=ControlRequest(request_id=finalkey,request_digest=digest(proof.model_dump(mode="json")),read_set=reads)
        view.receipt(finalrequest,(*quotas,index,obkey,outkey,finalkey),
            exact={k:view.hash(k) for k in (*quotas,index,obkey,outkey,finalkey)},
            result={"fulfillment":"fulfilled","confirmation_level":"client_received","confirmed_levels":["client_received"],
                    "fulfillment_receipt_id":proof.evidence_id,"channel_handoff_id":s.handoff_id})
        for rid in quotas:
            q=view.row(rid,d.QuotaRecord)
            earlier=[v for v in view.receipts.values() if v.guard_revision<p.inventory_guard_revision+1 and rid in dict(v.writes)]
            require(earlier and view.image_hash(rid,d._update(q,pending=q.pending+1))==
                    dict(max(earlier,key=lambda v:v.guard_revision).writes)[rid],"only original pending quota may decrease")
        idx=view.row(index,d.PendingIndexRecord)
        require(idx==d._update(p.original_index,revision=p.original_index.revision+1,
            entries=tuple(e for e in p.original_index.entries if e.obligation_id!=b.obligation_id)),
            "only the original responsibility may leave its exact pending index")
    require(ob==expected_ob and box==expected_box,"handoff durable phase image is not exact")
    return auth
