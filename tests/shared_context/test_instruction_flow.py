"""Real authority/HTTP/Table producers; pinned SDK I/O doubles, no executable or model."""
import asyncio
import base64
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import httpx
import pytest
from copilot import CopilotClient
from copilot.client import GetStatusResponse
from copilot.session import CopilotSession

from test_behavior_assistant import assistant
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled
from shared_context import m2_copilot
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_checkpoint_privacy import jsonl_lines
from shared_context.behavior_work_policy_api import install_work_policy_routes


@pytest.fixture
def runtime_path():
    return Path(__file__)  # Only path validation; Popen is forbidden below.


@pytest.fixture(autouse=True)
def instruction_models(tmp_path,monkeypatch):
    catalog=tmp_path/"OFFLINE-models.json"
    facts=tmp_path/"OFFLINE-model-facts.json"
    names=("gpt-4.1-mini","configured-second-model")
    catalog.write_text(json.dumps([{"deployment":name,"provisioning":"Succeeded",
        "capabilities":{"chatCompletion":"true"}} for name in names]),encoding="utf-8")
    facts.write_text(json.dumps({name:{"max_output_tokens":32768,"input_microusd_per_million":400000,
        "output_microusd_per_million":1600000} for name in names}),encoding="utf-8")
    monkeypatch.setenv("FA_INSTRUCTION_MODEL_CATALOG_FILE",str(catalog))
    monkeypatch.setenv("FA_INSTRUCTION_MODEL_FACTS_FILE",str(facts))
    monkeypatch.setenv("FA_INSTRUCTION_DEFAULT_MODEL",names[0])


def sdk_io(monkeypatch,after_response=None,archive_oversize=False,*,expected_tools=("read_context","execute_operation")):
    calls = []
    def denied(*args, **kwargs):
        pytest.fail("no runtime process or real network is allowed")
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)
    def factory(**connection):
        from threading import Event
        sdk = create_autospec(CopilotClient, instance=True)
        exited=Event()
        sdk._cli_process = SimpleNamespace(pid=999,poll=lambda:0 if exited.is_set() else None,
                                           wait=lambda:exited.wait(),returncode=0)
        sdk.stop.side_effect=lambda:exited.set()
        sdk.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)
        async def opened(*args, **options):
            resume = bool(args)
            sid = args[0] if resume else options["session_id"]
            if resume:
                assert options["continue_pending_work"] is False
            assert {t.name for t in options["tools"]} == set(expected_tools)
            directory = Path(options["config_directory"]) / "session-state" / sid
            fs=options["create_session_fs_handler"](SimpleNamespace(session_id=sid)) if "create_session_fs_handler" in options else None
            if resume:
                raw=await fs.read_file("/session-state/events.jsonl") if fs is not None else (directory/"events.jsonl").read_text(encoding="utf-8")
                events = [json.loads(line) for line in jsonl_lines(raw)]
            else:
                events = [{"type":"session.start","data":{"sessionId":sid}}]
                metadata="X"*(128*1024+1) if archive_oversize else "OFFLINE SDK I/O metadata\n"
                if fs is not None:
                    await fs.write_file("/session-state/workspace.yaml",metadata)
                else:
                    directory.mkdir(parents=True)
                    (directory/"workspace.yaml").write_text(metadata,encoding="utf-8")
            session = create_autospec(CopilotSession, instance=True)
            handlers=[]
            def subscribe(handler):
                handlers.append(handler)
                return lambda:handlers.remove(handler)
            session.on.side_effect=subscribe
            async def abort():
                from copilot.session_events import SessionIdleData
                for handler in handlers:handler(SimpleNamespace(data=SessionIdleData(aborted=True)))
            session.abort.side_effect=abort
            session.session_id = sid
            session.get_events.return_value = []
            async def send(prompt, **kwargs):
                messages = [{"role":"user" if event["type"]=="user.message" else "assistant",
                             "content":event["data"]["content"]}
                            for event in events if event["type"] in {"user.message","assistant.message"}]
                messages.append({"role":"user","content":prompt})
                response = await connection["request_handler"].send_request(
                    httpx.Request("POST","https://context.invalid/model/v1/chat/completions",
                                  json={"model":options["model"],"messages":messages,"stream":True}),
                    SimpleNamespace(request_id=f"offline-{len(calls)}",session_id=sid))
                await response.aread()
                data = [json.loads(line[6:]) for line in response.content.decode().splitlines()
                        if line.startswith("data: {")]
                answer = "".join(item["choices"][0]["delta"].get("content","") for item in data if item["choices"])
                calls.append({"resume":resume,"sdk_session_id":sid,"prompt":prompt,"messages":messages,"answer":answer})
                events.extend([{"type":"user.message","data":{"content":prompt}},
                               {"type":"assistant.message","data":{"content":answer}}])
                text="\n".join(json.dumps(e,ensure_ascii=False) for e in events)+"\n"
                if fs is not None:
                    await fs.write_file("/session-state/events.jsonl",text)
                else:
                    (directory/"events.jsonl").write_text(text,encoding="utf-8")
                if after_response is not None:
                    after_response()
                    await asyncio.sleep(0)
                    return "OFFLINE-cancelled-message"
                from copilot.session_events import AssistantMessageData,SessionIdleData
                final=SimpleNamespace(data=AssistantMessageData(content=answer,message_id="OFFLINE-reply"))
                for handler in handlers:
                    handler(final)
                    handler(SimpleNamespace(data=SessionIdleData()))
                return "OFFLINE-message-id"
            session.send_and_wait.side_effect = send
            session.send.side_effect = send
            return session
        sdk.create_session.side_effect = opened
        sdk.resume_session.side_effect = opened
        return sdk
    monkeypatch.setattr(m2_copilot,"CopilotClient",create_autospec(CopilotClient,side_effect=factory))
    return calls


