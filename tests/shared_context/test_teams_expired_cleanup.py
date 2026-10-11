"""Expired business window is not renewed by original-work-only cleanup."""
from datetime import timedelta
from pathlib import Path
import json
import os
import copy
from types import SimpleNamespace
import pytest
from shared_context.errors import Problem
from shared_context.behavior_recovery import (
    ExpiredTeamsCleanupScope, ExpiredTeamsCleanupObservation, PlatformStopObservation, stop_endpoint, code_sha256)
from shared_context.behavior_teams_cleanup import ExpiredTeamsCleanup, retained_native
from shared_context.behavior_teams_recovery import original_teams_state
from shared_context.behavior_recovery_history import _Inventory
from shared_context.source_materials import digest
from shared_context.behavior_operations import control_key
from shared_context.behavior_migration import MigrationStore, installed_origin

pytest.importorskip("test_channel_window", reason="pinned offline original Window2 fixture bundle required")
from test_teams_pre_acquire_recovery import (
    original_teams_gap, original_finite_worker_contract, next_window, next_review, original_version,
    bounded_initial_control_quota, closed_v2, original_completed_nonempty_page, installed_empty, operator,
    exact_source_initialization, fixed_original_high_water, expired_claim, source_contract, closed_case,
    offline_socket_guard, original_captured_route_producer, closed_finished, sufficient_initial_budget,
    durable_result, native_acquire, current_rotation, two_rotations, current_cancelled, rotated,
    jwk_client, signing_key, service_for,
)

pytestmark = pytest.mark.parametrize("source_contract", ["claim"], indirect=True)


def cleanup_service(c, *, before_stop=None, unknown=False):
    from shared_context.behavior_recovery import code_sha256
    inventory = c.store.read_inventory()
    launch = c.native.launch(c.launch_id)
    _, _, rows, receipts = original_teams_state(inventory, c.config, launch)
    native = retained_native(_Inventory(inventory, c.config), c.config)
    now = c.clock[0]
    observation = ExpiredTeamsCleanupObservation(
        purpose="stop_original_and_deliver_failure", evidence_id="offline-current-cleanup-observation",
        observer_oid=c.config.evidence.observer_oid, configuration_sha256=c.config.sha256,
        code_sha256=code_sha256(), event_id=c.work.event_id, job_id=c.work.binding.job_id, launch_id=c.launch_id,
        inventory_sha256=digest(dict(inventory.entity_sha256)), guard_revision=inventory.guard.payload["revision"],
        target_sha256=digest(launch.target.model_dump(mode="json")),
        original_native_evidence_sha256=digest(native.model_dump(mode="json")),
        retained_authority_sha256=digest([rows, receipts]), protection_observation_sha256="d"*64,
        deployment_observation_sha256="e"*64, observed_at=now, expires_at=now+timedelta(minutes=60),
        coverage="original-execution-and-exact-failure-only",
        proof=b"OFFLINE current cleanup observation; original history is immutable; not native evidence")
    scope = ExpiredTeamsCleanupScope(
        purpose="stop_original_and_deliver_failure", recovery_phase="expired_original_teams_pre_acquire",
        recovery_id="offline-original-expired-cleanup", configuration_sha256=c.config.sha256,
        event_id=c.work.event_id, job_id=c.work.binding.job_id, launch_id=c.launch_id,
        operator_oid=c.config.identity.human_oid, not_before=now, expires_at=now+timedelta(minutes=60),
        observation_sha256=digest(observation.model_dump(mode="json")))
    calls = []
    def stop(target, deadline, *, before_send):
        if before_stop:
            before_stop()
        before_send()
        calls.append(target)
        if unknown:
            raise Problem(503, "RECOVERY_STOP_UNKNOWN", "synthetic original response unavailable")
        return PlatformStopObservation(target=target, endpoint=stop_endpoint(target),
            credential_client_id=c.config.identity.managed_identity_client_id, status_code=204, observed_at=c.clock[0])
    return ExpiredTeamsCleanup(c.parent, scope, stop, observation=observation,
                               current_observation=lambda: observation), calls


