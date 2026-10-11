"""SDK/prompt and original-reader seams; no cloud, model or external effects."""

import json
import asyncio

import httpx
import pytest

from shared_context import native_sdk
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
from shared_context.m2_copilot import ToolFailure
from test_native_routine_turns import routine_request
from test_native_responses import response_submit, responses
from test_native_turn_wire import submit
from test_native_turns import harness, local_env, native_retry_policy


async def sdk_answer(options):
    options["session_files"].files.update({
        "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
        "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
    })
    return {"final_message": "OFFLINE original answer",
            "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}


def test_only_the_scheduled_sdk_input_carries_its_original_occurrence_and_started_state(
        harness, monkeypatch, tmp_path):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    seen = []

    async def sdk(**options):
        seen.append(options)
        return await sdk_answer(options)

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "context", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE", routine_client=routine_client())
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    assert harness.invoke(submit("human-first")).status_code == 202
    first = harness.wait("human-first", "completed")
    request = routine_request(first.conversation_id)
    assert harness.invoke(request).status_code == 202
    scheduled = harness.wait(request.command.request_id, "completed")
    assert "routine_source" not in seen[0]
    source = seen[1]["routine_source"]
    assert source.routine == request.routine and source.request_id == request.command.request_id
    assert source.status == "started" and source.authorization is False
    assert seen[1]["prompt"] == "Read the original work."
    parts = {part.name: part for part in scheduled.observed_assembly.parts}
    assert parts["routines"].state == "configured"
    assert parts["routines"].source_sha256 == "a" * 64


def routine_client(active=lambda: True, *, checked=None):
    from shared_context.native_routine_client import NativeRoutineClient

    def reply(request):
        if request.url.path == "/work-policy/v1/native-capabilities/routine/reminders":
            return httpx.Response(200, json={
                "purpose": "attention-only-not-turn-dispatch", "reminders": [], "next_after": None})
        assert request.url.path == "/work-policy/v1/native-capabilities/routine/check"
        if checked is not None:
            checked.append(json.loads(request.content))
        if not active():
            return httpx.Response(403, json={"error": {"code": "NATIVE_ROUTINE_INACTIVE"}})
        body = json.loads(request.content)
        return httpx.Response(200, json={**body, "definition_active": True, "authorization": False,
                                        "purpose": "routine-version-current-not-operation-approval"})

    return NativeRoutineClient("https://api.offline.invalid", lambda: "OFFLINE-agent",
                               transport=httpx.MockTransport(reply))


def test_routine_checks_entry_once_and_keeps_running_after_a_future_definition_change(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    active, observed, checked = [True], [], []

    async def sdk(**options):
        if options.get("routine_source") is not None:
            tool = next(item for item in options["tools"] if item.name == "read_context")
            packet = {"arguments": {}, "session_id": options["sdk_session_id"], "tool_call_id": "read-before-stop"}
            await tool.handler(packet)
            active[0] = False
            result = await tool.handler({**packet, "tool_call_id": "read-after-definition-change"})
            assert json.loads(result.text)["turn_origin"]["source_kind"] == "routine"
            await options["behavior_boundary"].before_request("model-after-definition-change")
            observed.append("continued-original-turn")
        return await sdk_answer(options)

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "withdrawal", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE", routine_client=routine_client(lambda: active[0], checked=checked))
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    values = submit("human-first").capture.model_dump(mode="json")
    values["template"]["settings"]["turn_origin_profile"] = "fa-instruction-context-origin-v1"
    selected = NativeTurnCapture.model_validate_json(json.dumps(values))
    assert harness.invoke(NativeSubmit(command=submit("human-first").command, capture=selected)).status_code == 202
    first = harness.wait("human-first", "completed")
    request = routine_request(first.conversation_id).model_copy(update={"capture": selected})
    assert harness.invoke(request).status_code == 202
    harness.wait(request.command.request_id, "completed")
    assert observed == ["continued-original-turn"]
    assert len(checked) == 1 and checked[0]["request_id"] == request.command.request_id


def test_work_context_reads_the_original_rid_instead_of_inventing_a_new_work(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    observed = []

    async def sdk(**options):
        tool = next((item for item in options["tools"] if item.name == "read_work_context"), None)
        if tool is None:
            observed.append(None)
        else:
            result = await tool.handler({
                "arguments": {}, "session_id": options["sdk_session_id"], "tool_call_id": "read-original-work"})
            observed.append(json.loads(result.text))
        return await sdk_answer(options)

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "work-context", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE", routine_client=routine_client())
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    assert harness.invoke(submit("human-first", text="Keep working on the original report.")).status_code == 202
    completed = harness.wait("human-first", "completed")
    assert observed[0] is not None
    assert observed[0]["request_id"] == "human-first"
    assert observed[0]["conversation_id"] == completed.conversation_id
    assert observed[0]["data"]["status"] == "started"
    assert observed[0]["data"]["content"] == "Keep working on the original report."
    assert observed[0]["data"]["source_kind"] == "human"
    assert observed[0]["authorization"] is False


def test_long_form_style_is_loaded_on_request_not_injected_into_short_answers(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    observed, prepared = [], []

    async def sdk(**options):
        prepared.append(options["system_message"] + options["prompt"])
        if options["prompt"] == "Write a long-form report using the supplied synthetic facts.":
            tool = next((item for item in options["tools"] if item.name == "read_writing_style"), None)
            if tool is not None:
                value = await tool.handler({
                    "arguments": {}, "session_id": options["sdk_session_id"], "tool_call_id": "read-long-form-style"})
                observed.append(json.loads(value.text))
        return await sdk_answer(options)

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "writing", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE", routine_client=routine_client())
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    assert harness.invoke(submit("short", text="What is 2 + 2?")).status_code == 202
    first = harness.wait("short", "completed")
    assert harness.invoke(submit(
        "long", first.conversation_id, text="Write a long-form report using the supplied synthetic facts.")).status_code == 202
    long = harness.wait("long", "completed")
    assert len(observed) == 1
    assert observed[0]["request_id"] == "long" and observed[0]["purpose"] == "this-human-request-only"
    assert observed[0]["body"] not in prepared[0] and observed[0]["body"] not in prepared[1]
    parts = {part.name: part for part in long.observed_assembly.parts}
    assert parts["writing_style"].state == "read"
    assert {part.name: part for part in first.observed_assembly.parts}["writing_style"].state == "omitted"


@pytest.mark.parametrize("lane", ["foundry-native-v1", "foundry-responses-v1"])
def test_both_native_lanes_reuse_the_original_routine_rid_and_executor(responses, monkeypatch, tmp_path, lane):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    seen = []

    async def sdk(**options):
        source = options.get("routine_source")
        seen.append(None if source is None else source.request_id)
        return await sdk_answer(options)

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "both-lanes", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE", routine_client=routine_client())
    executor.reader = responses.runtime
    responses.runtime.executor, responses.runtime.preflight = executor, executor.preflight
    first_request = response_submit("human-first") if lane == "foundry-responses-v1" else submit("human-first")
    assert responses.invoke(first_request).status_code == 202
    if lane == "foundry-responses-v1":
        assert responses.respond(responses.call(responses.runtime.read, "human-first")).status_code == 200
    first = responses.wait("human-first", "completed")
    request = routine_request(first.conversation_id).model_copy(update={"capture": first.capture, "state_profile": lane})
    assert responses.invoke(request).status_code == 202
    if lane == "foundry-responses-v1":
        assert responses.respond(responses.call(responses.runtime.read, request.command.request_id)).status_code == 200
    original = responses.wait(request.command.request_id, "completed")
    assert original.routine == request.routine and original.turn_number == 2
    assert responses.invoke(request).json()["submit_digest"] == original.submit_digest
    assert seen == [None, request.command.request_id]


def test_cancelled_routine_observation_is_not_injected_as_unfinished_work(harness, monkeypatch, tmp_path):
    from shared_context.native_routine_client import NativeRoutineClient

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    prepared = []

    def api(request):
        query = json.loads(request.content)
        return httpx.Response(200, json={
            "purpose": "attention-only-not-turn-dispatch", "reminders": [], "next_after": None,
            "routine_turns": [{
                "routine_id": "cancelled-routine", "scheduled_for": "2026-10-04T01:00:00Z",
                "request_id": "cancelled-original-rid", "conversation_id": query["conversation_id"],
                "status": "unknown", "failure_code": "NATIVE_TURN_CANCELLED",
                "authorization": False, "purpose": "original-native-routine-receipt",
            }]})

    async def sdk(**options):
        prepared.append(options["system_message"] + options["prompt"])
        return await sdk_answer(options)

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    client = NativeRoutineClient("https://api.offline.invalid", lambda: "OFFLINE-agent", transport=httpx.MockTransport(api))
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "no-revival", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE", routine_client=client)
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    assert harness.invoke(submit("new-question", text="Answer this new question.")).status_code == 202
    harness.wait("new-question", "completed")
    assert "cancelled-original-rid" not in prepared[0]
    assert "## Original work reminders" not in prepared[0]
    assert "could not be read" not in prepared[0]


@pytest.mark.parametrize("status", [401, 429])
def test_temporary_entry_failure_is_not_reported_as_a_disabled_definition(status):
    from shared_context.errors import Problem
    from shared_context.native_routine_client import NativeRoutineClient
    from test_native_routine_wire import routine_origin

    client = NativeRoutineClient(
        "https://api.offline.invalid", lambda: "OFFLINE-agent",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": {"code": "OFFLINE"}})))
    with pytest.raises(Problem) as error:
        asyncio.run(client.check(routine_origin()))
    assert error.value.status == 503
    assert error.value.body["error"]["code"] == "NATIVE_ROUTINE_UNCONFIRMED"
