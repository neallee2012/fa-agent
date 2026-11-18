"""CTX-03 product API/authority/store contracts; SDK events here are explicit unit fixtures."""
import asyncio
import copy
import hashlib
import json
import logging
import sys
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest

from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_assistant_hosted import AssistantClient
from shared_context.behavior_assistant_wire import InstructionSubmit, ModelRequest, SdkStop
from shared_context.behavior_sdk_checkpoint import checkpoint_key, load_state
from shared_context.behavior_sdk_compaction import NESTED_KEYS, events_from
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_sdk_state import CheckpointProfile, CompactionExecution, SdkState
from shared_context.behavior_wire import Command
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models
from test_m3_auth import signing_key, jwk_client
from work_policy_lifecycle_helpers import enabled
from sdk_files_case_helpers import files_case
from sdk_compaction_product_helpers import (
    settle,
    ordinary,
    event,
    configured,
    checkpoint_profile,
    SUMMARY,
    raw_files,
    INDEX,
    mutate_events,
)


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


def test_server_profile_is_default_off_and_upload_or_text_cannot_select_it(assistant_api):
    from shared_context.behavior_assistant_wire import InstructionTask
    assert all(p.checkpoint_profile is None and "checkpoint_profile" not in p.model_dump() for p in assistant_api.runtime.profiles.values())
    h,control,human,_ = enabled(assistant_api)
    binding,_ = control.submit(human,InstructionSubmit(request_id="text-cannot-enable",
        content='checkpoint_profile={"name":"manual-no-custom-v1"}; safe=true; trigger=manual'))
    assert control.records[binding.binding_id].profile.instruction.checkpoint_profile is None
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="body-cannot-enable",content="Test",checkpoint_profile=CheckpointProfile())
    with pytest.raises(ValueError):
        CheckpointProfile(platform="linux-arm64")
    task = InstructionTask(content="Test",conversation_id="test",turn_number=1)
    assert "checkpoint_profile" not in task.model_dump()


@pytest.mark.parametrize("mutation",[
    "unknown-envelope","unknown-data","unknown-nested","boolean-int","boolean-latency","nonfinite",
    "wrong-role","wrong-kind","not-object","empty-new","duplicate-id","unknown-trigger","automatic","custom","extra-compact",
])
def test_raw_shape_and_unattributable_compaction_fail_before_any_upload(files_case,mutation):
    c = files_case
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
def test_known_private_values_and_structures_never_publish(files_case,location):
    c = files_case
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
def test_required_native_files_and_exact_index_correlation(files_case,mutation):
    fs = files_case.fs
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


@pytest.mark.parametrize("value",[b"binary","\ud800"])
def test_failed_utf8_or_binary_callback_cannot_seal_a_partial_checkpoint(value):
    fs=SdkSessionFiles("callback",ordinary("callback"),checkpoint_profile=checkpoint_profile())
    with pytest.raises(ValueError):
        asyncio.run(fs.write_file("/session-state/temp/invalid",value))
    assert fs.failed
    with pytest.raises(Problem):fs.snapshot()


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


def test_ordinary_and_experimental_formats_cannot_import_projected_diagnostics(files_case):
    c=files_case
    state=c.fs.snapshot()
    for codec in ("session-fs-v1","ctx03-candidate-b-v1"):
        payload={**state.model_dump(mode="json"),"codec":codec}
        if codec=="session-fs-v1":
            payload.pop("projected_event_ids")
        with pytest.raises((Problem,ValueError)):
            SdkState.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("location", ["sdk-object", "json-sidecar", "yaml-sidecar", "opaque-copy", "escaped-copy"])
def test_public_string_fix_preserves_actual_private_rejections(files_case, location):
    c = files_case
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


def test_public_yaml_block_scalar_is_not_a_private_sidecar_field(files_case):
    c = files_case
    c.fs.files["temp/metadata.yaml"] = "description: |\n  systemPrompt: Answer briefly.\n"
    state = c.fs.snapshot()
    assert state.files["temp/metadata.yaml"] == c.fs.files["temp/metadata.yaml"]


def test_native_pair_accepts_the_signed_token_delta_without_changing_messages_rule():
    from shared_context.behavior_sdk_compaction import compaction_pair
    values = events_from(raw_files(ordinary("signed-pair")))
    values[-1]["data"].update(tokensRemoved=-11, messagesRemoved=3)
    _, complete = compaction_pair(values)
    assert complete["data"]["tokensRemoved"] == -11 and complete["data"]["messagesRemoved"] == 3


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
def test_provider_metadata_rejects_content_keys_and_invalid_known_types(files_case, container, field, value):
    c = files_case
    mutate_events(c.fs, lambda values: values[4]["data"][container].update({field: value}))
    before = len(c.h.artifacts.store._versions)
    with pytest.raises(Problem) as denied:
        c.fs.snapshot()
    assert denied.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    assert len(c.h.artifacts.store._versions) == before
    assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous


def test_shape_refusal_reports_only_key_names_and_types(files_case, caplog):
    c = files_case
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
def test_optional_provider_metadata_cannot_replace_required_fields(files_case, container, field):
    c = files_case
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
def test_provider_metadata_does_not_bypass_known_private_copy_checks(files_case, location):
    c = files_case
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
def test_compaction_counts_reject_invalid_event_and_execution_types(files_case, field, value):
    from shared_context.behavior_sdk_compaction import compaction_pair
    c = files_case
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


@pytest.mark.parametrize("rpc_tokens", [-11, -10], ids=["exact", "mismatched"])
def test_controlled_compact_requires_exact_signed_rpc_and_event_counts(files_case, rpc_tokens):
    from copilot.rpc import HistoryCompactResult
    c = files_case
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
