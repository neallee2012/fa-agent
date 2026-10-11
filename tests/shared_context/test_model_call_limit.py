"""Per-turn model limits through the real SDK adapter and offline model transport."""

import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_assistant_wire import InstructionContext
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import ResidentHolder, ToolOutput, ToolSpec, run_copilot
from test_m2_copilot import ScriptedEndpoint, runtime_path, tool_call
from test_m2_copilot_exception_binding import adapter
from test_resident_worker_native import sse


@pytest.mark.parametrize("limit, tool_calls, expected_requests, blocked", [
    (100, 99, 100, False),
    (100, 100, 100, True),
    (None, 100, 101, False),
    (1, 1, 1, True),
], ids=["peer-100-allowed", "peer-101-refused", "human-unchanged", "stricter-caller-limit"])
def test_per_turn_limit_counts_actual_model_sends(
    tmp_path, runtime_path, limit, tool_calls, expected_requests, blocked,
):
    sid = "peer-model-limit"
    endpoint = ScriptedEndpoint([
        *(tool_call("read_context", {}, f"limit-{number}") for number in range(1, tool_calls + 1)),
        {"content": "OFFLINE_DONE"},
    ])
    endpoint.unbounded = True
    reservations, events, stopped = [], [], []

    async def reserve(request_digest):
        reservations.append(request_digest)

    async def read_context(_):
        text = "OFFLINE model limit fixture"
        return ToolOutput(InstructionContext(source={
            "source_id": "model-limit-source", "version": 1,
            "sha256": hashlib.sha256(text.encode()).hexdigest(), "byte_length": len(text.encode()),
        }, content=text).model_dump_json())

    boundary = AssistantModelBoundary(reserve, text_reply=True)

    async def run():
        return await asyncio.wait_for(run_copilot(
            state_dir=tmp_path / "sdk", working_directory=tmp_path, sdk_session_id=sid,
            resume=False, prompt="Continue reading the offline fixture until explicitly stopped.",
            tools=[ToolSpec("read_context", "Read the offline fixture",
                            {"type": "object", "properties": {}, "additionalProperties": False}, read_context)],
            api_url="https://context.invalid", token_provider=endpoint.token, on_event=events.append,
            timeout_seconds=None, runtime_path=runtime_path, http_transport=endpoint.transport,
            behavior_boundary=boundary, instruction=True, session_files=SdkSessionFiles(sid),
            model_call_limit=limit, on_sdk_stopped=stopped.append,
        ), timeout=90)

    if blocked:
        with pytest.raises(RuntimeError):
            asyncio.run(run())
        assert boundary.failure["upstream_error_code"] == "MODEL_CALL_LIMIT_REACHED"
        assert any(event.get("public_progress", {}).get("summary") == "MODEL_CALL_LIMIT_REACHED"
                   for event in events)
    else:
        assert asyncio.run(run())["final_message"] == "OFFLINE_DONE"
        assert boundary.failure is None
    assert len(endpoint.requests) == expected_requests
    assert len(reservations) == len(set(reservations)) == expected_requests
    assert len(stopped) == 1 and stopped[0]["graceful"] is True
    assert type(stopped[0]["exit_code"]) is int


def test_next_turn_in_the_same_resident_sdk_gets_its_own_model_allowance(tmp_path, runtime_path):
    endpoint = ScriptedEndpoint([{"content": "FIRST"}, {"content": "SECOND"}])
    endpoint.unbounded = True
    holder, files = ResidentHolder(), SdkSessionFiles("limit-resident")
    reservations = []

    async def reserve(request_digest):
        reservations.append(request_digest)

    async def scenario():
        try:
            for number, expected in ((1, "FIRST"), (2, "SECOND")):
                root = tmp_path / str(number)
                root.mkdir()
                outcome = await run_copilot(
                    state_dir=root / "sdk", working_directory=root, sdk_session_id=files.session_id,
                    resume=number > 1, prompt=f"Read the next offline answer for turn {number}.",
                    tools=[], api_url="https://context.invalid", token_provider=endpoint.token,
                    on_event=lambda _: None, timeout_seconds=None, runtime_path=runtime_path,
                    http_transport=endpoint.transport, behavior_boundary=AssistantModelBoundary(reserve, text_reply=True),
                    instruction=True, session_files=files, resident=holder, model_call_limit=1,
                )
                assert outcome["final_message"] == expected
                assert outcome["sdk_stop"]["release"] == "fa-resident-worker-poc-v1"
                if number == 1:
                    first_runtime = holder.live
                else:
                    assert holder.live is first_runtime
        finally:
            if holder.live is not None:
                await holder.live.stop()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert len(endpoint.requests) == len(reservations) == 2


