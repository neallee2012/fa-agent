"""C4: original native RID -> real SDK task/custom agent -> official local effects.

The provider is scripted. This is not a Hosted deployment or real-model quality claim.
"""

import asyncio
from dataclasses import replace
import json
import threading
import time

from copilot import CopilotClient
import httpx
import pytest

from shared_context import m2_copilot
from shared_context.behavior_capabilities import CapabilityCapture, available_grants
from shared_context.behavior_delegation_wire import CAPABILITY_ID
from shared_context.behavior_native_turn_wire import NativeCancel, NativeSubmit, NativeTurnCapture
from shared_context.native_sdk import NativeSdkExecutor, native_capability_providers
from sdk_checkpoint_local import sse
from test_m2_copilot import runtime_path
from test_native_turn_wire import capture, submit
from test_native_turns import harness, local_env, native_retry_policy
from test_skill_isolation_native import child_environment
from test_instruction_responses_native import response_bytes


TASK = {
    "name": "native-checker", "description": "Compare the supplied public sample",
    "prompt": "Original public sample: total 850, used 320. Report the remaining amount.",
    "agent_type": CAPABILITY_ID, "mode": "sync",
}
MODEL_URL = "https://offline-model.services.ai.azure.com/api/projects/offline/openai/v1/chat/completions"


def delegate_request(rid="native-parent-rid", *, responses=False):
    granted = available_grants(None, (CAPABILITY_ID,))[CAPABILITY_ID]
    values = capture().model_dump()
    values["template"]["settings"]["model_provider"] = "openai-responses" if responses else None
    values["model"] = {"id": "gpt-5.4-mini" if responses else "gpt-4.1-mini", "display_name": "Offline scripted model"}
    values["template"]["settings"]["capabilities"] = (granted.ref,)
    values["capabilities"] = CapabilityCapture(head_selection_id="captured-native-delegate", grants=(granted,))
    captured = NativeTurnCapture.model_validate(values)
    return NativeSubmit(command=submit(rid).command, capture=captured)


