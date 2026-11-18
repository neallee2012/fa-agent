"""Pinned SDK/runtime Responses, synthetic HTTP only; no provider-side state."""
import asyncio
import json
import sys

import httpx

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import ToolOutput, ToolSpec, run_copilot
from instruction_flow_helpers import instruction_models
from test_m2_copilot import runtime_path, workspace

from responses_bytes_helpers import OPAQUE, SUMMARY, response_bytes


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
        # Runtime 1.0.103 alone sends "low"; FA keeps 1.0.83's answer length, on a new and a resumed session.
        assert body["text"] == {"verbosity": "medium"}
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
    from sdk_compaction_product_helpers import SUMMARY as INLINE_SUMMARY
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
