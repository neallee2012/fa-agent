"""ADR-0005 PoC on the real local SDK runtime; scripted model, no cloud or paid model."""
import asyncio
import json
import os
from pathlib import Path

import httpx

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import ResidentHolder, ToolOutput, ToolSpec, run_copilot


def sse(request, delta, count):
    base = {"id": f"resident-{count}", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4.1-mini"}
    reason = "tool_calls" if "tool_calls" in delta else "stop"
    chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
              {**base, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
    return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
        content=("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode())


def test_real_sdk_turns_reuse_one_released_runtime_and_restore_cold(tmp_path):
    runtime = Path(os.environ["M2_TEST_RUNTIME"])
    assert runtime.is_file(), "Provision the pinned runtime; do not silently skip this acceptance"
    sid = "resident-native-session"
    bodies, tools, events = [], [], []

    async def read_context(_):
        import hashlib
        from shared_context.behavior_assistant_wire import InstructionContext
        tools.append("read_context")
        content = '{"fact":"RESIDENT_CANARY_APPLES"}'
        return ToolOutput(InstructionContext(source={
            "source_id": "resident-fact", "version": 1, "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "byte_length": len(content.encode())}, content=content).model_dump_json())

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        transcript = json.dumps(body["messages"], ensure_ascii=False)
        if len(bodies) == 1:
            return sse(request, {"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                "function": {"name": "read_context", "arguments": "{}"}}]}, len(bodies))
        if len(bodies) == 2:
            assert "RESIDENT_CANARY_APPLES" in transcript
            return sse(request, {"content": "answer one"}, len(bodies))
        assert "answer one" in transcript and "RESIDENT_CANARY_APPLES" in transcript
        return sse(request, {"content": f"answer {len(bodies) - 1}"}, len(bodies))

    async def reserve(_):
        pass

    async def one_turn(holder, files, prompt, *, resume, root):
        root.mkdir()
        return await run_copilot(
            state_dir=root / "sdk", working_directory=root, sdk_session_id=sid, resume=resume, prompt=prompt,
            tools=[ToolSpec("read_context", "Read the fact", {"type": "object", "properties": {}}, read_context)],
            api_url="https://offline.invalid", token_provider=lambda: "RESIDENT-OFFLINE-TOKEN",
            on_event=events.append, timeout_seconds=None, runtime_path=runtime,
            http_transport=httpx.MockTransport(respond), behavior_boundary=AssistantModelBoundary(reserve, text_reply=True),
            instruction=True, session_files=files, resident=holder, system_message="Resident PoC test.")

    async def scenario():
        holder, files = ResidentHolder(), SdkSessionFiles(sid)
        try:
            first = await one_turn(holder, files, "Read the fact.", resume=False, root=tmp_path / "t1")
            live = holder.live
            assert first["final_message"] == "answer one" and tools == ["read_context"]
            assert first["sdk_stop"]["release"] == "fa-resident-worker-poc-v1" and "exit_code" not in first["sdk_stop"]
            assert live is not None and live.alive() and live.turn is None
            files.snapshot(freeze=False)
            second = await one_turn(holder, live.files, "Repeat it.", resume=True, root=tmp_path / "t2")
            assert second["final_message"] == "answer 2" and holder.live is live
            assert second["sdk_stop"]["pid"] == first["sdk_stop"]["pid"] and tools == ["read_context"]
            published = live.files.snapshot(freeze=False)
            # A lost worker: a new holder cold-restores the published state in a new runtime process.
            cold_holder = ResidentHolder()
            try:
                third = await one_turn(cold_holder, SdkSessionFiles(sid, published), "Again.", resume=True,
                                       root=tmp_path / "t3")
                assert third["final_message"] == "answer 3" and tools == ["read_context"]
                assert third["sdk_stop"]["pid"] != first["sdk_stop"]["pid"]
            finally:
                if cold_holder.live is not None:
                    await cold_holder.live.stop()
        finally:
            if holder.live is not None:
                process = holder.live.process
                await holder.live.stop()
                assert process.poll() is not None

    asyncio.run(scenario())
    assert len(bodies) == 4
