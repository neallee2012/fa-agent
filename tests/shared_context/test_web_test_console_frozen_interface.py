"""Current API/Table/Hosted producer loop; only SDK/runtime/model I/O is replaced."""
import copy
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A, TENANT
from test_m3_auth import signing_key, jwk_client
from test_web_test_console_browser_fixture import assistant_harness
from test_work_policy_lifecycle import enabled
from test_web_test_console_standing import configuration
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_assistant_hosted import AssistantClient, AssistantSession
from shared_context.behavior_assistant_checkpoint import unpack
from shared_context.behavior_checkpoint_privacy import jsonl_lines
from shared_context.behavior_instruction_models import InstructionModels
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import ExecutorAdapter, ExecutorEvidence
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from web_test_console.adapter import parse_config, canonical, sha
from web_test_console.bridge import create_app
from web_test_console.instructions import InstructionConnection
from web_test_console.freeform import InstructionReply, ConversationProjection, AvailableModels


FIXTURES=Path(__file__).parent/"fixtures"/"web_test_console"


def open_ui(c):
    app=create_app(c.config,c.journal,backend=c.backend,token_provider=lambda:c.h.headers()["Authorization"][7:],
        clock=lambda:c.h.time,environment="OFFLINE_TEST",instruction_connection=c.connection)
    ui=TestClient(app,base_url="http://127.0.0.1:8765")
    ui.get("/")
    headers={"Origin":"http://127.0.0.1:8765","X-Console-CSRF":ui.get("/api/state").json()["csrf"]}
    authenticated=ui.post("/api/authenticate",json={},headers=headers)
    assert authenticated.status_code==200,authenticated.text
    return SimpleNamespace(app=app,ui=ui,headers=headers)


def test_sidebar_agent_is_derived_from_the_actual_bound_configuration(real_interface):
    c = real_interface
    state = c.browser.ui.get("/api/state").json()
    assert state["agents"] == [{"id": c.config.standing_policy.template.agent_oid,
                               "name": "FA 助理", "description": "目前配置的工作助理"}]
    original = state["agents"]
    new = c.browser.ui.post("/api/instructions/conversations/new", json={}, headers=c.browser.headers)
    assert new.status_code == 200
    assert new.json()["agents"] == original
    assert len(new.json()["composer"]["threads"]) > len(state["composer"]["threads"])


