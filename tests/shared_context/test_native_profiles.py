"""Native profile consumers use the real receipt/capture, never a synthetic legacy job."""

import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context import native_sdk
from shared_context.behavior_code_identity import code_sha256
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
from shared_context.native_turns import NativeTurnRuntime
from test_native_capabilities import selected_capture
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, local_env


def profile_capture(*, platform="linux-x64"):
    data = selected_capture().model_dump(mode="json")
    data["template"]["settings"].update(
        checkpoint_profile={"name": "manual-no-custom-v1", "platform": platform, "provider": "openai-responses"},
        sdk_start_profile="fa-sdk-start-gate-v1", turn_origin_profile="fa-instruction-context-origin-v1",
    )
    data["sdk_start_code_sha256"] = code_sha256()
    return NativeTurnCapture.model_validate_json(json.dumps(data))


def test_original_a_profiles_reach_sdk_start_and_native_origin_without_clearing_capture(
        local_env, monkeypatch, tmp_path):
    from shared_context import behavior_sdk_files

    # This verifies the Linux profile mapping, not an actual Linux/Hosted runtime.
    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    selected, seen = profile_capture(), []

    async def sdk(**options):
        seen.append(options)
        assert options["session_files"].checkpoint_profile == selected.template.settings.checkpoint_profile
        checkpoint = options["native_checkpoint_turn"]
        assert checkpoint.request_id == "rid-1"
        assert checkpoint.scope.sdk_session_id == options["sdk_session_id"]
        assert checkpoint.capture_digest == selected.sha256 and checkpoint.previous is None
        options["sdk_start_gate"].enter(options["sdk_session_id"])
        tool = next(spec for spec in options["tools"] if spec.name == "read_context")
        result = await tool.handler({
            "session_id": options["sdk_session_id"], "tool_call_id": "origin-call",
            "tool_name": "read_context", "arguments": {},
        })
        checked = json.loads(options["behavior_boundary"].tool_result("read_context", result.text))
        assert checked["content"] == "Original user text"
        assert checked["turn_origin"]["source_kind"] == "human"
        assert checked["turn_origin"]["request_id"] == "rid-1"
        assert checked["turn_origin"]["capture_digest"] == selected.sha256
        assert checked["turn_origin"]["authorization"] is False
        assert not {"job_id", "work_id", "binding_id"} & checked["turn_origin"].keys()
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {
                "sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "OFFLINE native profiles",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token",
    )
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    request = NativeSubmit(command=submit().command, capture=selected)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        response = harness.invoke(request)
        assert response.status_code == 202, response.text
        receipt = harness.wait("rid-1", "completed")
        assert receipt.capture == selected and len(seen) == 1


async def controlled_compaction(files, turn, pid=123):
    from copilot.rpc import HistoryCompactResult
    from shared_context.behavior_sdk_compaction import events_from
    from test_sdk_compaction_product import SUMMARY, raw_files

    handlers = []

    def subscribe(callback):
        handlers.append(callback)
        return lambda: handlers.remove(callback)

    async def compact(request):
        assert request.to_dict() == {"trigger": "manual"}
        files.observe_model_request("d" * 64)
        raw = raw_files(files.previous)
        values = events_from(raw)
        for record in (values[3], values[-1]):
            files.observe_compaction(record)
            packet = SimpleNamespace(type=SimpleNamespace(value=record["type"]), to_dict=lambda record=record: record)
            for callback in tuple(handlers):
                callback(packet)
        files.files = raw
        return HistoryCompactResult(
            success=True, messages_removed=2, tokens_removed=20, summary_content=SUMMARY)

    session = SimpleNamespace(
        session_id=files.session_id, on=subscribe, rpc=SimpleNamespace(history=SimpleNamespace(compact=compact)),
    )
    await files.compact(session, turn, pid)
    assert not handlers


