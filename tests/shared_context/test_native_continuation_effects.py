"""Recovery decisions use durable effect watermarks, never a new model call ID alone."""

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.errors import Problem
from shared_context.native_turns import NativeTurnRuntime
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env
from test_m2_copilot import runtime_path, workspace


def test_only_new_operation_records_prove_the_pre_dispatch_watermark(local_env):
    from shared_context.native_continuation_wire import NativeOperation
    from shared_context.native_effects import NativeEffects

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        harness.invoke(submit("watermark-source"))
        receipt = harness.wait("watermark-source", "completed")

        async def scenario():
            effects = NativeEffects(runtime, receipt)
            legacy = await effects.reserve("legacy-call", {"value": 1})
            assert legacy.record.dispatch_started is None
            operation = NativeOperation(
                root_request_id=receipt.request_id, operation_id="a" * 64,
                tool_name="sample_tool", arguments={"value": 2}, binding_digest="b" * 64,
            )
            prepared = await effects.reserve("new-call", {"value": 2}, operation=operation)
            assert prepared.fresh and prepared.record.status == "pending"
            assert prepared.record.dispatch_started is False
            assert (await effects.read("new-call", {"value": 2}, operation=operation)).operation == operation
            dispatched = await effects.mark_dispatched(prepared)
            assert dispatched.record.status == "pending" and dispatched.record.dispatch_started is True
            done = await effects.finish(dispatched, {"value": "original result"})
            assert done.status == "done" and done.operation == operation
            assert (await effects.read("legacy-call", {"value": 1})) == legacy.record

        client.portal.call(scenario)


def test_operation_index_distinguishes_missing_legacy_state_from_proven_empty_or_original_calls(local_env):
    from shared_context.native_continuation_wire import NativeOperation
    from shared_context.native_effects import NativeEffects

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        harness.invoke(submit("indexed-source"))
        receipt = harness.wait("indexed-source", "completed")

        async def scenario():
            effects = NativeEffects(runtime, receipt)
            with pytest.raises(Problem) as legacy:
                await effects.operation_records()
            assert legacy.value.body["error"]["code"] == "NATIVE_CONTINUATION_WATERMARK_UNAVAILABLE"
            await effects.start_operations()
            assert await effects.operation_records() == ()
            operation = NativeOperation(
                root_request_id=receipt.request_id, operation_id="c" * 64,
                tool_name="sample_tool", arguments={"original": True}, binding_digest="d" * 64)
            claim = await effects.prepare_operation("sdk-call", operation)
            assert claim.fresh
            assert await effects.operation_records() == (claim.record,)
            again = await effects.prepare_operation("sdk-call", operation)
            assert not again.fresh and again.record == claim.record
            await effects.start_operations()
            assert await effects.operation_records() == (claim.record,)
            assert await effects.read("sdk-call", {"unrelated": True}) is None

        client.portal.call(scenario)


def test_new_sdk_call_ids_cannot_repeat_done_or_unknown_operations_but_unstarted_calls_can_be_reviewed(local_env):
    from shared_context.m2_copilot import ToolFailure, ToolOutput
    from shared_context.native_effects import NativeEffects
    from shared_context.native_operations import NativeOperationGuard

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        harness.invoke(submit("original-operations"))
        original = harness.wait("original-operations", "completed")
        harness.invoke(submit("continuing-operations", original.conversation_id))
        continuing = harness.wait("continuing-operations", "completed")
        calls = []

        async def execute(packet):
            calls.append(packet["arguments"]["value"])
            return ToolOutput("saved-" + str(packet["arguments"]["value"]))

        async def scenario():
            first = NativeOperationGuard(NativeEffects(runtime, original), {"sample_tool": "a" * 64})
            await first.start()
            packets = [{
                "tool_name": "sample_tool", "tool_call_id": "old-" + str(value), "arguments": {"value": value},
            } for value in (1, 2, 3)]
            for packet in packets:
                await first.prepare(packet)
            assert (await first.invoke(packets[0], execute)).text == "saved-1"
            await first.dispatched(packets[1])
            prior = await NativeEffects(runtime, original).operation_records()
            second = NativeOperationGuard(
                NativeEffects(runtime, continuing), {"sample_tool": "a" * 64}, prior=prior)
            await second.start()
            for packet in packets:
                packet["tool_call_id"] = packet["tool_call_id"].replace("old-", "new-")
            assert await second.prepare(packets[0]) == "cached"
            assert (await second.invoke(packets[0], execute)).text == "saved-1"
            with pytest.raises(ToolFailure) as unknown:
                await second.prepare(packets[1])
            assert unknown.value.public_code == "NATIVE_CONTINUATION_EFFECT_UNKNOWN"
            assert await second.prepare(packets[2]) == "execute"
            assert (await second.invoke(packets[2], execute)).text == "saved-3"
            with pytest.raises(ToolFailure):
                await second.prepare({
                    "tool_name": "sample_tool", "tool_call_id": "changed-args", "arguments": {"value": 4}})
            assert calls == [1, 3]
            assert await NativeEffects(runtime, original).operation_records() == prior

        client.portal.call(scenario)


