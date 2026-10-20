"""C0's real native executor seam; named consumers and model I/O are offline doubles."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import httpx
from starlette.testclient import TestClient

from shared_context import native_sdk
from shared_context.behavior_capabilities import BUILTIN_CAPABILITIES, CapabilityCapture, _grant
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeResult, NativeSubmit, NativeTurnCapture
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_wire import WireModel
from shared_context.m2_copilot import ToolOutput, ToolSpec
from shared_context.native_turns import TURNS_STORE, NativeTurnRuntime, _json, _Rid
from test_native_turn_wire import capture, submit
from test_native_turns import Harness, IDENTITY, local_env


class OfflineResult(WireModel):
    text: str


def selected_capture(*entries, profile="fa-instruction-v3"):
    data = capture().model_dump(mode="json")
    role = assemble_prompt(profile).record
    data["template"]["role"] = {
        "profile": role.profile, "prefix_sha256": role.prefix_sha256,
        "capability_catalog_version": role.capability_catalog_version,
    }
    data["role_prompt"] = None
    grants = tuple(_grant(BUILTIN_CAPABILITIES[name]) for name in sorted(entries))
    data["template"]["settings"]["capabilities"] = [grant.ref.model_dump(mode="json") for grant in grants]
    data["capabilities"] = (CapabilityCapture(head_selection_id="OFFLINE-selection", grants=grants).model_dump(mode="json")
                            if grants else None)
    return NativeTurnCapture.model_validate_json(json.dumps(data))


def test_selected_consumer_runs_on_original_native_turn_and_releases_after_sdk(local_env, monkeypatch, tmp_path):
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider

    seen, order = [], []

    async def bind(context, grants):
        seen.append(context)
        assert context.receipt.status == "started"
        assert context.receipt.request_id == "rid-1"
        assert context.receipt.capture == selected_capture("fa-shell-v1")
        assert tuple(grant.entry_id for grant in grants) == ("fa-shell-v1",)
        assert context.identity == IDENTITY
        assert context.root == tmp_path / "native"
        assert context.turn_root.is_relative_to(context.root)
        assert await context.current() is True

        async def run(packet):
            assert packet["tool_call_id"] == "OFFLINE-call"
            assert packet["session_id"] == context.scope.sdk_session_id
            order.append("tool")
            return ToolOutput(OfflineResult(text="original RID result").model_dump_json())

        async def close():
            order.append("released")

        return NativeCapabilityBinding(
            tools=(ToolSpec("run_command", "Offline consumer only.", {"type": "object"}, run),),
            result_schemas={"run_command": OfflineResult}, close=close,
        )

    provider = NativeCapabilityProvider(
        accepts=lambda grant: grant.entry_id == "fa-shell-v1", bind=bind,
    )

    async def sdk(**options):
        order.append("sdk")
        assert options["capability_session"].custom_tools == ("run_command",)
        assert await options["capability_current"]() is True
        assert "fa-shell-v1" in options["system_message"]
        spec = next(tool for tool in options["tools"] if tool.name == "run_command")
        output = await spec.handler({
            "session_id": options["sdk_session_id"], "tool_call_id": "OFFLINE-call",
            "tool_name": spec.name, "arguments": {},
        })
        assert options["behavior_boundary"].tool_result(spec.name, output.text) == output.text
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {
                "sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "original RID result",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token", capability_providers=(provider,),
    )
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    request = NativeSubmit(command=submit().command, capture=selected_capture("fa-shell-v1"))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        assert harness.invoke(request).status_code == 202
        receipt = harness.wait("rid-1", "completed")
        result = harness.call(runtime.result, NativeResult(
            conversation_id=receipt.conversation_id, request_id=receipt.request_id))
        assert result.text == "original RID result"
        assert receipt.capture == request.capture
        assert receipt.usage.state == "none"
        assert harness.invoke(request).status_code == 202
    assert len(seen) == 1
    assert order == ["sdk", "tool", "released"]


@pytest.mark.parametrize("selection", ["unselected", "unsupported", "missing", "extra", "duplicate"])
def test_only_actual_selected_tools_are_bound_and_misconfigured_consumers_never_start_sdk(
        local_env, monkeypatch, tmp_path, selection):
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider

    entered, closed, models = [], [], []

    async def bind(context, grants):
        entered.append(context.receipt.request_id)

        async def close():
            closed.append(True)

        async def handler(packet):
            pytest.fail("No actual tool call is allowed in this setup-only test")

        names = {"missing": (), "extra": ("run_command", "unselected"),
                 "duplicate": ("run_command", "run_command")}.get(selection, ("run_command",))
        return NativeCapabilityBinding(
            tools=tuple(ToolSpec(name, "OFFLINE", {"type": "object"}, handler) for name in names),
            result_schemas={name: OfflineResult for name in names}, close=close,
        )

    async def sdk(**options):
        models.append(True)
        assert "capability_session" not in options
        assert not any(tool.name == "run_command" for tool in options["tools"])
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {
                "sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "OFFLINE no capability",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    provider = NativeCapabilityProvider(accepts=lambda grant: grant.entry_id == "fa-shell-v1", bind=bind)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token", capability_providers=() if selection == "unsupported" else (provider,),
    )
    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    selected = selected_capture(*(() if selection == "unselected" else ("fa-shell-v1",)))
    request = NativeSubmit(command=submit().command, capture=selected)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        response = harness.invoke(request)
        if selection == "unsupported":
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "NATIVE_PROFILE_UNSUPPORTED"
        else:
            receipt = harness.wait("rid-1", "completed" if selection == "unselected" else "unknown")
            assert receipt.capture == selected
            if selection != "unselected":
                assert receipt.failure_code == "NATIVE_EXECUTION_UNAVAILABLE"
    assert len(models) == (1 if selection == "unselected" else 0)
    assert len(entered) == len(closed) == (0 if selection in {"unselected", "unsupported"} else 1)


def test_factory_passes_native_consumers_without_changing_original_private_root(local_env, monkeypatch, tmp_path):
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
    from shared_context.native_turns import NativeTurnControl

    owned = tmp_path / "owned"
    runtime_path = tmp_path / "OFFLINE-runtime"
    runtime_path.write_text("OFFLINE never executed", encoding="utf-8")
    seen = []

    async def bind(context, grants):
        assert context.root == owned / "native-turns"
        assert context.identity == IDENTITY
        assert callable(context.api_token_provider)
        seen.append(context)

        async def tool(packet):
            return ToolOutput(OfflineResult(text="OFFLINE").model_dump_json())

        return NativeCapabilityBinding(
            tools=(ToolSpec("run_command", "OFFLINE", {"type": "object"}, tool),),
            result_schemas={"run_command": OfflineResult},
        )

    provider = NativeCapabilityProvider(accepts=lambda grant: grant.entry_id == "fa-shell-v1", bind=bind)
    monkeypatch.setattr(native_sdk, "native_capability_providers", lambda *_: (provider,))
    hosted = SimpleNamespace(
        identity=IDENTITY, supervisor=SimpleNamespace(journal=SimpleNamespace(root=owned)),
        api=SimpleNamespace(api_url="https://api.offline.invalid", audience="api://OFFLINE"),
    )
    # Hosted settings for the factory only; the turn then uses local_env's offline state store (ee8ef6b7 reads it).
    with monkeypatch.context() as environment:
        environment.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
        environment.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-hosted-config-only")
        environment.setenv("AGENTSERVER_TASKS_BACKEND", "hosted")
        environment.setenv("BEHAVIOR_ASSISTANT_RUNTIME_PATH", str(runtime_path))
        environment.setenv("FOUNDRY_PROJECT_ENDPOINT",
                           "https://offline-fixture.services.ai.azure.com/api/projects/offline")
        runtime = native_sdk.native_runtime_from_environment(hosted)
    request = NativeSubmit(command=submit().command, capture=selected_capture("fa-shell-v1"))
    receipt = NativeReceipt(
        agent_id=IDENTITY.agent_id, hosted_session_id=IDENTITY.session_id,
        conversation_id="OFFLINE-cid", request_id="rid-1", submit_digest=request.sha256,
        capture_digest=request.capture.sha256, command=request.command, capture=request.capture,
        status="started", turn_number=1,
    )
    scope = NativeCheckpointScope(
        agent_id=receipt.agent_id, conversation_id=receipt.conversation_id, sdk_session_id="OFFLINE-sdk",
        model_provider=request.capture.template.settings.model_provider,
    )

    async def sdk(**options):
        assert options["capability_session"].custom_tools == ("run_command",)
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": scope.sdk_session_id}}) + "\n",
            "workspace.yaml": "id: " + scope.sdk_session_id + "\n",
        })
        return {"final_message": "OFFLINE factory",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    async def execute():
        # Since ee8ef6b7 the factory's executor fences effects on the stored original RID.
        await runtime.initialize()
        async with runtime._store(TURNS_STORE) as turns:
            await turns.create_item("rid:" + receipt.request_id, _json(_Rid(receipt=receipt, call_id="OFFLINE-seed", run_token="OFFLINE-run")))
        return await runtime.executor(receipt, scope, None, NativeTurnControl())

    result = asyncio.run(execute())
    assert result.text == "OFFLINE factory" and len(seen) == 1
    assert runtime.cache_root == owned / "native-turns" / "checkpoint-cache"


def test_consumer_close_failure_keeps_parent_model_usage_and_does_not_replay(local_env, monkeypatch, tmp_path):
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider

    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":8,"output_tokens":3}}}\n\n'
        ))

    async def bind(context, grants):
        async def handler(packet):
            return ToolOutput(OfflineResult(text="OFFLINE").model_dump_json())

        async def close():
            raise OSError("OFFLINE consumer did not confirm release")

        return NativeCapabilityBinding(
            tools=(ToolSpec("run_command", "OFFLINE", {"type": "object"}, handler),),
            result_schemas={"run_command": OfflineResult}, close=close)

    async def sdk(**options):
        boundary = options["behavior_boundary"]
        await boundary.before_request("OFFLINE-request")
        result = await boundary.sender(httpx.Request(
            "POST", "https://api.offline.invalid/model", json={"model": "OFFLINE-model", "stream": True}))
        async for _ in result.aiter_bytes():
            pass
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {
                "sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "not yet publishable",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token", model_transport=httpx.MockTransport(provider),
        capability_providers=(NativeCapabilityProvider(accepts=lambda grant: grant.entry_id == "fa-shell-v1", bind=bind),))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    request = NativeSubmit(command=submit().command, capture=selected_capture("fa-shell-v1"))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        harness.invoke(request)
        unknown = harness.wait("rid-1", "unknown")
        assert unknown.usage.usage[0].input_tokens == 8
        assert unknown.usage.state == "complete"
        assert harness.invoke(request).status_code == 202
        assert harness.call(runtime.read, "rid-1") == unknown
    assert len(calls) == 1


@pytest.mark.parametrize("exits_before_abort", [False, True])
def test_native_abort_does_not_wait_for_delegation_cancel_observation(monkeypatch, tmp_path, exits_before_abort):
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
    from shared_context.native_turns import NativeSdkFailure, NativeTurnControl

    order = []

    async def check():
        aborted, allow_observation = asyncio.Event(), asyncio.Event()
        control = NativeTurnControl()

        class Delegate:
            sdk_session_id = "OFFLINE-sdk"

            async def request_cancel(self):
                order.append("cancel-requested")
                await allow_observation.wait()
                order.append("cancel-recorded")

        delegate = Delegate()

        async def bind(context, grants):
            return NativeCapabilityBinding(delegation_runtime=delegate)

        class Session:
            async def abort(self):
                assert not allow_observation.is_set()
                order.append("sdk-abort")
                aborted.set()

        async def sdk(**options):
            assert options["delegation_runtime"] is delegate
            control.cancelled = True
            if exits_before_abort:
                allow_observation.set()
                raise RuntimeError("OFFLINE SDK stopped before abort watcher")
            options["on_session_ready"](Session())
            await asyncio.wait_for(aborted.wait(), timeout=2)
            allow_observation.set()
            raise RuntimeError("OFFLINE cancelled SDK")

        monkeypatch.setattr(native_sdk, "run_copilot", sdk)
        executor = native_sdk.NativeSdkExecutor(
            root=tmp_path, runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
            model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
            token_provider=lambda: "OFFLINE-token",
            capability_providers=(NativeCapabilityProvider(accepts=lambda grant: grant.entry_id == "fa-delegate-v1",
                                                           bind=bind),))
        request = NativeSubmit(command=submit().command, capture=selected_capture("fa-delegate-v1"))
        receipt = NativeReceipt(
            agent_id=IDENTITY.agent_id, hosted_session_id=IDENTITY.session_id, request_id="rid-1",
            conversation_id="OFFLINE-cid", command=request.command, capture=request.capture,
            submit_digest=request.sha256, capture_digest=request.capture.sha256, status="started", turn_number=1,
        )
        scope = NativeCheckpointScope(
            agent_id=receipt.agent_id, conversation_id=receipt.conversation_id, sdk_session_id="OFFLINE-sdk",
            model_provider=request.capture.template.settings.model_provider)
        with pytest.raises(NativeSdkFailure):
            await executor(receipt, scope, None, control)
        assert sorted(order) == (["cancel-recorded", "cancel-requested"] if exits_before_abort else
                                 ["cancel-recorded", "cancel-requested", "sdk-abort"])
        if not exits_before_abort:
            assert order.index("sdk-abort") < order.index("cancel-recorded")

    asyncio.run(check())