@pytest.fixture
def real_interface(auth,jwk_client,signing_key,tmp_path,tmp_path_factory,monkeypatch):
    from shared_context.behavior_channels_pin_refresh import core_code_sha256
    current_core=core_code_sha256()
    import test_teams_metrics
    monkeypatch.setattr(test_teams_metrics,"TENANT",TENANT)
    h,control,human,facts=enabled(assistant_harness(auth,jwk_client,signing_key),after_global=True)
    catalog=[{"deployment":name,"provisioning":"Succeeded","capabilities":{"chatCompletion":"true"}}
             for name in ("gpt-4.1-mini","configured-second-model")]
    control._models=InstructionModels(catalog,{
        name:{"max_output_tokens":32768,"input_microusd_per_million":1000000,"output_microusd_per_million":1000000}
        for name in ("gpt-4.1-mini","configured-second-model")},"gpt-4.1-mini")
    install_work_policy_routes(h.app,SimpleNamespace(runtime=h.runtime,verifier=h.verifier,
        installation=lambda:control.installed(),require_current_native_evidence=lambda:h.delivery._readiness(),
        assistant=SimpleNamespace(authority=h.assistant)))
    local=_LocalHttp(h.client)
    c=SimpleNamespace(h=h,control=control,config=parse_config(json.dumps(configuration(h,control)).encode()),
        journal=tmp_path/"same.sqlite3",core=current_core,calls=[],sdk_calls=[],sdk_errors=[],evidence={},starts=[],errors=[],
        mutate=lambda req,res:res,lost=False,reply_text=None)
    c.connection=InstructionConnection(schema_version="fa-console-instructions-connection-v1",
        configuration_sha256=c.config.sha256,backend_core_sha256=current_core)
    # Keep the SDK's nested 64-character session path out of the long test-name directory.
    sdk_root=tmp_path_factory.mktemp("sdk")

    async def sdk_io_inner(**options):
        # Actual AssistantSession chooses prompt/model/resume and restores the
        # original archive before this I/O-only substitute is entered.
        sid=options["sdk_session_id"];folder=options["state_dir"]/"config"/"session-state"/sid
        fs=options.get("session_files")
        prior=[]
        if options["resume"]:
            raw=await fs.read_file("/session-state/events.jsonl") if fs is not None else (folder/"events.jsonl").read_text(encoding="utf-8")
            prior=[json.loads(line) for line in jsonl_lines(raw)]
            assert prior[0]["data"]["sessionId"]==sid
        else:
            assert not folder.exists()
            if fs is None:folder.mkdir(parents=True)
            prior=[{"type":"session.start","data":{"sessionId":sid}}]
        c.sdk_calls.append({"sid":sid,"resume":options["resume"],"model":options["model"],"prompt":options["prompt"],
            "prior_user_messages":[e["data"]["content"] for e in prior if e["type"]=="user.message"],
            "timeout_seconds":options["timeout_seconds"]})
        text=c.reply_text if c.reply_text is not None else "OFFLINE SDK I/O substitute reply "+str(len(c.sdk_calls))
        entries=prior+[{"type":"user.message","data":{"content":options["prompt"]}},
                       {"type":"assistant.message","data":{"content":text}}]
        serialized="\n".join(json.dumps(e,ensure_ascii=False) for e in entries)
        if fs is not None:
            await fs.write_file("/session-state/events.jsonl",serialized)
            await fs.write_file("/session-state/workspace.yaml","offline: true\n")
        else:
            (folder/"events.jsonl").write_text(serialized,encoding="utf-8")
            (folder/"workspace.yaml").write_text("offline: true\n",encoding="utf-8")
        return {"final_message":text,"sdk_stop":aw.AssistantSdkStop(pid=123,exit_code=0,graceful=True)}
    async def sdk_io(**options):
        try:return await sdk_io_inner(**options)
        except Exception as error:
            c.sdk_errors.append(error)
            raise
    monkeypatch.setattr("shared_context.behavior_assistant_hosted.run_copilot",sdk_io)

    def start(request):
        try:
            c.starts.append(request)
            assignment,ticket=h.assistant.issue(request)
            client=AssistantClient("https://interface.example",assignment,ticket,
                token_provider=lambda:h.headers("agent")["Authorization"][7:],transport=local)
            try:
                session=AssistantSession(client,root=sdk_root/str(len(c.starts)),
                                         runtime_path=tmp_path/"OFFLINE-NOT-LAUNCHED.exe")
                session.dispatch(aw.Start(action="resume" if assignment.resume else "start").model_dump(mode="json"))
                result=h.assistant.result(request.binding)
                eid=control_key("offline-ui-instruction-execution",request.binding.start_request_id)
                c.evidence[eid]=ExecutorEvidence(evidence_id=eid,binding=request.binding,sequence=1,
                    native_execution_id="OFFLINE-no-runtime",state="succeeded",result=result,
                    entire_tree_idle=True,all_inflight_starts_fenced=True,proof=b"OFFLINE SDK I/O substitute, not native")
                return eid
            finally:client.close()
        except Exception as error:
            c.errors.append(error)
            raise
    h.workers.adapter=ExecutorAdapter(start=start,query=lambda binding:control_key(
        "offline-ui-instruction-execution",binding.start_request_id),stop=lambda _:None,verify=c.evidence.__getitem__)

    def request(req):
        before=copy.deepcopy(h.transport.backing.rows)
        response=local.handle_request(req)
        c.calls.append({"method":req.method,"path":req.url.path,"status":response.status_code,
                        "body_sha256":sha(req.content)})
        if req.method=="GET" or req.url.path.endswith("/preflight"):
            assert h.transport.backing.rows==before,"read-only API changed actual fixture inventory"
        if c.lost and req.url.path=="/work-policy/v1/submit" and response.status_code==200:
            raise httpx.ReadTimeout("OFFLINE response lost after actual admission")
        return c.mutate(req,response)
    c.backend=httpx.Client(transport=httpx.MockTransport(request))
    c.browser=open_ui(c)
    yield c
    c.browser.ui.close();c.backend.close();h.client.close()


def post(c,path,body=None):
    return c.browser.ui.post(path,json=body or {},headers=c.browser.headers)


def complete(c,binding_id):
    try:
        state=c.h.runtime.controller.step(binding_id)
    except Problem:
        if c.sdk_errors:raise c.sdk_errors[0]
        if c.errors:raise c.errors[0]
        raise
    if c.errors:raise c.errors[0]
    assert state.state=="succeeded" and state.result_forwarded
    return state


@pytest.mark.parametrize("separator", ["\n", "\u0085", "\u2028", "\u2029"],
                         ids=["lf", "next-line", "line-separator", "paragraph-separator"])