def test_expired_original_work_has_only_new_purpose_cleanup(original_teams_gap, monkeypatch):
    c = original_teams_gap
    c.clock[0] = c.config.channels.valid_until + timedelta(minutes=1)
    old, _ = service_for(c)
    with pytest.raises(Problem):
        old.plan(c.principal)
    service, stops = cleanup_service(c)
    before = c.store.read_inventory()
    plan = service.plan(c.principal)
    assert c.store.read_inventory() == before and stops == []
    transact = c.store.transact
    stages, sizes, inline, multipart = [], [], [], []
    entity, transport = c.store._entity, c.transport.send
    def encode(*args, **kwargs):
        result = entity(*args, **kwargs)
        sizes.append(len(result["Payload"]))
        if getattr(args[1], "material", None) is not None:
            inline.append(len(args[1].material.data))
        return result
    def send(request, **kwargs):
        if request.method == "POST":
            multipart.append(len(request.body))
        return transport(request, **kwargs)
    monkeypatch.setattr(c.store, "_entity", encode)
    monkeypatch.setattr(c.transport, "send", send)
    def committed(request, decide, **kwargs):
        value = transact(request, decide, **kwargs)
        installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
        stages.append(value.payload)
        return value
    monkeypatch.setattr(c.store, "transact", committed)
    approval = service.authorize(c.principal, plan, "offline-new-cleanup-only-decision")
    assert approval.expires_at == service.scope.expires_at
    done = service.execute(c.principal, approval)
    assert done["state"] == "cancelled" and done["model_outcome"] == "not_requested" and len(stops) == 1
    sent = service.deliver(c.principal, approval)
    assert sent.payload["result"]["fulfillment"] == "fulfilled" and c.sends == ["original-Teams-result"]
    assert service.deliver(c.principal, approval) and c.sends == ["original-Teams-result"]
    final = c.store.read_inventory()
    assert all(final.receipts[k] == row for k, row in before.receipts.items())
    job = c.workers.inspect(c.work.binding.job_id)
    assert job.control_remaining == 0 and job.queries_reserved == 9 and job.result_forwarded
    from shared_context.behavior_channels_migration import channel_key
    immutable = [channel_key("current", c.config.table.scope.authority_domain),
                 control_key("teams-native-evidence", c.config.channels.native_evidence_id),
                 control_key("teams-native-current", c.config.channels.personal.configuration_digest),
                 control_key("teams-capture-route-binding", c.config.channels.personal.configuration_digest)]
    for k in immutable:
        assert final.rows[k] == before.rows[k]
    assert max(sizes) <= 49152 and max(inline) <= 32768 and max(multipart) <= 4*1024*1024
    print(f"expired_window_unchanged=true stop=1 Teams_POST=1 control=0 query=9 "
          f"max_entity={max(sizes)} max_inline={max(inline)} max_multipart={max(multipart)}")
    # The normal business gates remain closed even while cleanup is valid.
    with pytest.raises(Problem):
        c.parent.teams.pointer()
    from test_teams_identity import activity, token as connector_token
    unchanged=c.store.read_inventory()
    rejected=c.client.post("/behavior/v1/teams/activities",json=activity(text="FA metrics",id="forbidden-new-activity"),
        headers={"Authorization":"Bearer "+connector_token(nbf=int(c.clock[0].timestamp())-60,exp=int(c.clock[0].timestamp())+300)})
    assert rejected.status_code in (403,503) and c.store.read_inventory()==unchanged
    from shared_context.behavior_teams_cleanup import CleanupDelivery
    from shared_context.behavior_delivery import MessageRequest
    authority = CleanupDelivery(service, c.principal, approval)
    with pytest.raises(Problem):
        authority.adapter.prepare(authority.delivery_id)
    from shared_context.teams_metrics import TeamsMetrics
    with pytest.raises(Problem):
        c.parent.teams.deliver(c.work.binding, {"delivery_id": authority.delivery_id}, cleanup_authority=object())
    if output := os.environ.get("TEAMS_CLEANUP_OUTPUT"):
        Path(output).write_text(json.dumps({
            "scope":service.scope.model_dump(mode="json"),"observation":service.observation.model_dump(mode="json"),
            "plan":plan.model_dump(mode="json"),"authorization":approval.model_dump(mode="json"),
            "done":done,"stages":stages,
            "sizes":{"entity":max(sizes),"inline":max(inline),"multipart":max(multipart)}},indent=2),encoding="utf-8")