def instruction_inventory(h):
    from shared_context.behavior_migration import AuthorityInventory
    from shared_context.records import Record
    from shared_context.behavior_store import GUARD_KEY
    rows,receipts,hashes={}, {}, {}
    for (_,_,key),value in h.transport.backing.rows.items():
        raw=value["Payload"]
        record=Record(json.loads(raw),value.metadata["etag"])
        hashes[key]=hashlib.sha256(raw).hexdigest()
        if key==GUARD_KEY:guard=record
        elif key.startswith("behavior:v1:row:"):rows[key.removeprefix("behavior:v1:row:")]=record
        elif key.startswith("behavior:v1:receipt:"):receipts[key.removeprefix("behavior:v1:receipt:")]=record
    return AuthorityInventory(h.store._scope,guard,rows,receipts,hashes)


def test_http_two_turns_use_saved_sdk_context_and_exact_reply_without_ack_gate(assistant,tmp_path,monkeypatch):
    h, control, human, _ = enabled(assistant)
    calls = sdk_io(monkeypatch)
    from dataclasses import replace
    failures=[]
    original_start=h.workers.adapter.start
    def start(command):
        try:return original_start(command)
        except Exception as error:
            import traceback
            failures.append(type(error).__name__+":"+str(getattr(error,"body",{}))+"\n"+"".join(traceback.format_tb(error.__traceback__)))
            raise
    h.workers.adapter=replace(h.workers.adapter,start=start)
    h.endpoint.replies = iter([{"content":"第一則真回覆：雨敲窗沿。\n"},
                               {"content":"第二則真回覆：雨聲輕，晚風停。\n"+"完整長回答。"*1600}])
    h.endpoint.unbounded=True
    composition=SimpleNamespace(runtime=h.runtime,verifier=h.verifier,installation=lambda:None,
                                 require_current_native_evidence=lambda:None)
    install_work_policy_routes(h.app,composition)
    def post(route,body):
        return h.client.post("/work-policy/v1/"+route,content=body.model_dump_json(),
                             headers=h.headers())
    first=InstructionSubmit(request_id="poem-one",content="  請寫一首關於雨的詩。\n")
    from shared_context.behavior_work_policy import WorkPreflightQuery
    pre=post("preflight",WorkPreflightQuery(read_id="read-one",request_id=first.request_id,
        submit_body_sha256=hashlib.sha256(first.model_dump_json().encode()).hexdigest(),content=first.content))
    assert pre.status_code==200,pre.text
    assert pre.json()["conversation_id"] and pre.json()["turn_number"]==1
    missing=post("submit-reconcile",first)
    assert missing.status_code==200 and missing.json()["state"]=="not_admitted"
    assert missing.json()["request_id"]==first.request_id and missing.json()["retry_same_request"] is True
    accepted=post("submit",first)
    assert accepted.status_code==200,accepted.text
    first_result=accepted.json()
    cid=first_result["conversation_id"]
    try:status=h.runtime.controller.step(first_result["binding_id"])
    except Exception:
        pytest.fail("\n".join(failures))
    assert status.state=="succeeded" and status.result_forwarded and not status.obligation_fulfilled
    current=h.client.get("/work-policy/v1/conversations/"+cid,headers=h.headers())
    assert current.status_code==200,current.text
    assert current.json()["can_follow_up"] is True
    assert current.json()["turns"][0]["model_requests"]==1
    guard=h.store._guard()
    assert post("submit-reconcile",first).json()["status"]["state"]=="succeeded"
    assert h.store._guard()==guard and len(calls)==1
    # A different private filesystem forces the exact stored SDK archive to restore.
    h.executor.root=tmp_path/"different-host-filesystem"
    second=InstructionSubmit(request_id="poem-two",conversation_id=cid,content="把上一首縮短，保留雨聲。")
    accepted2=post("submit",second)
    assert accepted2.status_code==200,accepted2.text
    assert accepted2.json()["conversation_id"]==cid and accepted2.json()["turn_number"]==2
    status2=h.runtime.controller.step(accepted2.json()["binding_id"])
    assert status2.state=="succeeded" and status2.result_forwarded
    assert [c["prompt"] for c in calls]==[first.content,second.content]
    assert calls[0]["sdk_session_id"]==calls[1]["sdk_session_id"] and calls[1]["resume"] is True
    assert calls[1]["messages"][:2]==[
        {"role":"user","content":first.content},{"role":"assistant","content":"第一則真回覆：雨敲窗沿。\n"}]
    for received,reply in ((first_result,calls[0]["answer"]),(accepted2.json(),calls[1]["answer"])):
        path="/behavior/v1/bindings/"+received["binding_id"]+"/delivery/pull"
        fetched=h.client.post(path,json={"request_id":"read-"+received["binding_id"][:16]},headers=h.headers())
        assert fetched.status_code==200,fetched.text
        payload=json.loads(base64.urlsafe_b64decode(fetched.json()["content"]))
        assert payload["schema_version"]=="fa-assistant-text-reference-v1"
        exact=h.client.get(f"/work-policy/v1/conversations/{cid}/turns/{received['turn_number']}/reply",headers=h.headers())
        assert exact.status_code==200 and exact.json()["text"]==reply
        assert hashlib.sha256(exact.content).hexdigest()==payload["sha256"]
        assert exact.json()["sdk_session_id"]==calls[0]["sdk_session_id"]
    assert len(h.endpoint.requests)==2
    pure=[]
    guard=h.store._guard()
    for _ in range(10):
        response=h.client.get(f"/work-policy/v1/conversations/{cid}/turns/2/reply",headers=h.headers())
        assert response.status_code==200,response.text
        assert response.headers["x-content-sha256"]==hashlib.sha256(response.content).hexdigest()
        assert response.json()["text"]==calls[1]["answer"]
        pure.append(response)
    assert h.store._guard()==guard and len(h.endpoint.requests)==2
    import os
    if os.environ.get("FA_INTERFACE_CAPTURE"):
        final=h.client.get("/work-policy/v1/conversations/"+cid,headers=h.headers())
        models=h.client.get("/work-policy/v1/models",headers=h.headers())
        Path(os.environ["FA_INTERFACE_CAPTURE"]).write_text(json.dumps({
            "evidence":"OFFLINE real HTTP/Table producers; SDK I/O is a test double; no native/provider/model",
            "first_command":json.loads(first.model_dump_json()),"second_command":json.loads(second.model_dump_json()),
            "preflight":pre.json(),"submit":first_result,"second_submit":accepted2.json(),
            "reconcile_missing":{"status":missing.status_code,"body":missing.json()},
            "reconcile_present":post("submit-reconcile",first).json(),"conversation":final.json(),
            "models":models.json(),"reply":{"body":pure[-1].json(),"headers":{
                k:pure[-1].headers[k] for k in ("content-type","cache-control","x-content-sha256")}},
        },ensure_ascii=False,indent=2),encoding="utf-8")