def test_frozen_http_table_two_turns_cold_archive_original_text_model_and_repeatable_reply(real_interface, separator):
    c=real_interface
    first=c.browser.ui.get("/api/state").json()["composer"]
    text="  請以白話描述雨聲。"+separator+"原文不截斷🙂  "
    sent=post(c,"/api/instructions/"+first["request_id"]+"/submit",{"content":text,"model":"gpt-4.1-mini"})
    assert sent.status_code==200,sent.text
    one=sent.json()["task"]
    complete(c,one["binding_id"])
    before=copy.deepcopy(c.h.transport.backing.rows)
    journal_before=c.journal.read_bytes()
    observed=c.browser.ui.get("/api/instructions/progress?thread_id="+first["thread_id"])
    assert observed.status_code==200,observed.text
    assert c.h.transport.backing.rows==before and c.journal.read_bytes()==journal_before
    state=observed.json()
    item=next(t for t in state["tasks"] if t["task_id"]==first["request_id"])
    assert item["reply"]["data"]["text"]=="OFFLINE SDK I/O substitute reply 1"
    assert item["acknowledgment"]["state"]=="NOT_SENT" and state["composer"]["can_follow_up"]
    second=post(c,"/api/instructions/"+first["thread_id"]+"/next").json()["composer"]
    follow=post(c,"/api/instructions/"+second["request_id"]+"/submit",
                {"content":"請補充前文的節奏。","model":"configured-second-model"})
    assert follow.status_code==200,follow.text
    two=follow.json()["task"]
    pending=c.browser.ui.get("/api/state?thread_id="+first["thread_id"]).json()["composer"]
    assert pending["can_follow_up"] is False
    assert pending["can_reset"] is False
    complete(c,two["binding_id"])
    outcome=c.browser.ui.get("/api/instructions/progress?thread_id="+first["thread_id"])
    assert outcome.status_code==200,outcome.text
    first_io,second_io=c.sdk_calls
    assert first_io["prompt"]==text and not first_io["resume"]
    assert second_io["resume"] and second_io["prior_user_messages"]==[text]
    assert first_io["sid"]==second_io["sid"]
    assert second_io["model"]=="configured-second-model"
    assert first_io["timeout_seconds"] is None and second_io["timeout_seconds"] is None
    stable=copy.deepcopy(c.h.transport.backing.rows)
    for _ in range(10):
        read=c.browser.ui.get("/api/instructions/"+second["request_id"]+"/reply")
        assert read.status_code==200,read.text
    assert c.h.transport.backing.rows==stable
    assert len(c.starts)==2 and len(c.sdk_calls)==2
    assert not any(v["path"].endswith(("/delivery/pull","/acknowledge")) for v in c.calls)
    confirmed=post(c,"/api/instructions/"+second["request_id"]+"/confirm-reply")
    assert confirmed.status_code==200,confirmed.text
    assert confirmed.json()["task"]["acknowledgment"]["confirmation_level"]=="client_received"
    count=len(c.calls)
    assert post(c,"/api/instructions/"+second["request_id"]+"/confirm-reply").status_code==200
    assert len(c.calls)==count
    assert len([v for v in c.calls if v["path"].endswith("/delivery/pull")])==1
    assert len([v for v in c.calls if v["path"].endswith("/acknowledge")])==1
    if folder:=os.environ.get("FA_UI_INTERFACE_RESULTS"):
        target=Path(folder);target.mkdir(parents=True,exist_ok=True)
        filename="two-turn-proof.json" if separator=="\n" else f"two-turn-proof-{ord(separator):04x}.json"
        (target/filename).write_text(json.dumps({
            "actual_environment":"NOT_RUN","interface_core":c.core,"real_http_table_producers":True,
            "sdk_io_substituted":True,"sdk_calls":c.sdk_calls,"pure_reply_reads":10,"inventory_unchanged":True,
            "first_ack_not_required_for_follow_up":True,"calls":c.calls,
            "second_manual_ack":confirmed.json()["task"]["acknowledgment"],
        },ensure_ascii=False,indent=2),encoding="utf-8")