def test_cleanup_scope_observer_and_late_original_state_are_fail_closed(original_teams_gap):
    from shared_context.behavior_assistant import key
    from shared_context.behavior_channels_migration import channel_key
    from test_behavior_settlement import write
    c = original_teams_gap
    c.clock[0] = c.config.channels.valid_until+timedelta(minutes=1)
    baseline, seq, now = copy.deepcopy(c.transport.backing.rows), c.transport.backing.sequence, c.clock[0]
    outcomes=[]
    for fault in ("future","expired","observer","current-observer","model","result","owner","window","unknown"):
        c.transport.backing.rows,c.transport.backing.sequence,c.clock[0] = copy.deepcopy(baseline),seq,now
        service, calls = cleanup_service(c)
        if fault in {"future","expired","observer"}:
            if fault=="future":
                c.clock[0]-=timedelta(seconds=1)
            elif fault=="expired":
                c.clock[0]=service.scope.expires_at
            else:
                bad=service.observation.model_copy(update={"observer_oid":"wrong-observer"})
                service.observation=bad
                service.current_observation=lambda:bad
            with pytest.raises(Problem):
                service.plan(c.principal)
        else:
            plan=service.plan(c.principal)
            approval=service.authorize(c.principal,plan,"offline-original-only")
            if fault=="current-observer":
                service.current_observation=lambda:service.observation.model_copy(update={"deployment_observation_sha256":"f"*64})
            else:
                sid=c.supervisor.clients[c.launch_id].assignment.session_id
                rid={"model":key("model-intent",sid,"e"*64),
                     "result":key("result",c.work.binding.work.work_id),
                     "owner":control_key("owner",c.work.binding.owner.owner_record_id),
                     "window":channel_key("current",c.config.table.scope.authority_domain),
                     "unknown":"orphan-cleanup-row"}[fault]
                row=c.store.read_snapshot((rid,))[rid]
                if fault=="window":
                    entry=c.transport.backing.rows[("Behavior",c.store._scope.partition_key,"behavior:v1:row:"+rid)]
                    body=json.loads(entry["Payload"]); body["data"]["window_id"]="replacement"
                    entry["Payload"]=json.dumps(body).encode()
                else:
                    body=({"request_sha256":"e"*64,"consumed":False} if fault=="model" else
                          {"bad":True} if row is None else {**row.payload["data"],"epoch":2})
                    write(c.store,rid,body)
            with pytest.raises(Problem):
                service.execute(c.principal,approval)
        assert calls==[]
        outcomes.append(fault)
    c.transport.backing.rows,c.transport.backing.sequence,c.clock[0]=copy.deepcopy(baseline),seq,now
    service,_=cleanup_service(c)
    for changes in ({"expires_at":now+timedelta(minutes=61)},{"purpose":"new_activity"}):
        with pytest.raises(ValueError):
            ExpiredTeamsCleanupScope.model_validate_json(json.dumps({**service.scope.model_dump(mode="json"),**{
                k:v.isoformat() if hasattr(v,"isoformat") else v for k,v in changes.items()}}))
    print("denied_without_stop="+",".join(outcomes)+",over60minutes,wrongpurpose")


