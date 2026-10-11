"""Current Window2/real retained producers to one standing policy, offline only."""
from datetime import timedelta
from pathlib import Path
import json
import socket
import pytest
_connect=socket.socket.connect
_connection=socket.create_connection
from shared_context import behavior_wire as wire
from shared_context.behavior_work_policy import SteadyWorkPolicy,CostObservation,work_policy_key
from shared_context.behavior_work_policy_installation import StandingPolicyInstaller
from shared_context.behavior_migration import MigrationStore,installed_origin
from shared_context.source_materials import digest
from test_web_first_backend import (
    stopped_original,web_handoff_for,original_teams_gap,original_finite_worker_contract,next_window,next_review,original_version,
    bounded_initial_control_quota,closed_v2,original_completed_nonempty_page,installed_empty,operator,
    exact_source_initialization,fixed_original_high_water,expired_claim,source_contract,closed_case,offline_socket_guard,
    original_captured_route_producer,closed_finished,sufficient_initial_budget,durable_result,native_acquire,current_rotation,
    two_rotations,current_cancelled,rotated,jwk_client,signing_key,
)
pytestmark=pytest.mark.parametrize("source_contract",["claim"],indirect=True)


def retained_handoff(stopped):
    c,_,_,_=stopped
    service=web_handoff_for(stopped)
    plan=service.preview(c.principal)
    service.authorize(c.principal,plan,"offline-original-web-transfer")
    got=service.fetch(c.principal,wire.Command(request_id="original-web-received"))
    service.acknowledge(c.principal,wire.PullAcknowledgment(request_id="original-web-ACK",
        exchange_id=got.exchange_id,acknowledgment_token=got.acknowledgment_token,
        binding=wire.DeliveryBinding.model_validate_json(got.binding.model_dump_json())))
    return service