@pytest.mark.parametrize("provider,codec", [
    (None, "native-session-fs-diag-empty-v1"),
    ("openai-responses", "native-session-fs-responses-diag-v1"),
])
def test_manual_checkpoint_and_cold_followup_retain_original_native_rid_lineage(
        local_env, monkeypatch, tmp_path, provider, codec):
    from shared_context import behavior_sdk_files
    from shared_context.behavior_sdk_state import SdkState
    from test_sdk_compaction_product import event, ordinary

    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    data = profile_capture().model_dump(mode="json")
    data["template"]["settings"]["model_provider"] = provider
    data["template"]["settings"]["checkpoint_profile"]["provider"] = provider or "openai-completions"
    selected = NativeTurnCapture.model_validate_json(json.dumps(data))
    seen, states = [], []

    async def sdk(**options):
        turn, files = options["native_checkpoint_turn"], options["session_files"]
        seen.append(turn)
        options["sdk_start_gate"].enter(options["sdk_session_id"])
        if turn.turn_number == 1:
            original = ordinary(files.session_id).model_dump(mode="json")
            if provider is not None:
                original.update(model_provider=provider, codec="session-fs-responses-v1")
            files.files = dict(SdkState.model_validate_json(json.dumps(original)).files)
        elif turn.turn_number == 2:
            assert options["resume"] and turn.previous.lineage.request_id == "rid-1"
            await controlled_compaction(files, turn)
            assert files.compaction.request_id == "rid-2"
            assert files.compaction.capture_digest == selected.sha256
            assert not hasattr(files.compaction, "job_id")
        else:
            assert options["resume"] and turn.previous.codec == codec
            assert turn.previous.lineage.request_id == "rid-2"
            assert turn.previous.source.execution.request_id == "rid-2"
            assert files.compaction is None
            files.files["events.jsonl"] += json.dumps(event(
                "user.message", "followup", {"content": "The corrected count is 851."}, "compact-complete")) + "\n"
        states.append(files)
        return {"final_message": "original saved response",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token",
    )
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        cid = None
        for index in range(1, 4):
            request = NativeSubmit(command=submit(f"rid-{index}", cid).command, capture=selected)
            assert harness.invoke(request).status_code == 202
            receipt = harness.wait(f"rid-{index}", "completed")
            cid = receipt.conversation_id
            assert receipt.capture == selected
        assert harness.invoke(request).status_code == 202
    assert len(seen) == 3
    assert seen[2].previous.source.execution == states[1].compaction
    assert seen[2].previous.lineage.previous_request_id == "rid-1"
    assert states[2].compaction is None
    assert "PRIVATE_FIXTURE_SYSTEM" not in states[2].previous.encoded().decode("utf-8")


@pytest.mark.parametrize("prefix,provider,accepted", [
    ("sdk-1234", "openai-responses", True),
    ("sdk-ffff", "openai-responses", False),
    ("sdk-1234", None, False),
])
def test_fixed_runtime_responses_checkpoint_title_is_bound_to_session_identity(prefix, provider, accepted):
    from shared_context.behavior_sdk_compaction import events_from, validate_native_files
    from shared_context.errors import Problem
    from test_sdk_compaction_product import ordinary, raw_files

    files = raw_files(ordinary("sdk-1234" + "a" * 44))
    filename = f"001-session-{prefix}.md"
    files["checkpoints/" + filename] = files.pop("checkpoints/001-inventory.md")
    files["checkpoints/index.md"] = files["checkpoints/index.md"].replace(
        "| 1 | Inventory | 001-inventory.md |", f"| 1 | Session {prefix} | {filename} |")
    files["events.jsonl"] = files["events.jsonl"].replace("001-inventory.md", filename)
    original = dict(files)
    if accepted:
        validate_native_files(files, events_from(files), model_provider=provider)
    else:
        with pytest.raises(Problem):
            validate_native_files(files, events_from(files), model_provider=provider)
    assert files == original