def test_original_cleanup_stop_and_sender_ackloss_do_not_resend(original_teams_gap, monkeypatch):
    from azure.core.exceptions import ServiceRequestError
    from test_behavior_store import parse_batch
    from shared_context.behavior_store import ControlRequest
    from shared_context.behavior_recovery import recovery_key
    c=original_teams_gap
    c.clock[0]=c.config.channels.valid_until+timedelta(minutes=1)
    baseline,seq=copy.deepcopy(c.transport.backing.rows),c.transport.backing.sequence
    transport=c.transport.send
    for stage in ("stop-intent","stop-observation","teams-provider"):
        c.transport.backing.rows,c.transport.backing.sequence=copy.deepcopy(baseline),seq
        c.sends.clear()
        service,calls=cleanup_service(c)
        plan=service.plan(c.principal)
        approval=service.authorize(c.principal,plan,"offline-original-cleanup-ackloss")
        did=control_key("delivery-final-id",c.work.binding.obligation_id)
        wanted=(control_key("teams-provider-receipt",did) if stage=="teams-provider" else
                recovery_key(stage,service.scope.recovery_id))
        lost,posts=[],[]
        def send(request,**kwargs):
            value=transport(request,**kwargs)
            if request.method=="POST":
                ops,_=parse_batch(request)
                receipt=next(json.loads(op[1]["Payload"]) for op in ops if "behavior:v1:receipt:" in op[1]["RowKey"])
                if receipt["request"]["request_id"]==wanted:
                    posts.append(True)
                    if not lost:
                        lost.append(receipt["request"])
                        raise ServiceRequestError("synthetic committed original response lost")
            return value
        monkeypatch.setattr(c.transport,"send",send)
        if stage=="teams-provider":
            service.execute(c.principal,approval)
            action=lambda:service.deliver(c.principal,approval)
        else:
            action=lambda:service.execute(c.principal,approval)
        with pytest.raises(Problem) as failure:
            action()
        assert failure.value.body["error"]["code"]=="BEHAVIOR_COMMIT_UNKNOWN"
        c.store.reconcile(ControlRequest.model_validate_json(json.dumps(lost[0])))
        installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
        second=[]
        def forbidden(*_,**__):
            second.append(True)
            pytest.fail("restart cannot repeat provider stop")
        restarted=ExpiredTeamsCleanup(c.parent,service.scope,forbidden,
            observation=service.observation,current_observation=service.current_observation)
        assert restarted.execute(c.principal,approval)["stop_resent"] is False
        if stage=="stop-observation":
            restarted.finish(c.principal,approval)
        elif stage=="teams-provider":
            assert restarted.deliver(c.principal,approval).payload["result"]["fulfillment"]=="fulfilled"
        assert second==[] and posts==[True] and len(calls)==(0 if stage=="stop-intent" else 1)
        assert len(c.sends)==(1 if stage=="teams-provider" else 0)
        monkeypatch.setattr(c.transport,"send",transport)
    print("exact_request_ACKloss=stop-intent,stop-observation,Teams-provider; provider_resend=0")


