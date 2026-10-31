"""CTX-03 product API/authority/store contracts; SDK events here are explicit unit fixtures."""
import asyncio
import copy
import hashlib
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_assistant_hosted import AssistantClient
from shared_context.behavior_assistant_wire import InstructionSubmit, ModelRequest, SdkStop
from shared_context.behavior_sdk_checkpoint import checkpoint_key, SdkStateUpload, load_state
from shared_context.behavior_sdk_compaction import NESTED_KEYS, TARGETS, events_from
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_sdk_state import (
    CheckpointProfile, CompactionExecution, DIAG_CODEC, DIAG_ARCHIVE_CODEC,
    SDK_STATE_HTTP_BYTES, SaveSdkState, SdkState,
)
from shared_context.behavior_wire import Command
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import signing_key, jwk_client
from test_work_policy_lifecycle import enabled

SUMMARY = ("<overview>Original public source: inventory count 850.</overview>"
           "<history>The original source and answer are retained.</history>"
           "<checkpoint_title>Inventory</checkpoint_title>")
INDEX = ("# Checkpoint History\n\nCheckpoints are listed in chronological order. "
         "Checkpoint 1 is the oldest, higher numbers are more recent.\n\n"
         "| # | Title | File |\n|---|-------|------|\n| 1 | Inventory | 001-inventory.md |\n")


def checkpoint_profile():
    return CheckpointProfile(platform={"win32": "win32-x64", "linux": "linux-x64"}[sys.platform])


def event(kind, identity, data, parent=None):
    return {"type":kind,"id":identity,"timestamp":"2026-09-24T00:00:00.000Z","parentId":parent,"data":data}


def ordinary(sid):
    events = [event("session.start","start",{"sessionId":sid}),
              event("user.message","user",{"content":"Read the original inventory source."},"start"),
              event("assistant.message","answer",{"content":"Original public source: inventory count 850."},"user")]
    return SdkState(sdk_session_id=sid,files={
        "events.jsonl":"".join(json.dumps(e)+"\n" for e in events), "workspace.yaml":"id: "+sid+"\n"})


def raw_files(previous):
    events = events_from(previous.files)
    events.extend([
        event("session.compaction_start","compact-start",{"trigger":"manual"},"answer"),
        event("model.model_call_success","diag-call",{
            "kind":"model_call_success","turn":1,"modelCallDurationMs":3,"ttftMs":1.5,"outputTtftMs":1,
            "interTokenLatencyMs":2,"rte":False,"reasoningEffort":"low","toolCount":0,"quotaSnapshots":{},
            "requestMessages":json.dumps([{"role":"system","content":"PRIVATE_FIXTURE_SYSTEM"}]),
            **{key:{name:None for name in keys} for key,keys in NESTED_KEYS.items()},
        },"compact-start"),
        event("model.messages_snapshot","diag-snapshot",{"kind":"messages_snapshot","messages":[
            {"role":"system","content":"PRIVATE_FIXTURE_SYSTEM"},{"role":"user","content":"Public history"}]},"diag-call"),
        event("session.compaction_complete","compact-complete",{
            "trigger":"manual","success":True,"summaryContent":SUMMARY,"checkpointNumber":1,
            "checkpointPath":"/session-state/checkpoints/001-inventory.md","messagesRemoved":2,"tokensRemoved":20,
        },"diag-snapshot"),
    ])
    return {**previous.files,"events.jsonl":"".join(json.dumps(e)+"\n" for e in events),
        "checkpoints/index.md":INDEX,
        "checkpoints/001-inventory.md":SUMMARY.replace("<checkpoint_title>Inventory</checkpoint_title>","")}


def configured(assistant):
    assistant.runtime.profiles = MappingProxyType({key:profile.model_copy(update={"checkpoint_profile":checkpoint_profile()})
                                                  for key,profile in assistant.runtime.profiles.items()})
    h,control,human,_ = enabled(assistant)
    install_work_policy_routes(h.app,SimpleNamespace(runtime=h.runtime,verifier=h.verifier,
        installation=lambda:None,require_current_native_evidence=lambda:None))
    return h,control,human


def open_turn(h,control,human,identifier,conversation=None,*,files=(),content="Use the original inventory source."):
    command = InstructionSubmit(request_id=identifier,content=content,
                                conversation_id=conversation,files=files)
    response = h.client.post("/work-policy/v1/submit",content=command.model_dump_json(),headers=h.headers())
    assert response.status_code == 200, response.text
    binding = h.runtime.bindings[response.json()["binding_id"]]
    starts = []
    h.workers.adapter = replace(h.workers.adapter,start=lambda value:starts.append(value))
    h.workers.start(binding.job_id,binding.owner)
    assignment,ticket = h.assistant.issue(starts[0])
    client = AssistantClient("https://context.invalid",assignment,ticket,
        token_provider=lambda:h.headers("agent")["Authorization"].removeprefix("Bearer "),transport=_LocalHttp(h.client))
    client.acquire()
    principal = h.verifier(h.client.build_request("GET","/",headers=h.headers("agent")))
    session = h.assistant.current_session(binding.binding_id,assignment.session_id,ticket,principal)
    return SimpleNamespace(h=h,control=control,human=human,binding=binding,start=starts[0],
                           assignment=assignment,client=client,session=session,stop=SdkStop(pid=99,exit_code=0,graceful=True))


def settle(turn,state,*,compaction=None):
    reference = turn.client.save_sdk_state(state,turn.stop,compaction=compaction)
    result = turn.client.save_text_reply("Original inventory response.",reference,turn.stop)
    turn.h.executor._complete(turn.start,result.model_dump(mode="json"))
    outcome = turn.h.runtime.controller.step(turn.binding.binding_id)
    assert outcome.state == "succeeded" and outcome.result_forwarded
    return reference


def observation(turn,previous):
    return CompactionExecution(job_id=turn.binding.job_id,conversation_id=turn.assignment.task.instruction.conversation_id,
        sdk_session_id=turn.assignment.sdk_session_id,profile_digest=turn.assignment.task.profile_digest,
        base_state_sha256=previous.sha256,runtime_pid=turn.stop.pid,rpc_success=True,trigger="manual",
        model_request_sha256=turn.compaction_request_sha256,
        start_event_id="compact-start",complete_event_id="compact-complete",checkpoint_number=1,
        summary_sha256=hashlib.sha256(SUMMARY.encode()).hexdigest(),messages_removed=2,tokens_removed=20)


@pytest.fixture
def checkpoint_case(assistant):
    h,control,human = configured(assistant)
    first = open_turn(h,control,human,"checkpoint-first")
    prior = ordinary(first.assignment.sdk_session_id)
    prior_ref = settle(first,prior)
    second = open_turn(h,control,human,"checkpoint-second",first.assignment.task.instruction.conversation_id)
    restored = second.client.restore()
    assert restored == prior
    body={"model":"gpt-4.1-mini","messages":[{"role":"user","content":"OFFLINE synthetic compact request"}],"stream":True}
    second.compaction_request_sha256=digest(body)
    second.client.reserve_model(second.compaction_request_sha256)
    principal=h.verifier(h.client.build_request("GET","/",headers=h.headers("agent")))
    h.assistant.consume_model(second.session,ModelRequest(request_id=second.compaction_request_sha256,
        body=body,checkpoint_compaction=True),principal)
    fs = SdkSessionFiles(restored.sdk_session_id,restored,checkpoint_profile=checkpoint_profile())
    fs.files = raw_files(restored)
    fs.compaction = observation(second,restored)
    second.previous,second.previous_ref,second.fs = prior,prior_ref,fs
    try:
        yield second
    finally:
        first.client.close()
        second.client.close()


