"""A client cancel of a recovered response reaches its handler: a cross-process recovery hands the handler a new cancel
signal that the official ``/cancel`` endpoint never sets, so the handler reads the cause flag ``/cancel`` stamps on
the same recovered context instead."""

import time

import pytest
from starlette.testclient import TestClient

from shared_context import native_continuations
from shared_context.errors import Problem
from shared_context.native_continuations import read_intent
from shared_context.native_turns import NativeTurnRuntime
from test_native_responses import ResponsesHarness, build_responses_host, response_submit
from test_native_turns import IDENTITY, OfflineSdk, local_env  # noqa: F401 - local_env is a fixture

# Promptly: well under the 10 s /cancel grace and the 60 s CLI exit bound (UNEXITED_WAIT_SECONDS).
PROMPT_SECONDS = 3


def first_process(root, rid, *, sdk_started):
    """An earlier process accepts ``rid`` and either starts its SDK turn (``started``) or leaves it ``accepted`` behind
    a CLI that never exits; its host shutdown defers the handler. Returns the official response id."""
    sdk = OfflineSdk()
    if sdk_started:
        sdk.block = True
    else:
        sdk.runtime_unexited = lambda: True  # the earlier CLI never exits: the RID waits for it, unstarted
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(build_responses_host(runtime, root, {})) as client:
        harness = ResponsesHarness(runtime, sdk, client, {})
        assert harness.invoke(response_submit(rid)).status_code == 202
        created = harness.respond(harness.call(runtime.read, rid))
        if sdk_started:
            harness.wait(rid, "started")
        else:
            deadline = time.monotonic() + 8
            while runtime.execution_owner != rid and time.monotonic() < deadline:
                time.sleep(0.01)
            assert runtime.execution_owner == rid
            assert harness.call(runtime.read, rid).status == "accepted"
    return created.json()["id"]


def cancel_recovered(root, rid, response_id, monkeypatch, *, cli_exits):
    """Recover ``rid`` waiting for the earlier CLI exit, POST the official cancel; (closed receipt, seconds, SDK calls,
    continuations scheduled, the read_intent error code)."""
    polled, exited, sdk = [], [], OfflineSdk()

    def unexited():
        polled.append(True)
        return not exited

    sdk.runtime_unexited = unexited
    schedule, scheduled = native_continuations.schedule_continuation, []

    async def observed(runtime, receipt):
        scheduled.append(receipt.request_id)
        return await schedule(runtime, receipt)

    monkeypatch.setattr(native_continuations, "schedule_continuation", observed)
    runtime, contexts = NativeTurnRuntime(IDENTITY, sdk), {}
    with TestClient(build_responses_host(runtime, root, contexts)) as client:
        harness = ResponsesHarness(runtime, sdk, client, contexts)
        deadline = time.monotonic() + 8
        while not (polled and runtime.execution_owner == rid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert polled and runtime.execution_owner == rid and contexts[rid][0][0].is_recovery
        cancelling = time.monotonic()
        cancelled = client.post("/responses/" + response_id + "/cancel", headers={
            "x-agent-session-id": IDENTITY.session_id,
        })
        assert cancelled.status_code == 200, cancelled.text
        assert contexts[rid][0][0].client_cancelled
        if cli_exits:
            exited.append(True)  # the earlier CLI exits after the cancel landed
        closed = harness.wait(rid, "unknown")
        settled = time.monotonic() - cancelling
        with pytest.raises(Problem) as missing:  # no continuation intent is prepared for a cancelled reentry
            harness.call(read_intent, runtime, rid)
    return closed, settled, sdk.calls, scheduled, missing.value.body["error"]["code"]


@pytest.mark.parametrize("cli_exits", [True, False], ids=["cli-exits-after-cancel", "cli-never-exits"])
def test_a_client_cancel_of_a_recovered_accepted_rid_closes_it_without_starting_the_sdk(
        local_env, monkeypatch, tmp_path, cli_exits):  # noqa: F811
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    root = tmp_path / "journal"
    root.mkdir()
    response_id = first_process(root, "accepted-rid", sdk_started=False)
    closed, settled, calls, scheduled, intent = cancel_recovered(
        root, "accepted-rid", response_id, monkeypatch, cli_exits=cli_exits)
    assert (closed.failure_code, closed.response_id) == ("NATIVE_TURN_CANCELLED", None)
    assert calls == [] and scheduled == [] and intent == "NATIVE_CONTINUATION_MISSING"
    assert settled < PROMPT_SECONDS, settled


@pytest.mark.parametrize("cli_exits", [True, False], ids=["cli-exits-after-cancel", "cli-never-exits"])
def test_a_client_cancel_of_a_recovered_started_rid_closes_it_without_a_continuation(
        local_env, monkeypatch, tmp_path, cli_exits):  # noqa: F811
    """The recovered record carries its context, so the official cancel reaches a recovered handler waiting for the
    CLI exit: that cancel makes the reentry not a recovery, and no continuation intent is prepared for it."""
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    root = tmp_path / "journal"
    root.mkdir()
    response_id = first_process(root, "started-rid", sdk_started=True)
    closed, settled, calls, scheduled, intent = cancel_recovered(
        root, "started-rid", response_id, monkeypatch, cli_exits=cli_exits)
    assert (closed.failure_code, closed.response_id, closed.cancel_requested) == (
        "NATIVE_RESULT_MISSING", response_id, False)
    assert calls == [] and scheduled == [] and intent == "NATIVE_CONTINUATION_MISSING"
    assert settled < PROMPT_SECONDS, settled


def test_a_client_cancel_stops_a_recovered_sdk_turn_that_is_already_running(
        local_env, monkeypatch, tmp_path):  # noqa: F811
    """The earlier CLI has exited, so the recovered handler starts its SDK turn; a cancel then stops that turn through
    the control the runtime polls (native_turns ``_observe_control``), not only before the start."""
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    root = tmp_path / "journal"
    root.mkdir()
    response_id = first_process(root, "running-rid", sdk_started=False)
    sdk = OfflineSdk()
    sdk.block = True
    sdk.runtime_unexited = lambda: False
    runtime, contexts = NativeTurnRuntime(IDENTITY, sdk), {}
    with TestClient(build_responses_host(runtime, root, contexts)) as client:
        harness = ResponsesHarness(runtime, sdk, client, contexts)
        harness.wait("running-rid", "started")
        assert len(sdk.calls) == 1 and contexts["running-rid"][0][0].is_recovery
        cancelling = time.monotonic()
        cancelled = client.post("/responses/" + response_id + "/cancel", headers={
            "x-agent-session-id": IDENTITY.session_id,
        })
        assert cancelled.status_code == 200, cancelled.text
        closed = harness.wait("running-rid", "unknown")
        settled = time.monotonic() - cancelling
        result = harness.response_result(response_id)
    assert closed.failure_code == "NATIVE_TURN_CANCELLED"
    assert result["status"] == "cancelled"
    assert len(sdk.calls) == 1 and sdk.controls["running-rid"].stop_requested() and sdk.active == 0
    assert settled < PROMPT_SECONDS, settled