def test_pinned_sdk_replays_original_results_and_blocks_unknown_operations_before_dispatch(
    local_env, runtime_path, workspace,
):
    from shared_context.m2_copilot import ToolOutput, ToolSpec, run_copilot
    from shared_context.native_effects import NativeEffects
    from shared_context.native_operations import NativeOperationGuard
    from test_m2_copilot import SCHEMA, ScriptedEndpoint, tool_call

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        harness.invoke(submit("sdk-watermark-source"))
        original = harness.wait("sdk-watermark-source", "completed")
        harness.invoke(submit("sdk-watermark-continuation", original.conversation_id))
        resumed = harness.wait("sdk-watermark-continuation", "completed")
        calls = []

        async def action(packet):
            calls.append(packet["arguments"]["label"])
            return ToolOutput("ORIGINAL_EFFECT_RESULT")

        async def scenario():
            first = NativeOperationGuard(NativeEffects(runtime, original), {"sample_action": "e" * 64})
            await first.start()
            packets = [{
                "tool_call_id": "old-" + label, "tool_name": "sample_action", "arguments": {"label": label},
            } for label in ("done", "unknown")]
            for packet in packets:
                await first.prepare(packet)
            await first.invoke(packets[0], action)
            await first.dispatched(packets[1])
            prior = await NativeEffects(runtime, original).operation_records()
            guard = NativeOperationGuard(NativeEffects(runtime, resumed), {"sample_action": "e" * 64}, prior=prior)
            await guard.start()
            endpoint = ScriptedEndpoint([
                tool_call("sample_action", {"label": "done"}, "new-done"),
                tool_call("sample_action", {"label": "unknown"}, "new-unknown"),
                {"content": "ORIGINAL_RESULT_REUSED_UNKNOWN_RETAINED"},
            ])
            result = await run_copilot(
                state_dir=workspace / "state", working_directory=workspace / "work",
                sdk_session_id="sdk-watermark-loop", resume=False, prompt="Continue the original work.",
                tools=[ToolSpec("sample_action", "Synthetic original action", SCHEMA, action)],
                api_url="https://context.invalid", token_provider=lambda: "synthetic-token",
                on_event=lambda _event: None,
                timeout_seconds=35, runtime_path=runtime_path, http_transport=endpoint.transport,
                native_operations=guard)
            await guard.close()
            assert calls == ["done"]
            feedback = [item["content"] for item in endpoint.requests[-1]["messages"] if item["role"] == "tool"]
            assert any("ORIGINAL_EFFECT_RESULT" in item for item in feedback)
            assert any("尚未確認" in item for item in feedback)
            assert result["final_message"] == "ORIGINAL_RESULT_REUSED_UNKNOWN_RETAINED"
            assert await NativeEffects(runtime, original).operation_records() == prior

        client.portal.call(scenario)


def test_parallel_original_calls_serialize_the_shared_index_without_refusing_unstarted_effects(
    local_env, monkeypatch,
):
    import asyncio
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from shared_context.m2_copilot import ToolOutput
    from shared_context.native_effects import NativeEffects
    from shared_context.native_operations import NativeOperationGuard

    read = FoundryStateStore.get_item

    async def remote_read(store, key, **options):
        result = await read(store, key, **options)
        if key.startswith("op-index:"):
            await asyncio.sleep(0)
        return result

    monkeypatch.setattr(FoundryStateStore, "get_item", remote_read)
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        assert harness.invoke(submit("parallel-index")).status_code == 202
        receipt = harness.wait("parallel-index", "completed")

        async def scenario():
            effects = NativeEffects(runtime, receipt)
            guard = NativeOperationGuard(effects, {"sample_tool": "a" * 64})
            await guard.start()
            packets = [{"tool_name": "sample_tool", "tool_call_id": f"parallel-{index}",
                        "arguments": {"value": index}} for index in range(6)]
            assert await asyncio.gather(*(guard.prepare(packet) for packet in packets)) == ["execute"] * 6
            records = await effects.operation_records()
            assert len(records) == 6 and all(record.dispatch_started is False for record in records)
            calls = []

            async def execute(packet):
                calls.append(packet["arguments"]["value"])
                return ToolOutput(str(packet["arguments"]["value"]))

            results = await asyncio.gather(*(guard.invoke(packet, execute) for packet in packets))
            assert sorted(calls) == list(range(6))
            assert [result.text for result in results] == [str(index) for index in range(6)]
            assert all(record.status == "done" for record in await effects.operation_records())

        client.portal.call(scenario)