@pytest.mark.parametrize("selected", [True, False], ids=["delegate-selected", "delegate-not-selected"])
@pytest.mark.parametrize("wire_api", ["completions", "responses"])
@pytest.mark.parametrize("record_operations", [False, True], ids=["legacy-watermarks", "recovery-watermarks"])
def test_original_native_rid_owns_real_sdk_child_result_and_metered_requests(
    harness, monkeypatch, tmp_path, runtime_path, selected, wire_api, record_operations,
):
    requests, bridges = [], []

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    async def provider(request):
        body = json.loads(request.content)
        tools = {entry["name"] if wire_api == "responses" else entry["function"]["name"]
                 for entry in body.get("tools", ())}
        requests.append(tools)
        if not selected:
            assert len(requests) == 1 and "task" not in tools and not bridges
            delta = {"content": "No child was requested or enabled."}
        elif len(requests) == 1:
            assert "task" in tools and "read_user_turn" in tools
            delta = {"tool_calls": [{"index": 0, "id": "native-original-tool-call", "type": "function",
                                    "function": {"name": "task", "arguments": json.dumps(TASK)}}]}
        elif len(requests) == 2:
            assert tools == set()
            original = await bridges[0].read()
            assert original.status == "running" and original.parent_request_id == "native-parent-rid"
            assert original.child_agent_id is not None
            delta = {"content": "Child verified the original public sample: 530."}
        elif len(requests) == 3:
            assert "task" in tools
            original = await bridges[0].read()
            assert original.status == "completed" and original.result.endswith("530.")
            assert "parent_job_id" not in original.model_dump()
            delta = {"content": "Parent result from the original child: 530."}
        else:
            assert len(requests) == 4 and "task" in tools
            assert "Parent result from the original child: 530." in json.dumps(
                body["input"] if wire_api == "responses" else body["messages"])
            delta = {"content": "Continued the same conversation without rerunning its original child."}
        if wire_api == "responses":
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=response_bytes(
                len(requests), tool="task" if "tool_calls" in delta else None, arguments=TASK,
                text=delta.get("content")))
        return sse(request, delta, len(requests))

    grant = available_grants(None, (CAPABILITY_ID,))[CAPABILITY_ID]
    providers = native_capability_providers("https://offline.invalid", lambda: "OFFLINE-api-token")
    matching = [provider for provider in providers if provider.accepts(grant)]
    assert len(matching) == 1, "the native environment must register the actual C4 consumer"
    real_provider = matching[0]

    async def bind(context, grants):
        binding = await real_provider.bind(context, grants)
        bridges.append(binding.delegation_runtime)
        return binding

    executor = NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://OFFLINE.invalid",
        model_url=MODEL_URL.removesuffix("/chat/completions"),
        token_provider=lambda: "OFFLINE-native-model-token", model_transport=httpx.MockTransport(provider),
        capability_providers=(replace(real_provider, bind=bind),))
    executor.reader = harness.runtime
    if record_operations:
        async def unexpected(_view):
            pytest.fail("this complete original child must not need recovery")
        harness.runtime.on_continuation = unexpected
    harness.runtime.executor = executor
    harness.runtime.preflight = executor.preflight
    request = delegate_request(responses=wire_api == "responses")
    if not selected:
        values = request.capture.model_dump()
        values["template"]["settings"]["capabilities"] = ()
        values["capabilities"] = None
        request = NativeSubmit(command=request.command, capture=NativeTurnCapture.model_validate(values))
    assert harness.invoke(request).status_code == 202
    completed = harness.wait(request.command.request_id, "completed")
    if not selected:
        assert len(requests) == 1 and not bridges
        assert completed.usage.usage[0].requests == 1
        assert harness.invoke(request).json()["status"] == "completed" and len(requests) == 1
        return
    original = harness.call(bridges[0].read)
    assert original.parent_request_id == completed.request_id and original.conversation_id == completed.conversation_id
    assert original.status == "completed"
    assert original.tool_call_id == ("call_1" if wire_api == "responses" else "native-original-tool-call")
    assert original.result == "Child verified the original public sample: 530."
    if record_operations:
        from shared_context.native_effects import NativeEffects
        records = harness.call(NativeEffects(harness.runtime, completed).operation_records)
        assert len(records) == 1 and records[0].operation.tool_name == "task"
        assert records[0].dispatch_started is True and records[0].status == "done"
    usage = completed.usage.usage[0]
    assert usage.requests == usage.measured_requests == 3 and len(requests) == 3
    assert (usage.input_tokens, usage.output_tokens) == ((63, 39) if wire_api == "responses" else (4500, 300))
    assert harness.invoke(request).json()["status"] == "completed"
    assert len(requests) == 3 and len(bridges) == 1
    follow_up = NativeSubmit(command=submit("later-original-rid", completed.conversation_id).command, capture=request.capture)
    assert harness.invoke(follow_up).status_code == 202
    continued = harness.wait(follow_up.command.request_id, "completed")
    assert continued.conversation_id == completed.conversation_id and continued.turn_number == 2
    assert continued.usage.usage[0].requests == 1 and len(requests) == 4
    assert len(bridges) == 2 and bridges[1].child_id is None
    assert harness.call(bridges[0].read) == original


def test_confirmed_child_is_not_replaced_by_a_second_task_call_in_the_same_native_turn(
    harness, monkeypatch, tmp_path, runtime_path,
):
    from shared_context.native_delegation import delegation_provider

    calls, children = [], []

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    async def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        number = len(calls)
        if number in {1, 3}:
            return sse(request, {"tool_calls": [{"index": 0, "id": "first-call" if number == 1 else "second-call",
                "type": "function", "function": {"name": "task", "arguments": json.dumps(TASK)}}]}, number)
        if number == 2:
            assert not body.get("tools")
            child = await children[0].read()
            assert child.tool_call_id == "first-call"
            return sse(request, {"content": "Original child result."}, number)
        assert number == 4 and body.get("tools")
        return sse(request, {"content": "Used the original child's result; the extra task was refused."}, number)

    provider_definition = delegation_provider()

    async def bind(context, grants):
        bound = await provider_definition.bind(context, grants)
        children.append(bound.delegation_runtime)
        return bound

    executor = NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://offline.invalid",
        model_url=MODEL_URL.removesuffix("/chat/completions"),
        token_provider=lambda: "OFFLINE-native-model-token", model_transport=httpx.MockTransport(provider),
        capability_providers=(replace(provider_definition, bind=bind),))
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    request = delegate_request("single-original-child")
    assert harness.invoke(request).status_code == 202
    result = harness.wait(request.command.request_id, "completed")
    child = harness.call(children[0].read)
    assert len(calls) == 4 and result.usage.usage[0].requests == 4
    assert child.tool_call_id == "first-call" and child.result == "Original child result."
    assert harness.invoke(request).json()["status"] == "completed" and len(calls) == 4


