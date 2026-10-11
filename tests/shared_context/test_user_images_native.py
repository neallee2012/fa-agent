"""B1 PA-08: images a human attaches to a native turn; offline stores and the pinned SDK 1.0.13 / runtime 1.0.83."""

import asyncio
import base64
import hashlib
import json
import struct
import zlib
from types import SimpleNamespace

import pytest

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_user_images import (
    NativeUserImageStored, NativeUserImageUpload, UserImage, canonical_png, stored,
)
from shared_context.errors import Problem
from test_m2_copilot import runtime_path, workspace  # noqa: F401
from test_teams_metrics import metrics  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_turns import local_env  # noqa: F401


def png(width=2, height=2, seed=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes((seed + x + y) % 256 for x in range(width * 3)) for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def encoded(seed=0):
    return base64.b64encode(png(seed=seed)).decode("ascii")


def reference(name="chart.png", seed=0):
    data = png(seed=seed)
    return UserImage(name=name, sha256=hashlib.sha256(data).hexdigest(), byte_length=len(data))


def test_an_instruction_carries_only_image_references_and_keeps_old_digests():
    plain = aw.InstructionSubmit(request_id="plain", content="Hello")
    assert "images" not in plain.model_dump(mode="json")
    attached = aw.InstructionSubmit(request_id="with-image", content="See the chart", images=(reference(),))
    assert attached.model_dump(mode="json")["images"][0]["media_type"] == "image/png"
    for bad in ((reference(), reference()), tuple(reference(f"i{n}.png", n) for n in range(5))):
        with pytest.raises(ValueError):
            aw.InstructionSubmit(request_id="bad", content="x", images=bad)


def test_only_one_canonical_png_within_the_screenshot_bound_is_uploaded():
    command = NativeUserImageUpload(png_base64=encoded())
    receipt = stored("agent-a", command.png_base64)
    assert receipt == NativeUserImageStored(agent_id="agent-a", sha256=hashlib.sha256(png()).hexdigest(),
                                            byte_length=len(png()))
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 64).decode("ascii")
    for bad in (jpeg, encoded() + "=", base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode("ascii")):
        with pytest.raises(ValueError):
            NativeUserImageUpload(png_base64=bad)
    assert canonical_png(command.png_base64) == png()
    # Within 2000 px the exact bytes reach the model; a larger side would be re-encoded by the runtime.
    NativeUserImageUpload(png_base64=base64.b64encode(png(2000, 1)).decode("ascii"))
    for width, height in ((2001, 1), (1, 2001)):
        with pytest.raises(ValueError):
            NativeUserImageUpload(png_base64=base64.b64encode(png(width, height)).decode("ascii"))


class _Item:
    def __init__(self, key, value):
        self.key, self.value, self.etag = key, value, "etag-" + key


class _Store:
    def __init__(self):
        self.items, self.creates, self.opened = {}, 0, []

    async def get_item(self, key):
        return self.items.get(key)

    async def create_item(self, key, value):
        from shared_context.native_turns import _CONFLICT
        self.creates += 1
        if key in self.items:
            raise _CONFLICT[0]("exists")
        self.items[key] = _Item(key, json.loads(json.dumps(value)))
        return self.items[key]


def runtime(store, agent_id="agent-a"):
    from contextlib import asynccontextmanager
    from shared_context.native_turns import NativeTurnRuntime

    @asynccontextmanager
    async def opened(name, *, create=False):
        assert name == "fa/v1/user-images"
        store.opened.append(create)
        yield store

    value = SimpleNamespace(identity=SimpleNamespace(agent_id=agent_id), _store=opened)
    value._immutable = lambda target, key, record: NativeTurnRuntime._immutable(value, target, key, record)
    return value


def request(images, *, supports=True):
    command = aw.InstructionSubmit(request_id="turn-1", content="See the chart", images=images)
    return SimpleNamespace(command=command, capture=SimpleNamespace(model=SimpleNamespace(supports_image_input=supports)))


