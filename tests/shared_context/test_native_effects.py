"""The original native effect key survives a lost outcome; no model or external provider."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeCancel
from shared_context.errors import Problem
from shared_context.native_turns import NativeTurnRuntime
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env


def test_effect_once_returns_original_done_and_never_reexecutes_unknown(local_env):
    from shared_context.native_effects import NativeEffects

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    calls = []

    async def effect():
        calls.append("sent")
        return {"original": "saved"}

    async def uncertain():
        calls.append("unknown")
        raise OSError("OFFLINE response lost after send")

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        harness.invoke(submit())
        receipt = harness.wait("rid-1", "completed")

        async def check():
            effects = NativeEffects(runtime, receipt)
            assert await effects.once("call-1", {"body": "same"}, effect) == {"original": "saved"}
            assert await effects.once("call-1", {"body": "same"}, effect) == {"original": "saved"}
            with pytest.raises(Problem) as changed:
                await effects.once("call-1", {"body": "different"}, effect)
            assert changed.value.body["error"]["code"] == "NATIVE_EFFECT_CONFLICT"
            with pytest.raises(OSError):
                await effects.once("call-2", {"body": "same"}, uncertain)
            with pytest.raises(Problem) as unknown:
                await NativeEffects(runtime, receipt).once("call-2", {"body": "same"}, effect)
            assert unknown.value.body["error"]["code"] == "NATIVE_EFFECT_UNKNOWN"
            pending = await effects.read("call-2", {"body": "same"})
            assert pending.status == "pending"

        client.portal.call(check)
    assert calls == ["sent", "unknown"]


def test_original_effect_observations_survive_cancel_without_allowing_new_effects(local_env):
    from shared_context.native_effects import NativeEffects

    sdk = OfflineSdk()
    sdk.block = True
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        assert harness.invoke(submit("original-observed-rid")).status_code == 202
        receipt = harness.wait("original-observed-rid", "started")
        effects = NativeEffects(runtime, receipt, sdk.controls[receipt.request_id])
        claim = client.portal.call(effects.reserve, "original-task-call", {"arguments": "fixed child"})
        assert claim.fresh
        assert harness.invoke(NativeCancel(
            conversation_id=receipt.conversation_id, request_id=receipt.request_id)).status_code == 200
        harness.wait(receipt.request_id, "unknown")

        async def check():
            current = claim
            with pytest.raises(Problem) as refused:
                await effects.reserve("another-call", {"arguments": "not approved after cancel"})
            assert refused.value.body["error"]["code"] == "NATIVE_EFFECT_CONFLICT"
            for stage in ("started", "result", "cancelled", "parent_idle"):
                progress = {"child_agent_id": "original-child", "stage": stage}
                current = await effects.observe(current, progress)
                assert current.record.status == "pending" and current.fresh
                retained = await effects.read("original-task-call", {"arguments": "fixed child"})
                assert retained.progress == progress and retained.result is None
            completed = await effects.finish(current, {"outcome": "cancelled", "stop_confirmed": True})
            assert completed.status == "done" and completed.progress["stage"] == "parent_idle"
            assert completed.result == {"outcome": "cancelled", "stop_confirmed": True}
            assert (await runtime.read(receipt.request_id)).status == "unknown"

        client.portal.call(check)
    assert len(sdk.calls) == 1


@pytest.mark.parametrize("changed", ["agent", "conversation", "rid", "token", "input", "fresh", "etag"])
def test_observation_rejects_a_foreign_or_reconstructed_claim_before_writing(local_env, changed):
    from shared_context.native_effects import NativeEffectRecord, NativeEffects

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        assert harness.invoke(submit()).status_code == 202
        receipt = harness.wait("rid-1", "completed")

        async def check():
            effects = NativeEffects(runtime, receipt)
            original = await effects.reserve("same-sdk-call", {"body": "original"})
            values = original.record.model_dump()
            fields = {"agent": "agent_id", "conversation": "conversation_id", "rid": "request_id",
                      "token": "token", "input": "input_digest"}
            if changed in fields:
                values[fields[changed]] = "f" * 64 if changed == "input" else "foreign"
            record = NativeEffectRecord.model_validate(values)
            supplied = replace(original, record=record,
                               fresh=changed != "fresh", etag="not-original" if changed == "etag" else original.etag)
            with pytest.raises(Problem) as refused:
                await effects.observe(supplied, {"stage": "started"})
            assert refused.value.body["error"]["code"] == "NATIVE_EFFECT_CONFLICT"
            with pytest.raises(Problem):
                await effects.finish(supplied, {"outcome": "not the original holder"})
            assert await effects.read("same-sdk-call", {"body": "original"}) == original.record
            cached = await effects.reserve("same-sdk-call", {"body": "original"})
            assert not cached.fresh
            with pytest.raises(Problem):
                await effects.observe(cached, {"stage": "started"})

        client.portal.call(check)


def test_duplicate_observation_reads_back_and_stale_progress_never_overwrites_newer_or_done(local_env):
    from shared_context.native_effects import NativeEffects

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        assert harness.invoke(submit()).status_code == 202
        receipt = harness.wait("rid-1", "completed")

        async def check():
            effects = NativeEffects(runtime, receipt)
            reserved = await effects.reserve("original", {"body": "same"})
            running = await effects.observe(reserved, {"stage": "running", "child": "one"})
            assert await effects.observe(reserved, {"stage": "running", "child": "one"}) == running
            observed = await effects.observe(running, {"stage": "result", "child": "one", "sha256": "a" * 64})
            with pytest.raises(Problem) as stale:
                await effects.observe(reserved, {"stage": "running", "child": "one"})
            assert stale.value.body["error"]["code"] == "NATIVE_EFFECT_CONFLICT"
            final = await effects.finish(observed, {"text": "original result"})
            assert await effects.finish(observed, {"text": "original result"}) == final
            with pytest.raises(Problem):
                await effects.observe(observed, {"stage": "running", "child": "one"})
            with pytest.raises(Problem):
                await effects.finish(observed, {"text": "different result"})
            assert await effects.read("original", {"body": "same"}) == final

        client.portal.call(check)


@pytest.mark.parametrize("applied", ["absent", "saved", "raced"])
def test_observation_loss_only_reads_original_progress_and_never_dispatches(local_env, monkeypatch, applied):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError
    from shared_context.native_effects import NativeEffects

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    written = []
    write = FoundryStateStore.set_item

    async def uncertain(store, key, value, **kwargs):
        if value.get("kind") == "fa-native-tool-effect-v1" and value.get("progress") == {"stage": "started"}:
            written.append(key)
            if applied == "saved":
                await write(store, key, value, **kwargs)
            elif applied == "raced":
                await write(store, key, {**value, "progress": {"stage": "newer-observation"}}, **kwargs)
            raise ServiceResponseError("OFFLINE observation response lost")
        return await write(store, key, value, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "set_item", uncertain)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        assert harness.invoke(submit()).status_code == 202
        receipt = harness.wait("rid-1", "completed")

        async def check():
            effects = NativeEffects(runtime, receipt)
            original = await effects.reserve("same-original-call", {"body": "unchanged"})
            if applied == "saved":
                observed = await effects.observe(original, {"stage": "started"})
                assert observed.record.progress == {"stage": "started"}
                assert await effects.observe(original, {"stage": "started"}) == observed
            else:
                with pytest.raises(Problem) as unknown:
                    await effects.observe(original, {"stage": "started"})
                assert unknown.value.body["error"]["code"] == "NATIVE_EFFECT_UNKNOWN"
            retained = await effects.read("same-original-call", {"body": "unchanged"})
            expected = {"saved": {"stage": "started"}, "raced": {"stage": "newer-observation"}, "absent": None}
            assert retained.progress == expected[applied]
            assert retained.status == "pending" and retained.token == original.record.token

        client.portal.call(check)
    assert len(written) == 1 and len(sdk.calls) == 1
