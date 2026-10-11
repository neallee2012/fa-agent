"""The sender releases its turn; the original SDK task owns subsequent delivery."""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_agent_wire import NativePeerRequest, NativeRelayAcceptance, NativeRelaySend
from shared_context.behavior_native_turn_wire import NativeEnvironmentView, NativeRead
from shared_context.native_agent_tools import NativeAgentRelayClient
from shared_context.native_agent_relay import prepare_request, queued_request_matches
from shared_context.native_turns import NativeTurnRuntime
from test_native_agent_client import token
from test_native_agent_relay import sender_case
from test_native_responses_relay import fast_follow, task_runs
from test_native_responses import ResponsesHarness, build_responses_host, response_submit
from test_native_turn_wire import capture, submit
from test_native_turns import IDENTITY, Harness, OfflineSdk, harness, local_env, native_retry_policy, sdk_result, seed_started


def test_sender_returns_after_task_acceptance_without_waiting_for_any_peer_http_reply(harness, task_runs):
    _, original = seed_started(harness)
    waiting, release = threading.Event(), asyncio.Event()
    calls = []

    async def peer(packet):
        calls.append(bytes(packet.content))
        waiting.set()
        await release.wait()
        return httpx.Response(202, content=NativeRelayAcceptance(
            message_id="original-immediate-message", recipient_agent_id="peer", conversation_id="peer-cid",
            submit_digest="f" * 64, status="accepted").model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(peer))
    client.runtime = harness.runtime
    sent = harness.client.portal.start_task_soon(client.send, original.value.receipt, "immediate-call",
                                                NativePeerRequest(recipient_agent_id="peer", kind="request", content="Work."))
    try:
        queued = sent.result(timeout=3)
        assert queued.status == "queued"
        assert len(task_runs) == 1 and queued.task_id == task_runs[0].task_id
        assert harness.call(harness.runtime.read, queued.request_id).queued_relay.status == "queued"
        assert waiting.wait(3), "An idle receiver is dispatched immediately, not on a later poll"
    finally:
        harness.call(release.set)
        sent.result(timeout=3)
    assert len(calls) == 1