def test_long_reference_get_and_original_ack_once(assistant,monkeypatch):
    import os
    from shared_context import behavior_wire as wire
    h,control,human,_=enabled(assistant)
    sdk_io(monkeypatch)
    text="OFFLINE完整長回答。\n"*1200
    h.endpoint.replies=iter([{"content":text}])
    h.endpoint.unbounded=True
    install_work_policy_routes(h.app,SimpleNamespace(runtime=h.runtime,verifier=h.verifier,
        installation=lambda:None,require_current_native_evidence=lambda:None))
    command=InstructionSubmit(request_id="long-reference",content="Return the complete explanation.")
    binding,_=control.submit(human,command)
    done=h.runtime.controller.step(binding.binding_id)
    assert done.state=="succeeded" and done.result_forwarded
    cid=control.records[binding.binding_id].profile.instruction.conversation_id
    reply=h.client.get(f"/work-policy/v1/conversations/{cid}/turns/1/reply",headers=h.headers())
    assert reply.status_code==200 and reply.json()["text"]==text and len(reply.content)>8192
    path="/behavior/v1/bindings/"+binding.binding_id+"/delivery"
    pulled=h.client.post(path+"/pull",json={"request_id":"original-long-pull"},headers=h.headers())
    assert pulled.status_code==200,pulled.text
    reference=json.loads(base64.urlsafe_b64decode(pulled.json()["content"]))
    assert reference["sha256"]==hashlib.sha256(reply.content).hexdigest()
    assert reference["byte_length"]==len(reply.content)
    descriptor=wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(pulled.headers["x-behavior-binding"]))
    acknowledgment=wire.PullAcknowledgment(request_id="original-long-ack",
        exchange_id=pulled.headers["x-behavior-exchange"],acknowledgment_token=pulled.headers["x-behavior-ack-token"],
        binding=descriptor)
    ack=h.client.post(path+"/acknowledge",content=acknowledgment.model_dump_json(),headers=h.headers())
    assert ack.status_code==200 and ack.json()["result"]["fulfillment"]=="fulfilled"
    guard=h.store._guard()
    assert h.client.post(path+"/acknowledge",content=acknowledgment.model_dump_json(),headers=h.headers()).status_code==200
    assert h.store._guard()==guard and len(h.endpoint.requests)==1
    if os.environ.get("FA_REFERENCE_CAPTURE"):
        Path(os.environ["FA_REFERENCE_CAPTURE"]).write_text(json.dumps({
            "evidence":"OFFLINE real HTTP/Table producers; SDK I/O double; no native/provider/model",
            "command":json.loads(command.model_dump_json()),"binding":descriptor.model_dump(mode="json"),
            "reply_json_utf8":reply.content.decode("utf-8"),"reply_headers":{
                k:reply.headers[k] for k in ("content-type","cache-control","x-content-sha256")},
            "pull_payload":pulled.json(),"reference":reference,"ack_result":ack.json(),
            "repeated_ack_wrote":False,"model_transport_requests":1,
        },ensure_ascii=False,indent=2),encoding="utf-8")


