"""Pinned SDK/runtime Responses, synthetic HTTP only; no provider-side state."""
import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import ToolOutput, ToolSpec, run_copilot
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_m2_copilot import runtime_path, workspace
from test_m3_auth import jwk_client, signing_key

SUMMARY = "The public summary says to check the original source."
OPAQUE = "PRIVATE_ENCRYPTED_REASONING_CANARY"


def response_bytes(ordinal, *, text=None, tool=None, public_summary=SUMMARY, observed_reasoning=False, arguments=None):
    rid, reason_id, message_id = f"resp_offline_{ordinal}", f"rs_offline_{ordinal}", f"msg_offline_{ordinal}"
    reason = {"id": reason_id, "type": "reasoning", "status": "completed", "encrypted_content": OPAQUE,
              "summary": [{"type": "summary_text", "text": public_summary}]}
    if observed_reasoning:
        # Field set seen in the 2026-09-30 Hosted rejection log (values are synthetic).
        reason = {"id": reason_id, "type": "reasoning", "encrypted_content": OPAQUE, "content": [],
                  "response_id": rid, "summary": reason["summary"]}
    part = {"type": "output_text", "text": text or f"Original public answer {ordinal}.", "annotations": []}
    message = {"id": message_id, "type": "message", "role": "assistant", "status": "completed", "content": [part]}
    if tool is not None:
        encoded_arguments = json.dumps({} if arguments is None else arguments)
        message = {"id": f"fc_{ordinal}", "type": "function_call", "call_id": f"call_{ordinal}",
                   "name": tool, "arguments": encoded_arguments, "status": "completed"}
    response = {"id": rid, "object": "response", "created_at": 1, "model": "gpt-5.4-mini", "status": "completed",
                "output": [reason, message], "error": None, "usage": {
                    "input_tokens": 21, "output_tokens": 13, "total_tokens": 34,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 8}}}
    events = []

    def add(kind, **data):
        events.append({"type": kind, "sequence_number": len(events), **data})

    add("response.created", response={**response, "status": "in_progress", "output": [], "usage": None})
    add("response.output_item.added", output_index=0, item={"id": reason_id, "type": "reasoning", "summary": []})
    add("response.reasoning_summary_part.added", item_id=reason_id, output_index=0, summary_index=0,
        part={"type": "summary_text", "text": ""})
    add("response.reasoning_summary_text.delta", item_id=reason_id, output_index=0, summary_index=0, delta=public_summary)
    add("response.reasoning_summary_text.done", item_id=reason_id, output_index=0, summary_index=0, text=public_summary)
    add("response.reasoning_summary_part.done", item_id=reason_id, output_index=0, summary_index=0, part=reason["summary"][0])
    add("response.output_item.done", output_index=0, item=reason)
    if tool is None:
        add("response.output_item.added", output_index=1, item={**message, "content": [], "status": "in_progress"})
        add("response.content_part.added", item_id=message_id, output_index=1, content_index=0,
            part={"type": "output_text", "text": "", "annotations": []})
        add("response.output_text.delta", item_id=message_id, output_index=1, content_index=0, delta=part["text"])
        add("response.output_text.done", item_id=message_id, output_index=1, content_index=0, text=part["text"])
        add("response.content_part.done", item_id=message_id, output_index=1, content_index=0, part=part)
    else:
        add("response.output_item.added", output_index=1, item={**message, "arguments": "", "status": "in_progress"})
        add("response.function_call_arguments.delta", item_id=message["id"], output_index=1, delta=encoded_arguments)
        add("response.function_call_arguments.done", item_id=message["id"], output_index=1, arguments=encoded_arguments)
    add("response.output_item.done", output_index=1, item=message)
    add("response.completed", response=response)
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def test_native_responses_summary_and_cold_resume_need_no_private_checkpoint_or_provider_storage(workspace, runtime_path):
    requests, public = [], []
    states = []
    reservations = []

    def provider(request):
        assert str(request.url) == "https://context.invalid/model/v1/responses"
        body = json.loads(request.content)
        assert body["store"] is False and not body.get("previous_response_id")
        assert all(item.get("type") != "item_reference" for item in body["input"])
        assert body["reasoning"]["summary"] == "auto"
        requests.append(body)
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                              content=response_bytes(len(requests)))

    async def reserve(identity):
        reservations.append(identity)

    for ordinal in (1, 2):
        files = SdkSessionFiles("responses-native", states[-1] if states else None, model_provider="openai-responses")
        result = asyncio.run(run_copilot(
            state_dir=workspace / f"state-{ordinal}", working_directory=workspace / "work",
            sdk_session_id="responses-native", resume=ordinal == 2, prompt=f"Original question {ordinal}.",
            tools=[], api_url="https://context.invalid", token_provider=lambda: "OFFLINE",
            on_event=public.append, timeout_seconds=30, runtime_path=runtime_path,
            http_transport=httpx.MockTransport(provider), instruction=True, model="gpt-5.4-mini",
            model_provider="openai-responses", reasoning_summary="detailed", session_files=files,
            behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True)))
        assert result["final_message"] == f"Original public answer {ordinal}."
        assert result["sdk_stop"]["graceful"] is True
        saved = files.snapshot()
        assert saved.model_provider == "openai-responses"
        assert OPAQUE not in saved.encoded().decode() and SUMMARY not in saved.encoded().decode()
        states.append(saved)
    summaries = [event["public_progress"]["reasoning_summary"] for event in public
                 if event.get("public_progress", {}).get("reasoning_summary")]
    assert summaries == [SUMMARY, SUMMARY]
    assert OPAQUE not in json.dumps(public)
    assert len(requests) == len(reservations) == 2
    assert "Original public answer 1." in json.dumps(requests[1]) and OPAQUE not in json.dumps(requests[1])


