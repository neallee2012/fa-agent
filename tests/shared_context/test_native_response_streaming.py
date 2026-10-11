"""The official Responses event stream carries public progress, never a second business result."""

import asyncio
import json

from test_native_responses import response_submit, responses, stored_rid
from test_native_turns import local_env


def sse_events(response):
    return [json.loads(line[5:].strip()) for line in response.text.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


def test_only_projected_tool_names_precede_the_saved_result_without_private_material(responses):
    execute = responses.runtime.executor

    async def progress(receipt, scope, previous, control):
        if control.on_event is not None:
            for event in (
                {"type": "assistant.message_delta", "data": {"delta_content": "PRIVATE-unchecked-text"}},
                {"type": "tool.execution_start", "public_progress": {
                    "kind": "tool_started", "tool_name": "read_context", "summary": "PRIVATE-arguments"},
                 "data": {"tool_name": "PRIVATE-raw-tool", "arguments": {"secret": "PRIVATE-arguments"}}},
                {"type": "tool.execution_complete", "data": {"output": "PRIVATE-tool-output"}},
                {"type": "assistant.reasoning_delta", "data": {"delta_content": "PRIVATE-reasoning"}},
                {"type": "assistant.message", "public_progress": {
                    "kind": "stage", "summary": "PRIVATE-note", "reasoning_summary": "PRIVATE-summary"}},
            ):
                control.on_event(event)
                await asyncio.sleep(0)
        return await execute(receipt, scope, previous, control)

    responses.runtime.executor = progress
    assert responses.invoke(response_submit()).status_code == 202
    original = responses.call(responses.runtime.read, "rid-1")
    streamed = responses.respond(original, stream=True)
    assert streamed.status_code == 200
    events = sse_events(streamed)
    assert sum(event["type"] == "response.created" for event in events) == 1
    numbers = [event["sequence_number"] for event in events]
    assert numbers == sorted(set(numbers))
    deltas = [event["delta"] for event in events if event["type"] == "response.output_text.delta"]
    assert any("read_context" in text for text in deltas)
    assert "PRIVATE-" not in streamed.text
    resets = [event for event in events if event["type"] == "response.in_progress"]
    assert len(resets) == 1 and resets[0]["response"]["output"] == []
    final = events[-1]
    assert final["type"] == "response.completed", final
    assert final["response"].get("usage") is None
    assert [item["phase"] for item in final["response"]["output"]] == ["commentary", "final_answer"]
    assert final["response"]["output"][0]["content"][0]["text"] == "\n正在使用工具：read_context\n"
    answer = final["response"]["output"][-1]
    text = "".join(part["text"] for part in answer["content"])
    assert text == "Complete public answer"
    stored = responses.response_result(final["response"]["id"])
    assert stored["output"] == final["response"]["output"]
    assert responses.wait("rid-1", "completed").result is not None


def test_completed_receipt_reentry_resets_the_public_stream_and_keeps_its_original_response_id(responses):
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    first = responses.respond(accepted)
    completed = responses.wait("rid-1", "completed")
    before = responses.call(stored_rid, "rid-1")
    replay = responses.respond(accepted, stream=True)
    events = sse_events(replay)
    display = "Old partial output must be replaced"
    for event in events:
        if event["type"] == "response.in_progress":
            display = ""
        elif event["type"] == "response.output_text.delta":
            display += event["delta"]
    assert [event["type"] for event in events[:2]] == ["response.created", "response.in_progress"]
    assert events[-1]["type"] == "response.completed"
    assert display == "Complete public answer"
    after = responses.call(stored_rid, "rid-1")
    assert after.etag == before.etag and after.value == before.value
    assert completed.response_id == first.json()["id"]
    assert len(responses.sdk.calls) == 1


def test_reply_preview_streams_every_delta_as_commentary_and_the_saved_answer_stays_final(responses, caplog):
    """RTT B: preview text is display-only commentary, merged without drops; final_answer is still the saved text."""
    execute = responses.runtime.executor
    parts = [f"預覽{index:03d} " for index in range(300)]

    async def progress(receipt, scope, previous, control):
        for part in parts:
            control.on_event({"type": "assistant.message_delta", "live_text": part})
        control.on_event({"type": "tool.execution_start", "public_progress": {
            "kind": "tool_started", "tool_name": "read_context"}})
        await asyncio.sleep(0)
        return await execute(receipt, scope, previous, control)

    responses.runtime.executor = progress
    caplog.set_level("INFO")
    assert responses.invoke(response_submit()).status_code == 202
    original = responses.call(responses.runtime.read, "rid-1")
    events = sse_events(responses.respond(original, stream=True))
    assert events[-1]["type"] == "response.completed", events[-1]
    output = events[-1]["response"]["output"]
    assert [item["phase"] for item in output] == ["commentary", "final_answer"]
    commentary = output[0]["content"][0]["text"]
    assert commentary == "".join(parts) + "\n正在使用工具：read_context\n"
    added = {event["item"]["id"]: event["item"]["phase"] for event in events
             if event["type"] == "response.output_item.added"}
    streamed = "".join(event["delta"] for event in events if event["type"] == "response.output_text.delta"
                       and added[event["item_id"]] == "commentary")
    assert streamed == commentary
    assert "".join(part["text"] for part in output[-1]["content"]) == "Complete public answer"
    assert "FA_NATIVE_PROGRESS_DROPPED" not in caplog.text and '"phase":"native_first_preview"' in caplog.text
    assert responses.wait("rid-1", "completed").result is not None