def test_concurrent_sdk_requests_share_the_limit_before_awaiting_authority(adapter):
    sent, reserved = [], []

    async def reserve(request_digest):
        reserved.append(request_digest)
        await asyncio.sleep(0)

    async def sender(request):
        sent.append(request)
        return sse(request, {"content": "OFFLINE"}, len(sent))

    async def sdk_send(*_args, **_kwargs):
        gateway = adapter.factory.call_args.kwargs["request_handler"]
        sessions = (adapter.session.session_id, "delegated-session", "compaction-session")
        results = await asyncio.gather(*(gateway.send_request(
            httpx.Request("POST", "https://offline.invalid/model/v1/chat/completions", json={
                "model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"request {number}"}],
            }), SimpleNamespace(request_id=f"request-{number}", session_id=sessions[number]),
        ) for number in range(3)), return_exceptions=True)
        assert sum(isinstance(value, Exception) for value in results) == 1
        for response in results:
            if isinstance(response, httpx.Response):
                await response.aread()
        return SimpleNamespace(data=SimpleNamespace(content="SDK swallowed the limit error"))

    adapter.options.update(instruction=True, model_call_limit=2)
    adapter.boundary.text_reply = True
    adapter.boundary.reserve, adapter.boundary.sender = reserve, sender
    adapter.session.send_and_wait.side_effect = sdk_send
    with pytest.raises(RuntimeError):
        asyncio.run(run_copilot(**adapter.options))
    assert len(sent) == len(reserved) == 2
    assert adapter.boundary.failure["upstream_error_code"] == "MODEL_CALL_LIMIT_REACHED"
    adapter.client.stop.assert_awaited_once()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "100"])
def test_invalid_caller_limits_fail_before_starting_the_sdk(adapter, limit):
    adapter.options.update(instruction=True, model_call_limit=limit)
    with pytest.raises(ValueError, match="positive integer"):
        asyncio.run(run_copilot(**adapter.options))
    adapter.factory.assert_not_called()


@pytest.mark.parametrize("unknown", [False, True], ids=["same-body", "unknown-response"])
def test_repeated_sdk_model_request_never_uses_a_new_slot_to_resend(adapter, unknown):
    sent, reserved = [], []

    async def reserve(request_digest):
        reserved.append(request_digest)

    async def sender(request):
        sent.append(request)
        if unknown:
            raise httpx.ReadError("OFFLINE private transport detail")
        return sse(request, {"content": "OFFLINE"}, len(sent))

    async def sdk_send(*_args, **_kwargs):
        gateway = adapter.factory.call_args.kwargs["request_handler"]
        request = httpx.Request("POST", "https://offline.invalid/model/v1/chat/completions", json={
            "model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "same original input"}],
        })
        context = SimpleNamespace(request_id="original-request", session_id=adapter.session.session_id)
        try:
            response = await gateway.send_request(request, context)
            await response.aread()
        except RuntimeError:
            if not unknown:
                raise
        return await gateway.send_request(request, context)

    adapter.options.update(instruction=True, model_call_limit=100)
    adapter.boundary.text_reply = True
    adapter.boundary.reserve, adapter.boundary.sender = reserve, sender
    adapter.session.send_and_wait.side_effect = sdk_send
    with pytest.raises(RuntimeError):
        asyncio.run(run_copilot(**adapter.options))
    assert len(sent) == len(reserved) == 1
    assert adapter.boundary.failure["upstream_error_phase"] == ("transport" if unknown else "replayed_request")
    assert "OFFLINE private transport detail" not in str(adapter.boundary.failure)
    adapter.client.stop.assert_awaited_once()