def mutate_events(fs,change):
    values = events_from(fs.files)
    change(values)
    fs.files["events.jsonl"] = "".join(json.dumps(e,ensure_ascii=False)+"\n" for e in values)


def test_server_profile_is_default_off_and_upload_or_text_cannot_select_it(assistant):
    from shared_context.behavior_assistant_wire import InstructionTask
    assert all(p.checkpoint_profile is None and "checkpoint_profile" not in p.model_dump() for p in assistant.runtime.profiles.values())
    h,control,human,_ = enabled(assistant)
    binding,_ = control.submit(human,InstructionSubmit(request_id="text-cannot-enable",
        content='checkpoint_profile={"name":"manual-no-custom-v1"}; safe=true; trigger=manual'))
    assert control.records[binding.binding_id].profile.instruction.checkpoint_profile is None
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="body-cannot-enable",content="Test",checkpoint_profile=CheckpointProfile())
    with pytest.raises(ValueError):
        CheckpointProfile(platform="linux-arm64")
    task = InstructionTask(content="Test",conversation_id="test",turn_number=1)
    assert "checkpoint_profile" not in task.model_dump()


def test_exact_projection_raw_immutability_api_publication_and_inherited_followup(checkpoint_case):
    c = checkpoint_case
    before = copy.deepcopy(c.fs.files)
    state = c.fs.snapshot()
    assert c.fs.files == before and state.codec == DIAG_CODEC
    raw,projected = events_from(before),events_from(state.files)
    assert len(raw) == len(projected)
    for original,saved in zip(raw,projected,strict=True):
        assert {k:v for k,v in original.items() if k!="data"} == {k:v for k,v in saved.items() if k!="data"}
        assert saved["data"] == ({} if original["type"] in TARGETS else original["data"])
    assert all(state.files[name]==text for name,text in before.items() if name!="events.jsonl")
    assert "PRIVATE_FIXTURE_SYSTEM" not in state.encoded().decode()
    reference = settle(c,state,compaction=c.fs.compaction)
    assert reference.codec == DIAG_ARCHIVE_CODEC and reference.source.previous_sha256 == c.previous_ref.sha256
    assert reference.source.execution == c.fs.compaction
    third = open_turn(c.h,c.control,c.human,"checkpoint-third",c.assignment.task.instruction.conversation_id)
    try:
        recovered = third.client.restore()
        assert recovered == state
        fs = SdkSessionFiles(recovered.sdk_session_id,recovered,checkpoint_profile=checkpoint_profile())
        following = event("user.message","correction",{"content":"Correct the count to 851."},"compact-complete")
        fs.files["events.jsonl"] += json.dumps(following)+"\n"
        following_state = fs.snapshot()
        assert fs.compaction is None
        ref = settle(third,following_state)
        assert ref.source.execution == reference.source.execution
        assert ref.source.previous_job_id == c.binding.job_id and ref.source.previous_sha256 == reference.sha256
        assert ref.source.execution.job_id != third.binding.job_id
    finally:
        third.client.close()


@pytest.mark.parametrize("mutation",[
    "unknown-envelope","unknown-data","unknown-nested","boolean-int","boolean-latency","nonfinite",
    "wrong-role","wrong-kind","not-object","empty-new","duplicate-id","unknown-trigger","automatic","custom","extra-compact",
])
def test_raw_shape_and_unattributable_compaction_fail_before_any_upload(checkpoint_case,mutation):
    c = checkpoint_case
    puts = len(c.h.artifacts.store._versions)
    def change(events):
        call,snapshot = events[4],events[5]
        if mutation=="unknown-envelope":call["unknown"]="not-allowed"
        elif mutation=="unknown-data":call["data"]["unknown"]=1
        elif mutation=="unknown-nested":call["data"]["modelCall"]["unknown"]=1
        elif mutation=="boolean-int":call["data"]["turn"]=True
        elif mutation=="boolean-latency":call["data"]["ttftMs"]=True
        elif mutation=="nonfinite":call["data"]["ttftMs"]=float("inf")
        elif mutation=="wrong-role":snapshot["data"]["messages"][0]["role"]="developer"
        elif mutation=="wrong-kind":snapshot["data"]["kind"]="different"
        elif mutation=="not-object":snapshot["data"]=[]
        elif mutation=="empty-new":snapshot["data"]={}
        elif mutation=="duplicate-id":snapshot["id"]=call["id"]
        elif mutation=="unknown-trigger":events[3]["data"].pop("trigger")
        elif mutation=="automatic":events[3]["data"]["trigger"]="background"
        elif mutation=="custom":events[-1]["data"]["customInstructions"]="Private custom focus"
        else:events.append(event("session.compaction_start","extra",{"trigger":"manual"}))
    mutate_events(c.fs,change)
    before = copy.deepcopy(c.fs.files)
    with pytest.raises(Problem):
        c.fs.snapshot()
    assert c.fs.files == before and len(c.h.artifacts.store._versions)==puts
    assert c.h.store.read_snapshot((checkpoint_key(c.binding.job_id),))[checkpoint_key(c.binding.job_id)] is None


@pytest.mark.parametrize("location",["envelope","sidecar","summary","filename","directory","json-structure","yaml-structure","escaped-copy"])
def test_known_private_values_and_structures_never_publish(checkpoint_case,location):
    c = checkpoint_case
    marker = "PRIVATE_FIXTURE_SYSTEM"
    if location=="envelope":
        mutate_events(c.fs,lambda events:events[4].update(parentId=marker))
    elif location=="summary":
        mutate_events(c.fs,lambda events:events[-1]["data"].update(summaryContent=SUMMARY+marker))
    elif location=="filename":c.fs.files["temp/"+marker]="public"
    elif location=="directory":c.fs.directories.add("temp/"+marker)
    elif location=="json-structure":c.fs.files["temp/copy.json"]=json.dumps({"nested":{"systemPrompt":"unapproved"}})
    elif location=="yaml-structure":c.fs.files["temp/copy.yaml"]="system_prompt: unapproved"
    elif location=="escaped-copy":
        marker='PRIVATE_"秘密"'
        c.fs.forbid_value(marker)
        c.fs.files["temp/escaped.json"]=json.dumps({"note":json.dumps({"note":marker},ensure_ascii=True)})
    else:c.fs.files["temp/copy.txt"]=marker
    with pytest.raises(Problem) as error:
        c.fs.snapshot()
    assert error.value.body["error"]["code"]=="SDK_STATE_PRIVATE_MATERIAL"


@pytest.mark.parametrize("mutation",["checkpoint","index","index-path","index-number","index-extra","empty","workspace","traversal","conflict"])
def test_required_native_files_and_exact_index_correlation(checkpoint_case,mutation):
    fs = checkpoint_case.fs
    if mutation=="checkpoint":del fs.files["checkpoints/001-inventory.md"]
    elif mutation=="index":del fs.files["checkpoints/index.md"]
    elif mutation=="index-path":fs.files["checkpoints/index.md"]=INDEX.replace("001-inventory.md","other-001-inventory.md")
    elif mutation=="index-number":fs.files["checkpoints/index.md"]=INDEX.replace("| 1 |","| 2 |")
    elif mutation=="index-extra":fs.files["checkpoints/index.md"]+="| 2 | Wrong | 002-wrong.md |\n"
    elif mutation=="empty":fs.files["checkpoints/001-inventory.md"]=""
    elif mutation=="workspace":fs.files["workspace.yaml"]=""
    elif mutation=="traversal":fs.files["../outside"]="public"
    else:fs.files["checkpoints"]="file-directory-conflict"
    with pytest.raises(Problem):
        fs.snapshot()