def test_cancel_reaches_official_sdk_abort_and_keeps_original_work(assistant,tmp_path,monkeypatch):
    from dataclasses import replace
    from shared_context.behavior_assistant import _LocalHttp
    from shared_context.behavior_assistant_hosted import AssistantSession,AssistantClient
    h,control,human,_=enabled(assistant)
    binding,_=control.submit(human,InstructionSubmit(request_id="cancel-original",content="Explain recursion."))
    starts=[]
    h.workers.adapter=replace(h.workers.adapter,start=lambda command:starts.append(command))
    h.workers.start(binding.job_id,binding.owner)
    assignment,ticket=h.assistant.issue(starts[0])
    client=AssistantClient("https://context.invalid",assignment,ticket,
        token_provider=lambda:h.headers("agent")["Authorization"].removeprefix("Bearer "),transport=_LocalHttp(h.client))
    h.endpoint.unbounded=True
    h.endpoint.replies=iter([{"content":"OFFLINE response before user cancellation"}])
    bridge=AssistantSession(client,root=tmp_path/"cancel-sdk",runtime_path=Path(__file__),
                            model_transport=h.endpoint.transport)
    def cancel():
        control.cancel(human,binding.binding_id)
        bridge.request_cancel()
    calls=sdk_io(monkeypatch,after_response=cancel)
    result=bridge.dispatch({"operation":"assistant","profile":"assistant-v1.2","action":"start"})
    assert result["state"]=="ready_to_finalize" and result["work_outcome"]=="cancelled"
    assert result["sdk_stop"]["graceful"] and len(calls)==1
    assert h.workers.inspect(binding.job_id).cancel_requested
    original=h.assistant.result(starts[0].binding)
    assert original.state=="cancelled"
    assert json.loads(original.content)["sdk_reply_available"] is False
    client.close()


