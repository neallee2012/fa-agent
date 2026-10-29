"""Original Teams responsibility is handed off only by explicit client ACK."""
from datetime import timedelta
import pytest

pytest.importorskip("test_channel_window", reason="pinned offline original Window2 fixture required")
from test_teams_delivery_continuation import (
    stopped_original, original_teams_gap, original_finite_worker_contract, next_window, next_review, original_version,
    bounded_initial_control_quota, closed_v2, original_completed_nonempty_page, installed_empty, operator,
    exact_source_initialization, fixed_original_high_water, expired_claim, source_contract, closed_case,
    offline_socket_guard, original_captured_route_producer, closed_finished, sufficient_initial_budget,
    durable_result, native_acquire, current_rotation, two_rotations, current_cancelled, rotated,
    jwk_client, signing_key,
)
from shared_context.behavior_wire import Command

pytestmark=pytest.mark.parametrize("source_contract",["claim"],indirect=True)


def test_original_stopped_teams_result_cannot_be_fetched_as_ordinary_pull(stopped_original,monkeypatch):
    c,old,authorization,stops=stopped_original
    from shared_context.errors import Problem
    with pytest.raises(Problem):
        c.runtime.fetch(c.work.binding.binding_id,Command(request_id="same-original-web-fetch"),c.principal)
    service=web_handoff_for(stopped_original)
    from shared_context.behavior_migration import MigrationStore,installed_origin
    transact=c.store.transact;phases=[]
    sizes=[];batches=[];encoded=c.store._entity;sent=c.transport.send
    def entity(*args,**kwargs):
        value=encoded(*args,**kwargs);sizes.append(len(value["Payload"]));return value
    def send(request,**kwargs):
        if request.method=="POST":batches.append(len(request.body))
        return sent(request,**kwargs)
    monkeypatch.setattr(c.store,"_entity",entity);monkeypatch.setattr(c.transport,"send",send)
    def committed(request,decide,**kwargs):
        value=transact(request,decide,**kwargs)
        installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
        phases.append(value.payload["request"]["request_id"])
        return value
    monkeypatch.setattr(c.store,"transact",committed)
    plan=service.preview(c.principal)
    approved=service.authorize(c.principal,plan,"offline-explicit-channel-handoff")
    got=service.fetch(c.principal,Command(request_id="same-original-web-fetch"))
    assert got.payload==plan.payload.data and got.binding.route.channel_id=="authenticated-pull"
    from shared_context.behavior_operations import control_key
    binding=c.work.binding
    channel_keys=(control_key("owner",binding.owner.owner_record_id),control_key("delivery-conversation",binding.owner.conversation_id),
                  control_key("delivery-obligation",binding.obligation_id))
    with pytest.raises(Problem) as mismatch:
        c.runtime.dep.delivery._obligation(c.store.read_snapshot(channel_keys),binding.obligation_id,binding.owner)
    assert mismatch.value.body["error"]["code"]=="DELIVERY_CHANNEL_MISMATCH"
    from shared_context import behavior_wire as wire
    acknowledgment=wire.PullAcknowledgment(request_id="same-original-web-ack",exchange_id=got.exchange_id,
        acknowledgment_token=got.acknowledgment_token,
        binding=wire.DeliveryBinding.model_validate_json(got.binding.model_dump_json()))
    service.acknowledge(c.principal,acknowledgment)
    assert service.status(c.principal)["fulfilled"]
    assert c.sends==[] and len(stops)==1
    print("original_signed_source_to_explicit_web_client_ACK; every_handoff_persist_installed="+str(len(phases)))
    assert max(sizes)<=49152 and max(batches)<=4*1024*1024
    print("max_entity="+str(max(sizes))+" max_multipart="+str(max(batches)))
    import os,json
    from pathlib import Path
    if output:=os.environ.get("WEB_HANDOFF_SHAPES"):
        from shared_context.behavior_web_handoff import handoff_key
        row=c.store.read_snapshot((handoff_key("fulfillment",service.scope.handoff_id),))[handoff_key("fulfillment",service.scope.handoff_id)]
        Path(output).write_text(json.dumps({"actual_environment":"NOT_RUN","examples_are_offline_not_native_inputs":True,
            "scope":service.scope.model_dump(mode="json"),"original_scope":old.scope.model_dump(mode="json"),
            "observation":service.observation.model_dump(mode="json"),"plan":plan.model_dump(mode="json"),
            "authorization":approved.model_dump(mode="json"),"fulfillment":row.payload["data"],
            "status":service.status(c.principal),"maximum_sizes":{"entity":max(sizes),"multipart":max(batches)},
            "current_installation_verified_after_persist":phases},indent=2),encoding="utf-8")