@pytest.mark.parametrize("mutation",["job","profile","pid","base","event","model-request","missing"])
def test_api_rejects_unbound_or_missing_execution_observation(checkpoint_case,mutation):
    c = checkpoint_case
    state = c.fs.snapshot()
    execution = c.fs.compaction
    updates = {"job":{"job_id":"other"},"profile":{"profile_digest":"f"*64},"pid":{"runtime_pid":100},
               "base":{"base_state_sha256":"f"*64},"event":{"complete_event_id":"other"},
               "model-request":{"model_request_sha256":"f"*64}}
    supplied = None if mutation=="missing" else execution.model_copy(update=updates[mutation])
    before = len(c.h.artifacts.store._versions)
    with pytest.raises(Problem) as error:
        c.client.save_sdk_state(state,c.stop,compaction=supplied)
    assert error.value.body["error"]["code"]=="SDK_STATE_SOURCE_UNVERIFIED"
    assert len(c.h.artifacts.store._versions)==before


@pytest.mark.parametrize("mutation",["id","data","envelope","order","extra-empty"])
def test_api_rejects_forged_inherited_history(checkpoint_case,mutation):
    c = checkpoint_case
    state = c.fs.snapshot()
    settle(c,state,compaction=c.fs.compaction)
    third = open_turn(c.h,c.control,c.human,"forged-followup",c.assignment.task.instruction.conversation_id)
    try:
        restored = third.client.restore()
        changed = copy.deepcopy(restored.model_dump())
        events = events_from(changed["files"])
        if mutation=="id":
            events[4]["id"]="forged"
            changed["projected_event_ids"]=("forged","diag-snapshot")
        elif mutation=="data":events[4]["data"]={"claim":"trusted"}
        elif mutation=="envelope":events[4]["parentId"]="forged"
        elif mutation=="order":
            events[4],events[5]=events[5],events[4]
            changed["projected_event_ids"]=("diag-snapshot","diag-call")
        else:
            events.insert(5,event("model.messages_snapshot","new-empty",{},"diag-call"))
            changed["projected_event_ids"]=("diag-call","new-empty","diag-snapshot")
        changed["files"]["events.jsonl"]="".join(json.dumps(e)+"\n" for e in events)
        # Bypass local DTO validation to exercise the actual untrusted HTTP entry.
        body={"request_id":"sdk-state","state":changed,"sdk_stop":third.stop.model_dump(mode="json")}
        url=f"/behavior/v1/bindings/{third.binding.binding_id}/assistant/sessions/{third.assignment.session_id}/sdk-state"
        before=len(c.h.artifacts.store._versions)
        response=c.h.client.post(url,json=body,headers={**c.h.headers("agent"),
            "x-behavior-assistant-ticket":third.client.ticket.get_secret_value()})
        assert response.status_code in {409,422}
        assert len(c.h.artifacts.store._versions)==before
    finally:
        third.client.close()


@pytest.mark.parametrize("failure",["put","partial-put","readback","prepare-unknown","publish-unknown"])
def test_unknown_product_upload_never_retries_or_changes_prior_reply(checkpoint_case,monkeypatch,failure):
    c=checkpoint_case
    if failure=="partial-put":
        c.fs.files["temp/public-part-data.txt"]="public state\n"*50000
    state=c.fs.snapshot()
    before_reply=c.h.client.get(
        f"/work-policy/v1/conversations/{c.assignment.task.instruction.conversation_id}/turns/1/reply",
        headers=c.h.headers()).content
    calls=[]
    put=c.h.artifacts.store.put
    read=c.h.artifacts.store.read
    transact=c.h.store.transact
    def put_once(*args):
        calls.append(args[2])
        if failure=="partial-put" and len(calls)==2:
            raise Problem(503,"ARTIFACT_UNKNOWN","offline second part was not acknowledged")
        result=put(*args)
        if failure=="put":raise Problem(503,"ARTIFACT_UNKNOWN","offline lost PUT acknowledgement")
        return result
    def readback(*args):
        data=read(*args)
        return b"wrong" if failure=="readback" and calls else data
    from shared_context.behavior_assistant import key
    def transaction(request,decide,**kwargs):
        target=key("sdk-state-"+("prepare" if failure=="prepare-unknown" else "complete"),c.assignment.session_id)
        if failure in {"prepare-unknown","publish-unknown"} and request.request_id==target:
            if failure=="prepare-unknown":transact(request,decide,**kwargs)
            raise Problem(503,"BEHAVIOR_COMMIT_UNKNOWN","offline unknown publication")
        return transact(request,decide,**kwargs)
    monkeypatch.setattr(c.h.artifacts.store,"put",put_once)
    monkeypatch.setattr(c.h.artifacts.store,"read",readback)
    monkeypatch.setattr(c.h.store,"transact",transaction)
    with pytest.raises(Problem):c.client.save_sdk_state(state,c.stop,compaction=c.fs.compaction)
    count=len(calls)
    assert count == (0 if failure=="prepare-unknown" else 2 if failure=="partial-put" else 1)
    # A caller retry can inspect the original intent, not repeat any PUT.
    with pytest.raises(Problem):c.client.save_sdk_state(state,c.stop,compaction=c.fs.compaction)
    assert len(calls)==count
    row=c.h.store.read_snapshot((checkpoint_key(c.binding.job_id),))[checkpoint_key(c.binding.job_id)]
    assert row is not None and SdkStateUpload.model_validate_json(json.dumps(row.payload["data"])).reference is None
    after=c.h.client.get(
        f"/work-policy/v1/conversations/{c.assignment.task.instruction.conversation_id}/turns/1/reply",
        headers=c.h.headers()).content
    assert after==before_reply


@pytest.mark.parametrize("value",[b"binary","\ud800"])
def test_failed_utf8_or_binary_callback_cannot_seal_a_partial_checkpoint(value):
    fs=SdkSessionFiles("callback",ordinary("callback"),checkpoint_profile=checkpoint_profile())
    with pytest.raises(ValueError):
        asyncio.run(fs.write_file("/session-state/temp/invalid",value))
    assert fs.failed
    with pytest.raises(Problem):fs.snapshot()