def test_unknown_checkpoint_upload_keeps_reply_but_never_restarts_empty(assistant,monkeypatch):
    from shared_context.errors import Problem
    h,control,human,_=enabled(assistant)
    calls=sdk_io(monkeypatch)
    def unavailable(*_):
        raise Problem(503,"ARTIFACT_UNKNOWN","OFFLINE checkpoint put outcome unknown")
    monkeypatch.setattr(h.assistant.artifacts.store,"put",unavailable)
    h.endpoint.unbounded=True
    h.endpoint.replies=iter([{"content":"Complete public answer retained even if SDK archive is unavailable."}])
    binding,_=control.submit(human,InstructionSubmit(request_id="archive-limit",content="Explain recursion."))
    result=h.runtime.controller.step(binding.binding_id)
    assert result.state=="succeeded" and result.result_forwarded and len(calls)==1
    cid=control.records[binding.binding_id].profile.instruction.conversation_id
    assert control.read_reply(human,cid,1).text==calls[0]["answer"]
    progress=control.conversation(human,cid)
    assert not progress["can_follow_up"] and progress["follow_up_blocker"]=="SDK_STATE_UPLOAD_UNKNOWN"
    guard=h.store._guard()
    with pytest.raises(Problem):
        control.submit(human,InstructionSubmit(request_id="no-empty-restart",content="Continue.",conversation_id=cid))
    assert h.store._guard()==guard and len(calls)==1


def test_missing_checkpoint_part_never_starts_an_empty_sdk_and_keeps_old_answer(assistant,monkeypatch):
    from shared_context.errors import Problem
    from shared_context.behavior_assistant import key
    from shared_context.behavior_instruction import TurnArchive
    from shared_context.behavior_operations import _row
    h,control,human,_=enabled(assistant)
    calls=sdk_io(monkeypatch)
    h.endpoint.unbounded=True
    h.endpoint.replies=iter([{"content":"Original answer is still readable."}])
    first,_=control.submit(human,InstructionSubmit(request_id="saved-before-loss",content="Explain."))
    assert h.runtime.controller.step(first.binding_id).state=="succeeded"
    cid=control.records[first.binding_id].profile.instruction.conversation_id
    archive_key=key("text-archive",first.job_id)
    saved=_row(h.store.read_snapshot((archive_key,)),archive_key,TurnArchive)
    ref=saved.archive
    part=ref.parts[0]
    del h.assistant.artifacts.store._versions[(first.team_id,ref.artifact_id,part.version_id)]
    second,_=control.submit(human,InstructionSubmit(request_id="cannot-restart-empty",content="Continue.",conversation_id=cid))
    with pytest.raises(Problem):
        h.runtime.controller.step(second.binding_id)
    assert len(calls)==1 and len(h.endpoint.requests)==1
    assert control.read_reply(human,cid,1).text=="Original answer is still readable."
    assert _row(h.store.read_snapshot((archive_key,)),archive_key,TurnArchive)==saved


