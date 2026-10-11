"""Original stopped/forwarded result cannot silently cross approved code pins."""
from datetime import timedelta
import copy
import json
from pathlib import Path
import os
import pytest
from shared_context.errors import Problem

pytest.importorskip("test_channel_window", reason="pinned offline original Window2 fixture required")
from test_teams_expired_cleanup import (
    original_teams_gap, original_finite_worker_contract, next_window, next_review, original_version,
    bounded_initial_control_quota, closed_v2, original_completed_nonempty_page, installed_empty, operator,
    exact_source_initialization, fixed_original_high_water, expired_claim, source_contract, closed_case,
    offline_socket_guard, original_captured_route_producer, closed_finished, sufficient_initial_budget,
    durable_result, native_acquire, current_rotation, two_rotations, current_cancelled, rotated,
    jwk_client, signing_key, cleanup_service,
)

pytestmark = pytest.mark.parametrize("source_contract", ["claim"], indirect=True)


@pytest.fixture
def stopped_original(original_teams_gap,monkeypatch):
    c=original_teams_gap
    c.clock[0]=c.config.channels.valid_until+timedelta(minutes=1)
    service,stops=cleanup_service(c)
    approved=service.authorize(c.principal,service.plan(c.principal),"offline-original-cleanup")
    service.execute(c.principal,approved)
    assert c.workers.inspect(c.work.binding.job_id).result_forwarded and len(stops)==1
    from shared_context.behavior_migration import code_sha256
    monkeypatch.setattr("shared_context.behavior_recovery.code_sha256",code_sha256)
    with pytest.raises(Problem) as old_writer:
        service.deliver(c.principal,approved)
    assert old_writer.value.body["error"]["code"]=="RECOVERY_AUTHORIZATION_INVALID"
    return c,service,approved,stops


def continuation_for(stopped):
    from shared_context.behavior_teams_continuation import (
        DeliveryContinuationScope,DeliveryContinuationObservation,OriginalFailureDeliveryContinuation,_original,code_sha256)
    from shared_context.behavior_recovery_history import _Inventory
    from shared_context.source_materials import digest
    c,old,authorization,_=stopped
    now=c.clock[0]+timedelta(seconds=1)
    c.clock[0]=now
    scope=DeliveryContinuationScope(purpose="deliver_original_confirmed_failure_only",
        continuation_id="offline-original-delivery-only",configuration_sha256=c.config.sha256,
        operator_oid=c.config.identity.human_oid,event_id=c.work.event_id,job_id=c.work.binding.job_id,
        launch_id=c.launch_id,original_recovery_id=old.scope.recovery_id,
        original_scope_sha256=digest(old.scope.model_dump(mode="json")),
        original_authorization_sha256=digest(authorization.model_dump(mode="json")),
        original_code_sha256=authorization.plan.code_sha256,delivery_code_sha256=code_sha256(),
        observation_sha256="0"*64,not_before=now,expires_at=authorization.expires_at)
    inv=c.store.read_inventory()
    _,_,_,_,_,facts=_original(_Inventory(inv,c.config),scope,old.scope)
    observed=DeliveryContinuationObservation(purpose=scope.purpose,evidence_id="offline-original-delivery-observer",
        observer_oid=c.config.evidence.observer_oid,configuration_sha256=c.config.sha256,
        delivery_code_sha256=scope.delivery_code_sha256,original_authorization_sha256=scope.original_authorization_sha256,
        inventory_sha256=digest(dict(inv.entity_sha256)),guard_revision=inv.guard.payload["revision"],
        **facts,protection_sha256="c"*64,deployment_sha256="d"*64,
        observed_at=now,expires_at=scope.expires_at,proof=b"OFFLINE current original failure delivery; not native evidence")
    scope=scope.model_copy(update={"observation_sha256":digest(observed.model_dump(mode="json"))})
    service=OriginalFailureDeliveryContinuation(c.parent,scope,original_scope=old.scope,
        observation=observed,current_observation=lambda:observed)
    return service