def test_native_cancel_preserves_child_stop_observations_and_unknown_usage_without_respawn(
    harness, monkeypatch, tmp_path, runtime_path,
):
    from shared_context.native_delegation import delegation_provider

    requests, bridges = [], []
    child_started, transport_cancelled = threading.Event(), threading.Event()

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    async def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse(request, {"tool_calls": [{"index": 0, "id": "native-cancelled-child", "type": "function",
                "function": {"name": "task", "arguments": json.dumps(TASK)}}]}, len(requests))
        assert len(requests) == 2 and not body.get("tools")
        child_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            transport_cancelled.set()
            raise

    real_provider = delegation_provider()

    async def bind(context, grants):
        binding = await real_provider.bind(context, grants)
        bridges.append(binding.delegation_runtime)
        return binding

    executor = NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://offline.invalid",
        model_url=MODEL_URL.removesuffix("/chat/completions"),
        token_provider=lambda: "OFFLINE-native-model-token", model_transport=httpx.MockTransport(provider),
        capability_providers=(replace(real_provider, bind=bind),))
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    request = delegate_request("cancel-parent")
    assert harness.invoke(request).status_code == 202
    assert child_started.wait(8), "the real SDK child must reach its own model stream before cancellation"
    running = harness.call(harness.runtime.read, request.command.request_id)
    response = harness.invoke(NativeCancel(conversation_id=running.conversation_id, request_id=running.request_id))
    assert response.status_code == 200
    cancelled = harness.wait(running.request_id, "unknown")
    child = harness.call(bridges[0].read)
    assert cancelled.failure_code == "NATIVE_TURN_CANCELLED" and cancelled.result is None
    assert child.parent_request_id == cancelled.request_id and child.cancel_requested
    assert child.status == "cancelled" and child.stop_confirmed and child.child_cancelled and child.parent_idle
    assert transport_cancelled.is_set()
    usage = cancelled.usage.usage[0]
    assert usage.requests == 2 and usage.measured_requests == 1
    assert harness.invoke(request).json()["status"] == "unknown"
    assert len(requests) == 2 and len(bridges) == 1


@pytest.mark.parametrize("scenario", ["mode", "model", "prepare-read-unknown"])
def test_refused_or_unknown_native_child_never_dispatches_another_sdk_child(
    harness, monkeypatch, tmp_path, runtime_path, scenario,
):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError
    from shared_context.native_delegation import delegation_provider

    calls, bridges, failed_reads = [], [], []
    args = {**TASK, **({"mode": "background"} if scenario == "mode" else
                       {"model": "unauthorized-model"} if scenario == "model" else {})}
    read = FoundryStateStore.get_item

    async def unavailable_prepare(store, key, **kwargs):
        if scenario == "prepare-read-unknown" and key.startswith("fx:") and not failed_reads:
            failed_reads.append(key)
            raise ServiceResponseError("OFFLINE original child intent readback lost")
        return await read(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "get_item", unavailable_prepare)

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    async def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        assert "task" in {tool["function"]["name"] for tool in body.get("tools", ())}
        if len(calls) == 1:
            return sse(request, {"tool_calls": [{"index": 0, "id": "not-replayed-child", "type": "function",
                "function": {"name": "task", "arguments": json.dumps(args)}}]}, 1)
        assert len(calls) == 2
        assert any(message.get("role") == "tool" for message in body["messages"])
        return sse(request, {"content": "The requested child did not return a confirmed result; do not redispatch."}, 2)

    real_provider = delegation_provider()

    async def bind(context, grants):
        binding = await real_provider.bind(context, grants)
        bridges.append(binding.delegation_runtime)
        return binding

    executor = NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://offline.invalid",
        model_url=MODEL_URL.removesuffix("/chat/completions"),
        token_provider=lambda: "OFFLINE-native-model-token", model_transport=httpx.MockTransport(provider),
        capability_providers=(replace(real_provider, bind=bind),))
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    command = delegate_request("original-refused-parent")
    assert harness.invoke(command).status_code == 202
    expected = "unknown" if scenario == "prepare-read-unknown" else "completed"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        receipt = harness.call(harness.runtime.read, command.command.request_id)
        if receipt.status in {"unknown", "completed"}:
            break
        time.sleep(0.02)
    assert receipt.status == expected
    request_count = 1 if scenario == "prepare-read-unknown" else 2
    assert bridges[0].child_id is None and len(calls) == request_count
    assert receipt.usage.usage[0].requests == request_count
    assert bool(failed_reads) is (scenario == "prepare-read-unknown")
    assert harness.invoke(command).json()["status"] == receipt.status
    assert len(calls) == request_count and len(bridges) == 1


