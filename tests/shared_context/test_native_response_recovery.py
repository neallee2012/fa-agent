"""Cancellation and official host restart reuse the original Responses RID and SID."""

import time

from starlette.testclient import TestClient

from shared_context.native_turns import NativeTurnRuntime
from test_native_responses import ResponsesHarness, build_responses_host, response_submit, responses
from test_native_turns import IDENTITY, OfflineSdk, local_env


def test_official_cancel_waiting_response_does_not_cancel_the_active_cid(responses):
    responses.sdk.block = True
    assert responses.invoke(response_submit("active")).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "active")).status_code == 200
    responses.wait("active", "started")
    assert responses.invoke(response_submit("cancel-waiting")).status_code == 202
    waiting = responses.call(responses.runtime.read, "cancel-waiting")
    created = responses.respond(waiting)
    responses.queued("cancel-waiting")
    cancelled = responses.client.post("/responses/" + created.json()["id"] + "/cancel", headers={
        "x-agent-session-id": IDENTITY.session_id,
    })
    assert cancelled.status_code == 200, cancelled.text
    original = responses.wait("cancel-waiting", "unknown")
    assert original.failure_code == "NATIVE_TURN_CANCELLED"
    assert original.response_id is None and original.turn_number is None
    assert not responses.sdk.controls["active"].stop_requested()
    assert [call[0] for call in responses.sdk.calls] == ["active"]
    responses.sdk.block = False
    responses.wait("active", "completed")


def test_non_client_cancel_retains_accepted_for_same_rid_dispatch(responses, caplog):
    responses.sdk.block = True
    assert responses.invoke(response_submit("active")).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "active")).status_code == 200
    responses.wait("active", "started")
    assert responses.invoke(response_submit("deferred")).status_code == 202
    accepted = responses.call(responses.runtime.read, "deferred")
    created = responses.respond(accepted)
    assert created.status_code == 200
    responses.queued("deferred")
    context, signal = responses.contexts["deferred"][0]
    assert not context.client_cancelled
    responses.call(signal.set)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        original = responses.call(responses.runtime.read, "deferred")
        if original.dispatch.state == "needs_response":
            break
        time.sleep(0.01)
    assert original.status == "accepted" and original.failure_code is None and original.response_id is None
    assert original.dispatch.state == "needs_response"
    assert "exit_for_recovery() may only be called" not in caplog.text
    assert responses.response_result(created.json()["id"])["status"] == "failed"
    assert not responses.sdk.controls["active"].stop_requested()
    responses.sdk.block = False
    responses.wait("active", "completed")
    assert responses.respond(original).status_code == 200
    responses.wait("deferred", "completed")
    assert [call[0] for call in responses.sdk.calls] == ["active", "deferred"]


def test_non_client_fence_closes_started_unknown_without_repeating_sdk_or_effects(responses, caplog):
    effects = []
    execute = responses.runtime.executor
    responses.sdk.block = True

    async def one_effect(*args):
        effects.append(args[0].request_id)
        return await execute(*args)

    responses.runtime.executor = one_effect
    assert responses.invoke(response_submit("started")).status_code == 202
    accepted = responses.call(responses.runtime.read, "started")
    created = responses.respond(accepted)
    responses.wait("started", "started")
    context, signal = responses.contexts["started"][0]
    assert not context.client_cancelled and not context.shutdown.is_set()
    responses.call(signal.set)
    unknown = responses.wait("started", "unknown")
    assert unknown.failure_code == "NATIVE_TURN_UNKNOWN"
    assert unknown.response_id == created.json()["id"]
    assert not unknown.cancel_requested and not responses.sdk.controls["started"].cancelled
    assert responses.sdk.controls["started"].fenced
    assert "exit_for_recovery() may only be called" not in caplog.text
    repeated = responses.respond(accepted)
    assert responses.response_result(repeated.json()["id"])["error"]["code"] == "NATIVE_TURN_UNKNOWN"
    assert effects == ["started"]
    assert [call[0] for call in responses.sdk.calls] == ["started"]


def test_official_shutdown_restart_recovers_accepted_but_does_not_repeat_started_sdk(
        local_env, monkeypatch, tmp_path):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    root = tmp_path / "journal"
    root.mkdir()
    first_sdk = OfflineSdk()
    first_sdk.block = True
    first_runtime = NativeTurnRuntime(IDENTITY, first_sdk)
    first_contexts = {}
    with TestClient(build_responses_host(first_runtime, root, first_contexts)) as client:
        first = ResponsesHarness(first_runtime, first_sdk, client, first_contexts)
        assert first.invoke(response_submit("started")).status_code == 202
        active = first.call(first_runtime.read, "started")
        active_response = first.respond(active)
        first.wait("started", "started")
        assert first.invoke(response_submit("accepted")).status_code == 202
        queued = first.call(first_runtime.read, "accepted")
        queued_response = first.respond(queued)
        first.queued("accepted")
    assert [call[0] for call in first_sdk.calls] == ["started"]
    assert first_sdk.controls["started"].shutdown and not first_sdk.controls["started"].cancelled

    recovered_sdk = OfflineSdk()
    recovered_runtime = NativeTurnRuntime(IDENTITY, recovered_sdk)
    recovered_contexts = {}
    with TestClient(build_responses_host(recovered_runtime, root, recovered_contexts)) as client:
        second = ResponsesHarness(recovered_runtime, recovered_sdk, client, recovered_contexts)
        unknown = second.wait("started", "unknown")
        completed = second.wait("accepted", "completed")
        assert unknown.response_id == active_response.json()["id"]
        assert unknown.failure_code != "NATIVE_TURN_CANCELLED"
        assert completed.response_id == queued_response.json()["id"]
        assert completed.submit_digest == queued.submit_digest and completed.command == queued.command
        assert [call[0] for call in recovered_sdk.calls] == ["accepted"]
        assert recovered_sdk.calls[0][3] == "OFFLINE-responses-call"
        assert recovered_contexts["accepted"][0][0].is_recovery
        assert recovered_contexts["started"][0][0].is_recovery
        # These were background non-stream requests; their stored result is JSON, not an SSE replay.
        saved = second.response_result(completed.response_id)
        assert saved["status"] == "completed" and saved["output"][-1]["phase"] == "final_answer"
        assert saved["output"][-1]["content"][0]["text"] == "Complete public answer"