def test_new_typed_continuation_delivers_original_failure_without_stop(stopped_original,monkeypatch):
    from shared_context.behavior_migration import MigrationStore,installed_origin
    c,old,original,stops=stopped_original
    service=continuation_for(stopped_original)
    before=c.store.read_inventory()
    plan=service.preview(c.principal)
    assert c.store.read_inventory()==before
    assert plan.scope.expires_at==original.expires_at
    transact=c.store.transact; phases=[]; sizes=[]; bodies=[]
    entity,send=c.store._entity,c.transport.send
    def encoded(*args,**kwargs):
        value=entity(*args,**kwargs);sizes.append(len(value["Payload"]));return value
    def transported(request,**kwargs):
        if request.method=="POST": bodies.append(len(request.body))
        return send(request,**kwargs)
    monkeypatch.setattr(c.store,"_entity",encoded)
    monkeypatch.setattr(c.transport,"send",transported)
    def committed(request,decide,**kwargs):
        value=transact(request,decide,**kwargs)
        installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
        phases.append(value.payload)
        return value
    monkeypatch.setattr(c.store,"transact",committed)
    approved=service.authorize(c.principal,plan,"offline-delivery-new-code-approved")
    result=service.deliver(c.principal,approved)
    assert result.payload["result"]["fulfillment"]=="fulfilled"
    assert result.payload["result"]["confirmed_levels"]==["provider_accepted"]
    assert c.sends==["original-Teams-result"] and len(stops)==1
    assert service.reconcile(c.principal,approved)["dispatch_performed"] is False
    assert service.deliver(c.principal,approved) and c.sends==["original-Teams-result"]
    after=c.store.read_inventory()
    assert all(after.receipts[k]==v for k,v in before.receipts.items())
    assert not hasattr(service,"execute") and not hasattr(service,"stop")
    assert max(sizes)<=49152 and max(bodies)<=4*1024*1024
    print(f"old_code={original.plan.code_sha256}; new_code={service.scope.delivery_code_sha256}; "
          f"same_deadline={approved.expires_at==original.expires_at}; max_entity={max(sizes)} multipart={max(bodies)} "
          f"continued_stop_calls=0 original_sender_POST=1")
    if output:=os.environ.get("CONTINUATION_OUTPUT"):
        Path(output).write_text(json.dumps({"scope":service.scope.model_dump(mode="json"),
            "observation":service.observation.model_dump(mode="json"),"plan":plan.model_dump(mode="json"),
            "authorization":approved.model_dump(mode="json"),"phases":phases,
            "original_authorization_sha256":service.scope.original_authorization_sha256,
            "sizes":{"entity":max(sizes),"multipart":max(bodies)}},indent=2),encoding="utf-8")


def test_continuation_has_only_four_http_actions_and_same_original_expiry(stopped_original):
    from fastapi.testclient import TestClient
    from shared_context.behavior_teams_continuation_api import create_continuation_app
    from test_m3_auth import token
    c,old,original,stops=stopped_original
    service=continuation_for(stopped_original)
    app=create_continuation_app(c.parent,service.scope,original_scope=old.scope,
        observation=service.observation,current_observation=service.current_observation)
    assert {route.path.rsplit("/",1)[-1] for route in app.routes}=={"preview","authorize","deliver","reconcile"}
    client=TestClient(app)
    prefix="/operator/v1/original-failure-delivery"
    identity=c.config.identity
    base={"tid":identity.tenant_id,"iss":f"https://sts.windows.net/{identity.tenant_id}/","aud":identity.audience,
          "oid":identity.human_oid,"appid":identity.admin_client_id,"scp":"Behavior.Read Behavior.Approve"}
    headers={"Authorization":"Bearer "+token(c.h.c.signing_key,base)}
    reader={"Authorization":"Bearer "+token(c.h.c.signing_key,{**base,"scp":"Behavior.Read"})}
    assert client.post(prefix+"/preview",json={},headers=reader).status_code==403
    for values in ({"oid":"wrong"},{"tid":"wrong"},{"appid":"wrong"}):
        assert client.post(prefix+"/preview",json={},headers={"Authorization":"Bearer "+
            token(c.h.c.signing_key,{**base,**values})}).status_code in (401,403)
    planned=client.post(prefix+"/preview",json={},headers=headers)
    assert planned.status_code==200,planned.text
    approved=client.post(prefix+"/authorize",json={"plan":planned.json(),"decision_id":"offline-delivery-approval"},
                         headers=headers)
    assert approved.status_code==200,approved.text
    for invalid in ("stop","execute","claim","acquire","model"):
        assert client.post(prefix+"/"+invalid,json=approved.json(),headers=headers).status_code==404
    assert client.post(prefix+"/deliver",json={**approved.json(),"body":"arbitrary"},headers=headers).status_code==422
    c.clock[0]=original.expires_at
    assert client.post(prefix+"/deliver",json=approved.json(),headers=headers).status_code==403
    assert client.post(prefix+"/reconcile",json=approved.json(),headers=reader).json()["dispatch_performed"] is False
    assert c.sends==[] and len(stops)==1