def test_native_child_failure_is_not_a_successful_parent_result_and_usage_stays_on_original_rid(
    harness, monkeypatch, tmp_path, runtime_path,
):
    from shared_context.native_delegation import delegation_provider

    requests, bridges = [], []

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    async def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse(request, {"tool_calls": [{"index": 0, "id": "original-empty-child", "type": "function",
                "function": {"name": "task", "arguments": json.dumps(TASK)}}]}, 1)
        if len(requests) == 2:
            assert not body.get("tools")
            return sse(request, {"content": ""}, 2)
        assert len(requests) == 3
        return sse(request, {"content": "Untrusted parent assertion that the child completed."}, 3)

    real_provider = delegation_provider()

    async def bind(context, grants):
        bound = await real_provider.bind(context, grants)
        bridges.append(bound.delegation_runtime)
        return bound

    executor = NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://offline.invalid",
        model_url=MODEL_URL.removesuffix("/chat/completions"),
        token_provider=lambda: "OFFLINE-native-model-token", model_transport=httpx.MockTransport(provider),
        capability_providers=(replace(real_provider, bind=bind),))
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    command = delegate_request("incomplete-child-parent")
    assert harness.invoke(command).status_code == 202
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = harness.call(harness.runtime.read, command.command.request_id)
        if result.status == "unknown":
            break
        time.sleep(0.02)
    original = harness.call(bridges[0].read)
    assert original.status == "incomplete" and original.failure_code == "DELEGATION_RESULT_INCOMPLETE"
    assert result.status == "unknown" and result.result is None
    assert result.failure_code == "DELEGATION_INCOMPLETE"
    assert result.usage.usage[0].requests == len(requests) == 3
    assert harness.invoke(command).json()["status"] == "unknown"
    assert len(requests) == 3 and len(bridges) == 1


@pytest.mark.parametrize("injection", ["completion-for-another-call", "result-for-another-parent"])
def test_a_child_result_injected_for_another_parent_never_completes_this_parent(
    harness, monkeypatch, tmp_path, runtime_path, injection,
):
    """CAP-01 C3: a completion naming another parent call, or a saved result naming another parent RID, is refused."""
    from shared_context.native_delegation import delegation_provider

    requests, bridges = [], []

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    async def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse(request, {"tool_calls": [{"index": 0, "id": "original-checked-child", "type": "function",
                "function": {"name": "task", "arguments": json.dumps(TASK)}}]}, 1)
        if len(requests) == 2:
            assert not body.get("tools")
            return sse(request, {"content": "Child result: remaining 530."}, 2)
        assert len(requests) == 3
        return sse(request, {"content": "Untrusted parent assertion that the child completed."}, 3)

    real_provider = delegation_provider()

    async def bind(context, grants):
        bound = await real_provider.bind(context, grants)
        runtime = bound.delegation_runtime
        if injection == "completion-for-another-call":
            original_observe = runtime.observe

            def observe(event):
                if event.get("type") == "subagent.completed":
                    data = dict(event.get("data") or {})
                    for key in ("tool_call_id", "toolCallId"):
                        if key in data:
                            data[key] = "another-parent-call"
                    event = {**event, "data": data}
                return original_observe(event)
            runtime.observe = observe
        else:
            original_call = runtime.call

            async def call(action, command):
                value = await original_call(action, command)
                if action == "observe" and getattr(command, "kind", None) == "result":
                    value = value.model_copy(update={"parent_request_id": "another-parent-rid"})
                return value
            runtime.call = call
        bridges.append(runtime)
        return bound

    executor = NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://offline.invalid",
        model_url=MODEL_URL.removesuffix("/chat/completions"),
        token_provider=lambda: "OFFLINE-native-model-token", model_transport=httpx.MockTransport(provider),
        capability_providers=(replace(real_provider, bind=bind),))
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    command = delegate_request("injected-parent")
    assert harness.invoke(command).status_code == 202
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = harness.call(harness.runtime.read, command.command.request_id)
        if result.status not in {"accepted", "started"}:
            break
        time.sleep(0.02)
    assert result.status == "unknown" and result.result is None, (result.status, result.failure_code)
    assert bridges[0].failure is not None
    assert bridges[0].failure.body["error"]["code"] == (
        "DELEGATION_OBSERVATION_INVALID" if injection == "completion-for-another-call" else "DELEGATION_UNKNOWN")
    assert len(bridges) == 1 and len(requests) <= 3
    assert harness.invoke(command).json()["status"] == "unknown"
    assert len(bridges) == 1


