"""Pinned real SDK/runtime with scripted local provider; no live model or Hosted."""

import asyncio
import hashlib
import json

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.m2_copilot import ToolOutput, ToolSpec, run_copilot
from test_m2_copilot import ScriptedEndpoint, runtime_path, workspace


def test_native_public_pairs_hide_sdk_ids_and_tool_bodies(workspace, runtime_path):
    events, handlers, reservations = [], [], []
    body = "PRIVATE_TOOL_BODY_CANARY"
    source = aw.ExactSourceRef(
        source_id="reviewed-source", version=1,
        sha256=hashlib.sha256(body.encode()).hexdigest(), byte_length=len(body.encode()),
    )

    async def read(packet):
        handlers.append(packet["tool_call_id"])
        return ToolOutput(aw.InstructionContext(source=source, content=body).model_dump_json())

    async def reserve(digest):
        reservations.append(digest)

    endpoint = ScriptedEndpoint([
        {"content": "I will read the approved sources first.", "tool_calls": [
            {"index": index, "id": identity, "type": "function",
             "function": {"name": "read_context", "arguments": "{}"}}
            for index, identity in enumerate(("PRIVATE_CALL_A", "PRIVATE_CALL_B"))
        ]},
        {"content": "OFFLINE public tool pairing complete"},
    ])
    endpoint.unbounded = True
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-public-tool-pairs", resume=False,
        prompt="Perform the two configured reads, then report completion.",
        tools=[ToolSpec("read_context", "Read the assigned source.", aw.Empty.model_json_schema(), read)],
        api_url="https://context.invalid", token_provider=endpoint.token,
        on_event=events.append, timeout_seconds=35, runtime_path=runtime_path,
        http_transport=endpoint.transport, instruction=True,
        behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True),
    ))
    public = [aw.PublicProgressEvent.model_validate_json(json.dumps(event["public_progress"]))
              for event in events if "public_progress" in event]
    starts = {event.seq: event for event in public if event.kind == "tool_started"}
    completed = [event for event in public if event.kind == "tool_completed"]
    assert len(starts) == len(completed) == len(handlers) == 2
    notes = [event for event in public if event.summary == "Assistant note: I will read the approved sources first."]
    assert len(notes) == 1 and notes[0].kind == "stage"
    assert notes[0].seq < min(starts)
    assert not any(event.summary and "OFFLINE public tool pairing complete" in event.summary for event in public)
    assert {event.tool_start_seq for event in completed} == set(starts)
    assert all(event.tool_start_seq == event.seq for event in starts.values())
    assert all(event.tool_name == "read_context" and event.status == "succeeded" for event in completed)
    assert all(event.source == source for event in completed)
    assert len(endpoint.requests) == len(reservations) == 2
    assert result["sdk_stop"]["runtime_version"] == "1.0.83" and result["sdk_stop"]["graceful"]
    assert "PRIVATE_" not in json.dumps(result)
    assert "PRIVATE_" not in json.dumps(events)
    assert result["final_message"] == "OFFLINE public tool pairing complete"