def test_foreign_scope_body_owner_effects_and_expiry_never_reauthorize(stopped_original):
    from shared_context.behavior_teams_continuation import OriginalFailureDeliveryContinuation
    from shared_context.behavior_assistant import key
    from shared_context.behavior_operations import control_key
    from test_behavior_settlement import write
    c,old,original,stops=stopped_original
    backing=c.transport.backing
    before,serial,time=copy.deepcopy(backing.rows),backing.sequence,c.clock[0]
    checks=[]
    for fault in ("extends-deadline","wrong-job","wrong-original-pin","original-auth-hash","observer",
                  "late-model","late-result","owner","route","unknown-row"):
        backing.rows,backing.sequence,c.clock[0]=copy.deepcopy(before),serial,time
        service=continuation_for(stopped_original)
        if fault in ("extends-deadline","wrong-job","wrong-original-pin","original-auth-hash"):
            changes={"extends-deadline":{"expires_at":original.expires_at+timedelta(microseconds=1)},
                     "wrong-job":{"job_id":"different-job"},"wrong-original-pin":{"original_code_sha256":"c"*64},
                     "original-auth-hash":{"original_authorization_sha256":"d"*64}}[fault]
            service.scope=service.scope.model_copy(update=changes)
            with pytest.raises(Problem):service.preview(c.principal)
        else:
            plan=service.preview(c.principal)
            auth=service.authorize(c.principal,plan,"offline-original-only")
            if fault=="observer":
                service.current_observation=lambda:service.observation.model_copy(update={"proof":b"revoked"})
            else:
                sid=c.supervisor.clients[c.launch_id].assignment.session_id
                rid={"late-model":key("model-intent",sid,"e"*64),"late-result":key("result",c.work.binding.work.work_id),
                     "owner":control_key("owner",c.work.binding.owner.owner_record_id),
                     "route":control_key("teams-route",c.config.channels.route_id),"unknown-row":"unclassified-after-continuation"}[fault]
                if fault=="route":
                    entry=backing.rows[("Behavior",c.store._scope.partition_key,"behavior:v1:row:"+rid)]
                    payload=json.loads(entry["Payload"]);payload["data"]["aad_object_id"]="other-recipient"
                    entry["Payload"]=json.dumps(payload).encode()
                else:
                    row=c.store.read_snapshot((rid,))[rid]
                    data=({"request_sha256":"e"*64,"consumed":False} if fault=="late-model"
                          else {"changed":True} if row is None else {**row.payload["data"],"epoch":2})
                    write(c.store,rid,data)
            with pytest.raises(Problem):service.deliver(c.principal,auth)
        assert c.sends==[] and len(stops)==1
        checks.append(fault)
    print("denied_same_original_continuation="+",".join(checks))


def test_continuation_authorization_and_sender_ackloss_reconcile_original_only(stopped_original,monkeypatch):
    from azure.core.exceptions import ServiceRequestError
    from shared_context.behavior_teams_continuation import OriginalFailureDeliveryContinuation,continuation_key
    from shared_context.behavior_store import ControlRequest
    from shared_context.behavior_operations import control_key
    from shared_context.behavior_migration import MigrationStore,installed_origin
    from test_behavior_store import parse_batch
    c,old,original,stops=stopped_original
    backing=c.transport.backing; before,serial,time=copy.deepcopy(backing.rows),backing.sequence,c.clock[0]
    transport=c.transport.send
    for stage in ("authorize","send-claim","provider-receipt"):
        backing.rows,backing.sequence,c.clock[0]=copy.deepcopy(before),serial,time
        c.sends.clear()
        service=continuation_for(stopped_original)
        plan=service.preview(c.principal)
        wanted=(continuation_key(plan.delivery_id) if stage=="authorize" else
                control_key("teams-claim" if stage=="send-claim" else "teams-provider-receipt",plan.delivery_id))
        lost,posts=[],[]
        def send(request,**kwargs):
            response=transport(request,**kwargs)
            if request.method=="POST":
                operations,_=parse_batch(request)
                receipt=next(json.loads(op[1]["Payload"]) for op in operations if "behavior:v1:receipt:" in op[1]["RowKey"])
                if receipt["request"]["request_id"]==wanted:
                    posts.append(True)
                    if not lost:
                        lost.append(receipt["request"])
                        raise ServiceRequestError("synthetic exact committed response loss")
            return response
        monkeypatch.setattr(c.transport,"send",send)
        if stage=="authorize":
            with pytest.raises(Problem):service.authorize(c.principal,plan,"offline-continuation-ACKloss")
            c.store.reconcile(ControlRequest.model_validate_json(json.dumps(lost[0])))
            auth=service.authorize(c.principal,plan,"offline-continuation-ACKloss")
        else:
            auth=service.authorize(c.principal,plan,"offline-continuation-ACKloss")
            with pytest.raises(Problem):service.deliver(c.principal,auth)
            c.store.reconcile(ControlRequest.model_validate_json(json.dumps(lost[0])))
        installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
        restarted=OriginalFailureDeliveryContinuation(c.parent,service.scope,original_scope=old.scope,
            observation=service.observation,current_observation=service.current_observation)
        assert restarted.reconcile(c.principal,auth)["dispatch_performed"] is False
        if stage=="send-claim":
            with pytest.raises(Problem):restarted.deliver(c.principal,auth)
            assert c.sends==[]
        else:
            assert restarted.deliver(c.principal,auth).payload["result"]["fulfillment"]=="fulfilled"
            assert c.sends==["original-Teams-result"]
        assert posts==[True] and len(stops)==1
        with pytest.raises(Problem):restarted.preview(c.principal)
        monkeypatch.setattr(c.transport,"send",transport)
    print("original_ACKloss=authorize,send-claim,provider-receipt; continuation_stop_calls=0; no_second_POST")