CITIES = {**TASK, "name": "cities", "description": "List the municipalities",
          "prompt": "List the five special municipalities of Taiwan."}
TEAS = {**TASK, "name": "teas", "description": "List the teas", "prompt": "List three Taiwanese teas."}
ANSWERS = {"municipalities": "Taipei, New Taipei, Taoyuan, Taichung, Tainan.", "teas": "Oolong, Pouchong, Oriental Beauty."}


def parallel_executor(harness, monkeypatch, tmp_path, runtime_path, provider, bridges, record_operations):
    from shared_context.native_delegation import delegation_provider

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)
    real_provider = delegation_provider()

    async def bind(context, grants):
        bound = await real_provider.bind(context, grants)
        bridges.append(bound.delegation_runtime)
        return bound

    executor = NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://offline.invalid",
        model_url=MODEL_URL.removesuffix("/chat/completions"),
        token_provider=lambda: "OFFLINE-native-model-token", model_transport=httpx.MockTransport(provider),
        capability_providers=(replace(real_provider, bind=bind),))
    executor.reader = harness.runtime
    if record_operations:
        async def unexpected(_view):
            pytest.fail("these original children must not need recovery")
        harness.runtime.on_continuation = unexpected
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight


def both_tasks(request, ordinal):
    return sse(request, {"tool_calls": [
        {"index": 0, "id": "call-cities", "type": "function", "function": {"name": "task", "arguments": json.dumps(CITIES)}},
        {"index": 1, "id": "call-teas", "type": "function", "function": {"name": "task", "arguments": json.dumps(TEAS)}},
    ]}, ordinal)


def child_answer(request, body, ordinal):
    text = json.dumps(body["messages"], ensure_ascii=False)
    return sse(request, {"content": ANSWERS["municipalities" if "municipalities" in text else "teas"]}, ordinal)


def tool_outputs(body):
    return {message["tool_call_id"]: json.dumps(message["content"], ensure_ascii=False)
            for message in body["messages"] if message.get("role") == "tool"}


def wait_terminal(harness, rid):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        receipt = harness.call(harness.runtime.read, rid)
        if receipt.status in {"unknown", "completed"}:
            return receipt
        time.sleep(0.02)
    pytest.fail("the native turn did not finish")


@pytest.mark.parametrize("record_operations", [False, True], ids=["legacy-watermarks", "recovery-watermarks"])
def test_two_parallel_task_calls_in_one_native_turn_each_complete_with_their_own_result(
    harness, monkeypatch, tmp_path, runtime_path, caplog, record_operations,
):
    """Stage B 2026-10-04: the second of two parallel task calls was refused as reserved or unknown."""
    parents, children, bridges = [], [], []

    async def provider(request):
        body = json.loads(request.content)
        if not body.get("tools"):
            children.append(body)
            return child_answer(request, body, 100 + len(children))
        parents.append(body)
        if len(parents) == 1:
            return both_tasks(request, 1)
        assert len(parents) == 2
        outputs = tool_outputs(body)
        assert set(outputs) == {"call-cities", "call-teas"}, outputs
        assert ANSWERS["municipalities"] in outputs["call-cities"] and ANSWERS["teas"] in outputs["call-teas"]
        return sse(request, {"content": "Both subtasks finished; merged answer."}, 2)

    parallel_executor(harness, monkeypatch, tmp_path, runtime_path, provider, bridges, record_operations)
    command = delegate_request("parallel-children-parent")
    assert harness.invoke(command).status_code == 202
    receipt = wait_terminal(harness, command.command.request_id)
    assert receipt.status == "completed", receipt.failure_code
    for call, answer in (("call-cities", ANSWERS["municipalities"]), ("call-teas", ANSWERS["teas"])):
        child = harness.call(bridges[0].read, call)
        assert (child.status, child.tool_call_id, child.result) == ("completed", call, answer)
    assert harness.call(bridges[0].read, "call-cities").child_agent_id != harness.call(
        bridges[0].read, "call-teas").child_agent_id
    assert len(children) == 2 and len(parents) == 2 and receipt.usage.usage[0].requests == 4
    assert "FA_DELEGATION_REFUSED" not in caplog.text
    assert harness.invoke(command).json()["status"] == "completed" and len(parents) == 2