def test_upload_is_content_addressed_and_a_turn_is_accepted_only_with_its_images(monkeypatch):
    from shared_context import native_user_images as images
    store = _Store()
    host = runtime(store)
    first = asyncio.run(images.upload(host, NativeUserImageUpload(png_base64=encoded())))
    assert asyncio.run(images.upload(host, NativeUserImageUpload(png_base64=encoded()))) == first
    assert list(store.items) == [images.user_image_key(first.sha256)] and store.opened == [True, True]
    asyncio.run(images.check_user_images(host, request((reference(),))))
    loaded = asyncio.run(images.turn_user_images(host, SimpleNamespace(agent_id="agent-a", command=request(
        (reference(),)).command)))
    assert loaded == ((reference(), encoded()),)
    for case, code in (((reference("other.png", 7),), True), ((reference(),), False)):
        with pytest.raises(Problem) as refused:
            asyncio.run(images.check_user_images(host, request(case, supports=code)))
        assert refused.value.body["error"]["code"] == ("NATIVE_IMAGE_MISSING" if code else "NATIVE_IMAGE_UNSUPPORTED")
    # Another Agent's item under the same digest is never used for this Agent's turn.
    with pytest.raises(Problem) as foreign:
        asyncio.run(images.check_user_images(runtime(store, "agent-b"), request((reference(),))))
    assert foreign.value.body["error"]["code"] == "NATIVE_IMAGE_INVALID"
    asyncio.run(images.check_user_images(host, SimpleNamespace(command=aw.InstructionSubmit(
        request_id="no-images", content="Hi"), capture=None)))


def test_the_job_lane_refuses_images_instead_of_dropping_them(metrics):
    from test_work_policy_lifecycle import enabled
    _, control, human, _ = enabled(metrics)
    with pytest.raises(Problem) as refused:
        control.submit(human, aw.InstructionSubmit(request_id="legacy-image", content="See", images=(reference(),)))
    assert refused.value.body["error"]["code"] == "WORK_POLICY_COMMAND"


def test_pinned_runtime_sends_attached_images_to_the_model_as_inline_png(workspace, runtime_path):
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.m2_copilot import run_copilot
    from test_m2_copilot import ScriptedEndpoint
    endpoint = ScriptedEndpoint([{"content": "Two charts seen."}])
    endpoint.unbounded = True

    async def reserve(identity):
        return None

    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="user-images-native",
        resume=False, prompt="Compare the two attached charts.", tools=[], api_url="https://context.invalid",
        token_provider=endpoint.token, on_event=lambda _: None, timeout_seconds=60, runtime_path=runtime_path,
        http_transport=endpoint.transport, instruction=True,
        user_images=(("a.png", encoded(0)), ("b.png", encoded(9))),
        behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True)))
    assert result["final_message"] == "Two charts seen."
    users = [message for message in endpoint.requests[0]["messages"] if message["role"] == "user"]
    images = [part["image_url"]["url"] for message in users if isinstance(message["content"], list)
              for part in message["content"] if part.get("type") == "image_url"]
    assert images == ["data:image/png;base64," + encoded(0), "data:image/png;base64," + encoded(9)]
    assert "Compare the two attached charts." in json.dumps(users)


def test_image_turns_keep_one_reply_reserve_in_the_saved_conversation():
    from shared_context.backup_models import MAX_BACKUP_BYTES
    from shared_context.behavior_sdk_files import LARGE_RESULT_RESERVE_BYTES
    from shared_context.native_user_images import require_state_room
    room = MAX_BACKUP_BYTES - LARGE_RESULT_RESERVE_BYTES
    require_state_room(room - 10, (10,))
    with pytest.raises(Problem) as full:
        require_state_room(room - 10, (11,))
    assert (full.value.status, full.value.body["error"]["code"]) == (409, "NATIVE_IMAGE_STATE_FULL")