def test_actual_unicode_http_envelope_and_sixteen_parts_cover_near_eight_mib(checkpoint_case):
    from shared_context.backup_models import MAX_BACKUP_BYTES
    c=checkpoint_case
    c.fs.files["temp/public-unicode.txt"]="界"*100
    state=c.fs.snapshot()
    files=dict(state.files)
    empty=state.model_copy(update={"files":{**files,"temp/public-unicode.txt":""}})
    available=MAX_BACKUP_BYTES-len(empty.encoded())
    files["temp/public-unicode.txt"]="界"*(available//3)+"x"*(available%3)
    large=SdkState.model_validate_json(empty.model_copy(update={"files":files}).model_dump_json())
    assert len(large.encoded())==MAX_BACKUP_BYTES
    packet=SaveSdkState(request_id="sdk-state",state=large,sdk_stop=c.stop,compaction=c.fs.compaction)
    assert len(packet.model_dump_json().encode("utf-8"))<=SDK_STATE_HTTP_BYTES
    url=f"/behavior/v1/bindings/{c.binding.binding_id}/assistant/sessions/{c.assignment.session_id}/sdk-state"
    before=len(c.h.artifacts.store._versions)
    expanded=json.dumps(packet.model_dump(mode="json"),ensure_ascii=True).encode()
    assert len(expanded)>SDK_STATE_HTTP_BYTES
    rejected=c.h.client.post(url,content=expanded,headers={**c.h.headers("agent"),
        "x-behavior-assistant-ticket":c.client.ticket.get_secret_value()})
    assert rejected.status_code==413 and len(c.h.artifacts.store._versions)==before
    reference=c.client.save_sdk_state(large,c.stop,compaction=c.fs.compaction)
    assert len(reference.parts)==16 and all(part.receipt.bytes<=512*1024 for part in reference.parts)
    assert load_state(c.h.assistant,c.session,reference,job_id=c.binding.job_id)==large
    c.client.save_text_reply("Large legal state saved.",reference,c.stop)
    c.h.executor._complete(c.start,{"state":"ready_to_finalize"})
    assert c.h.runtime.controller.step(c.binding.binding_id).state=="succeeded"
    following=open_turn(c.h,c.control,c.human,"large-restore",c.assignment.task.instruction.conversation_id)
    try:
        assert following.client.restore()==large
    finally:
        following.client.close()
    with pytest.raises(ValueError):
        SdkState.model_validate_json(large.model_copy(update={"files":{**files,"temp/extra":"x"}}).model_dump_json())


@pytest.mark.parametrize("profile_enabled",[False,True])
@pytest.mark.parametrize("kind",["background","missing","manual-outside","multiple"])
def test_unexpected_live_compaction_is_fenced_before_model_authorization(kind,profile_enabled):
    import httpx
    from shared_context.m2_copilot import _ModelGateway
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    fs=SdkSessionFiles("fenced",ordinary("fenced"),
                       checkpoint_profile=checkpoint_profile() if profile_enabled else None)
    data={} if kind=="missing" else {"trigger":"background" if kind=="background" else "manual"}
    if kind=="multiple":
        fs._compacting=True
        for identity,event_type in (("first","session.compaction_start"),("second","session.compaction_complete")):
            fs.observe_compaction(event(event_type,identity,{"trigger":"manual"}))
    fs.observe_compaction(event("session.compaction_start","unexpected",data))
    called=[]
    async def reserve(_):called.append("reserved")
    def external(_):called.append("sent");raise AssertionError("no external effect may start")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(external)) as http:
            endpoint=httpx.URL("https://offline.invalid/model/v1/chat/completions")
            gateway=_ModelGateway(endpoint,http,lambda _:None,
                boundary=AssistantModelBoundary(reserve,sender=http.send,text_reply=True),instruction=True,session_files=fs)
            with pytest.raises(RuntimeError,match="unapproved compaction"):
                await gateway.send_request(httpx.Request("POST",endpoint,json={"model":"gpt-4.1-mini",
                    "stream":True,"messages":[{"role":"user","content":"Synthetic summary input."}]}),
                    SimpleNamespace(request_id="only-request",session_id="fenced"))
    asyncio.run(run())
    assert called==[] and fs.checkpoint_error=="SDK_STATE_SOURCE_UNVERIFIED"


def test_unconfigured_checkpoint_still_observes_ordinary_events_without_a_false_fence():
    fs=SdkSessionFiles("ordinary",ordinary("ordinary"))
    for kind in ("session.start","user.message","assistant.message","tool.execution_complete","session.idle"):
        fs.observe_compaction(event(kind,"ordinary-event",{}))
    assert fs.checkpoint_error is None
    assert fs.snapshot().codec=="session-fs-v1"


def test_windows_sdk_paths_remain_scoped_and_persist_only_unambiguous_relative_paths():
    fs=SdkSessionFiles("windows",ordinary("windows"),checkpoint_profile=checkpoint_profile())
    asyncio.run(fs.write_file("\\session-state\\temp\\allowed","public"))
    assert fs.files["temp/allowed"]=="public"
    with pytest.raises(PermissionError):
        asyncio.run(fs.write_file("/session-state\\temp\\ambiguous","public"))
    assert fs.failed


@pytest.mark.parametrize("failure",["missing-stop","callback-failed","late-write"])
def test_hosted_refuses_to_upload_unsealed_or_failed_state(checkpoint_case,failure,tmp_path):
    from shared_context.behavior_assistant_hosted import AssistantSession
    c=checkpoint_case
    bridge=AssistantSession(c.client,root=tmp_path,runtime_path=tmp_path/"not-started")
    bridge._session_files=c.fs
    bridge._sdk_stop=None if failure=="missing-stop" else c.stop
    if failure=="callback-failed":c.fs.failed=True
    elif failure=="late-write":
        c.fs.snapshot()
        with pytest.raises(PermissionError):
            asyncio.run(c.fs.append_file("/session-state/events.jsonl","\n"))
    before=len(c.h.artifacts.store._versions)
    result=bridge._archive()
    assert result.error=="SDK_STATE_INCOMPLETE" and len(c.h.artifacts.store._versions)==before


@pytest.mark.parametrize("failure",["disconnect","stop"])
def test_unresolved_normal_shutdown_cannot_create_an_upload_intent(assistant,monkeypatch,tmp_path,failure):
    from shared_context import m2_copilot
    from shared_context.behavior_assistant_hosted import AssistantSession
    from test_instruction_flow import sdk_io
    h,control,human=configured(assistant)
    turn=open_turn(h,control,human,"failed-native-stop")
    sdk_io(monkeypatch)
    factory=m2_copilot.CopilotClient.side_effect
    calls=[]
    def broken(**options):
        sdk=factory(**options)
        exit_process=sdk.stop.side_effect
        async def force():
            calls.append("force")
            exit_process()
        sdk.force_stop.side_effect=force
        if failure=="stop":
            async def stop():
                calls.append("stop")
                exit_process()
                raise RuntimeError("OFFLINE stop failure")
            sdk.stop.side_effect=stop
        else:
            original=sdk.create_session.side_effect
            async def create(*args,**kwargs):
                session=await original(*args,**kwargs)
                async def disconnect():
                    calls.append("disconnect")
                    raise RuntimeError("OFFLINE flush failure")
                session.disconnect.side_effect=disconnect
                return session
            sdk.create_session.side_effect=create
        return sdk
    m2_copilot.CopilotClient.side_effect=broken
    h.endpoint.unbounded=True
    h.endpoint.replies=iter([{"content":"Public response, not a checkpoint success."}])
    bridge=AssistantSession(turn.client,root=tmp_path/"failed-shutdown",runtime_path=Path(__file__),
                            model_transport=h.endpoint.transport)
    try:
        # Acquire was already performed through the real API by open_turn.
        bridge._session_files=SdkSessionFiles(turn.assignment.sdk_session_id,checkpoint_profile=checkpoint_profile())
        work=tmp_path/"work"
        work.mkdir()
        with pytest.raises(Problem):
            asyncio.run(bridge._run(work))
        assert bridge._sdk_stop is None and calls==[failure,"force"]
        assert h.store.read_snapshot((checkpoint_key(turn.binding.job_id),))[checkpoint_key(turn.binding.job_id)] is None
    finally:
        turn.client.close()


def test_ordinary_and_experimental_formats_cannot_import_projected_diagnostics(checkpoint_case):
    c=checkpoint_case
    state=c.fs.snapshot()
    for codec in ("session-fs-v1","ctx03-candidate-b-v1"):
        payload={**state.model_dump(mode="json"),"codec":codec}
        if codec=="session-fs-v1":
            payload.pop("projected_event_ids")
        with pytest.raises((Problem,ValueError)):
            SdkState.model_validate_json(json.dumps(payload))


