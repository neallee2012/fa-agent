"""ADR-0009 3d-i: a native Responses conversation saved by the older known release continues on the pinned runtime.

``fixtures/native-responses-runtime-1.0.83/state`` is the local State store after one real turn:
``native_responses_checkpoint_local.py`` run in a tree of e672599d (merge base) with SDK 1.0.13 and runtime 1.0.83.
Its venv held this build's packages with only github-copilot-sdk at 1.0.13, which is that base's requirements-hosted.txt.
"""
import asyncio
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import httpx
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeResult, NativeSubmit
from shared_context.native_checkpoint import conversation_store_name, open_durable_store, verify_checkpoint
from shared_context.native_responses_wire import NATIVE_V2_PROFILE, NATIVE_V3_PROFILE
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from shared_context.sdk_pin import OLDER_RELEASES, RUNTIME_VERSION, SDK_VERSION
from native_responses_checkpoint_local import ANSWER, QUESTION, URL
from test_instruction_responses_native import response_bytes
from test_m2_copilot import runtime_path
from test_native_auto_compaction import isolated
from test_native_turn_wire import submit
from test_native_turns import IDENTITY

FIXTURE = Path(__file__).parent / "fixtures" / "native-responses-runtime-1.0.83"
FOLLOW_UP = "MARK-QUESTION-2: which shipment code did I give you?"
REPLY = "MARK-ANSWER-2: AZ-7731."


def test_a_responses_conversation_saved_by_runtime_1_0_83_continues_on_the_pinned_runtime(monkeypatch, runtime_path):
    root = isolated(monkeypatch, "cross-version-responses")
    shutil.copytree(FIXTURE / "state", root / "state")
    inputs, stops = [], []

    def provider(request):
        assert str(request.url) == URL + "/responses"
        inputs.append(json.dumps(json.loads(request.content)["input"], ensure_ascii=False))
        assert len(inputs) == 1, "one model exchange is planned"
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                              content=response_bytes(2, text=REPLY))

    async def token():
        return "OFFLINE-native-MI"

    class ObserveStop(NativeSdkExecutor):
        async def __call__(self, *args):
            outcome = await super().__call__(*args)
            stops.append(outcome.sdk_stop)
            return outcome

    executor = ObserveStop(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                           model_url=URL, token_provider=token, model_transport=httpx.MockTransport(provider))
    # The v3 image; the API still sends v2 (header alias and body label).
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight, state_profile=NATIVE_V3_PROFILE)
    executor.reader = runtime

    async def checkpoint(receipt):
        meta, _ = await runtime._meta(receipt.conversation_id)
        async with await open_durable_store(conversation_store_name(receipt.conversation_id), create=False) as store:
            ref = (await runtime._turn(store, receipt)).checkpoints[0]
            return ref, await verify_checkpoint(store, runtime._scope(meta), ref,
                                                checkpoint_profile=receipt.capture.template.settings.checkpoint_profile)

    async def finished(rid):
        while (value := await runtime.read(rid)).status not in {"completed", "unknown", "busy_refused"}:
            await asyncio.sleep(0.02)
        return value

    async def bounded():
        return await asyncio.wait_for(finished("rid-2"), timeout=120)

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        first = client.portal.call(runtime.read, "rid-1")
        old_ref, old_state = client.portal.call(checkpoint, first)
        assert (old_ref.sdk_version, old_ref.runtime_version) in OLDER_RELEASES
        assert old_ref.codec == "native-session-fs-responses-v1"
        saved = old_state.encoded().decode()
        assert ANSWER in saved
        assert not any(canary in saved for canary in ("PRIVATE_ENCRYPTED_REASONING_CANARY", "OFFLINE-native-MI", "nx-native-ckpt"))

        request = NativeSubmit(command=submit(rid="rid-2", cid=first.conversation_id, text=FOLLOW_UP).command,
                               capture=first.capture, state_profile=NATIVE_V2_PROFILE)
        accepted = client.post("/invocations", json=request.model_dump(mode="json"), headers={
            "x-client-fa-state-profile": NATIVE_V2_PROFILE, "x-agent-session-id": IDENTITY.session_id,
            "x-agent-invocation-id": "OFFLINE-platform-2", "x-agent-foundry-call-id": "OFFLINE-call-2"})
        assert accepted.status_code == 202, accepted.text
        receipt = client.portal.call(bounded)
        assert receipt.status == "completed", receipt.failure_code
        assert (receipt.conversation_id, receipt.turn_number) == (first.conversation_id, 2)
        result = client.portal.call(runtime.result, NativeResult(conversation_id=receipt.conversation_id,
                                                                 request_id="rid-2"))
        assert result.text == REPLY and result.complete
        new_ref, _ = client.portal.call(checkpoint, receipt)
        assert client.portal.call(checkpoint, first)[0] == old_ref
    # The earlier turn reached the model from the restored state, not from a fresh session.
    assert QUESTION in inputs[0] and ANSWER in inputs[0] and FOLLOW_UP in inputs[0]
    assert stops[0].runtime_version == RUNTIME_VERSION
    assert (new_ref.sdk_version, new_ref.runtime_version, new_ref.codec) == (
        SDK_VERSION, RUNTIME_VERSION, "native-session-fs-responses-v1")