@pytest.mark.parametrize("change", ["build", "missing-build", "input", "stopped", "second-entry"])
def test_native_start_rechecks_exact_build_input_and_original_entry(monkeypatch, change):
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.errors import Problem
    from shared_context.native_sdk_profiles import NativeSdkStart
    from shared_context.native_turns import NativeTurnControl

    request = NativeSubmit(command=submit().command, capture=profile_capture())
    receipt = NativeReceipt(state_profile=request.state_profile,
        agent_id=IDENTITY.agent_id, hosted_session_id=IDENTITY.session_id, request_id="rid-1",
        conversation_id="cid", capture=request.capture, capture_digest=request.capture.sha256,
        submit_digest=request.sha256, command=request.command, status="started", turn_number=1,
    )
    scope = NativeCheckpointScope(
        agent_id=receipt.agent_id, conversation_id=receipt.conversation_id, sdk_session_id="sdk",
        model_provider="openai-responses",
    )
    control = NativeTurnControl()
    if change == "input":
        changed = receipt.model_copy(update={"command": receipt.command.model_copy(update={"content": "changed"})})
        with pytest.raises(ValueError):
            NativeSdkStart(changed, scope, control)
        return
    if change == "missing-build":
        raw = request.capture.model_dump(mode="json")
        raw.pop("sdk_start_code_sha256")
        with pytest.raises(Problem):
            from shared_context.native_sdk_profiles import check_start_capture
            check_start_capture(NativeTurnCapture.model_validate_json(json.dumps(raw)))
        return
    gate = NativeSdkStart(receipt, scope, control)
    if change == "build":
        monkeypatch.setattr("shared_context.native_sdk_profiles.code_sha256", lambda: "f" * 64)
    elif change == "stopped":
        control.fenced = True
    else:
        gate.enter("sdk")
    with pytest.raises(Problem) as refused:
        gate.enter("sdk")
    assert refused.value.body["error"]["code"] == {
        "build": "SDK_START_GATE_CODE_CHANGED", "stopped": "SDK_START_GATE_FENCED",
        "second-entry": "SDK_START_GATE_REPLAY",
    }[change]


def test_agent_origin_is_not_human_and_legacy_context_reader_stays_separate():
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.native_sdk_profiles import native_context
    from test_native_turn_wire import agent_receive

    request = agent_receive()
    receipt = NativeReceipt(state_profile=request.state_profile,
        agent_id=IDENTITY.agent_id, hosted_session_id=IDENTITY.session_id, request_id=request.command.request_id,
        conversation_id="cid", capture=request.capture, capture_digest=request.capture.sha256,
        submit_digest=request.sha256, command=request.command, source=request.source, status="started", turn_number=2,
    )
    context = native_context(receipt, None)
    assert context.content == request.command.content
    assert context.turn_origin.source_kind == "agent"
    assert context.turn_origin.actor_role == "sending-agent"
    assert context.turn_origin.source == request.source
    assert context.turn_origin.authorization is False
    legacy = AssistantModelBoundary(None, text_reply=True)
    with pytest.raises(ValueError):
        legacy.tool_result("read_context", context.model_dump_json())


def test_unknown_codec_keeps_last_legal_checkpoint_and_original_rid_unknown(local_env):
    from dataclasses import replace
    from test_native_turns import OfflineSdk

    original = OfflineSdk()
    calls, previous_states = [], []

    async def sdk(receipt, scope, previous, control):
        calls.append(receipt.request_id)
        previous_states.append(previous)
        outcome = await original(receipt, scope, previous, control)
        return (replace(outcome, state=outcome.state.model_copy(update={"codec": "test-only-future-codec"}))
                if receipt.request_id == "rid-2" else outcome)

    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, original, client)
        harness.invoke(submit())
        first = harness.wait("rid-1", "completed")
        second = submit("rid-2", first.conversation_id)
        harness.invoke(second)
        unknown = harness.wait("rid-2", "unknown")
        assert unknown.failure_code == "SDK_STATE_UNSUPPORTED_FORMAT"
        assert unknown.result is None
        assert harness.invoke(second).status_code == 202
        harness.invoke(submit("rid-3", first.conversation_id))
        third = harness.wait("rid-3", "completed")
        assert third.turn_number == 3
        assert harness.call(runtime.read, "rid-2") == unknown
    assert calls == ["rid-1", "rid-2", "rid-3"]
    assert previous_states[1] == previous_states[2]
    assert previous_states[2].sha256 == first.result.checkpoint_sha256