def installer(c,handoff,tmp_path):
    from shared_context.behavior_runtime_rotation import RotationEvidenceFiles,RotationRetirement,CurrentTarget
    from shared_context.behavior_runtime_transition import RetiredTarget
    config=c.config;now=c.clock[0]
    policy=SteadyWorkPolicy(policy_id="offline-standing-original-metrics",revision=1,configuration_sha256=config.sha256,
        evidence_mode="native",authority_domain=config.table.scope.authority_domain,control_binding=config.workers.control_binding,
        active=True,read_enabled=True,template=config.bindings[0],
        profile=config.assistant.profiles[0].model_copy(update={"claim_id":"standing-metrics-reviewed"}),
        legacy_template_policy=config.policies[0].policy,cost_scope_sha256=digest(config.resource_bindings()),
        cost_observer_oid=config.evidence.observer_oid,work_estimate_microusd=2_000_000,cost_max_age_seconds=300,
        minimum_observed_microusd=45_480_000,
        queue_seconds=min(600,config.delivery.max_queue_seconds),max_model_requests=6)
    observation=CostObservation(evidence_id="offline-original-cost",evidence_mode="native",observer_oid=policy.cost_observer_oid,
        authority_domain=policy.authority_domain,control_binding=policy.control_binding,cost_scope_sha256=policy.cost_scope_sha256,
        observed_cumulative_microusd=45_480_000,legacy_unresolved_reserve_microusd=2_000_000,
        legacy_liability_sha256=digest(dict(c.store.read_inventory().entity_sha256)),evidence_sha256="e"*64,observed_at=now)
    facts=[observation];c.parent.work_cost_reader=lambda:facts[0]
    retirement=RotationRetirement(operator_oid=config.identity.human_oid,authority_domain=policy.authority_domain,
        old_configuration_sha256=config.sha256,new_configuration_sha256=config.sha256,old_code_sha256=handoff.observation.code_sha256,
        controller_resource_id=config.controller_resource_id,api_resource_id=config.api_resource_id,
        no_active_controller_revision=True,no_running_controller_jobs=True,old_business_admission_disabled=True,
        compute_observation_sha256="f"*64,targets=tuple(RetiredTarget(target=t,provider_session_status="idle",
            provider_observation_sha256="b"*64) for t in config.active_targets),
        observer_oid=config.evidence.observer_oid,observed_at=now,valid_until=now+timedelta(minutes=15),
        current_targets=(CurrentTarget(target=config.active_targets[0],session_owner_oid="88888888-8888-4888-8888-888888888888",
            credential_client_id=config.identity.managed_identity_client_id,credential_principal_oid="88888888-8888-4888-8888-888888888888",
            credential_tenant_id=config.identity.tenant_id,identity_observation_sha256="d"*64,agent_image_sha256="c"*64,
            provider_session_status="idle",provider_observation_sha256="b"*64),))
    paths=RotationEvidenceFiles(tmp_path/"standing-sources.json",tmp_path/"standing-backups.json",tmp_path/"standing-retirement.json")
    packet=json.loads(Path(config.evidence.path).read_text(encoding="utf-8"))
    def fresh(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in {"observed_at","inventory_observed_at"}:value[key]=now.isoformat()
                elif key in {"valid_until","inventory_valid_until"}:value[key]=(now+timedelta(minutes=15)).isoformat()
                else:fresh(item)
        elif isinstance(value,list):
            for item in value:fresh(item)
    for lane in ("sources","backups"):
        fresh(packet[lane]);getattr(paths,lane).write_text(json.dumps(packet[lane]),encoding="utf-8")
    paths.retirement.write_text(retirement.model_dump_json(),encoding="utf-8")
    return StandingPolicyInstaller(c.parent,policy,paths,lambda:facts[0],allow_apply=True),facts


def test_original_full_history_standing_install_and_source_review_remain_readable(stopped_original,tmp_path,monkeypatch):
    c,_,_,stops=stopped_original
    handoff=retained_handoff(stopped_original)
    install,facts=installer(c,handoff,tmp_path)
    before=c.store.read_inventory()
    plan=install.plan(c.principal,installation_id="offline-one-standing-installation")
    approval=install.authorize(c.principal,plan,decision_id="offline-standing-approved",
        expires_at=c.clock[0]+timedelta(minutes=4))
    receipt=install.apply(c.principal,plan,approval)
    assert receipt.payload["result"]["business_deadline"] is None
    installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
    original=c.store.transact;phases=[]
    def persist(request,decide,**kwargs):
        result=original(request,decide,**kwargs)
        installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
        phases.append(result.payload["request"]["request_id"])
        return result
    monkeypatch.setattr(c.store,"transact",persist)
    install.source(c.principal,plan,approval)
    c.parent.installation()
    assert c.runtime.work_policy is not None
    assert c.runtime.work_policy.source_ready(c.principal,install.policy).claims
    assert len(phases)==2 and c.sends==[] and len(stops)==1
    after=c.store.read_inventory()
    assert all(after.receipts[k]==v for k,v in before.receipts.items())
    print("native-mode OFFLINE: exact old history -> one standing policy -> normal reviewed-source producers; no global business deadline")
    native_sdk_flow(c,install,facts,tmp_path,monkeypatch)


def test_selected_new_conversation_template_survives_real_installation_history_reader(stopped_original,tmp_path):
    from shared_context.behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from shared_context.behavior_skill_prompt import assemble_prompt
    c,_,_,_=stopped_original
    handoff=retained_handoff(stopped_original)
    install,_=installer(c,handoff,tmp_path)
    plan=install.plan(c.principal,installation_id="profile-selection-standing")
    approval=install.authorize(c.principal,plan,decision_id="profile-selection-standing-approval",
        expires_at=c.clock[0]+timedelta(minutes=4))
    install.apply(c.principal,plan,approval)
    install.source(c.principal,plan,approval)
    c.parent.installation()
    control=c.runtime.work_policy
    role=assemble_prompt("fa-instruction-v1").record
    imported=AgentTemplate(role=TemplateRole(profile=role.profile,prefix_sha256=role.prefix_sha256,
        capability_catalog_version=role.capability_catalog_version),settings=TemplateSettings(
            checkpoint_profile=control.policy.profile.checkpoint_profile,
            sdk_start_profile=control.policy.profile.sdk_start_profile))
    authority=c.parent.assistant.start()
    first=select_profile(control,authority,c.principal,SelectConversationProfile(
        request_id="select-first-profile",expected_selection_id=None,template=imported))
    first_id=first.payload["result"]["selection_id"]
    installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
    second=select_profile(control,authority,c.principal,SelectConversationProfile(
        request_id="select-second-profile",expected_selection_id=first_id,template=imported))
    assert second.payload["result"]["selection_id"]!=first_id
    installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
    assert c.sends==[]


def test_memory_capture_survives_native_installation_history_without_sdk_or_model(stopped_original,tmp_path):
    from shared_context.behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_instruction_models import InstructionModels
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.memory_bridge_settings import (
        MemoryBridgeSettingsService, MemoryRecallChoice, SelectMemoryBridgeSettings,
    )
    import hashlib

    c,_,_,_=stopped_original
    install,_=installer(c,retained_handoff(stopped_original),tmp_path)
    plan=install.plan(c.principal,installation_id="memory-setting-standing")
    approval=install.authorize(c.principal,plan,decision_id="memory-setting-standing-approval",
        expires_at=c.clock[0]+timedelta(minutes=4))
    install.apply(c.principal,plan,approval)
    install.source(c.principal,plan,approval)
    c.parent.installation()
    control=c.runtime.work_policy
    control._models=InstructionModels(
        [{"deployment":"gpt-4.1-mini","provisioning":"Succeeded","capabilities":{"chatCompletion":"true"}}],
        {"gpt-4.1-mini":{"max_output_tokens":32768,"input_microusd_per_million":400000,
                        "output_microusd_per_million":1600000}}, "gpt-4.1-mini")
    authority=c.parent.assistant.start()
    role=assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE).record
    chosen=AgentTemplate(role=TemplateRole(profile=role.profile,prefix_sha256=role.prefix_sha256,
        capability_catalog_version=role.capability_catalog_version),settings=TemplateSettings(
            checkpoint_profile=control.policy.profile.checkpoint_profile,
            sdk_start_profile=control.policy.profile.sdk_start_profile,
            user_text_file_profile="fa-user-text-files-v1"))
    select_profile(control,authority,c.principal,SelectConversationProfile(
        request_id="memory-history-base",expected_selection_id=None,template=chosen))
    service=MemoryBridgeSettingsService(c.runtime)
    enabled=service.select(c.principal,SelectMemoryBridgeSettings(
        schema_version="fa-memory-bridge-settings-command-v1",request_id="enable-memory",
        expected_settings_id=None,enabled=True))
    first=InstructionSubmit(request_id="memory-original-work",content="Keep an original note scope.")
    record=control.register(c.principal,first.request_id,
        hashlib.sha256(first.model_dump_json().encode()).hexdigest(),instruction=first)
    current=c.runtime.current_claim(record.binding.binding_id,record.profile.claim_id,c.principal)
    service.select(c.principal,SelectMemoryBridgeSettings(
        schema_version="fa-memory-bridge-settings-command-v1",request_id="select-memory",
        expected_settings_id=enabled.payload["result"]["settings_id"],enabled=True,
        recall=MemoryRecallChoice(origin_binding_id=record.binding.binding_id,claim_id=current.reference.claim_id,
            expected_claim_version=current.reference.version,expected_claim_sha256=current.reference.sha256,
            use_in_new_conversations=True)))
    second=InstructionSubmit(request_id="memory-consumer-work",content="Read that reviewed note.")
    control.register(c.principal,second.request_id,
        hashlib.sha256(second.model_dump_json().encode()).hexdigest(),instruction=second)
    installed_origin(MigrationStore(c.parent.clients[0],c.config.table.scope),c.config)
    assert c.sends==[]