def test_failed_instruction_has_failure_delivery_not_a_nonexistent_text_reply(assistant, monkeypatch):
    from shared_context.errors import Problem
    h, control, human, _ = enabled(assistant)
    def failure():
        raise RuntimeError("OFFLINE SDK turn failure")
    calls = sdk_io(monkeypatch, after_response=failure)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "OFFLINE incomplete turn"}])
    binding, _ = control.submit(human, InstructionSubmit(request_id="failed-original", content="Explain recursion."))
    status = h.runtime.controller.step(binding.binding_id)
    assert status.state == "failed" and status.result_forwarded and len(calls) == 1
    cid = control.records[binding.binding_id].profile.instruction.conversation_id
    projection = control.conversation(human, cid)
    assert projection["turns"][0]["state"] == "failed"
    assert projection["turns"][0]["reply_available"] is False
    assert projection["turns"][0]["delivery_id"] is not None
    assert projection["can_follow_up"] is True
    with pytest.raises(Problem) as absent:
        control.read_reply(human, cid, 1)
    assert absent.value.body["error"]["code"] == "INSTRUCTION_REPLY_NOT_READY"


def test_completion_notification_requires_original_saved_session_and_ticket(assistant, monkeypatch):
    from fastapi.testclient import TestClient
    from shared_context.behavior_assistant_api import create_assistant_app, TICKET_HEADER
    h, control, human, _ = enabled(assistant)
    sdk_io(monkeypatch)
    issued = []
    original = h.assistant.issue
    def issue(start):
        value = original(start)
        issued.append(value)
        return value
    monkeypatch.setattr(h.assistant, "issue", issue)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "OFFLINE complete answer"}])
    binding, _ = control.submit(human, InstructionSubmit(request_id="notify-original", content="Explain recursion."))
    assert h.runtime.controller.step(binding.binding_id).state == "succeeded"
    assignment, ticket = issued[0]
    notified = []
    client = TestClient(create_assistant_app(h.assistant, h.verifier, on_result_ready=notified.append))
    path = f"/behavior/v1/bindings/{binding.binding_id}/assistant/sessions/{assignment.session_id}/"
    headers = {**h.headers("agent"), TICKET_HEADER: ticket.get_secret_value()}
    assert client.post(path+"status", json={"request_id": "status"}, headers=headers).status_code == 200
    assert notified == []
    assert client.post(path+"execution-ready", json={"request_id": "execution-ready"},
                       headers={**headers, TICKET_HEADER: "0"*64}).status_code == 403
    assert client.post(path+"execution-ready", json={"request_id": "other"}, headers=headers).status_code == 422
    assert client.post(path+"execution-ready", json={"request_id": "execution-ready"},
                       headers={**h.headers(), TICKET_HEADER: ticket.get_secret_value()}).status_code == 403
    assert notified == []
    guard = h.store._guard()
    response = client.post(path+"execution-ready", json={"request_id": "execution-ready"}, headers=headers)
    assert response.status_code == 200, response.text
    assert notified == [binding.binding_id] and h.store._guard() == guard


def test_seven_turn_conversation_remains_readable_with_unrelated_registered_work(assistant, monkeypatch):
    h, control, human, _ = enabled(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": f"Saved answer {n}"} for n in range(1, 8)])
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    for n in range(40):
        control.submit(human, InstructionSubmit(request_id=f"unrelated-{n}", content="Independent pending work."))
    cid = None
    for n in range(1, 8):
        binding, _ = control.submit(human, InstructionSubmit(
            request_id=f"conversation-turn-{n}", content=f"Question {n}", conversation_id=cid))
        cid = control.records[binding.binding_id].profile.instruction.conversation_id
        result = h.runtime.controller.step(binding.binding_id)
        assert result.state == "succeeded" and result.result_forwarded
    guard = h.store._guard()
    response = h.client.get(f"/work-policy/v1/conversations/{cid}", headers=h.headers())
    assert response.status_code == 200, response.text
    assert len(response.json()["turns"]) == 7
    assert all(turn["reply_available"] for turn in response.json()["turns"])
    reply = h.client.get(f"/work-policy/v1/conversations/{cid}/turns/7/reply", headers=h.headers())
    assert reply.status_code == 200 and reply.json()["text"] == "Saved answer 7"
    assert h.store._guard() == guard and len(h.endpoint.requests) == 7