def test_fullcheck_shortphysical_and_wire_expiry_never_extend_original_deadline(stopped_original,monkeypatch):
    from shared_context.behavior_teams_cleanup import CleanupDelivery
    c,old,original,stops=stopped_original
    backing=c.transport.backing; before,serial,time=copy.deepcopy(backing.rows),backing.sequence,c.clock[0]
    guard=c.store._guard; check=CleanupDelivery.check_wire
    for phase in ("fullcheck","shortphysical","wire"):
        backing.rows,backing.sequence,c.clock[0]=copy.deepcopy(before),serial,time
        service=continuation_for(stopped_original)
        auth=service.authorize(c.principal,service.preview(c.principal),"offline-original-expiry")
        hit=[]
        if phase=="fullcheck":
            original_gate=service._write_gate
            def gate(*args):
                original_gate(*args)
                c.clock[0]=original.expires_at
                hit.append(True)
                service._time(auth)
            monkeypatch.setattr(service,"_write_gate",gate)
        elif phase=="shortphysical":
            def physical():
                import sys
                value=guard()
                if sys._getframe(1).f_code.co_name=="final":
                    c.clock[0]=original.expires_at;hit.append(True)
                return value
            monkeypatch.setattr(c.store,"_guard",physical)
        else:
            def wire(authority,send):
                observed=service.current_observation
                def expire_after_read():
                    value=observed();c.clock[0]=original.expires_at;hit.append(True);return value
                service.current_observation=expire_after_read
                return check(authority,send)
            monkeypatch.setattr(CleanupDelivery,"check_wire",wire)
        with pytest.raises(Problem):service.deliver(c.principal,auth)
        assert hit and c.sends==[] and len(stops)==1
        assert auth.expires_at==original.expires_at and service.scope.expires_at==original.expires_at
        monkeypatch.setattr(c.store,"_guard",guard)
        monkeypatch.setattr(CleanupDelivery,"check_wire",check)
    print("zero_POST_at=fullcheck,shortphysical,wire; same_original_hard_deadline=true")


def test_unknown_original_provider_result_never_mints_another_post(stopped_original,monkeypatch):
    import httpx
    from shared_context.teams_identity import PublicCloudTransport
    from shared_context.behavior_migration import MigrationStore,installed_origin
    c,old,original,stops=stopped_original
    service=continuation_for(stopped_original)
    plan=service.preview(c.principal)
    auth=service.authorize(c.principal,plan,"offline-original-unknown")
    transport=PublicCloudTransport.handle_request
    def unknown(adapter,request):
        transport(adapter,request)
        return httpx.Response(503,json={"error":"offline-unknown"})
    monkeypatch.setattr(PublicCloudTransport,"handle_request",unknown)
    result=service.deliver(c.principal,auth)
    assert result.payload["result"]["fulfillment"]=="pending" and c.sends==["original-Teams-result"]
    installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
    before=c.store.read_inventory()
    assert service.deliver(c.principal,auth).payload["result"]["fulfillment"]=="pending"
    assert c.store.read_inventory()==before and c.sends==["original-Teams-result"] and len(stops)==1
    with pytest.raises(Problem):service.preview(c.principal)
    c.clock[0]=original.expires_at
    assert service.reconcile(c.principal,auth)["fulfillment"]=="pending"
    assert service.reconcile(c.principal,auth)["dispatch_performed"] is False