def web_handoff_for(stopped):
    from shared_context.behavior_web_handoff import (
        OriginalFailureWebHandoff,WebHandoffScope,WebHandoffObservation,original_failure,code_sha256)
    from shared_context.source_materials import digest
    from shared_context.behavior_recovery_history import _Inventory
    c,old,auth,_=stopped
    c.clock[0]=auth.expires_at+timedelta(minutes=2)
    now=c.clock[0]
    scope=WebHandoffScope(purpose="handoff_original_failure_to_same_human_web",handoff_id="offline-original-web-handoff",
        configuration_sha256=c.config.sha256,original_recovery_id=old.scope.recovery_id,
        original_authorization_sha256=digest(auth.model_dump(mode="json")),event_id=c.work.event_id,
        original_scope_sha256=digest(old.scope.model_dump(mode="json")),
        job_id=c.work.binding.job_id,launch_id=c.launch_id,operator_oid=c.config.identity.human_oid,
        not_before=now,expires_at=now+timedelta(minutes=30))
    inv=c.store.read_inventory()
    _,_,result,_,_=original_failure(_Inventory(inv,c.config),scope,old.scope)
    from shared_context.behavior_delivery import _material
    observation=WebHandoffObservation(configuration_sha256=c.config.sha256,code_sha256=code_sha256(),
        observer_oid=c.config.evidence.observer_oid,scope_sha256=digest(scope.model_dump(mode="json")),
        inventory_sha256=digest(dict(inv.entity_sha256)),guard_revision=inv.guard.payload["revision"],
        original_result_sha256=_material(result.model_dump(mode="json")).sha256,receiver_origin="https://original.example",
        protection_sha256="a"*64,deployment_sha256="b"*64,observed_at=now,expires_at=scope.expires_at)
    return OriginalFailureWebHandoff(c.parent,scope,observation,lambda:observation,original_scope=old.scope)


def test_web_handoff_authorize_denies_changed_plan(stopped_original):
    from shared_context.errors import Problem
    c,_,_,_=stopped_original
    service=web_handoff_for(stopped_original)
    before=c.store.read_inventory()
    plan=service.preview(c.principal)
    for changes in ({"original_route":plan.original_route.model_copy(update={"recipient_id":"other"})},
                    {"code_sha256":"a"*64},{"payload":plan.payload.model_copy(update={"data":b"arbitrary"})}):
        with pytest.raises(Problem):service.authorize(c.principal,plan.model_copy(update=changes),"not-exact")
        assert c.store.read_inventory()==before


def test_web_handoff_fetch_denies_late_contract_changes(stopped_original):
    import copy
    from shared_context.behavior_operations import control_key
    from shared_context.behavior_assistant import key
    from shared_context.behavior_foundry import native_key
    from shared_context.errors import Problem
    from test_behavior_settlement import write
    c,_,_,stops=stopped_original
    service=web_handoff_for(stopped_original)
    plan=service.preview(c.principal)
    service.authorize(c.principal,plan,"offline-web-approved")
    backing=c.transport.backing
    baseline,sequence,time=copy.deepcopy(backing.rows),backing.sequence,c.clock[0]
    restore=service.current_observation
    for fault in ("owner","late-model","late-acquire","late-result","late-claim","route","unknown-row","scope-expired","observer"):
        backing.rows,backing.sequence,c.clock[0]=copy.deepcopy(baseline),sequence,time
        service.current_observation=restore
        if fault=="scope-expired":c.clock[0]=service.scope.expires_at
        elif fault=="observer":service.current_observation=lambda:service.observation.model_copy(update={"deployment_sha256":"c"*64})
        else:
            sid=c.supervisor.clients[c.launch_id].assignment.session_id
            rid={"owner":control_key("owner",c.work.binding.owner.owner_record_id),
                "late-model":key("model-intent",sid,"e"*64),"late-acquire":key("session",sid),
                "late-result":key("result",c.work.binding.work.work_id),"late-claim":native_key("launch",c.launch_id),
                "route":control_key("teams-route",c.config.channels.route_id),"unknown-row":"unexpected-web-row"}[fault]
            row=c.store.read_snapshot((rid,))[rid]
            data=({"request_sha256":"e"*64,"consumed":False} if fault=="late-model" else {"unknown":True} if row is None
                  else {**row.payload["data"],**({"state":"running"} if fault=="late-acquire" else {"claim_id":"late-other-claim"} if fault=="late-claim"
                        else {"aad_object_id":"other"} if fault=="route" else {"epoch":2})})
            write(c.store,rid,data)
        frozen=c.store.read_inventory()
        with pytest.raises(Problem):service.fetch(c.principal,Command(request_id="exact-web-original"))
        assert c.store.read_inventory()==frozen,fault
    backing.rows,backing.sequence,c.clock[0]=copy.deepcopy(baseline),sequence,time
    service.current_observation=restore
    # positive control: the faults, not a broken fetch, caused the denials above
    assert service.fetch(c.principal,Command(request_id="exact-web-original")).payload==plan.payload.data
    assert c.sends==[] and len(stops)==1