def current_machine_evidence(c,install):
    from shared_context.behavior_server_config import NativeEvidenceBundle,ProtectionObservation
    now=c.clock[0]
    existing=NativeEvidenceBundle.model_validate_json(Path(c.config.evidence.path).read_bytes())
    worker=existing.worker.model_copy(update={"evidence_id":"standing-worker-current","observed_at":now,"expires_at":now+timedelta(minutes=10)})
    ready=existing.delivery.model_copy(update={"evidence_id":"standing-delivery-current","observed_at":now,"expires_at":now+timedelta(minutes=10)})
    inbox=existing.inbox.model_copy(update={"evidence_id":"standing-inbox-current","worker_evidence_id":worker.evidence_id,"readiness":ready})
    def fresh(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in {"observed_at","inventory_observed_at"}:value[key]=now.isoformat()
                elif key in {"valid_until","inventory_valid_until"}:value[key]=(now+timedelta(minutes=10)).isoformat()
                else:fresh(item)
        elif isinstance(value,list):
            for item in value:fresh(item)
    observations={}
    for lane in ("sources","backups"):
        raw=json.loads(getattr(install.evidence,lane).read_bytes());fresh(raw)
        observations[lane]=ProtectionObservation.model_validate_json(json.dumps(raw))
    bundle=existing.model_copy(update={**observations,"captured_at":now,"expires_at":now+timedelta(minutes=10),
                                      "worker":worker,"delivery":ready,"inbox":inbox})
    Path(c.config.evidence.path).write_text(bundle.model_dump_json(),encoding="utf-8")
    c.runtime.dep.workers.publish_readiness(worker.evidence_id)
    c.runtime.dep.inbox.publish_readiness(inbox.evidence_id)


def native_sdk_flow(c,install,facts,tmp_path,monkeypatch):
    from dataclasses import replace
    from contextlib import closing
    from types import SimpleNamespace
    import os,hashlib,base64
    from fastapi.testclient import TestClient
    from shared_context.behavior_assistant_wire import AssistantSubmit
    from shared_context.behavior_server import application
    from shared_context.behavior_assistant_hosted import AssistantSupervisor,AssistantHostedRuntime
    from shared_context.behavior_hosted import BehaviorExecutorClient,HostedInvocation
    from shared_context.behavior_work_policy import WorkPreflightQuery
    from test_m3_auth import token
    from test_behavior_model_diagnostics import LocalHTTP,LocalAsyncHTTP,socket_server,send_bytes
    from test_behavior_model_deadlines import asgi_server
    from test_behavior_assistant_sse import stream
    c.clock[0]+=timedelta(days=1)
    facts[0]=facts[0].model_copy(update={"observed_at":c.clock[0]})
    current_machine_evidence(c,install)
    parent=c.parent;control=c.runtime.work_policy
    app=application(parent);client=TestClient(app)
    ids=c.config.identity
    claims={"tid":ids.tenant_id,"iss":f"https://sts.windows.net/{ids.tenant_id}/","aud":ids.audience,
            "oid":ids.human_oid,"appid":ids.admin_client_id,"scp":"Behavior.Read"}
    headers={"Authorization":"Bearer "+token(c.h.c.signing_key,claims),"Content-Type":"application/json"}
    command=AssistantSubmit(request_id="standing-one-new-metrics")
    query=WorkPreflightQuery(read_id="pure-current-decision",request_id=command.request_id,
        submit_body_sha256=hashlib.sha256(command.model_dump_json().encode()).hexdigest())
    before=c.store.read_inventory()
    read=client.post("/work-policy/v1/preflight",content=query.model_dump_json(),headers=headers)
    assert read.status_code==200,read.text
    assert read.json()["decision"]=="allow" and c.store.read_inventory()==before
    response=client.post("/work-policy/v1/submit",content=command.model_dump_json(),headers=headers)
    assert response.status_code==200,response.text
    binding=control.records[response.json()["binding_id"]].binding
    profile=control.records[binding.binding_id].profile
    native,workers=c.runtime.dep.native_launches,c.runtime.dep.workers
    starts=[]
    workers.adapter=replace(workers.adapter,start=lambda start:starts.append(native.prepare(start)))
    workers.start(binding.job_id,binding.owner)
    launch,permit=starts[0]
    identity=wire.HostedIdentity(**{k:getattr(c.config.active_targets[0],k) for k in ("agent_id","agent_name","agent_version","session_id")})
    agent={**claims,"oid":binding.agent_oid,"appid":binding.agent_oid,"roles":["Behavior.Agent"]}
    agent.pop("scp")
    bearer=token(c.h.c.signing_key,agent,remove=("scp",))
    policy=control.policy.legacy_template_policy
    context=c.runtime._source_context(binding,policy,c.principal)
    source=c.runtime.dep.sources.read_exact(context,c.runtime._reference(profile.source,binding,policy,c.principal))
    values=json.loads(source.original)
    answer={"item_count":values["item_count"],"completed_count":values["completed_count"],
            "remaining_count":values["item_count"]-values["completed_count"]}
    replies=[{"tool_calls":[{"index":0,"function":{"name":name,"arguments":json.dumps(args)}}]}
             for name,args in (("read_context",{}),("save_result",answer),("finish_task",{}))]
    models=[]
    def provider(handler,_):
        n=len(models);models.append(True);assert n<3
        send_bytes(handler,stream([replies[n]]).content)
    with socket_server(provider) as endpoint,asgi_server(app) as port:
        def local_connect(sock,address):
            import sys
            allowed=isinstance(address,tuple) and address[0]=="127.0.0.1" and address[1] in {port,endpoint.port}
            fallback=getattr(socket,"_fallback_socketpair",lambda:None).__code__
            assert allowed or (sys._getframe(1).f_code is fallback and address[0] in {"127.0.0.1","::1"})
            return _connect(sock,address)
        def local_connection(address,*args,**kwargs):
            assert address[0]=="127.0.0.1" and address[1] in {port,endpoint.port}
            return _connection(address,*args,**kwargs)
        monkeypatch.setattr(socket.socket,"connect",local_connect);monkeypatch.setattr(socket,"create_connection",local_connection)
        parent.assistant.provider.transport=LocalAsyncHTTP(endpoint.port)
        parent.assistant.provider.credential=SimpleNamespace(get_token=lambda _:SimpleNamespace(token="OFFLINE-MODEL"))
        supervisor=AssistantSupervisor(tmp_path/"standing-native-sdk",runtime_path=Path(os.environ["M2_TEST_RUNTIME"]))
        with closing(BehaviorExecutorClient("https://context.invalid",ids.audience,
                SimpleNamespace(get_token=lambda _:SimpleNamespace(token=bearer)),transport=LocalHTTP(port))) as api:
            host=AssistantHostedRuntime(identity,supervisor,api,clock=lambda:c.clock[0])
            host.start(HostedInvocation(binding_id=binding.binding_id,launch_id=launch.launch_id,launch_permit=permit),
                "offline-standing-native-invocation",identity.session_id)
            supervisor.join(timeout=280)
            finished=supervisor.observe(launch.launch_id)
            installed_origin(MigrationStore(parent.clients[0],c.config.table.scope),c.config)
            assert finished.phase=="sealed" and finished.sdk_stop.graceful and finished.outcome=="succeeded"
            def query(binding):
                probe=native.probe(binding)
                return host.observe("offline-standing-native-invocation",identity.session_id,probe.probe_id).evidence_id
            workers.adapter=replace(workers.adapter,query=query)
            status=c.runtime.controller.step(binding.binding_id)
            assert status.state=="succeeded" and status.result_forwarded and not status.obligation_fulfilled
    second,queued=control.submit(c.principal,AssistantSubmit(request_id="independent-queued-second"))
    assert queued.state=="queued" and not c.runtime.job_status(binding.binding_id,c.principal).obligation_fulfilled
    c.clock[0]+=timedelta(days=1)
    # Machine/cost observations are intentionally stale; original result/ACK stay live.
    rebuilt=TestClient(application(parent))
    path="/behavior/v1/bindings/"+binding.binding_id
    state=rebuilt.get(path+"/jobs/"+binding.job_id,headers=headers)
    assert state.status_code==200,state.text
    fetched=rebuilt.post(path+"/delivery/pull",json={"request_id":"read-original-after-execution"},headers=headers)
    assert fetched.status_code==200,fetched.text
    acknowledgment=wire.PullAcknowledgment(request_id="ACK-original-after-execution",
        exchange_id=fetched.headers["X-Behavior-Exchange"],acknowledgment_token=fetched.headers["X-Behavior-Ack-Token"],
        binding=wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(fetched.headers["X-Behavior-Binding"])))
    ack=rebuilt.post(path+"/delivery/acknowledge",content=acknowledgment.model_dump_json(),headers=headers)
    assert ack.status_code==200 and ack.json()["result"]["fulfillment"]=="fulfilled",ack.text
    cancelled=rebuilt.post("/work-policy/v1/bindings/"+second.binding_id+"/cancel",json={"request_id":"cancel-original-queued"},headers=headers)
    assert cancelled.status_code==200 and cancelled.json()["result"]["cancel_accepted"],cancelled.text
    second_path="/behavior/v1/bindings/"+second.binding_id
    failed=rebuilt.post(second_path+"/delivery/pull",json={"request_id":"read-original-cancelled"},headers=headers)
    assert failed.status_code==200,failed.text
    assert json.loads(failed.content)["work_state"]=="cancelled"
    failure_ack=wire.PullAcknowledgment(request_id="ACK-original-cancelled",
        exchange_id=failed.headers["X-Behavior-Exchange"],acknowledgment_token=failed.headers["X-Behavior-Ack-Token"],
        binding=wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(failed.headers["X-Behavior-Binding"])))
    settled=rebuilt.post(second_path+"/delivery/acknowledge",content=failure_ack.model_dump_json(),headers=headers)
    assert settled.status_code==200 and settled.json()["result"]["fulfillment"]=="fulfilled",settled.text
    assert len(models)==3 and c.sends==[]
    installed_origin(MigrationStore(parent.clients[0],c.config.table.scope),c.config)
    print("native-mode OFFLINE standing policy: old operator deadline passed -> fresh scoped work -> real SDK3 -> owned stop -> stale-evidence original result/ACK")
    import copy
    from shared_context.behavior_assistant import key as assistant_key
    from shared_context.behavior_runtime import runtime_key
    from shared_context.errors import Problem
    from test_behavior_settlement import write
    backing=c.transport.backing
    saved,serial=copy.deepcopy(backing.rows),backing.sequence
    for fault in ("unknown-row","orphan-model","renew-work-deadline","refund-cost","changed-owner"):
        backing.rows,backing.sequence=copy.deepcopy(saved),serial
        if fault=="unknown-row":rid,data="unclassified-standing-row",{"unrecognized":True}
        elif fault=="orphan-model":rid,data=assistant_key("model-intent","foreign-session","f"*64),{"request_sha256":"f"*64,"consumed":False}
        else:
            from shared_context.behavior_operations import control_key
            rid={"renew-work-deadline":runtime_key("policy",binding.binding_id),
                 "refund-cost":control.cost_key,"changed-owner":control_key("owner",binding.owner.owner_record_id)}[fault]
            old=c.store.read_snapshot((rid,))[rid].payload["data"]
            data={**old,**({"valid_until":(c.clock[0]+timedelta(days=20)).isoformat()} if fault=="renew-work-deadline" else
                         {"work_reservations_microusd":0} if fault=="refund-cost" else {"epoch":old["epoch"]+1})}
        with pytest.raises(Problem):
            write(c.store,rid,data)
            installed_origin(MigrationStore(parent.clients[0],c.config.table.scope),c.config)
    backing.rows,backing.sequence=saved,serial
    installed_origin(MigrationStore(parent.clients[0],c.config.table.scope),c.config)
    print("standing current history rejects unknown/orphan/refund/deadline-renewal/owner mutations")
    if output:=os.environ.get("WORK_POLICY_SHAPES"):
        Path(output).write_text(json.dumps({"actual_environment":"NOT_RUN","not_native_operator_inputs":True,
            "policy":control.policy.model_dump(mode="json"),"preflight":read.json(),"submit":response.json(),
            "result_ack":ack.json(),"source_reference":profile.source.model_dump(mode="json"),
            "configuration_sha256":c.config.sha256,"model_stub_calls":len(models),
            "original_operator_change_expired":True,"current_execution_deadline_expired":True,
            "legacy_global_deadlines_unchanged":True},indent=2),encoding="utf-8")
