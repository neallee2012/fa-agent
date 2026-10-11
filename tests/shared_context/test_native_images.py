"""Native screenshot delivery against the official local store; SDK/image callback is an explicit double."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context import native_sdk
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSubmit
from shared_context.native_turns import NativeTurnRuntime
from test_assistant_images import png
from test_m2_copilot import runtime_path
from test_native_capabilities import selected_capture
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, harness, local_env, native_retry_policy


def image_turn(monkeypatch, tmp_path, calls, *, parallel_tail=0):
    from shared_context.native_images import image_delivery_provider, read_image, read_images
    from shared_context.native_image_wire import NativeImageRead, NativeImagesRead, NativeImageReceipt

    outputs, executions = [], []

    async def sdk(**options):
        executions.append(options["sdk_session_id"])
        tool = next(tool for tool in options["tools"] if tool.name == "deliver_image")
        async def call_tool(index, call):
            if call.get("image") is not None:
                options["on_tool_image"](base64.b64encode(call["image"]).decode("ascii"))
            try:
                result = await tool.handler({
                    "session_id": options["sdk_session_id"], "tool_call_id": f"image-call-{index}",
                    "tool_name": "deliver_image", "arguments": call.get("arguments", {})})
                assert options["behavior_boundary"].tool_result(tool.name, result.text) == result.text
                return NativeImageReceipt.model_validate_json(result.text)
            except native_sdk.ToolFailure as error:
                return error.public_code
        pending = []
        for index, call in enumerate(calls):
            if index < len(calls) - parallel_tail:
                outputs.append(await call_tool(index, call))
            else:
                pending.append(asyncio.create_task(call_tool(index, call)))
                await asyncio.sleep(0)
        outputs.extend(await asyncio.gather(*pending))
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n"})
        return {"final_message": "The original screenshot is saved for this conversation's owner.",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-model.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-model", capability_providers=(image_delivery_provider(),))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    command = NativeSubmit(command=submit("native-image-rid").command, capture=selected_capture("fa-image-delivery-v1"))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        assert harness.invoke(command).status_code == 202
        original = harness.wait(command.command.request_id, "completed")
        scope = dict(conversation_id=original.conversation_id, request_id=original.request_id)
        listing = harness.call(read_images, runtime, NativeImagesRead(**scope))
        images = tuple(harness.call(read_image, runtime, NativeImageRead(**scope, version_id=entry.version_id))
                       for entry in listing.entries)
        assert harness.invoke(command).json()["status"] == "completed" and len(executions) == 1
        return SimpleNamespace(outputs=outputs, listing=listing, images=images, original=original)


def test_same_original_screenshot_is_saved_once_and_read_as_complete_original_png(
    local_env, monkeypatch, tmp_path,
):
    data = png(8)
    digest = hashlib.sha256(data).hexdigest()
    turn = image_turn(monkeypatch, tmp_path, [{"image": data}, {}])
    assert len(turn.outputs) == 2 and turn.outputs[0] == turn.outputs[1]
    assert [(entry.number, entry.sha256, entry.byte_length) for entry in turn.listing.entries] == [(1, digest, len(data))]
    assert turn.images[0].data == data and turn.images[0].entry == turn.listing.entries[0]
    assert turn.listing.submit_sha256 == turn.original.submit_digest and turn.listing.agent_id == turn.original.agent_id
    assert not {"job_id", "binding_id", "source_work"}.intersection(json.loads(turn.outputs[0].model_dump_json()))


@pytest.mark.parametrize("phase", ["png", "list"])
def test_lost_state_store_ack_returns_original_image_without_another_copy(local_env, monkeypatch, tmp_path, phase):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError

    create = FoundryStateStore.create_item
    stored = []

    async def lose(store, key, value, **kwargs):
        result = await create(store, key, value, **kwargs)
        if key.startswith("img:") or key.startswith("images:"):
            stored.append(key)
        if key.startswith("img:" if phase == "png" else "images:"):
            raise ServiceResponseError("OFFLINE original State store acknowledgment lost")
        return result

    monkeypatch.setattr(FoundryStateStore, "create_item", lose)
    data = png(4)
    turn = image_turn(monkeypatch, tmp_path, [{"image": data}, {}])
    assert turn.outputs[0] == turn.outputs[1] and len(turn.images) == 1 and turn.images[0].data == data
    assert len(stored) == len(set(stored)) == 2


def test_four_original_images_are_kept_but_the_fifth_is_not_written(local_env, monkeypatch, tmp_path):
    from azure.ai.agentserver.core.storage import FoundryStateStore

    create, stored = FoundryStateStore.create_item, []

    async def track(store, key, value, **kwargs):
        result = await create(store, key, value, **kwargs)
        if key.startswith("img:"):
            stored.append(key)
        return result

    monkeypatch.setattr(FoundryStateStore, "create_item", track)
    images = [png(number) for number in range(5)]
    turn = image_turn(monkeypatch, tmp_path, [{"image": image} for image in images] + [{"image": images[0]}])
    assert [item.data for item in turn.images] == images[:4]
    assert turn.outputs[4] == "IMAGE_DELIVERY_LIMIT" and turn.outputs[5] == turn.outputs[0]
    assert len(stored) == 4


def test_concurrent_deliveries_cannot_publish_a_fifth_image_or_erase_earlier_images(local_env, monkeypatch, tmp_path):
    images = [png(number) for number in range(5)]
    turn = image_turn(monkeypatch, tmp_path, [{"image": image} for image in images], parallel_tail=2)
    assert [image.data for image in turn.images[:3]] == images[:3]
    assert len(turn.images) == 4 and turn.images[3].data in images[3:]
    assert len([output for output in turn.outputs if not isinstance(output, str)]) == 4
    assert any(output in {"IMAGE_DELIVERY_LIMIT", "IMAGE_DELIVERY_UNKNOWN"}
               for output in turn.outputs if isinstance(output, str))


@pytest.mark.parametrize("call,code", [
    ({}, "IMAGE_DELIVERY_NO_IMAGE"),
    ({"image": b"not a PNG screenshot"}, "IMAGE_DELIVERY_INVALID"),
    ({"image": png(2), "arguments": {"path": "another-agent.png"}}, "CAPABILITY_ARGUMENTS"),
])
def test_missing_or_model_supplied_image_material_never_publishes_a_screenshot(
    local_env, monkeypatch, tmp_path, call, code,
):
    turn = image_turn(monkeypatch, tmp_path, [call])
    assert turn.outputs == [code] and turn.listing.entries == () and turn.images == ()


def test_maximum_png_stays_below_one_state_store_item_and_is_read_without_truncation(
    local_env, monkeypatch, tmp_path,
):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from shared_context.assistant_image_wire import MAX_IMAGE_BYTES

    create, sizes = FoundryStateStore.create_item, []

    async def track(store, key, value, **kwargs):
        if key.startswith("img:"):
            sizes.append(len(json.dumps(value).encode("utf-8")))
        return await create(store, key, value, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "create_item", track)
    small = png(1)
    data = small + b"\0" * (MAX_IMAGE_BYTES - len(small))
    turn = image_turn(monkeypatch, tmp_path, [{"image": data}])
    assert turn.images[0].data == data and turn.listing.entries[0].byte_length == 512 * 1024
    assert len(sizes) == 1 and sizes[0] < 1_000_000


@pytest.mark.parametrize("wire_api,selected", [
    ("completions", True), ("responses", True), ("completions", False),
], ids=["completions", "responses", "not-selected"])
def test_real_sdk_image_callback_reaches_native_store_and_does_not_leak_into_the_next_turn(
    harness, monkeypatch, tmp_path, runtime_path, wire_api, selected,
):
    from copilot import CopilotClient
    import httpx
    from shared_context import m2_copilot
    from shared_context.behavior_capabilities import CapabilityGrant
    from shared_context.behavior_native_turn_wire import NativeTurnCapture
    from shared_context.native_image_wire import NativeImageContent, NativeImageRead, NativeImagesRead, NativeTurnImages
    from shared_context.tool_catalog import StdioEntry
    from sdk_checkpoint_local import sse
    from test_instruction_responses_native import response_bytes
    from test_native_local_capabilities import with_grants
    from test_skill_isolation_native import child_environment

    requests = []

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    async def model(request):
        body = json.loads(request.content)
        requests.append(body)
        number = len(requests)
        names = {entry["name"] if wire_api == "responses" else entry["function"]["name"]
                 for entry in body.get("tools", ())}
        assert "playwright-browser_take_screenshot" in names
        assert ("deliver_image" in names) is selected
        tool = ("playwright-browser_take_screenshot" if number == 1 else
                "deliver_image" if selected and number in {2, 3, 5} else None)
        if number == 6:
            assert "No screenshot from this turn" in json.dumps(body["input"] if wire_api == "responses" else body["messages"])
        assert number <= 6
        text = "Original screenshot stored." if number == 4 else "No old screenshot was reused."
        if wire_api == "responses":
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  content=response_bytes(number, tool=tool, arguments={}, text=text if tool is None else None))
        delta = {"content": text} if tool is None else {"tool_calls": [{
            "index": 0, "id": f"image-tool-{number}", "type": "function",
            "function": {"name": tool, "arguments": "{}"}}]}
        return sse(request, delta, number)

    browser = CapabilityGrant(
        entry_id="playwright", entry_sha256="0" * 64, kind="mcp-hosted",
        tools=("playwright-browser_take_screenshot",), vision=True,
        launch=StdioEntry(type="stdio", command=sys.executable,
            args=(str(Path(__file__).with_name("fake_playwright_stdio_server.py")),),
            env=(("PYTHONPATH", os.environ["PYTHONPATH"]),)))
    entries = ("fa-image-delivery-v1",) if selected else ()
    values = with_grants(*entries, browser=browser).model_dump()
    values["model"] = {"id": "gpt-5.4-mini" if wire_api == "responses" else "gpt-4.1-mini",
                       "display_name": "Offline image model", "supports_image_input": True}
    values["template"]["settings"]["model_provider"] = "openai-responses" if wire_api == "responses" else None
    capture = NativeTurnCapture.model_validate(values)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
        model_url="https://offline-model.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-model", model_transport=httpx.MockTransport(model),
        capability_providers=native_sdk.native_capability_providers("https://api.offline.invalid", None))
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    command = NativeSubmit(command=submit("real-native-image").command, capture=capture)
    assert harness.invoke(command).status_code == 202
    original = harness.wait(command.command.request_id, "completed")
    scope = dict(conversation_id=original.conversation_id, request_id=original.request_id)
    listing = NativeTurnImages.model_validate_json(harness.invoke(NativeImagesRead(**scope)).content)
    if not selected:
        assert listing.entries == () and original.usage.usage[0].requests == 2 and len(requests) == 2
        return
    assert len(listing.entries) == 1 and original.usage.usage[0].requests == 4
    content = NativeImageContent.model_validate_json(
        harness.invoke(NativeImageRead(**scope, version_id=listing.entries[0].version_id)).content)
    assert content.data.startswith(b"\x89PNG\r\n\x1a\n") and hashlib.sha256(content.data).hexdigest() == listing.entries[0].sha256
    followup = NativeSubmit(command=submit("next-image-turn", original.conversation_id).command, capture=capture)
    assert harness.invoke(followup).status_code == 202
    second = harness.wait(followup.command.request_id, "completed")
    empty = NativeTurnImages.model_validate_json(harness.invoke(NativeImagesRead(
        conversation_id=second.conversation_id, request_id=second.request_id)).content)
    assert empty.entries == () and second.usage.usage[0].requests == 2 and len(requests) == 6
    assert NativeTurnImages.model_validate_json(harness.invoke(NativeImagesRead(**scope)).content) == listing
