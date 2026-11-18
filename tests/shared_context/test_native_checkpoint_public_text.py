"""Legal public text in a native manual checkpoint is saved, answered and restored; it is never taken for private.

Native counterpart of the legacy product-archive cases in test_sdk_compaction_product.py: the real native turn
runtime and State store, with only the SDK run replaced by an offline double (as in test_native_profiles.py).
"""
import hashlib
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context import behavior_sdk_files, native_sdk
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
from shared_context.behavior_sdk_compaction import events_from
from shared_context.native_turns import NativeTurnRuntime
from shared_context.sdk_pin import RUNTIME_VERSION
from sdk_compaction_product_helpers import PUBLIC_CHECKPOINT_TEXT, event, ordinary, public_checkpoint_content
from test_native_profiles import controlled_compaction, profile_capture
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, local_env


def assert_native_public_checkpoint_roundtrip(monkeypatch, tmp_path, payload, location):
    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    data = profile_capture().model_dump(mode="json")
    data["template"]["settings"]["model_provider"] = None
    data["template"]["settings"]["checkpoint_profile"]["provider"] = "openai-completions"
    selected = NativeTurnCapture.model_validate_json(json.dumps(data))
    seen, states, originals = [], [], []

    async def sdk(**options):
        turn, files = options["native_checkpoint_turn"], options["session_files"]
        seen.append(turn)
        options["sdk_start_gate"].enter(options["sdk_session_id"])
        answer = "Original inventory response."
        if turn.turn_number == 1:
            files.files = dict(ordinary(files.session_id).files)
        elif turn.turn_number == 2:
            await controlled_compaction(files, turn)
            public_checkpoint_content(SimpleNamespace(fs=files), payload, location)
            answer = payload
        else:
            files.files["events.jsonl"] += json.dumps(event(
                "user.message", "next-public-content", {"content": "Continue normally."},
                events_from(files.files)[-1]["id"])) + "\n"
        states.append(files)
        originals.append(dict(files.files))
        return {"final_message": answer,
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": RUNTIME_VERSION}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token",
    )
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    receipts = []
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        cid = None
        for index in range(1, 4):
            assert harness.invoke(NativeSubmit(command=submit(f"rid-{index}", cid).command,
                                               capture=selected)).status_code == 202
            # A legal public text that made the checkpoint unavailable would end rid-2 unknown, not completed.
            receipts.append(harness.wait(f"rid-{index}", "completed"))
            cid = receipts[-1].conversation_id
    saved = receipts[1].result
    assert states[1].files == originals[1], "saving must not rewrite the SDK files"
    assert saved.text_sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    restored = states[2].previous
    assert restored.sha256 == saved.checkpoint_sha256 == seen[2].previous.sha256
    assert any(payload in value["data"].get("summaryContent" if location == "summary" else "content", "")
               for value in events_from(restored.files))
    assert seen[2].previous.source.execution == states[1].compaction
    assert (seen[2].previous.lineage.request_id, seen[2].previous.lineage.previous_request_id) == ("rid-2", "rid-1")
    assert seen[1].previous.sha256 == receipts[0].result.checkpoint_sha256
    assert states[2].compaction is None


PAYLOAD_IDS = ("role-json", "private-key-json", "yaml-looking", "deep-public-json", "u2028", "u2029", "u0085")
# Inside the summary markup the JSON/YAML-looking payloads are not a whole string or line, so no structure or YAML
# check can see them there; only the line separators reach a summary rule (JSONL splitting).
CASES = [(payload, location, f"{location}-{name}")
         for location in ("user.message", "assistant.message", "tool.execution_complete", "summary")
         for name, payload in zip(PAYLOAD_IDS, PUBLIC_CHECKPOINT_TEXT)
         if location != "summary" or name.startswith("u")]


@pytest.mark.parametrize("payload,location", [case[:2] for case in CASES], ids=[case[2] for case in CASES])
def test_native_public_text_survives_checkpoint_final_answer_and_restore(
        local_env, monkeypatch, tmp_path, payload, location):
    assert_native_public_checkpoint_roundtrip(monkeypatch, tmp_path, payload, location)


@pytest.mark.parametrize("location", ["assistant.message", "summary"])
def test_native_long_malformed_escape_content_preserves_answer_and_checkpoints(
        local_env, monkeypatch, tmp_path, location):
    payload = ('\\"' * 32768) + "\n"
    assert len(payload.encode("utf-8")) == 65537
    assert_native_public_checkpoint_roundtrip(monkeypatch, tmp_path, payload, location)