def test_web_handoff_all_ackloss_restart_original_only(stopped_original,monkeypatch):
    import copy,json
    from azure.core.exceptions import ServiceRequestError
    from shared_context.behavior_web_handoff import handoff_key,OriginalFailureWebHandoff
    from shared_context.behavior_store import ControlRequest
    from shared_context.behavior_operations import control_key
    from shared_context.behavior_migration import MigrationStore,installed_origin
    from shared_context import behavior_wire as wire
    from shared_context.errors import Problem
    from test_behavior_store import parse_batch
    c,old,original,stops=stopped_original
    backing=c.transport.backing;start,serial,time=copy.deepcopy(backing.rows),backing.sequence,c.clock[0]
    transport=c.transport.send
    for stage in ("authorize","fetch","acknowledge","fulfillment"):
        backing.rows,backing.sequence,c.clock[0]=copy.deepcopy(start),serial,time
        service=web_handoff_for(stopped_original);plan=service.preview(c.principal)
        did=plan.web_delivery.delivery_id
        target={"authorize":handoff_key("authorization",service.scope.handoff_id),
            "fetch":control_key("inbox-fetch",did,handoff_key("fetch",service.scope.handoff_id)),
            "acknowledge":control_key("inbox-acknowledge",did),"fulfillment":handoff_key("fulfillment",service.scope.handoff_id)}[stage]
        lost=[];posts=[]
        def send(request,**kwargs):
            response=transport(request,**kwargs)
            if request.method=="POST":
                ops,_=parse_batch(request)
                receipt=next(json.loads(op[1]["Payload"]) for op in ops if "behavior:v1:receipt:" in op[1]["RowKey"])
                if receipt["request"]["request_id"]==target:
                    posts.append(True)
                    if not lost:
                        lost.append(receipt["request"]);raise ServiceRequestError("offline saved-response loss")
            return response
        monkeypatch.setattr(c.transport,"send",send)
        if stage=="authorize":
            with pytest.raises(Problem):service.authorize(c.principal,plan,"offline-ACKloss-handoff")
        else:
            service.authorize(c.principal,plan,"offline-ACKloss-handoff")
            if stage=="fetch":
                with pytest.raises(Problem):service.fetch(c.principal,wire.Command(request_id="same-web-fetch"))
            else:
                got=service.fetch(c.principal,wire.Command(request_id="same-web-fetch"))
                ack=wire.PullAcknowledgment(request_id="same-web-ACK",exchange_id=got.exchange_id,
                    acknowledgment_token=got.acknowledgment_token,binding=wire.DeliveryBinding.model_validate_json(got.binding.model_dump_json()))
                with pytest.raises(Problem):service.acknowledge(c.principal,ack)
        assert lost and len(posts)==1
        c.store.reconcile(ControlRequest.model_validate_json(json.dumps(lost[0])))
        installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
        restarted=OriginalFailureWebHandoff(c.parent,service.scope,service.observation,service.current_observation,original_scope=old.scope)
        if stage=="authorize":restarted.authorize(c.principal,plan,"offline-ACKloss-handoff")
        elif stage=="fetch":restarted.fetch(c.principal,wire.Command(request_id="same-web-fetch"))
        else:restarted.reconcile(c.principal,wire.Command(request_id="original-ACK-readback"))
        assert len(posts)==1 and c.sends==[] and len(stops)==1
        installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
    print("authorization/fetch/clientACK/fulfillment ACKloss: restart original receipt only; zero second EGT/Teams POST/stop")


def test_web_handoff_last_full_physical_and_serialized_expiry_deny(stopped_original,monkeypatch):
    import copy
    from shared_context.errors import Problem
    c,old,original,stops=stopped_original
    service=web_handoff_for(stopped_original);plan=service.preview(c.principal)
    service.authorize(c.principal,plan,"offline-time-bound-handoff")
    backing=c.transport.backing;baseline,serial,time=copy.deepcopy(backing.rows),backing.sequence,c.clock[0]
    read,guard,entity,transport=c.store.read_inventory,c.store._guard,c.store._entity,c.transport.send
    for stage in ("full","physical","serialization"):
        backing.rows,backing.sequence,c.clock[0]=copy.deepcopy(baseline),serial,time
        flags={"armed":False,"expired":False};posts=[]
        def expire():
            if flags["armed"] and not flags["expired"]:
                flags["expired"]=True;c.clock[0]=service.scope.expires_at
        def encoded(*args,**kwargs):
            value=entity(*args,**kwargs);flags["armed"]=True
            if stage=="serialization":expire()
            return value
        def observed():
            value=read()
            if stage=="full":expire()
            return value
        def physical():
            value=guard()
            if stage=="physical":expire()
            return value
        def sent(request,**kwargs):
            if request.method=="POST":posts.append(True)
            return transport(request,**kwargs)
        monkeypatch.setattr(c.store,"_entity",encoded);monkeypatch.setattr(c.store,"read_inventory",observed)
        monkeypatch.setattr(c.store,"_guard",physical);monkeypatch.setattr(c.transport,"send",sent)
        frozen=read()
        with pytest.raises(Problem):service.fetch(c.principal,Command(request_id="original-finite-fetch"))
        assert flags["expired"] and posts==[] and read()==frozen and c.sends==[]
    assert len(stops)==1
    print("advancing clock after serialization/full validation/physical guard: 0 EGT POST; no expiry renewal")