def _native_case(local_env, monkeypatch, tmp_path, state_padding=0):
    from shared_context import native_sdk
    from shared_context.behavior_native_turn_wire import NativeTurnCapture
    from shared_context.native_turns import NativeTurnRuntime
    from test_native_capabilities import selected_capture
    from test_native_turns import IDENTITY
    seen = []

    async def sdk(**options):
        seen.append(options.get("user_images"))
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}})
            + "\n" + json.dumps({"type": "padding", "data": "x" * state_padding}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "Seen.", "sdk_stop": {"pid": 1, "exit_code": 0, "graceful": True,
                                                        "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native-turns", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token")
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    data = selected_capture().model_dump(mode="json")
    data["model"]["supports_image_input"] = True
    return runtime, NativeTurnCapture.model_validate_json(json.dumps(data)), seen


def test_a_native_turn_needs_its_uploaded_images_and_hands_them_to_the_sdk(local_env, monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from shared_context import native_sdk
    from shared_context.behavior_hosted import create_host
    from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
    from shared_context.native_turns import NativeTurnRuntime
    from test_native_capabilities import selected_capture
    from test_native_turns import Harness, IDENTITY
    seen = []

    async def sdk(**options):
        seen.append(options.get("user_images"))
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "Seen.", "sdk_stop": {"pid": 1, "exit_code": 0, "graceful": True,
                                                        "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native-turns", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid", model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token")
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    data = selected_capture().model_dump(mode="json")
    data["model"]["supports_image_input"] = True
    capture = NativeTurnCapture.model_validate_json(json.dumps(data))
    command = aw.InstructionSubmit(request_id="rid-image", content="See the chart", images=(reference(),))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, SimpleNamespace(controls={}), client)
        missing = h.invoke(NativeSubmit(command=command, capture=capture))
        assert missing.status_code == 409 and missing.json()["error"]["code"] == "NATIVE_IMAGE_MISSING"
        uploaded = h.invoke(NativeUserImageUpload(png_base64=encoded()))
        assert uploaded.status_code == 200 and uploaded.json()["sha256"] == reference().sha256
        assert h.invoke(NativeSubmit(command=command, capture=capture)).status_code == 202
        h.wait("rid-image", "completed")
    assert seen == [(("chart.png", encoded()),)]


def test_a_full_conversation_refuses_more_images_before_any_rid_or_model_call(local_env, monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from shared_context import native_user_images
    from shared_context.behavior_hosted import create_host
    from shared_context.behavior_native_turn_wire import NativeSubmit
    from test_native_turns import Harness, IDENTITY
    runtime, capture, seen = _native_case(local_env, monkeypatch, tmp_path, state_padding=5 * 1024 * 1024)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, SimpleNamespace(controls={}), client)
        assert h.invoke(NativeSubmit(command=aw.InstructionSubmit(request_id="rid-text", content="Hi"),
                                     capture=capture)).status_code == 202
        cid = h.wait("rid-text", "completed").conversation_id
        assert h.invoke(NativeUserImageUpload(png_base64=encoded())).status_code == 200
        follow = aw.InstructionSubmit(request_id="rid-full", content="See", conversation_id=cid, images=(reference(),))
        full = h.invoke(NativeSubmit(command=follow, capture=capture))
        assert full.status_code == 409 and full.json()["error"]["code"] == "NATIVE_IMAGE_STATE_FULL"
        # The same state grown by an earlier queued turn after acceptance: refused at run time, before the model.
        async def accepted(*_args):
            return None
        monkeypatch.setattr(native_user_images, "check_user_images", accepted)
        raced = aw.InstructionSubmit(request_id="rid-raced", content="See", conversation_id=cid, images=(reference(),))
        assert h.invoke(NativeSubmit(command=raced, capture=capture)).status_code == 202
        deadline = __import__("time").monotonic() + 8
        while (receipt := h.call(runtime.read, "rid-raced")).status not in {"completed", "unknown"}:
            assert __import__("time").monotonic() < deadline
            __import__("time").sleep(0.01)
        assert (receipt.status, receipt.failure_code) == ("unknown", "NATIVE_IMAGE_STATE_FULL")
    assert seen == [None]