def test_frozen_examples_and_public_progress_dto_match_exact_not_private_shapes():
    root=Path(os.environ.get("FA_UI_INTERFACE_ART",FIXTURES))
    raw=(root/"interface-producer-examples.json").read_bytes()
    assert sha(raw)=="06435bdcb78e9b51ba75fbd58e7f527aece24d3c53fe76741736a3b2f77a43f7"
    examples=json.loads(raw)
    command=aw.InstructionSubmit.model_validate_json(canonical(examples["first_command"]))
    assert sha(command.model_dump_json().encode())==examples["preflight"]["submit_body_sha256"]
    assert command.content.startswith("  ") and command.content.endswith("\n")
    for name in ("submit","second_submit","reconcile_present"):
        assert InstructionReply.model_validate_json(canonical(examples[name])).model=="gpt-4.1-mini"
    conversation=ConversationProjection.model_validate_json(canonical(examples["conversation"]))
    assert all(turn.progress.reasoning_summary.text is None for turn in conversation.turns)
    assert all(turn.progress.events==() for turn in conversation.turns)
    assert AvailableModels.model_validate_json(canonical(examples["models"])).default_model=="gpt-4.1-mini"
    reply=aw.TextReply.model_validate_json(canonical(examples["reply"]["body"]))
    assert sha(reply.model_dump_json().encode())==examples["reply"]["headers"]["x-content-sha256"]
    assert examples["reconcile_missing"]["status"]==503
    assert examples["reconcile_missing"]["body"]["error"]["code"]=="WORK_SUBMIT_UNKNOWN"
    event=aw.PublicProgressEvent.model_validate_json(json.dumps({
        "seq":1,"at":"2026-09-15T10:00:00Z","kind":"tool_started","tool_name":"read_context","status":"started",
        "summary":"OFFLINE DTO mapping only","source":{"source_id":"source-one","version":1,"sha256":"a"*64,"byte_length":1}}))
    assert event.source.source_id=="source-one"
    with pytest.raises(ValueError):
        aw.PublicProgressEvent.model_validate_json(json.dumps({
            "seq":2,"at":"2026-09-15T10:00:00Z","kind":"reasoning_delta","summary":"PRIVATE"}))


def test_frozen_lost_admission_restart_reconciles_original_without_new_submit(real_interface):
    c=real_interface
    draft=c.browser.ui.get("/api/state").json()["composer"]
    path="/api/instructions/"+draft["request_id"]+"/submit"
    c.lost=True
    lost=post(c,path,{"content":"OFFLINE 原回應遺失，不重送","model":"gpt-4.1-mini"})
    assert lost.status_code==503,lost.text
    with sqlite3.connect(c.journal) as db:
        original=db.execute("SELECT intent,outcome FROM intents WHERE task_id=? AND phase='submit'",
                            (draft["request_id"],)).fetchone()
    c.browser.ui.close();c.browser=open_ui(c)
    count=len(c.calls)
    assert post(c,path,{"content":"OFFLINE 原回應遺失，不重送","model":"gpt-4.1-mini"}).status_code==200
    assert len(c.calls)==count
    before=copy.deepcopy(c.h.transport.backing.rows)
    reconciled=post(c,"/api/tasks/"+draft["request_id"]+"/submit-reconcile")
    assert reconciled.status_code==200,reconciled.text
    assert c.h.transport.backing.rows==before
    assert reconciled.json()["task"]["submission"]["state"]=="UNKNOWN"
    assert reconciled.json()["task"]["admission"]["model"]=="gpt-4.1-mini"
    with sqlite3.connect(c.journal) as db:
        assert db.execute("SELECT intent,outcome FROM intents WHERE task_id=? AND phase='submit'",
                          (draft["request_id"],)).fetchone()==original
    assert len([r for r in c.calls if r["path"]=="/work-policy/v1/submit"])==1
    assert not c.starts and not c.sdk_calls


def test_direct_submit_model_binding_and_pure_reply_digest_fail_closed(real_interface):
    c=real_interface
    draft=c.browser.ui.get("/api/state").json()["composer"]
    path="/api/instructions/"+draft["request_id"]+"/submit"
    text="OFFLINE model binding"
    def wrong_model(req,response):
        if req.url.path.endswith("/submit") and response.status_code==200:
            data=response.json();data["model"]="configured-second-model"
            return httpx.Response(200,json=data)
        return response
    c.mutate=wrong_model
    refused=post(c,path,{"content":text,"model":"gpt-4.1-mini"})
    assert refused.status_code==502,refused.text
    assert sum(r["path"]=="/work-policy/v1/submit" for r in c.calls)==1
    c.mutate=lambda req,response:response
    submitted=post(c,"/api/tasks/"+draft["request_id"]+"/submit-reconcile")
    assert submitted.status_code==200,submitted.text
    assert sum(r["path"]=="/work-policy/v1/submit" for r in c.calls)==1
    complete(c,submitted.json()["task"]["binding_id"])
    c.mutate=lambda req,response:(httpx.Response(200,content=response.content,
        headers={**response.headers,"x-content-sha256":"f"*64}) if req.url.path.endswith("/reply") else response)
    before=copy.deepcopy(c.h.transport.backing.rows)
    read=c.browser.ui.get("/api/instructions/progress?thread_id="+draft["thread_id"])
    assert read.status_code==502,read.text
    assert c.h.transport.backing.rows==before
    state=c.browser.ui.get("/api/state?thread_id="+draft["thread_id"]).json()
    item=next(t for t in state["tasks"] if t["task_id"]==draft["request_id"])
    assert item["reply"] is None and item["acknowledgment"]["state"]=="NOT_SENT"