def test_not_ready_recipient_is_normal_waiting_not_a_ten_failure_budget(harness, fast_follow):
    _, original = seed_started(harness)
    calls = []
    accepted = NativeRelayAcceptance(
        message_id="original-later-ready", recipient_agent_id="peer", conversation_id="peer-cid",
        submit_digest="a" * 64, status="accepted")

    def peer(packet):
        calls.append(bytes(packet.content))
        if len(calls) <= 12:
            return httpx.Response(409, json={"error": {"code": "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"}})
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(peer))
    client.runtime = harness.runtime
    queued = harness.call(client.send, original.value.receipt, "not-ready",
                          NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original request."))
    assert queued.status == "queued"

    async def ready():
        for _ in range(200):
            value = await harness.runtime.read(queued.request_id)
            if value.delegation is not None:
                return value
            await asyncio.sleep(0.01)
        raise AssertionError("Normal recipient readiness was treated as a failed delivery")

    assert harness.call(ready).delegation == accepted
    assert len(calls) == 13 and len(set(calls)) == 1


def test_sender_finishes_and_accepts_another_turn_before_queued_delivery(harness, fast_follow, monkeypatch):
    from shared_context import native_agent_tools

    _, scope, recipient, principal, _ = sender_case()
    scope = scope.model_copy(update={"target": scope.target.model_copy(update={"agent_id": IDENTITY.agent_id})})
    ready, delivered = asyncio.Event(), asyncio.Event()
    calls, receives = [], []

    async def relay(packet):
        command = NativeRelaySend.model_validate_json(packet.content)
        sender = await harness.runtime.read(command.request_id)
        calls.append((packet.url.path, bytes(packet.content), sender.status))
        if "/environment" in packet.url.path:
            assert sender.status == "started" or queued_request_matches(sender, command)
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id=recipient.target.agent_id, request_id="same-follow",
                previous_hosted_session_id="old-peer", previous_agent_version="41",
                hosted_session_id="new-peer", hosted_agent_version="42",
                state="ready" if ready.is_set() else "pending",
                reason=None if ready.is_set() else "accepted_or_started",
                progress_token=("2" if ready.is_set() else "1") * 64).model_dump_json().encode())
        if not ready.is_set():
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        receive = prepare_request(principal, scope, sender, recipient, capture(), command.message,
                                  tool_call_id=command.tool_call_id)
        receives.append(receive)
        delivered.set()
        return httpx.Response(202, content=NativeRelayAcceptance(
            message_id=receive.command.request_id, recipient_agent_id=recipient.target.agent_id,
            conversation_id="peer-original-cid", submit_digest=receive.sha256, status="accepted").model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime

    async def executor(receipt, scope, previous, control):
        if receipt.request_id == "rid-1":
            result = await client.send(receipt, "same-tool-call", NativePeerRequest(
                recipient_agent_id=recipient.target.agent_id, kind="request", content="Original queued request."))
            assert result.status == "queued"
        return sdk_result(scope, "Queued, not delivered." if receipt.request_id == "rid-1" else "Another task finished.")

    harness.runtime.executor = executor
    assert harness.invoke(submit()).status_code == 202
    first = harness.wait("rid-1", "completed")
    assert first.queued_relay.status == "queued" and first.delegation is None
    raw = harness.call(harness.runtime._rid, "rid-1").value.model_dump_json()
    assert harness.invoke(submit("rid-2", first.conversation_id)).status_code == 202
    assert harness.wait("rid-2", "completed").turn_number == 2
    assert receives == []
    harness.call(ready.set)
    harness.call(asyncio.wait_for, delivered.wait(), 3)

    async def confirmed():
        for _ in range(100):
            value = await harness.runtime.read("rid-1")
            if value.delegation is not None:
                return value
            await asyncio.sleep(0.01)
        raise AssertionError("The original SDK task did not retain its acceptance")

    result = harness.call(confirmed)
    assert result.queued_relay.status == "accepted" and result.delegation.message_id == receives[0].command.request_id
    assert len(receives) == 1
    assert receives[0].source.sender.request_id == "rid-1" and receives[0].source.sender.conversation_id == first.conversation_id
    assert harness.call(harness.runtime._rid, "rid-1").value.model_dump_json() == raw
    assert [status for path, _body, status in calls if path.endswith("/send")][-1] == "completed"
    assert len({body for path, body, _status in calls if path.endswith("/send")}) == 1


@pytest.mark.parametrize("unknown_first_send", [False, True, "admission"])
def test_official_timeout_distinguishes_unsent_wait_from_an_unconfirmed_post(
    harness, fast_follow, monkeypatch, unknown_first_send,
):
    from datetime import timedelta
    from shared_context import native_agent_tools

    decorator = native_agent_tools.task
    monkeypatch.setattr(native_agent_tools, "task", lambda **kwargs: decorator(
        **kwargs, timeout=timedelta(seconds=0.25)))
    _, original = seed_started(harness)
    calls = []

    async def relay(packet):
        calls.append(packet.url.path)
        if packet.url.path.endswith("/send"):
            code = ("NATIVE_ADMISSION_PENDING" if unknown_first_send == "admission" else "FOUNDRY_INVOCATION_UNKNOWN"
                    ) if unknown_first_send and len(calls) == 1 else "AGENT_VERSION_FOLLOWING"
            return httpx.Response(503, json={"error": {"code": code}})
        return httpx.Response(200, content=NativeEnvironmentView(
            agent_id="peer", request_id="same-follow", previous_hosted_session_id="old-peer",
            previous_agent_version="41", hosted_session_id="new-peer", hosted_agent_version="42",
            state="pending", reason="task_active", progress_token="1" * 64).model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime
    queued = harness.call(client.send, original.value.receipt, "timeout-call",
                          NativePeerRequest(recipient_agent_id="peer", kind="request", content="Keep the original."))
    assert queued.status == "queued"
    raw = harness.call(harness.runtime._rid, original.value.receipt.request_id).value.model_dump_json()

    async def expired():
        for _ in range(200):
            value = await harness.runtime.read(original.value.receipt.request_id)
            if value.queued_relay.status != "queued":
                return value
            await asyncio.sleep(0.01)
        raise AssertionError("The official task timeout was not reflected by the original delivery receipt")

    value = harness.call(expired)
    assert value.queued_relay.status == ("unknown" if unknown_first_send else "not_delivered")
    assert value.queued_relay.reason == "NATIVE_RELAY_WAIT_EXPIRED" and value.delegation is None
    assert calls.count("/work-policy/v1/native-agent-messages/send") == (2 if unknown_first_send is True else 1)
    assert harness.call(harness.runtime._rid, original.value.receipt.request_id).value.model_dump_json() == raw


def test_a_background_receiver_busy_refusal_is_not_left_queued_forever(harness, fast_follow, monkeypatch):
    from shared_context import native_agent_tools

    _, original = seed_started(harness)
    released = asyncio.Event()

    async def relay(packet):
        if "/environment" in packet.url.path:
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id="peer", request_id="same-follow", previous_hosted_session_id="old-peer",
                previous_agent_version="41", hosted_session_id="new-peer", hosted_agent_version="42",
                state="ready" if released.is_set() else "pending",
                reason=None if released.is_set() else "task_active",
                progress_token=("2" if released.is_set() else "1") * 64).model_dump_json().encode())
        if not released.is_set():
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        return httpx.Response(202, content=NativeRelayAcceptance(
            message_id="refused-original", recipient_agent_id="peer", conversation_id="peer-cid",
            submit_digest="f" * 64, status="busy_refused").model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime
    queued = harness.call(client.send, original.value.receipt, "refused-call",
                          NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original brief."))
    assert queued.status == "queued"
    harness.call(released.set)

    async def refused():
        for _ in range(100):
            receipt = await harness.runtime.read(original.value.receipt.request_id)
            if receipt.queued_relay.status != "queued":
                return receipt
            await asyncio.sleep(0.01)
        raise AssertionError("The stopped original delivery still claims it is queued")

    result = harness.call(refused)
    assert result.queued_relay.status == "not_delivered"
    assert result.queued_relay.reason == "NATIVE_RELAY_QUEUE_FULL" and result.delegation is None


def test_ready_response_arriving_after_official_timeout_does_not_send(harness, fast_follow, monkeypatch):
    from datetime import timedelta
    from shared_context import native_agent_tools

    decorate = native_agent_tools.task
    monkeypatch.setattr(native_agent_tools, "task", lambda **kw: decorate(**kw, timeout=timedelta(seconds=0.1)))
    _, original = seed_started(harness)
    sends = []
    replied = asyncio.Event()

    async def relay(packet):
        if packet.url.path.endswith("/send"):
            sends.append(bytes(packet.content))
            if len(sends) == 1:
                return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
            return httpx.Response(202, content=NativeRelayAcceptance(
                message_id="too-late", recipient_agent_id="peer", conversation_id="late-cid",
                submit_digest="b" * 64, status="accepted").model_dump_json().encode())
        await asyncio.sleep(0.25)
        replied.set()
        return httpx.Response(200, content=NativeEnvironmentView(
            agent_id="peer", request_id="same-follow", previous_hosted_session_id="old-peer",
            previous_agent_version="41", hosted_session_id="new-peer", hosted_agent_version="42",
            state="ready").model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime
    reference = harness.call(client.send, original.value.receipt, "late-ready",
                             NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original input."))
    assert reference.status == "queued"
    harness.call(asyncio.wait_for, replied.wait(), 3)

    async def settled():
        for _ in range(100):
            value = await harness.runtime.read(reference.request_id)
            if value.queued_relay.status != "queued":
                return value
            await asyncio.sleep(0.01)
        raise AssertionError("The expired delivery did not retain a terminal observation")

    result = harness.call(settled)
    assert len(sends) == 1
    assert result.queued_relay.status == "not_delivered" and result.delegation is None


def test_same_id_reentry_after_an_unknown_delivery_only_reads_original_records(harness, fast_follow, monkeypatch):
    from shared_context import native_agent_tools

    _, original = seed_started(harness)
    failed = asyncio.Event()
    calls = []

    async def relay(packet):
        calls.append(packet.url.path)
        if packet.url.path.endswith("/send"):
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        if failed.is_set():
            return httpx.Response(503, json={"error": {"code": "NATIVE_FILES_UNKNOWN"}})
        return httpx.Response(200, content=NativeEnvironmentView(
            agent_id="peer", request_id="same-follow", previous_hosted_session_id="old-peer",
            previous_agent_version="41", hosted_session_id="new-peer", hosted_agent_version="42",
            state="pending", reason="task_active", progress_token="1" * 64).model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime
    message = NativePeerRequest(recipient_agent_id="peer", kind="request", content="One original message.")
    first = harness.call(client.send, original.value.receipt, "unknown-call", message)
    assert first.status == "queued"
    harness.call(failed.set)

    async def ended():
        for _ in range(100):
            value = await harness.runtime.read(first.request_id)
            if value.queued_relay.status == "unknown":
                return value
            await asyncio.sleep(0.01)
        raise AssertionError("The original delivery did not retain its unknown outcome")

    before = harness.call(ended)
    count = len(calls)
    again = harness.call(client.send, original.value.receipt, "unknown-call", message)
    assert again.status == "unknown"
    assert len(calls) == count
    assert harness.call(harness.runtime.read, first.request_id) == before


@pytest.mark.parametrize("stored", [False, True], ids=["no-task", "lost-task-ack"])
def test_task_start_ack_loss_only_joins_the_original_task_and_never_claims_a_missing_queue(
    harness, fast_follow, monkeypatch, stored,
):
    from azure.ai.agentserver.core.tasks import Task
    from azure.core.exceptions import ServiceResponseError
    from shared_context import native_agent_tools
    from shared_context.errors import Problem

    _, original = seed_started(harness)
    start = Task.start
    starts, posts, notices = [], [], []

    async def lost(task, **kwargs):
        starts.append(kwargs["task_id"])
        if stored:
            await start(task, **kwargs)
        raise ServiceResponseError("OFFLINE task start ACK lost")

    monkeypatch.setattr(Task, "start", lost)

    def relay(packet):
        if packet.url.path.endswith("/send"):
            posts.append(bytes(packet.content))
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        return httpx.Response(200, content=NativeEnvironmentView(
            agent_id="peer", request_id="same-follow", previous_hosted_session_id="old-peer",
            previous_agent_version="41", hosted_session_id="new-peer", hosted_agent_version="42",
            state="pending", reason="task_active", progress_token="1" * 64).model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime

    async def send():
        return await client.send(original.value.receipt, "lost-task-call",
                                 NativePeerRequest(recipient_agent_id="peer", kind="request", content="One original."),
                                 on_queued=lambda: notices.append("queued"))

    if stored:
        queued = harness.call(send)
        assert queued.status == "queued" and notices == ["queued"]
        assert harness.call(harness.runtime.read, queued.request_id).queued_relay == queued
    else:
        with pytest.raises(Problem):
            harness.call(send)
        assert not notices
        assert harness.call(harness.runtime.read, original.value.receipt.request_id).queued_relay is None
    assert len(starts) == 1 and len(posts) <= int(stored)


@pytest.mark.parametrize("lane", ["invocations", "responses"])
@pytest.mark.parametrize("interrupted", [False, True], ids=["sender-completed", "sender-interrupted"])
def test_finished_sender_queue_survives_two_host_restarts_without_replaying_the_model(
    local_env, fast_follow, monkeypatch, tmp_path, lane, interrupted,
):
    from shared_context import native_agent_tools

    if lane == "responses":
        monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    state = {}
    ready, entered, delivered = threading.Event(), threading.Event(), threading.Event()
    calls, sent = [], []
    accepted = NativeRelayAcceptance(
        message_id="restored-message", recipient_agent_id="peer", conversation_id="restored-peer-cid",
        submit_digest="e" * 64, status="accepted")

    async def relay(packet):
        command = NativeRelaySend.model_validate_json(packet.content)
        sender = await state["runtime"].read(command.request_id)
        calls.append((packet.url.path, bytes(packet.content)))
        if sender.status != "started" and not queued_request_matches(sender, command):
            return httpx.Response(403, json={"error": {"code": "NATIVE_AGENT_SOURCE_INVALID"}})
        if "/environment" in packet.url.path:
            entered.set()
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id="peer", request_id="restored-follow", previous_hosted_session_id="old-peer",
                previous_agent_version="41", hosted_session_id="new-peer", hosted_agent_version="42",
                state="ready" if ready.is_set() else "pending",
                reason=None if ready.is_set() else "task_active", progress_token="a" * 64).model_dump_json().encode())
        if not ready.is_set():
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        sent.append(command)
        delivered.set()
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    class Sender(OfflineSdk):
        async def __call__(self, receipt, scope, previous, control):
            self.calls.append(receipt.request_id)
            result = await self.client.send(receipt, "restored-call", NativePeerRequest(
                recipient_agent_id="peer", kind="request", content="Original asynchronous request."))
            assert result.status == "queued"
            if interrupted:
                from shared_context.errors import Problem
                while not control.stop_requested():
                    await asyncio.sleep(0.01)
                raise Problem(503, "NATIVE_TURN_UNKNOWN", "OFFLINE sender interrupted after persistent queue acceptance")
            return sdk_result(scope, "Queued, awaiting peer.")

    journal = tmp_path / "original-journal"
    journal.mkdir()

    def build(sdk):
        runtime = NativeTurnRuntime(IDENTITY, sdk)
        relay_client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
        relay_client.runtime = runtime
        sdk.client = relay_client
        runtime.on_recover_queued = lambda original: relay_client.recover_queued(runtime, original)
        state["runtime"] = runtime
        host = (build_responses_host(runtime, journal, {}) if lane == "responses" else
                create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime))
        return runtime, host

    original_sdk = Sender()
    runtime, host = build(original_sdk)

    async def task_started(reference):
        from azure.ai.agentserver.core.tasks._manager import get_task_manager
        task = await get_task_manager().provider.get(reference.task_id)
        assert task is not None
        return task.payload["turn_started_at"]

    with TestClient(host) as http:
        h = ResponsesHarness(runtime, original_sdk, http) if lane == "responses" else Harness(runtime, original_sdk, http)
        assert h.invoke(response_submit() if lane == "responses" else submit()).status_code == 202
        if lane == "responses":
            assert h.respond(h.call(runtime.read, "rid-1")).status_code == 200
        async def queued():
            for _ in range(100):
                value = await runtime.read("rid-1")
                if value.queued_relay is not None:
                    return value
                await asyncio.sleep(0.01)
            raise AssertionError("The original sender did not retain the queued task")

        completed = h.call(queued) if interrupted else h.wait("rid-1", "completed")
        assert completed.queued_relay.status == "queued"
        raw = h.call(runtime._rid, "rid-1").value.model_dump_json()
        started_at = h.call(task_started, completed.queued_relay)
        assert len(original_sdk.calls) == 1 and not sent
    entered.clear()
    middle_sdk = OfflineSdk()
    runtime, host = build(middle_sdk)
    with TestClient(host) as http:
        assert entered.wait(4), "The original task must recover even though its sender is completed"
        if interrupted:
            completed = Harness(runtime, middle_sdk, http).wait("rid-1", "unknown")
            raw = http.portal.call(runtime._rid, "rid-1").value.model_dump_json()
        assert http.portal.call(runtime.read, "rid-1").queued_relay.status == "queued"
        assert http.portal.call(task_started, completed.queued_relay) == started_at
        assert not middle_sdk.calls and not sent
    ready.set()
    final_sdk = OfflineSdk()
    runtime, host = build(final_sdk)
    with TestClient(host) as http:
        assert delivered.wait(4)

        async def saved():
            for _ in range(100):
                value = await runtime.read("rid-1")
                if value.delegation is not None:
                    return value
                await asyncio.sleep(0.01)
            raise AssertionError("The original acceptance did not persist")

        result = http.portal.call(saved)
        assert result.status == ("unknown" if interrupted else "completed") and result.result == completed.result
        assert result.queued_relay.status == "accepted" and result.delegation == accepted
        assert http.portal.call(runtime._rid, "rid-1").value.model_dump_json() == raw
    assert not final_sdk.calls and len(sent) == 1
    assert len([path for path, _ in calls if path.endswith("/send")]) == 3
    assert len({body for _, body in calls}) == 1