def test_native_responses_tool_loop_carries_its_own_history_without_provider_storage(workspace, runtime_path):
    from shared_context import behavior_assistant_wire as aw
    requests, tools, events, reservations = [], [], [], []
    files = SdkSessionFiles("responses-tools", model_provider="openai-responses")

    def provider(request):
        body = json.loads(request.content)
        assert body["store"] is False and not body.get("previous_response_id")
        assert all(item.get("type") != "item_reference" for item in body["input"])
        requests.append(body)
        if len(requests) == 2:
            assert any(item.get("type") == "function_call_output" for item in body["input"])
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
            content=response_bytes(len(requests), tool="read_context" if len(requests) == 1 else None))

    async def read(packet):
        tools.append(packet["tool_call_id"])
        return ToolOutput(aw.InstructionContext(source={
            "source_id": "source", "version": 1, "sha256": "a"*64, "byte_length": 4}, content="Data").model_dump_json())

    async def reserve(identity):
        reservations.append(identity)

    result = asyncio.run(run_copilot(
        state_dir=workspace / "tools", working_directory=workspace / "work",
        sdk_session_id="responses-tools", resume=False, prompt="Read the source, then answer.",
        tools=[ToolSpec("read_context", "Read the source.", aw.Empty.model_json_schema(), read)],
        api_url="https://context.invalid", token_provider=lambda: "OFFLINE", on_event=events.append,
        timeout_seconds=30, runtime_path=runtime_path, http_transport=httpx.MockTransport(provider),
        instruction=True, model="gpt-5.4-mini", model_provider="openai-responses",
        reasoning_summary="detailed", session_files=files,
        behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True)))
    assert len(requests) == len(reservations) == 2 and len(tools) == 1
    assert result["final_message"] == "Original public answer 2."
    public = [event["public_progress"] for event in events if "public_progress" in event]
    started = next(event for event in public if event["kind"] == "tool_started")
    completed = next(event for event in public if event["kind"] == "tool_completed")
    assert completed["tool_start_seq"] == started["seq"] and completed["status"] == "succeeded"
    assert OPAQUE not in files.snapshot().encoded().decode()


def test_native_responses_manual_checkpoint_keeps_its_provider_on_later_cold_resume(workspace, runtime_path):
    from shared_context import behavior_assistant_wire as aw
    from shared_context.behavior_sdk_state import CheckpointProfile, RESPONSES_DIAG_CODEC
    from test_sdk_compaction_product import SUMMARY as INLINE_SUMMARY
    checkpoint_summary = INLINE_SUMMARY.replace("</overview>", "</overview>\n").replace("</history>", "</history>\n")
    profile = CheckpointProfile(provider="openai-responses",
                                platform="win32-x64" if sys.platform == "win32" else "linux-x64")
    states, requests, reserved = [], [], []

    def provider(request):
        body = json.loads(request.content)
        assert str(request.url).endswith("/responses") and body["store"] is False
        assert not body.get("previous_response_id")
        requests.append(body)
        ordinal = len(requests)
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
            content=response_bytes(ordinal, text=checkpoint_summary if ordinal == 2 else None,
                                   public_summary=checkpoint_summary if ordinal == 2 else SUMMARY))

    async def reserve(identity):
        reserved.append(identity)

    for ordinal in (1, 2, 3):
        instruction = aw.InstructionTask(content=f"Question {ordinal}.", conversation_id="responses-compact",
            turn_number=ordinal, model="gpt-5.4-mini", prompt_profile="fa-instruction-v1",
            model_provider="openai-responses", reasoning_summary="detailed", checkpoint_profile=profile)
        task = aw.Task(job_id=f"job-{ordinal}", work_id="work", definition_digest="a"*64, profile_digest="b"*64,
            source={"source_id":"source","version":1,"sha256":"c"*64,"byte_length":2},
            artifact_id="output", artifact_version="d"*32, operation_id="unused", instruction=instruction)
        files = SdkSessionFiles("responses-compact", states[-1] if states else None,
                               checkpoint_profile=profile, model_provider="openai-responses")
        result = asyncio.run(run_copilot(
            state_dir=workspace / f"compact-{ordinal}", working_directory=workspace / "work",
            sdk_session_id="responses-compact", resume=ordinal > 1, prompt=instruction.content,
            tools=[], api_url="https://context.invalid", token_provider=lambda: "OFFLINE",
            on_event=lambda _: None, timeout_seconds=30, runtime_path=runtime_path,
            http_transport=httpx.MockTransport(provider), instruction=True, model="gpt-5.4-mini",
            model_provider="openai-responses", reasoning_summary="detailed", session_files=files, checkpoint_task=task,
            behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True)))
        assert result["sdk_stop"]["graceful"]
        original_files = dict(files.files)
        original_events = [json.loads(line) for line in original_files["events.jsonl"].splitlines()]
        saved = files.snapshot()
        assert saved.model_provider == "openai-responses"
        assert OPAQUE not in saved.encoded().decode()
        retained_events = [json.loads(line) for line in saved.files["events.jsonl"].splitlines()]
        assert [(e["id"], e["type"], e.get("parentId")) for e in retained_events] == [
            (e["id"], e["type"], e.get("parentId")) for e in original_events]
        assert files.files == original_files
        for raw, kept in zip(original_events, retained_events, strict=True):
            if raw["type"] in {"user.message", "assistant.message"}:
                assert raw["data"].get("content") == kept["data"].get("content")
            if raw["type"] in {"model.message", "model.response"}:
                key = raw["type"].split(".")[1]
                assert raw["data"][key]["content"] == kept["data"][key]["content"]
        if ordinal > 1:
            assert saved.codec == RESPONSES_DIAG_CODEC
        states.append(saved)
    assert len(requests) == len(reserved) == 4
    assert "Original public source: inventory count 850." in json.dumps(requests[-1])