@pytest.mark.parametrize("record_operations", [False, True], ids=["legacy-watermarks", "recovery-watermarks"])
def test_a_parallel_child_whose_reservation_is_unconfirmed_stays_unknown(
    harness, monkeypatch, tmp_path, runtime_path, record_operations,
):
    """Counterexample: only a really unresolved reservation keeps the unknown wording and an unknown turn."""
    from shared_context.errors import Problem
    from shared_context.native_effects import NativeEffects

    parents, children, bridges, reserved = [], [], [], []
    original_reserve = NativeEffects.reserve

    async def reserve(self, call_id, material, **kwargs):
        if kwargs.get("operation") is not None:
            return await original_reserve(self, call_id, material, **kwargs)
        if call_id == "call-teas":
            for _ in range(1000):
                if reserved:
                    break
                await asyncio.sleep(0.01)
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "effect reservation is unconfirmed; do not execute")
        claim = await original_reserve(self, call_id, material, **kwargs)
        reserved.append(call_id)
        return claim

    monkeypatch.setattr(NativeEffects, "reserve", reserve)

    async def provider(request):
        body = json.loads(request.content)
        if not body.get("tools"):
            children.append(body)
            return child_answer(request, body, 100 + len(children))
        parents.append(body)
        assert len(parents) == 1, "an unresolved reservation stops further parent model requests"
        return both_tasks(request, 1)

    parallel_executor(harness, monkeypatch, tmp_path, runtime_path, provider, bridges, record_operations)
    command = delegate_request("parallel-unknown-parent")
    assert harness.invoke(command).status_code == 202
    receipt = wait_terminal(harness, command.command.request_id)
    assert receipt.status == "unknown" and receipt.failure_code == "DELEGATION_UNKNOWN", receipt.failure_code
    # The unresolved tea reservation is never retried or replaced; the turn is not reported as complete.
    assert reserved == ["call-cities"] and len(parents) == 1
    assert harness.invoke(command).json()["status"] == "unknown" and len(parents) == 1


@pytest.mark.parametrize("record_operations", [False, True], ids=["legacy-watermarks", "recovery-watermarks"])
def test_a_task_call_denied_before_it_started_says_so_and_its_same_retry_runs(
    harness, monkeypatch, tmp_path, runtime_path, record_operations,
):
    """Stage B: the retry of a refused call was 'ambiguous' and the reply said the effect awaited human checking."""
    parents, children, bridges, checks = [], [], [], []

    async def provider(request):
        body = json.loads(request.content)
        if not body.get("tools"):
            children.append(body)
            return child_answer(request, body, 100 + len(children))
        parents.append(body)
        if len(parents) in {1, 2}:
            if len(parents) == 2:
                denied = tool_outputs(body)["call-teas"]
                assert "not started" in denied and "unresolved" not in denied and "真人" not in denied, denied
            return sse(request, {"tool_calls": [{"index": 0, "id": f"call-teas-{len(parents)}" if len(parents) == 2
                                                 else "call-teas", "type": "function",
                                                 "function": {"name": "task", "arguments": json.dumps(TEAS)}}]}, len(parents))
        assert ANSWERS["teas"] in tool_outputs(body)["call-teas-2"]
        return sse(request, {"content": "The retried tea subtask finished."}, 3)

    parallel_executor(harness, monkeypatch, tmp_path, runtime_path, provider, bridges, record_operations)
    real_bind = harness.runtime.executor.capability_providers[0].bind

    async def bind(context, grants):
        bound = await real_bind(context, grants)
        original = bound.delegation_runtime.current

        async def once_denied():
            checks.append(True)
            return False if len(checks) == 1 else await original()

        bound.delegation_runtime.current = once_denied
        return bound

    harness.runtime.executor.capability_providers = (replace(harness.runtime.executor.capability_providers[0], bind=bind),)
    command = delegate_request("retried-child-parent")
    assert harness.invoke(command).status_code == 202
    receipt = wait_terminal(harness, command.command.request_id)
    assert receipt.status == "completed", receipt.failure_code
    assert harness.call(bridges[0].read, "call-teas-2").result == ANSWERS["teas"]
    assert len(parents) == 3 and len(children) == 1
