"""Manual native publication rejects mismatched source or codec before replacing a legal checkpoint."""

import asyncio
import json

import pytest

from shared_context import native_checkpoint
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_sdk_state import CheckpointProfile, NativeCheckpointScope, NativeCheckpointTurn, local_platform
from shared_context.behavior_wire import AssistantSdkStop
from shared_context.errors import Problem
from test_native_profiles import controlled_compaction
from test_native_turns import local_env
from test_sdk_compaction_product import ordinary


@pytest.mark.parametrize("changed", [
    "missing", "request", "capture", "base", "pid", "event", "profile-off", "unknown-codec", "downgrade", "raw-shape",
])
def test_native_manual_publication_preserves_last_legal_state_on_source_or_codec_error(local_env, changed):
    async def check():
        scope = NativeCheckpointScope(agent_id="agent", conversation_id="cid", sdk_session_id="sdk-native")
        profile = CheckpointProfile(platform=local_platform())  # SdkSessionFiles accepts only this host's
        stop = AssistantSdkStop(pid=123, exit_code=0)
        first = ordinary(scope.sdk_session_id)
        first_turn = NativeCheckpointTurn(
            scope=scope, request_id="rid-1", turn_number=1, capture_digest="a" * 64, profile=profile, previous=None)
        async with await native_checkpoint.open_durable_store(
                native_checkpoint.conversation_store_name("cid"), create=True) as store:
            first_ref = await native_checkpoint.save_checkpoint(store, scope, first, turn=first_turn)
            second_turn = NativeCheckpointTurn(
                scope=scope, request_id="rid-2", turn_number=2, capture_digest="b" * 64,
                profile=profile, previous=first_ref)
            files = SdkSessionFiles(scope.sdk_session_id, first, checkpoint_profile=profile)
            await controlled_compaction(files, second_turn)
            if changed == "raw-shape":
                events = [json.loads(line) for line in files.files["events.jsonl"].splitlines()]
                events[4]["data"]["unexpected_content"] = "Unknown data must not be silently dropped"
                files.files["events.jsonl"] = "".join(json.dumps(event) + "\n" for event in events)
                before = dict(files.files)
                with pytest.raises(Problem) as unknown_shape:
                    files.snapshot()
                assert unknown_shape.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
                assert files.files == before and files.previous == first
                assert await native_checkpoint.load_checkpoint(store, scope, first_ref) == first
                return
            compacted = files.snapshot()
            observation = files.compaction
            state, turn, execution = compacted, second_turn, observation
            if changed == "missing":
                execution = None
            elif changed in {"request", "capture", "base", "pid", "event"}:
                execution = observation.model_copy(update={
                    "request": {"request_id": "another-rid"}, "capture": {"capture_digest": "c" * 64},
                    "base": {"base_state_sha256": "c" * 64}, "pid": {"runtime_pid": 999},
                    "event": {"complete_event_id": "another-completion"},
                }[changed])
            elif changed == "profile-off":
                turn = second_turn.model_copy(update={"profile": None})
            elif changed == "unknown-codec":
                state = compacted.model_copy(update={"codec": "test-only-future-codec"})
            else:
                second_ref = await native_checkpoint.save_checkpoint(
                    store, scope, compacted, turn=second_turn, compaction=observation, sdk_stop=stop)
                turn = NativeCheckpointTurn(
                    scope=scope, request_id="rid-3", turn_number=3, capture_digest="d" * 64,
                    profile=profile, previous=second_ref)
                state, execution = first, None
            before = await store.list_keys()
            with pytest.raises(Problem) as refused:
                await native_checkpoint.save_checkpoint(store, scope, state, turn=turn, compaction=execution, sdk_stop=stop)
            assert refused.value.body["error"]["code"] in {"SDK_STATE_SOURCE_UNVERIFIED", "SDK_STATE_UNSUPPORTED_FORMAT"}
            assert await store.list_keys() == before
            assert await native_checkpoint.load_checkpoint(store, scope, first_ref) == first
            valid = await native_checkpoint.save_checkpoint(
                store, scope, compacted, turn=second_turn, compaction=observation, sdk_stop=stop)
            assert valid.source.execution.request_id == "rid-2"
            assert valid.lineage.previous_request_id == "rid-1"
            with pytest.raises(Problem) as disabled:
                await native_checkpoint.load_checkpoint(store, scope, valid)
            assert disabled.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
            unknown = valid.model_copy(update={"codec": "test-only-future-codec"})
            with pytest.raises(Problem) as unsupported:
                await native_checkpoint.load_checkpoint(store, scope, unknown, checkpoint_profile=profile)
            assert unsupported.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
            assert await native_checkpoint.verify_checkpoint(store, scope, valid, checkpoint_profile=profile) == compacted

    asyncio.run(check())