def test_native_responses_crosses_hosted_api_checkpoint_progress_and_original_ledger(assistant, workspace):
    from shared_context.behavior_assistant_api import create_assistant_app
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_instruction_models import InstructionModel
    from shared_context.behavior_operations import _row
    from shared_context.behavior_sdk_checkpoint import SdkStateUpload, checkpoint_key
    from shared_context.behavior_sdk_state import CheckpointProfile
    from shared_context.behavior_work_policy import CostLedger
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    from test_conversation_profile_selection import configured, select, template

    h, control, human = configured(assistant)
    control.models.models["gpt-5.4-mini"] = InstructionModel(id="gpt-5.4-mini", display_name="gpt-5.4-mini",
        max_output_tokens=32768, input_microusd_per_million=400000, output_microusd_per_million=1600000,
        supports_reasoning_effort=True, supports_reasoning_summary=True)
    profile = CheckpointProfile(provider="openai-responses",
        platform="win32-x64" if sys.platform == "win32" else "linux-x64")
    assert select(h, value=template(model_provider="openai-responses", checkpoint_profile=profile))[0].status_code == 200
    calls = []
    compact_summary = ("<overview>Original conversation.</overview>\n"
                       "<history>Original public answer 1.</history>\n"
                       "<checkpoint_title>Responses conversation</checkpoint_title>")

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/responses"
        body = json.loads(request.content)
        assert body["store"] is False and not body.get("previous_response_id")
        calls.append(body)
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
            content=response_bytes(len(calls), text=compact_summary if len(calls) == 2 else None,
                                   observed_reasoning=True))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE")), transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    cid = None
    for turn in (1, 2, 3):
        h.executor.root = workspace / f"product-{turn}"
        command = InstructionSubmit(request_id=f"responses-product-{turn}", content=f"Original request {turn}.",
                                    conversation_id=cid, model="gpt-5.4-mini")
        accepted = h.client.post("/work-policy/v1/submit", content=command.model_dump_json(), headers=h.headers())
        assert accepted.status_code == 200, accepted.text
        cid, binding_id = accepted.json()["conversation_id"], accepted.json()["binding_id"]
        outcome = h.runtime.controller.step(binding_id)
        assert outcome.state == "succeeded" and outcome.result_forwarded and not outcome.obligation_fulfilled
        projection = control.conversation(human, cid)
        assert projection["can_follow_up"]
        progress = projection["turns"][-1]["progress"]
        assert progress["reasoning_summary"] == {"availability": "provided", "source": "openai-responses", "text": SUMMARY}
        assert any(event.get("reasoning_summary") == SUMMARY for event in progress["events"])
        context = h.client.get(f"/work-policy/v1/conversations/{cid}/turns/{turn}/context", headers=h.headers())
        assert context.status_code == 200, context.text
        assert context.json()["archive"]["codec"] == (
            "artifact-session-fs-responses-v1" if turn == 1 else "artifact-session-fs-responses-diag-v1")
        job = h.runtime.bindings[binding_id].job_id
        upload = _row(h.store.read_snapshot((checkpoint_key(job),)), checkpoint_key(job), SdkStateUpload)
        assert upload.reference.model_provider == "openai-responses"
    ledger = _row(h.store.read_snapshot((control.cost_key,)), control.cost_key, CostLedger)
    assert len(calls) == 4 and ledger.model_pending_microusd == 0 and ledger.model_metered_microusd == 120