def test_cleanup_wire_expiry_and_wrong_payload_are_not_channel_authority(original_teams_gap, monkeypatch):
    from shared_context.behavior_teams_cleanup import CleanupDelivery
    from shared_context.behavior_delivery import MessageRequest
    from shared_context import behavior_wire as wire
    c=original_teams_gap
    c.clock[0]=c.config.channels.valid_until+timedelta(minutes=1)
    service,calls=cleanup_service(c)
    approval=service.authorize(c.principal,service.plan(c.principal),"offline-wire-bound")
    service.execute(c.principal,approval)
    snapshot,seq=copy.deepcopy(c.transport.backing.rows),c.transport.backing.sequence
    original_check=CleanupDelivery.check_wire
    denied=[]
    for fault in ("wrong-job","wrong-body","recipient","last-send-expiry","observer-change","post-consume-write"):
        c.transport.backing.rows,c.transport.backing.sequence=copy.deepcopy(snapshot),seq
        c.clock[0]=approval.approved_at
        c.sends.clear()
        reached=[]
        def check(authority,send):
            reached.append(True)
            if fault=="last-send-expiry":
                observed=service.current_observation
                def delayed_observer():
                    result=observed()
                    c.clock[0]=approval.expires_at
                    return result
                service.current_observation=delayed_observer
            elif fault=="observer-change":
                service.current_observation=lambda:service.observation.model_copy(update={"proof":b"revoked"})
            elif fault=="post-consume-write":
                from test_behavior_settlement import write
                write(c.store,"late-after-consume",{"foreign":True})
            return original_check(authority,send)
        monkeypatch.setattr(CleanupDelivery,"check_wire",check)
        if fault in {"wrong-job","wrong-body","recipient"}:
            original_deliver=c.parent.teams.deliver
            def replaced(binding,prepared,*,cleanup_authority=None):
                if fault=="wrong-job":
                    return original_deliver(binding.model_copy(update={"job_id":"other-job"}),prepared,
                                            cleanup_authority=cleanup_authority)
                if fault=="recipient":
                    return original_deliver(binding.model_copy(update={"route":binding.route.model_copy(
                        update={"recipient_id":"someone-else"})}),prepared,cleanup_authority=cleanup_authority)
                cmd=MessageRequest(obligation_id=binding.obligation_id,delivery_id=cleanup_authority.delivery_id,
                                   expected_version=1,message_kind="final")
                return cleanup_authority.core.prepare_message(cmd,binding.owner)
            monkeypatch.setattr(c.parent.teams,"deliver",replaced)
        with pytest.raises(Problem):
            service.deliver(c.principal,approval)
        if fault in {"wrong-job","wrong-body","recipient"}:
            monkeypatch.setattr(c.parent.teams,"deliver",original_deliver)
        else:
            assert reached==[True]
        assert c.sends==[] and len(calls)==1
        denied.append(fault)
        service.current_observation=lambda:service.observation
    c.transport.backing.rows,c.transport.backing.sequence=copy.deepcopy(snapshot),seq
    c.clock[0]=approval.expires_at
    assert service.reconcile(c.principal,approval)["stop_resent"] is False
    print("no_original_Teams_POST_for="+",".join(denied))


def test_cleanup_http_requires_new_human_purpose_and_does_not_refresh_old_rule(original_teams_gap):
    from fastapi.testclient import TestClient
    from shared_context.behavior_recovery_api import create_recovery_app
    from test_m3_auth import token
    c=original_teams_gap
    c.clock[0]=c.config.channels.valid_until+timedelta(minutes=1)
    service,calls=cleanup_service(c)
    app=create_recovery_app(c.parent,service.scope,stop=service._platform_stop,
        cleanup_observation=service.observation,current_cleanup_observation=service.current_observation)
    client=TestClient(app)
    identity=c.config.identity
    base={"tid":identity.tenant_id,"iss":f"https://sts.windows.net/{identity.tenant_id}/",
          "aud":identity.audience,"oid":identity.human_oid,"appid":identity.admin_client_id,
          "scp":"Behavior.Read Behavior.Approve"}
    headers={"Authorization":"Bearer "+token(c.h.c.signing_key,base)}
    prefix="/operator/v1/sdk-recovery"
    reader={"Authorization":"Bearer "+token(c.h.c.signing_key,{**base,"scp":"Behavior.Read"})}
    assert client.post(prefix+"/plan",json={},headers=reader).status_code==403
    for wrong in ({"oid":"wrong"},{"appid":"wrong"},{"tid":"wrong"}):
        assert client.post(prefix+"/plan",json={},headers={
            "Authorization":"Bearer "+token(c.h.c.signing_key,{**base,**wrong})}).status_code in (401,403)
    planned=client.post(prefix+"/plan",json={},headers=headers)
    assert planned.status_code==200,planned.text
    grant=client.post(prefix+"/authorize",json={"plan":planned.json(),"decision_id":"offline-expired-cleanup-human"},
                      headers=headers)
    assert grant.status_code==200,grant.text
    assert grant.json()["schema_version"]=="fa-expired-teams-cleanup-authorization-v1"
    assert client.post(prefix+"/execute",json={**grant.json(),"result_body":"arbitrary"},headers=headers).status_code==422
    c.clock[0]=service.scope.expires_at
    for stage in ("execute","finish","deliver"):
        assert client.post(prefix+"/"+stage,json=grant.json(),headers=headers).status_code==403
    assert client.post(prefix+"/reconcile",json=grant.json(),headers=reader).status_code==200
    assert calls==[] and c.sends==[]