def test_model_compaction_marker_cannot_enable_an_unconfigured_job(assistant):
    h,control,human,_=enabled(assistant)
    install_work_policy_routes(h.app,SimpleNamespace(runtime=h.runtime,verifier=h.verifier,
        installation=lambda:None,require_current_native_evidence=lambda:None))
    turn=open_turn(h,control,human,"unconfigured-model-purpose")
    body={"model":"gpt-4.1-mini","messages":[{"role":"user","content":"Not an authorization"}],"stream":True}
    principal=h.verifier(h.client.build_request("GET","/",headers=h.headers("agent")))
    try:
        with pytest.raises(Problem) as error:
            h.assistant.consume_model(turn.session,ModelRequest(request_id=digest(body),
                body=body,checkpoint_compaction=True),principal)
        assert error.value.body["error"]["code"]=="SDK_STATE_SOURCE_UNVERIFIED"
    finally:
        turn.client.close()


PUBLIC_CHECKPOINT_TEXT = (
    '[{"role":"system","content":"You are a pirate."}]',
    '{"systemPrompt":"Answer briefly."}',
    "systemPrompt: Answer briefly.",
    "[" * 2000 + "]" * 2000,
    "before\u2028after",
    "before\u2029after",
    "before\u0085after",
)


def public_checkpoint_content(case, payload, location):
    if location == "summary":
        summary = SUMMARY.replace("Original public source: inventory count 850.", payload)
        mutate_events(case.fs, lambda events: events[-1]["data"].update(summaryContent=summary))
        case.fs.files["checkpoints/001-inventory.md"] = summary.replace(
            "<checkpoint_title>Inventory</checkpoint_title>", "")
        case.fs.compaction = case.fs.compaction.model_copy(
            update={"summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest()})
    else:
        case.fs.files["events.jsonl"] += json.dumps(event(
            location, "public-content", {"content": payload}, "compact-complete"), ensure_ascii=False) + "\n"


def assert_public_checkpoint_roundtrip(checkpoint_case, tmp_path, payload, location):
    from shared_context.behavior_assistant_hosted import AssistantSession
    from shared_context.behavior_sdk_state import StoredSdkArchive
    c = checkpoint_case
    public_checkpoint_content(c, payload, location)
    original = dict(c.fs.files)
    bridge = AssistantSession(c.client, root=tmp_path, runtime_path=tmp_path / "not-started")
    bridge._session_files, bridge._sdk_stop = c.fs, c.stop
    reference = bridge._archive()
    assert isinstance(reference, StoredSdkArchive), "legal public text made the checkpoint unavailable"
    assert c.fs.files == original
    state = c.fs.snapshot()
    saved = c.client.save_text_reply(payload, reference, c.stop)
    c.h.executor._complete(c.start, saved.model_dump(mode="json"))
    assert c.h.runtime.controller.step(c.binding.binding_id).state == "succeeded"
    cid = c.assignment.task.instruction.conversation_id
    reply = c.h.client.get(f"/work-policy/v1/conversations/{cid}/turns/2/reply", headers=c.h.headers())
    assert reply.status_code == 200 and reply.json()["text"] == payload
    following = open_turn(c.h, c.control, c.human, "public-content-followup", cid)
    try:
        recovered = following.client.restore()
        assert recovered == state
        values = events_from(recovered.files)
        assert any(payload in value["data"].get("summaryContent" if location == "summary" else "content", "")
                   for value in values)
        next_fs = SdkSessionFiles(recovered.sdk_session_id, recovered, checkpoint_profile=checkpoint_profile())
        next_fs.files["events.jsonl"] += json.dumps(event(
            "user.message", "next-public-content", {"content": "Continue normally."}, values[-1]["id"])) + "\n"
        settle(following, next_fs.snapshot())
        assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous
    finally:
        following.client.close()


@pytest.mark.parametrize("payload", PUBLIC_CHECKPOINT_TEXT,
                         ids=["role-json", "private-key-json", "yaml-looking", "deep-public-json", "u2028", "u2029", "u0085"])
@pytest.mark.parametrize("location", ["user.message", "assistant.message", "tool.execution_complete", "summary"])
def test_public_text_survives_product_archive_final_answer_and_restore(checkpoint_case, tmp_path, payload, location):
    assert_public_checkpoint_roundtrip(checkpoint_case, tmp_path, payload, location)


@pytest.mark.parametrize("location", ["assistant.message", "summary"])
def test_long_malformed_escape_content_preserves_answer_and_checkpoints(checkpoint_case, tmp_path, location):
    payload = ('\\"' * 32768) + "\n"
    assert len(payload.encode("utf-8")) == 65537
    assert_public_checkpoint_roundtrip(checkpoint_case, tmp_path, payload, location)


def test_long_malformed_prefix_does_not_hide_private_copy_or_discard_public_answer(checkpoint_case, tmp_path):
    from shared_context.behavior_assistant_hosted import AssistantSession
    c = checkpoint_case
    marker = 'PRIVATE_SCAN_"秘密"'
    c.fs.forbid_value(marker)
    encoded = '"' + "".join(f"\\u{ord(char):04x}" for char in marker) + '"'
    for _ in range(4):
        encoded = json.dumps(encoded)
    c.fs.files["temp/opaque.txt"] = ('\\"' * 32768) + "\n" + encoded
    before, puts = dict(c.fs.files), len(c.h.artifacts.store._versions)
    bridge = AssistantSession(c.client, root=tmp_path, runtime_path=tmp_path / "not-started")
    bridge._session_files, bridge._sdk_stop = c.fs, c.stop
    archive = bridge._archive()
    assert archive.error == "SDK_STATE_PRIVATE_MATERIAL"
    assert c.fs.files == before and len(c.h.artifacts.store._versions) == puts
    result = c.client.save_text_reply("Keep the completed public answer.", archive, c.stop)
    c.h.executor._complete(c.start, result.model_dump(mode="json"))
    assert c.h.runtime.controller.step(c.binding.binding_id).state == "succeeded"
    cid = c.assignment.task.instruction.conversation_id
    reply = c.h.client.get(f"/work-policy/v1/conversations/{cid}/turns/2/reply", headers=c.h.headers())
    assert reply.status_code == 200 and reply.json()["text"] == "Keep the completed public answer."
    assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous
    state = c.h.client.get(f"/work-policy/v1/conversations/{cid}", headers=c.h.headers()).json()
    assert not state["can_follow_up"] and state["follow_up_blocker"] == "SDK_STATE_PRIVATE_MATERIAL"


@pytest.mark.parametrize("location", ["event", "request-messages", "json-sidecar"])
def test_deep_sdk_structure_keeps_final_answer_and_last_lawful_checkpoint(checkpoint_case, tmp_path, location):
    from shared_context.behavior_assistant_hosted import AssistantSession
    c = checkpoint_case
    nested = "[" * 2000 + "0" + "]" * 2000
    if location == "event":
        template = event("session.metadata", "deep-object", {"structure": "NESTED_VALUE"}, "compact-complete")
        c.fs.files["events.jsonl"] += json.dumps(template).replace('"NESTED_VALUE"', nested) + "\n"
    elif location == "request-messages":
        mutate_events(c.fs, lambda events: events[4]["data"].update(requestMessages=nested))
    else:
        c.fs.files["temp/structured.json"] = nested
    before = dict(c.fs.files)
    puts = len(c.h.artifacts.store._versions)
    bridge = AssistantSession(c.client, root=tmp_path, runtime_path=tmp_path / "not-started")
    bridge._session_files, bridge._sdk_stop = c.fs, c.stop
    archive = bridge._archive()
    assert archive.error == "SDK_STATE_UNSUPPORTED_FORMAT"
    assert c.fs.files == before and len(c.h.artifacts.store._versions) == puts
    result = c.client.save_text_reply("Preserve this completed public answer.", archive, c.stop)
    c.h.executor._complete(c.start, result.model_dump(mode="json"))
    assert c.h.runtime.controller.step(c.binding.binding_id).state == "succeeded"
    cid = c.assignment.task.instruction.conversation_id
    reply = c.h.client.get(f"/work-policy/v1/conversations/{cid}/turns/2/reply", headers=c.h.headers())
    assert reply.status_code == 200 and reply.json()["text"] == "Preserve this completed public answer."
    assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous
    state = c.h.client.get(f"/work-policy/v1/conversations/{cid}", headers=c.h.headers()).json()
    assert not state["can_follow_up"] and state["follow_up_blocker"] == "SDK_STATE_UNSUPPORTED_FORMAT"


def test_disabled_consumer_profile_blocks_status_admission_and_restore_without_rewriting_history(
        checkpoint_case, monkeypatch):
    from shared_context.behavior_sdk_state import StoredSdkArchive
    c = checkpoint_case
    reference = settle(c, c.fs.snapshot(), compaction=c.fs.compaction)
    cid = c.assignment.task.instruction.conversation_id
    url = f"/work-policy/v1/conversations/{cid}"
    before = c.h.client.get(url + "/turns/2/reply", headers=c.h.headers()).content
    assert c.h.client.get(url, headers=c.h.headers()).json()["can_follow_up"]
    require_profile = StoredSdkArchive.require_profile
    # Consumer rollback fault injection, not a rewrite of the immutable standing policy.
    with monkeypatch.context() as patch:
        patch.setattr(StoredSdkArchive, "require_profile", lambda ref, expected, **options: require_profile(ref, None, **options))
        state = c.h.client.get(url, headers=c.h.headers()).json()
        assert not state["can_follow_up"] and state["follow_up_blocker"] == "SDK_STATE_UNSUPPORTED_FORMAT"
        response = c.h.client.post("/work-policy/v1/submit",
            content=InstructionSubmit(request_id="disabled-followup", content="Continue.", conversation_id=cid).model_dump_json(),
            headers=c.h.headers())
        assert response.status_code == 409 and response.json()["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    following = open_turn(c.h, c.control, c.human, "restore-after-disable", cid)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(StoredSdkArchive, "require_profile", lambda ref, expected, **options: require_profile(ref, None, **options))
            with pytest.raises(Problem) as error:
                following.client.restore()
            assert error.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
        assert following.client.restore().sha256 == reference.sha256
        assert c.h.client.get(url + "/turns/2/reply", headers=c.h.headers()).content == before
    finally:
        following.client.close()


@pytest.mark.parametrize("location", ["sdk-object", "json-sidecar", "yaml-sidecar", "opaque-copy", "escaped-copy"])
def test_public_string_fix_preserves_actual_private_rejections(checkpoint_case, location):
    c = checkpoint_case
    if location == "sdk-object":
        mutate_events(c.fs, lambda events: events.append(event("session.metadata", "private-structure",
            {"role": "system", "content": "Unapproved native system structure"}, "compact-complete")))
    elif location == "json-sidecar":
        c.fs.files["temp/native.json"] = json.dumps({"role": "system", "content": "Unapproved native system structure"})
    elif location == "yaml-sidecar":
        c.fs.files["temp/native.yaml"] = "metadata:\n  systemPrompt: Unapproved native system structure\n"
    else:
        c.fs.forbid_value('PRIVATE_P2_"秘密"')
        content = 'PRIVATE_P2_"秘密"'
        if location == "escaped-copy":
            for _ in range(4):
                content = json.dumps(content, ensure_ascii=True)
        c.fs.files["temp/opaque.txt"] = content
    before = dict(c.fs.files)
    with pytest.raises(Problem) as error:
        c.fs.snapshot()
    assert error.value.body["error"]["code"] == "SDK_STATE_PRIVATE_MATERIAL"
    assert c.fs.files == before


def test_public_yaml_block_scalar_is_not_a_private_sidecar_field(checkpoint_case):
    c = checkpoint_case
    c.fs.files["temp/metadata.yaml"] = "description: |\n  systemPrompt: Answer briefly.\n"
    state = c.fs.snapshot()
    assert state.files["temp/metadata.yaml"] == c.fs.files["temp/metadata.yaml"]


@pytest.mark.parametrize("tokens_removed", [-11, 0, 35], ids=["negative", "zero", "positive"])
def test_signed_compaction_token_delta_survives_publication_and_inherited_restore(checkpoint_case, tokens_removed):
    c = checkpoint_case
    mutate_events(c.fs, lambda events: events[-1]["data"].update(tokensRemoved=tokens_removed))
    execution = c.fs.compaction.model_dump(mode="json")
    execution["tokens_removed"] = tokens_removed
    c.fs.compaction = CompactionExecution.model_validate_json(json.dumps(execution))
    raw = dict(c.fs.files)
    state = c.fs.snapshot()
    assert c.fs.files == raw
    reference = settle(c, state, compaction=c.fs.compaction)
    record = SdkStateUpload.model_validate_json(json.dumps(
        c.h.store.read_snapshot((checkpoint_key(c.binding.job_id),))[checkpoint_key(c.binding.job_id)].payload["data"]))
    assert record.reference == reference
    assert record.source.execution.tokens_removed == reference.source.execution.tokens_removed == tokens_removed
    assert record.prepare.request_digest == digest(record.model_dump(
        mode="json", exclude={"kind", "prepare", "reference"}))
    assert load_state(c.h.assistant, c.session, reference, job_id=c.binding.job_id) == state
    following = open_turn(c.h, c.control, c.human, "signed-token-followup",
                          c.assignment.task.instruction.conversation_id)
    try:
        recovered = following.client.restore()
        assert recovered == state
        complete = next(e for e in events_from(recovered.files) if e["type"] == "session.compaction_complete")
        assert type(complete["data"]["tokensRemoved"]) is int
        assert complete["data"]["tokensRemoved"] == tokens_removed
        fs = SdkSessionFiles(recovered.sdk_session_id, recovered, checkpoint_profile=checkpoint_profile())
        fs.files["events.jsonl"] += json.dumps(event(
            "user.message", "signed-followup", {"content": "Keep the original metadata."}, complete["id"])) + "\n"
        inherited = settle(following, fs.snapshot())
        assert fs.compaction is None
        assert inherited.source.execution == reference.source.execution
        assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous
    finally:
        following.client.close()


def test_native_pair_accepts_the_signed_token_delta_without_changing_messages_rule():
    from shared_context.behavior_sdk_compaction import compaction_pair
    values = events_from(raw_files(ordinary("signed-pair")))
    values[-1]["data"].update(tokensRemoved=-11, messagesRemoved=3)
    _, complete = compaction_pair(values)
    assert complete["data"]["tokensRemoved"] == -11 and complete["data"]["messagesRemoved"] == 3


@pytest.mark.parametrize("variant", ["fingerprint", "prompt-details", "completion-details", "all", "nulls",
                                    "service-tier", "filters", "numeric-extension", "project-front-door"])
def test_documented_provider_metadata_survives_projection_and_restore(checkpoint_case, variant):
    c = checkpoint_case
    def add_metadata(values):
        data = values[4]["data"]
        if variant in {"fingerprint", "all", "nulls"}:
            data["responseChunk"]["system_fingerprint"] = None if variant == "nulls" else "fp_offline"
        if variant in {"prompt-details", "all", "nulls"}:
            data["responseUsage"]["prompt_tokens_details"] = (
                None if variant == "nulls" else {"cached_tokens": 0, "audio_tokens": 0})
        if variant in {"completion-details", "all", "nulls"}:
            data["responseUsage"]["completion_tokens_details"] = (
                None if variant == "nulls" else {"reasoning_tokens": 0, "audio_tokens": 0,
                                                "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0})
        if variant in {"service-tier", "all", "nulls"}:
            data["responseChunk"]["service_tier"] = None if variant == "nulls" else "default"
        if variant in {"filters", "all", "nulls"}:
            data["responseChunk"]["prompt_filter_results"] = None if variant == "nulls" else [{
                "prompt_index": 0, "content_filter_results": {"hate": {"filtered": False, "severity": "safe"}}}]
        if variant == "numeric-extension":
            data["responseUsage"].update({"additional_count": 0, "measured": True, "estimate": 0.5, "unreported": None,
                "prompt_tokens_details": {"future_counter": 1, "measured": False, "unreported": None},
                "completion_tokens_details": {"reasoning_tokens": 13, "future_counter": 2}})
        if variant == "project-front-door":
            data["responseChunk"].update({
                "latency_checkpoint": {key: 1 for key in (
                    "engine_tbt_ms", "engine_ttft_ms", "engine_ttlt_ms", "pre_inference_ms",
                    "service_tbt_ms", "service_ttft_ms", "service_ttlt_ms", "user_visible_ttft_ms")},
                "routing": {"serving_pipereplica": "synthetic-replica"},
                "obfuscation": "synthetic-padding",
            })
    mutate_events(c.fs, add_metadata)
    original = copy.deepcopy(c.fs.files)
    state = c.fs.snapshot()
    assert c.fs.files == original
    assert events_from(state.files)[4]["data"] == {}
    reference = settle(c, state, compaction=c.fs.compaction)
    assert load_state(c.h.assistant, c.session, reference, job_id=c.binding.job_id) == state
    next_turn = open_turn(c.h, c.control, c.human, "provider-metadata-next",
                          c.assignment.task.instruction.conversation_id)
    try:
        assert next_turn.client.restore() == state
        assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous
    finally:
        next_turn.client.close()


@pytest.mark.parametrize("container,field,value", [
    ("responseChunk", "system_fingerprint", 7),
    ("responseChunk", "obfuscation", 7),
    ("responseChunk", "routing", []),
    ("responseChunk", "latency_checkpoint", "not-an-object"),
    ("responseChunk", "content", "not-metadata"),
    ("responseChunk", "future_metadata", {"messages": ["not-metadata"]}),
    ("responseUsage", "prompt_tokens_details", []),
    ("responseUsage", "prompt_tokens_details", {"cached_tokens": True}),
    ("responseUsage", "prompt_tokens_details", {"cached_tokens": 0.5}),
    ("responseUsage", "prompt_tokens_details", {"cached_tokens": -1}),
    ("responseUsage", "prompt_tokens_details", {"future_count": {"text": "not-metadata"}}),
    ("responseUsage", "completion_tokens_details", "not-an-object"),
    ("responseUsage", "completion_tokens_details", {"reasoning_tokens": False}),
    ("responseUsage", "completion_tokens_details", {"reasoning_tokens": 0.5}),
    ("responseUsage", "completion_tokens_details", {"future_field": {"arguments": "not-metadata"}}),
    ("responseUsage", "future_counter", {"tool_calls": []}),
    ("responseUsage", "future_counter", {"nested": {"systemPrompt": "not-metadata"}}),
    ("responseUsage", "future_counter", float("inf")),
    ("responseChunk", "service_tier", {"tier": "not a string"}),
    ("responseChunk", "prompt_filter_results", "not a list"),
    ("responseChunk", "prompt_filter_results", [{"prompt_index": True, "content_filter_results": {}}]),
    ("responseChunk", "prompt_filter_results", [{"prompt_index": 0, "content_filter_results": "not an object"}]),
    ("responseChunk", "prompt_filter_results", [{"prompt_index": 0, "content_filter_results": {}, "future": "unknown"}]),
    ("modelCall", "system_fingerprint", "not-a-provider-chunk"),
])
def test_provider_metadata_rejects_content_keys_and_invalid_known_types(checkpoint_case, container, field, value):
    c = checkpoint_case
    mutate_events(c.fs, lambda values: values[4]["data"][container].update({field: value}))
    before = len(c.h.artifacts.store._versions)
    with pytest.raises(Problem) as denied:
        c.fs.snapshot()
    assert denied.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    assert len(c.h.artifacts.store._versions) == before
    assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous


@pytest.mark.parametrize("container,field,value", [
    ("responseChunk", "future_metadata", "VALUE_MUST_NOT_BE_LOGGED"),
    ("responseChunk", "future_metadata", {"trace_hint": ["VALUE_MUST_NOT_BE_LOGGED"], "enabled": True}),
    ("responseUsage", "future_counter", {"measurement": 1.5, "availability": None}),
    ("responseUsage", "prompt_tokens_details", {"cached_tokens": 0, "future_count": "VALUE_MUST_NOT_BE_LOGGED"}),
])
def test_unknown_bounded_provider_metadata_projects_with_value_free_warning(checkpoint_case, caplog, container, field, value):
    c = checkpoint_case
    mutate_events(c.fs, lambda values: values[4]["data"][container].update({field: value}))
    original = copy.deepcopy(c.fs.files)
    with caplog.at_level(logging.WARNING):
        state = c.fs.snapshot()
    warnings = [record.getMessage() for record in caplog.records
                if record.getMessage().startswith("SDK_CHECKPOINT_PROVIDER_METADATA ")]
    assert len(warnings) == 1
    assert "model.model_call_success" in warnings[0] and "future_" in warnings[0]
    assert "VALUE_MUST_NOT_BE_LOGGED" not in warnings[0]
    assert "PRIVATE_FIXTURE_SYSTEM" not in warnings[0]
    assert c.fs.files == original and events_from(state.files)[4]["data"] == {}
    reference = settle(c, state, compaction=c.fs.compaction)
    assert load_state(c.h.assistant, c.session, reference, job_id=c.binding.job_id) == state


def test_shape_refusal_reports_only_key_names_and_types(checkpoint_case, caplog):
    c = checkpoint_case
    marker = "PRIVATE_VALUE_NEVER_LOGGED"
    def wrong_shape(values):
        data = values[4]["data"]
        del data["responseChunk"]["choices"]
        data["responseChunk"]["future_metadata"] = {"messages": [marker]}
    mutate_events(c.fs, wrong_shape)
    with caplog.at_level(logging.WARNING), pytest.raises(Problem) as denied:
        c.fs.snapshot()
    assert denied.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    warnings = [record.getMessage() for record in caplog.records
                if record.getMessage().startswith("SDK_CHECKPOINT_SHAPE_REJECTED ")]
    assert len(warnings) == 1
    assert "model.model_call_success" in warnings[0] and "choices" in warnings[0]
    assert "future_metadata" in warnings[0] and "dict" in warnings[0]
    assert marker not in warnings[0] and "PRIVATE_FIXTURE_SYSTEM" not in warnings[0]


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_provider_metadata_limit_counts_actual_utf8_bytes(extra_bytes):
    from shared_context.behavior_sdk_compaction import MAX_PROVIDER_METADATA_BYTES, raw_shape
    call = events_from(raw_files(ordinary("metadata-bytes")))[4]
    path = "responseChunk.future_metadata"
    overhead = len(json.dumps({path: ""}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    budget = MAX_PROVIDER_METADATA_BYTES - overhead + extra_bytes
    value = "\u754c" * (budget // 3) + "x" * (budget % 3)
    assert len(json.dumps({path: value}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) == (
        MAX_PROVIDER_METADATA_BYTES + extra_bytes)
    call["data"]["responseChunk"]["future_metadata"] = value
    if extra_bytes:
        with pytest.raises(Problem) as denied:
            raw_shape(call)
        assert denied.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    else:
        raw_shape(call)


@pytest.mark.parametrize("extra_depth", [0, 1])
def test_provider_metadata_structure_depth_is_bounded(extra_depth):
    from shared_context.behavior_sdk_compaction import MAX_PROVIDER_METADATA_DEPTH, raw_shape
    call = events_from(raw_files(ordinary("metadata-depth")))[4]
    value = 1
    for _ in range(MAX_PROVIDER_METADATA_DEPTH + extra_depth):
        value = {"measurement": value}
    call["data"]["responseUsage"]["future_metadata"] = value
    if extra_depth:
        with pytest.raises(Problem) as denied:
            raw_shape(call)
        assert denied.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    else:
        raw_shape(call)


def test_provider_metadata_byte_limit_covers_both_containers():
    from shared_context.behavior_sdk_compaction import MAX_PROVIDER_METADATA_BYTES, raw_shape
    call = events_from(raw_files(ordinary("metadata-total")))[4]
    for container in ("responseChunk", "responseUsage"):
        call["data"][container]["future_metadata"] = "x" * (MAX_PROVIDER_METADATA_BYTES // 2)
    with pytest.raises(Problem) as denied:
        raw_shape(call)
    assert denied.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"


@pytest.mark.parametrize("field", [
    "content", "text", "message", "messages", "tool_calls", "arguments",
    "requestMessages", "System-Prompt", " Reasoning_Text ",
])
def test_unknown_provider_metadata_cannot_nest_content_fields(field):
    from shared_context.behavior_sdk_compaction import raw_shape
    call = events_from(raw_files(ordinary("metadata-content")))[4]
    call["data"]["responseChunk"]["future_metadata"] = [{"details": {field: "PRIVATE_VALUE"}}]
    with pytest.raises(Problem) as denied:
        raw_shape(call)
    assert denied.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"


@pytest.mark.parametrize("container,field", [
    ("responseChunk", "choices"), ("responseUsage", "total_tokens"), ("modelCall", "request_id"),
])
def test_optional_provider_metadata_cannot_replace_required_fields(checkpoint_case, container, field):
    c = checkpoint_case
    def remove_required(values):
        data = values[4]["data"]
        del data[container][field]
        data["responseChunk"]["system_fingerprint"] = "fp_offline"
        data["responseUsage"]["prompt_tokens_details"] = {"cached_tokens": 0}
    mutate_events(c.fs, remove_required)
    with pytest.raises(Problem) as error:
        c.fs.snapshot()
    assert error.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"


@pytest.mark.parametrize("location", ["envelope", "sidecar", "summary"])
def test_provider_metadata_does_not_bypass_known_private_copy_checks(checkpoint_case, location):
    c = checkpoint_case
    marker = "PRIVATE_PROVIDER_COPY_CANARY"
    c.fs.forbid_value(marker)
    def change(values):
        values[4]["data"]["responseChunk"]["system_fingerprint"] = "fp_offline"
        values[4]["data"]["responseChunk"]["future_metadata"] = {"trace_hint": "synthetic"}
        values[4]["data"]["responseUsage"]["completion_tokens_details"] = {"reasoning_tokens": 13}
        if location == "envelope":
            values[4]["parentId"] = marker
        elif location == "summary":
            values[-1]["data"]["summaryContent"] += marker
    mutate_events(c.fs, change)
    if location == "sidecar":
        c.fs.files["temp/private-copy.txt"] = marker
    before = len(c.h.artifacts.store._versions)
    with pytest.raises(Problem) as error:
        c.fs.snapshot()
    assert error.value.body["error"]["code"] == "SDK_STATE_PRIVATE_MATERIAL"
    assert len(c.h.artifacts.store._versions) == before
    assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous


@pytest.mark.parametrize("field,value", [
    ("tokensRemoved", True), ("tokensRemoved", False), ("tokensRemoved", -11.0),
    ("tokensRemoved", None), ("tokensRemoved", "missing"),
    ("messagesRemoved", -1), ("messagesRemoved", True), ("messagesRemoved", 3.0),
], ids=["true", "false", "float", "null", "missing", "negative-messages", "bool-messages", "float-messages"])
def test_compaction_counts_reject_invalid_event_and_execution_types(checkpoint_case, field, value):
    from shared_context.behavior_sdk_compaction import compaction_pair
    c = checkpoint_case
    values = events_from(c.fs.files)
    metadata = c.fs.compaction.model_dump(mode="json")
    execution_field = "tokens_removed" if field == "tokensRemoved" else "messages_removed"
    if value == "missing":
        values[-1]["data"].pop(field)
        metadata.pop(execution_field)
    else:
        values[-1]["data"][field] = value
        metadata[execution_field] = value
    before = len(c.h.artifacts.store._versions)
    with pytest.raises(Problem) as error:
        compaction_pair(values)
    assert error.value.body["error"]["code"] == "SDK_STATE_SOURCE_UNVERIFIED"
    with pytest.raises(ValueError):
        CompactionExecution.model_validate_json(json.dumps(metadata))
    assert len(c.h.artifacts.store._versions) == before


def test_signed_token_delta_mismatch_is_refused_before_upload(checkpoint_case):
    c = checkpoint_case
    mutate_events(c.fs, lambda events: events[-1]["data"].update(tokensRemoved=-11))
    c.fs.compaction = c.fs.compaction.model_copy(update={"tokens_removed": -11})
    state = c.fs.snapshot()
    mismatched = CompactionExecution.model_validate_json(
        c.fs.compaction.model_copy(update={"tokens_removed": -10}).model_dump_json())
    before = len(c.h.artifacts.store._versions)
    with pytest.raises(Problem) as error:
        c.client.save_sdk_state(state, c.stop, compaction=mismatched)
    assert error.value.body["error"]["code"] == "SDK_STATE_SOURCE_UNVERIFIED"
    assert len(c.h.artifacts.store._versions) == before
    assert c.h.store.read_snapshot((checkpoint_key(c.binding.job_id),))[checkpoint_key(c.binding.job_id)] is None
    assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous


@pytest.mark.parametrize("rpc_tokens", [-11, -10], ids=["exact", "mismatched"])
def test_controlled_compact_requires_exact_signed_rpc_and_event_counts(checkpoint_case, rpc_tokens):
    from copilot.rpc import HistoryCompactResult
    c = checkpoint_case
    fs = SdkSessionFiles(c.previous.sdk_session_id, c.previous, checkpoint_profile=checkpoint_profile())
    handlers = []
    values = events_from(raw_files(c.previous))
    values[-1]["data"]["tokensRemoved"] = -11
    def subscribe(callback):
        handlers.append(callback)
        return lambda: handlers.remove(callback)
    async def compact(request):
        assert request.to_dict() == {"trigger": "manual"}
        fs.observe_model_request(c.compaction_request_sha256)
        for record in (values[3], values[-1]):
            packet = SimpleNamespace(type=SimpleNamespace(value=record["type"]), to_dict=lambda record=record: record)
            for callback in tuple(handlers):
                callback(packet)
        return HistoryCompactResult(success=True, messages_removed=2, tokens_removed=rpc_tokens, summary_content=SUMMARY)
    session = SimpleNamespace(session_id=fs.session_id, on=subscribe,
                              rpc=SimpleNamespace(history=SimpleNamespace(compact=compact)))
    if rpc_tokens == -11:
        asyncio.run(fs.compact(session, c.assignment.task, c.stop.pid))
        assert fs.compaction.tokens_removed == -11
    else:
        with pytest.raises(Problem) as error:
            asyncio.run(fs.compact(session, c.assignment.task, c.stop.pid))
        assert error.value.body["error"]["code"] == "SDK_STATE_SOURCE_UNVERIFIED"
        assert fs.compaction is None
    assert handlers == []
