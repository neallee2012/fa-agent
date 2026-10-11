"""N6 T5 caller through official local tasks; the receiving API is an explicit HTTP boundary double."""

import asyncio
import json
import threading

import httpx
import pytest

from shared_context.behavior_native_agent_wire import NativePeerRequest, NativeRelayAcceptance
from shared_context.errors import Problem
from shared_context.native_agent_tools import NativeAgentRelayClient
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NativeResponseDispatch
from test_native_agent_client import token
from test_native_agent_relay import sender_case
from test_native_turns import harness, local_env, native_retry_policy
from test_native_turn_wire import agent_receive


@pytest.fixture
def fast_follow(monkeypatch):
    from shared_context import native_agent_tools

    policy = native_agent_tools.RetryPolicy

    def shorter_clock(**options):
        return policy(**{**options, "initial_delay": 0.001, "max_delay": 0.002, "jitter": False})

    monkeypatch.setattr(native_agent_tools, "RetryPolicy", shorter_clock)


@pytest.fixture
def task_runs(monkeypatch):
    from azure.ai.agentserver.core.tasks import Task

    runs, start = [], Task.start

    async def observed(task, *args, **kwargs):
        run = await start(task, *args, **kwargs)
        runs.append(run)
        return run

    monkeypatch.setattr(Task, "start", observed)
    return runs


async def task_results(runs):
    return await asyncio.wait_for(asyncio.gather(*(run.result() for run in runs)), timeout=5)


@pytest.mark.parametrize("failure", ["timeout", "http-500", "http-503"])
def test_lost_first_peer_ack_keeps_original_follow_until_responses_dispatch_is_owned(
    harness, fast_follow, task_runs, failure,
):
    _, _, recipient, _, sender = sender_case()
    calls, taken, reading = [], threading.Event(), threading.Event()
    admit, dispatch = asyncio.Event(), asyncio.Event()
    accepted = NativeRelayAcceptance(
        message_id="lost-first-response-ack", recipient_agent_id=recipient.target.agent_id,
        conversation_id="lost-first-response-cid", submit_digest="a" * 64, status="accepted",
        state_profile=NATIVE_RESPONSES_PROFILE,
        dispatch=NativeResponseDispatch(state="needs_response", inflight_request_id="lost-first-response-ack"))

    async def relay(request):
        calls.append((request.url.path, bytes(request.content)))
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("The original peer ACK was lost.", request=request)
            return httpx.Response(int(failure.removeprefix("http-")), json={
                "error": {"code": "FOUNDRY_INVOCATION_UNKNOWN"}})
        if len(calls) == 2:
            reading.set()
            await admit.wait()
            return httpx.Response(202, content=accepted.model_dump_json().encode())
        await dispatch.wait()
        taken.set()
        queued = accepted.model_copy(update={"dispatch": NativeResponseDispatch(
            state="already_queued", inflight_request_id=accepted.message_id)})
        return httpx.Response(202, content=queued.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(
        recipient_agent_id=recipient.target.agent_id, kind="request", content="Keep the original uncertain handoff.")
    sent = harness.client.portal.start_task_soon(client.send, sender, "lost-ack-call", message)
    try:
        assert reading.wait(3)
        assert not sent.done(), "the sender must remain in its original turn until the admission ACK is known"
        harness.call(admit.set)
        assert sent.result(timeout=3) == accepted
        assert not taken.is_set(), "the sender waits for admission, never the Responses dispatch or model"
    finally:
        harness.call(admit.set)
        harness.call(dispatch.set)
    assert taken.wait(3), "a lost first ACK must not strand the peer's durable accepted request"
    results = harness.call(task_results, task_runs)
    assert len(results) == 2
    assert sum(result.dispatch.state == "already_queued" for result in results) == 1
    assert len(calls) == 3 and len(set(calls)) == 1
    assert not harness.sdk.calls


@pytest.mark.parametrize("changing_environment", [False, True])
def test_persisted_peer_acceptance_returns_before_follow_dispatches_the_same_message(
    harness, fast_follow, task_runs, changing_environment,
):
    _, _, recipient, _, sender = sender_case()
    gate = asyncio.Event()
    following, taken = threading.Event(), threading.Event()
    calls = []
    accepted = NativeRelayAcceptance(
        message_id="original-response-message", recipient_agent_id=recipient.target.agent_id,
        conversation_id="original-peer-cid", submit_digest="a" * 64, status="accepted",
        state_profile=NATIVE_RESPONSES_PROFILE,
        dispatch=NativeResponseDispatch(state="not_taken", inflight_request_id="busy-original-rid"))

    async def relay(request):
        calls.append((request.url.path, json.loads(request.content)))
        if changing_environment and len(calls) == 1:
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        if request.url.path.endswith("/environment"):
            from shared_context.behavior_native_turn_wire import NativeEnvironmentView
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id=recipient.target.agent_id, request_id="existing-follow",
                previous_hosted_session_id="old-peer", previous_agent_version="41",
                hosted_session_id="new-peer", hosted_agent_version="42", state="ready").model_dump_json().encode())
        if len(calls) == 1 + 2 * int(changing_environment):
            return httpx.Response(202, content=accepted.model_dump_json().encode())
        following.set()
        await gate.wait()
        taken.set()
        started = accepted.model_copy(update={"status": "started", "response_id": "resp-original", "dispatch": None})
        return httpx.Response(202, content=started.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original peer input.")
    sent = harness.client.portal.start_task_soon(client.send, sender, "original-tool-call", message)
    try:
        assert sent.result(timeout=3) == accepted
        assert following.wait(3), "accepted Responses work must retain an official follow, not disappear after the ACK"
        assert not taken.is_set(), "the source must not wait for the receiver's turn"
    finally:
        harness.call(gate.set)
    assert taken.wait(3)
    results = harness.call(task_results, task_runs)
    assert any(isinstance(result, NativeRelayAcceptance) and result.status == "started" for result in results)
    assert len(calls) == 2 + 2 * int(changing_environment)
    assert all(body == calls[0][1] for _path, body in calls)
    assert calls[0][1]["request_id"] == sender.request_id
    assert calls[0][1]["tool_call_id"] == "original-tool-call"
    assert not harness.sdk.calls


def test_uncertain_first_send_uses_the_original_finite_retry_budget(harness, fast_follow, task_runs):
    from azure.ai.agentserver.core.tasks import TaskFailed

    _, _, recipient, _, sender = sender_case()
    calls = []

    def relay(request):
        calls.append(bytes(request.content))
        return httpx.Response(503, json={"error": {"code": "FOUNDRY_INVOCATION_UNKNOWN"}})

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original input.")
    with pytest.raises(Problem) as unknown:
        harness.call(client.send, sender, "original-finite-call", message)
    assert unknown.value.status == 503 and unknown.value.body["error"]["code"] == "NATIVE_RELAY_OUTCOME_UNKNOWN"
    assert len(calls) == 11
    with pytest.raises(TaskFailed):
        harness.call(task_results, task_runs)
    assert len(task_runs) == 1 and len(calls) == 11 and len(set(calls)) == 1
    assert not harness.sdk.calls


def test_cancelled_original_ack_task_preserves_unknown_instead_of_claiming_refusal(harness, task_runs):
    from azure.ai.agentserver.core.tasks import TaskCancelled

    _, _, recipient, _, sender = sender_case()
    following = threading.Event()
    calls = []

    def relay(request):
        calls.append(bytes(request.content))
        if len(calls) > 1:
            following.set()
        return httpx.Response(503, json={"error": {"code": "FOUNDRY_INVOCATION_UNKNOWN"}})

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original input.")
    sent = harness.client.portal.start_task_soon(client.send, sender, "original-cancelled-ack", message)
    assert following.wait(3)
    harness.call(task_runs[0].cancel)
    with pytest.raises(Problem) as unknown:
        sent.result(timeout=3)
    assert unknown.value.body["error"]["code"] == "NATIVE_RELAY_OUTCOME_UNKNOWN"
    with pytest.raises(TaskCancelled):
        harness.call(task_results, task_runs)
    assert len(task_runs) == 1 and len(set(calls)) == 1


@pytest.mark.parametrize("status,code", [(403, "NATIVE_AGENT_SOURCE_INVALID"), (503, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE")])
def test_an_explicit_first_refusal_does_not_start_an_uncertain_send_follow(harness, task_runs, status, code):
    _, _, recipient, _, sender = sender_case()
    calls = []

    def relay(request):
        calls.append(bytes(request.content))
        return httpx.Response(status, json={"error": {"code": code}})

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original input.")
    with pytest.raises(Problem):
        harness.call(client.send, sender, "original-refused-call", message)
    assert len(calls) == 1 and not task_runs


def test_normal_waiting_exceeds_failure_budget_then_stops_when_already_queued(harness, fast_follow, task_runs):
    _, _, recipient, _, sender = sender_case()
    calls, queued = [], threading.Event()
    original = NativeRelayAcceptance(
        message_id="same-waiting-message", recipient_agent_id=recipient.target.agent_id,
        conversation_id="same-waiting-cid", submit_digest="b" * 64, status="accepted",
        state_profile=NATIVE_RESPONSES_PROFILE,
        dispatch=NativeResponseDispatch(state="not_taken", inflight_request_id="busy-original-rid"))

    async def relay(request):
        calls.append(bytes(request.content))
        if len(calls) < 14:
            return httpx.Response(202, content=original.model_dump_json().encode())
        queued.set()
        value = original.model_copy(update={"dispatch": NativeResponseDispatch(
            state="already_queued", inflight_request_id=original.message_id)})
        return httpx.Response(202, content=value.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Same original input.")
    assert harness.call(client.send, sender, "same-call", message) == original
    assert queued.wait(3), "waiting for the original CID slot is not a failed attempt"
    results = harness.call(task_results, task_runs)
    assert len(results) == 1 and results[0].dispatch.state == "already_queued"
    assert len(calls) == 14 and len(set(calls)) == 1
    assert not harness.sdk.calls


def test_recipient_version_follow_waits_in_the_original_sdk_task_without_a_portal_or_failure_budget(
    harness, fast_follow, task_runs,
):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    _, _, recipient, _, sender = sender_case()
    calls, steps = [], []
    accepted = NativeRelayAcceptance(
        message_id="same-environment-wait-message", recipient_agent_id=recipient.target.agent_id,
        conversation_id="same-environment-peer-cid", submit_digest="e" * 64, status="accepted")

    async def relay(request):
        command = json.loads(request.content)
        calls.append((request.url.path, command))
        if request.url.path.endswith("/environment"):
            steps.append(command)
            ready = len(steps) >= 14
            view = NativeEnvironmentView(
                agent_id=recipient.target.agent_id, request_id="same-server-follow",
                previous_hosted_session_id="old-peer-session", previous_agent_version="41",
                hosted_session_id="new-peer-session", hosted_agent_version="42",
                state="ready" if ready else "pending", reason=None if ready else "scan_pending",
                progress_token=str(len(steps)).zfill(64))
            return httpx.Response(200, content=view.model_dump_json().encode())
        if request.url.path.endswith("/environment/observe"):
            view = NativeEnvironmentView(
                agent_id=recipient.target.agent_id, request_id="same-server-follow",
                previous_hosted_session_id="old-peer-session", previous_agent_version="41",
                hosted_session_id="new-peer-session", hosted_agent_version="42",
                state="pending", reason="scan_pending", progress_token=str(len(steps)).zfill(64))
            return httpx.Response(200, content=view.model_dump_json().encode())
        assert request.url.path.endswith("/send")
        if len(steps) < 14:
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Keep this queued input.")
    result = harness.call(client.send, sender, "same-queued-call", message)
    assert result == accepted
    assert len(steps) == 14 and all(command == steps[0] for command in steps)
    sends = [command for path, command in calls if path.endswith("/send")]
    assert len(sends) == 2 and sends[0] == sends[1]
    assert len(task_runs) == 1 and not harness.sdk.calls


def test_waiting_notice_only_follows_durable_task_acceptance_and_is_not_a_delivery_receipt(
    harness, fast_follow, task_runs,
):
    from functools import partial
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    _, _, recipient, _, sender = sender_case()
    held, queued, entered = asyncio.Event(), threading.Event(), threading.Event()
    calls, notice = [], []

    async def relay(request):
        calls.append((request.url.path, bytes(request.content)))
        if request.url.path.endswith("/environment"):
            entered.set()
            await held.wait()
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id=recipient.target.agent_id, request_id="original-follow",
                previous_hosted_session_id="old-peer", previous_agent_version="41",
                hosted_session_id="new-peer", hosted_agent_version="42", state="ready").model_dump_json().encode())
        if not held.is_set():
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        return httpx.Response(202, content=NativeRelayAcceptance(
            message_id="unchanged-message", recipient_agent_id=recipient.target.agent_id,
            conversation_id="original-peer-cid", submit_digest="f" * 64, status="accepted").model_dump_json().encode())

    def on_queued():
        assert len(task_runs) == 1
        notice.append("queued-not-delivered")
        queued.set()

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Wait for the peer.")
    result = harness.client.portal.start_task_soon(partial(
        client.send, sender, "same-queue-call", message, on_queued=on_queued))
    try:
        assert queued.wait(3) and entered.wait(3)
        assert not result.done(), "a queued transport task is not the receiver's acceptance"
        assert notice == ["queued-not-delivered"]
        assert len([call for call in calls if call[0].endswith("/send")]) == 1
    finally:
        harness.call(held.set)
    assert result.result(timeout=3).status == "accepted"
    sends = [body for path, body in calls if path.endswith("/send")]
    assert len(sends) == 2 and len(set(sends)) == 1
    assert len(task_runs) == 1 and not harness.sdk.calls


def test_same_queued_message_joins_the_original_active_task_instead_of_starting_another(
    harness, fast_follow, task_runs,
):
    from functools import partial
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    _, _, recipient, _, sender = sender_case()
    held, entered, joined = asyncio.Event(), threading.Event(), threading.Event()
    sends, environment_calls, notices = [], [], []

    async def relay(packet):
        if packet.url.path.endswith("/environment"):
            environment_calls.append(bytes(packet.content))
            entered.set()
            await held.wait()
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id=recipient.target.agent_id, request_id="same-follow",
                previous_hosted_session_id="old-peer", previous_agent_version="41",
                hosted_session_id="new-peer", hosted_agent_version="42", state="ready").model_dump_json().encode())
        sends.append(bytes(packet.content))
        if not held.is_set():
            if len(sends) == 2:
                joined.set()
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        return httpx.Response(202, content=NativeRelayAcceptance(
            message_id="same-message", recipient_agent_id=recipient.target.agent_id,
            conversation_id="same-peer-cid", submit_digest="d" * 64, status="accepted").model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original shared wait.")
    send = partial(client.send, sender, "same-call", message, on_queued=lambda: notices.append("queued"))
    first = harness.client.portal.start_task_soon(send)
    assert entered.wait(3)
    second = harness.client.portal.start_task_soon(send)
    try:
        assert joined.wait(3)
        assert len(task_runs) == 1 and notices == ["queued"]
    finally:
        harness.call(held.set)
    assert first.result(timeout=3) == second.result(timeout=3)
    assert len(environment_calls) == 1 and len(sends) == 3 and len(set(sends)) == 1
    assert not harness.sdk.calls


def test_sender_returns_queued_immediately_while_original_delivery_keeps_waiting(
    harness, fast_follow, task_runs, monkeypatch,
):
    from shared_context import native_agent_tools
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    from test_native_turns import seed_started
    _, _, recipient, _, _ = sender_case()
    _, started = seed_started(harness)
    sender = started.value.receipt
    ready, entered = asyncio.Event(), threading.Event()
    calls = []
    accepted = NativeRelayAcceptance(
        message_id="retained-async-message", recipient_agent_id=recipient.target.agent_id,
        conversation_id="retained-async-peer-cid", submit_digest="d" * 64, status="accepted")

    async def relay(packet):
        calls.append((packet.url.path, bytes(packet.content)))
        if "/environment" in packet.url.path:
            entered.set()
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id=recipient.target.agent_id, request_id="retained-async-follow",
                previous_hosted_session_id="old-peer", previous_agent_version="41",
                hosted_session_id="new-peer", hosted_agent_version="42",
                state="ready" if ready.is_set() else "pending",
                reason=None if ready.is_set() else "accepted_or_started",
                progress_token=("2" if ready.is_set() else "1") * 64,
            ).model_dump_json().encode())
        if not ready.is_set():
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Keep this queued.")
    sent = harness.client.portal.start_task_soon(client.send, sender, "original-async-call", message)
    try:
        assert entered.wait(3)
        queued = sent.result(timeout=2)
        assert queued.model_dump(mode="json")["status"] == "queued"
        assert queued.recipient_agent_id == recipient.target.agent_id
        assert len(task_runs) == 1
        assert sum(path.endswith("/send") for path, _body in calls) == 1
    finally:
        harness.call(ready.set)
        sent.result(timeout=3)
    assert harness.call(task_results, task_runs) == [accepted]
    sends = [body for path, body in calls if path.endswith("/send")]
    assert len(sends) == 2 and len(set(sends)) == 1
    assert not harness.sdk.calls


def test_environment_batch_waits_for_progress_and_cancellation_never_sends_the_message(
    harness, fast_follow, task_runs,
):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    _, _, recipient, _, sender = sender_case()
    advanced, observed, waiting, calls = [], [], threading.Event(), []

    async def relay(packet):
        calls.append((packet.url.path, bytes(packet.content)))
        if packet.url.path.endswith("/send"):
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        if packet.url.path.endswith("/environment/observe"):
            observed.append(True)
            if len(observed) >= 4:
                waiting.set()
        else:
            advanced.append(True)
        return httpx.Response(200, content=NativeEnvironmentView(
            agent_id=recipient.target.agent_id, request_id="waiting-follow",
            previous_hosted_session_id="old-peer", previous_agent_version="41",
            hosted_session_id="new-peer", hosted_agent_version="42", state="pending", reason="task_active",
            progress_token="1" * 64).model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original pending input.")
    sent = harness.client.portal.start_task_soon(client.send, sender, "cancel-original-queue", message)
    assert waiting.wait(3)
    assert len(advanced) == 10 and len(task_runs) == 1
    harness.call(task_runs[0].cancel)
    with pytest.raises(Problem) as uncertain:
        sent.result(timeout=3)
    assert uncertain.value.body["error"]["code"] == "NATIVE_RELAY_OUTCOME_UNKNOWN"
    assert len([path for path, _ in calls if path.endswith("/send")]) == 1
    assert len(advanced) == 10 and not harness.sdk.calls


@pytest.mark.parametrize("code,status", [
    ("NATIVE_FILES_UNKNOWN", 503), ("NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE", 409),
    ("NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE", 403),
])
def test_real_environment_failure_never_retries_the_message_or_advances_another_batch(
    harness, fast_follow, task_runs, caplog, code, status,
):
    _, _, recipient, _, sender = sender_case()
    calls = []

    def relay(packet):
        calls.append(packet.url.path)
        if packet.url.path.endswith("/send"):
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        return httpx.Response(status, json={"error": {"code": code, "message": "PRIVATE-original-content"}})

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original pending input.")
    with pytest.raises(Problem) as failure:
        harness.call(client.send, sender, "blocked-original", message)
    assert failure.value.body["error"]["code"] == "NATIVE_RELAY_OUTCOME_UNKNOWN"
    assert len(task_runs) == 1 and len(calls) == 2
    assert calls[0].endswith("/send") and calls[1].endswith("/environment")
    assert not harness.sdk.calls
    assert f"NATIVE_RELAY_REQUEST_FAILED route=/environment status={status} code={code}" in caplog.text
    assert "PRIVATE-original-content" not in caplog.text


def test_completed_peer_wake_uses_the_same_follow_without_waiting_for_a_new_parent_turn(harness, fast_follow, task_runs):
    request = agent_receive("original-peer-result")
    calls, followed = [], threading.Event()

    async def relay(packet):
        body = json.loads(packet.content)
        calls.append((packet.url.path, body))
        reply = NativeRelayAcceptance(
            message_id=request.source.message_id, recipient_agent_id=request.source.sender.agent_id,
            conversation_id=request.source.sender.conversation_id, submit_digest="c" * 64, status="accepted",
            source_conversation_id=body["conversation_id"], source_request_id=body["request_id"],
            state_profile=NATIVE_RESPONSES_PROFILE,
            dispatch=NativeResponseDispatch(
                state="not_taken" if len(calls) == 1 else "already_queued",
                inflight_request_id="parent-busy" if len(calls) == 1 else request.source.message_id))
        if len(calls) > 1:
            followed.set()
        return httpx.Response(202, content=reply.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    harness.runtime.on_completed = client.completed
    assert harness.invoke(request).status_code == 202
    complete = harness.wait(request.command.request_id, "completed")
    assert followed.wait(3)
    results = harness.call(task_results, task_runs)
    assert any(isinstance(result, NativeRelayAcceptance) and result.dispatch.state == "already_queued" for result in results)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][0].endswith("/result")
    assert complete.result is not None and len(harness.sdk.calls) == 1


@pytest.mark.parametrize("status,queued", [
    ("accepted", True), ("started", False), ("completed", False), ("unknown", False), ("busy_refused", False),
])
def test_an_already_owned_or_terminal_response_never_starts_another_follow(status, queued):
    _, _, recipient, _, sender = sender_case()
    calls = []
    reply = NativeRelayAcceptance(
        message_id="owned-original-message", recipient_agent_id=recipient.target.agent_id,
        conversation_id="owned-original-cid", submit_digest="d" * 64, status=status,
        state_profile=NATIVE_RESPONSES_PROFILE, response_id="resp-original" if status in {"started", "completed"} else None,
        dispatch=NativeResponseDispatch(state="already_queued", inflight_request_id="owned-original-message") if queued else None)

    def relay(request):
        calls.append(request)
        return httpx.Response(202, content=reply.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Already accepted.")
    assert asyncio.run(client.send(sender, "original-call", message)) == reply
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["transport", "denied", "changed-scope"])
def test_real_errors_keep_the_original_retry_boundary_and_never_replace_the_message(
    harness, fast_follow, task_runs, failure,
):
    from azure.ai.agentserver.core.tasks import TaskFailed

    _, _, recipient, _, sender = sender_case()
    calls = []
    accepted = NativeRelayAcceptance(
        message_id="original-error-message", recipient_agent_id=recipient.target.agent_id,
        conversation_id="original-error-cid", submit_digest="e" * 64, status="accepted",
        state_profile=NATIVE_RESPONSES_PROFILE,
        dispatch=NativeResponseDispatch(state="needs_response", inflight_request_id="original-error-message"))

    async def relay(packet):
        calls.append(bytes(packet.content))
        if len(calls) == 1:
            return httpx.Response(202, content=accepted.model_dump_json().encode())
        if failure == "transport":
            return httpx.Response(503, json={"error": {"code": "FOUNDRY_INVOCATION_UNKNOWN"}})
        if failure == "denied":
            return httpx.Response(403, json={"error": {"code": "NATIVE_AGENT_SOURCE_INVALID"}})
        changed = accepted.model_copy(update={
            "submit_digest": "f" * 64,
            "dispatch": NativeResponseDispatch(state="already_queued", inflight_request_id=accepted.message_id)})
        return httpx.Response(202, content=changed.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original accepted input.")
    assert harness.call(client.send, sender, "original-error-call", message) == accepted
    with pytest.raises(TaskFailed):
        harness.call(task_results, task_runs)
    assert len(calls) == (11 if failure == "transport" else 2)
    assert len(set(calls)) == 1 and not harness.sdk.calls


def test_follow_registration_unknown_preserves_the_confirmed_acceptance_and_says_it_is_unconfirmed(monkeypatch):
    from types import SimpleNamespace
    from azure.ai.agentserver.core.tasks import Task
    from azure.core.exceptions import AzureError
    from shared_context.native_agent_tools import NativeAgentTools
    from test_native_agent_tools import collaboration_turn

    original, _ = collaboration_turn()
    accepted = NativeRelayAcceptance(
        message_id="accepted-before-follow-fault", recipient_agent_id="receiver-agent",
        conversation_id="receiver-cid", submit_digest="f" * 64, status="accepted",
        state_profile=NATIVE_RESPONSES_PROFILE,
        dispatch=NativeResponseDispatch(state="needs_response", inflight_request_id="accepted-before-follow-fault"))
    retained, calls = [], []

    async def unavailable(_task, **_kwargs):
        raise AzureError("OFFLINE follow registration outcome unknown")

    async def retain(value):
        retained.append(value)

    def relay(packet):
        calls.append(packet.url.path)
        if packet.method == "GET":
            return httpx.Response(200, json={"agents": [{"agent_id": "receiver-agent", "display_name": "Receiver"}]})
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    monkeypatch.setattr(Task, "start", unavailable)
    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    tools = NativeAgentTools(client, original, SimpleNamespace(stop_requested=lambda: False, retain_delegation=retain))

    async def invoke():
        send = next(tool for tool in await tools.prepare() if tool.name == "send_agent_message")
        return await send.handler({"tool_call_id": "original-call", "arguments": {
            "recipient_agent_id": "receiver-agent", "kind": "request", "content": "Original peer input."}})

    output = asyncio.run(invoke())
    assert NativeRelayAcceptance.model_validate_json(output.text) == accepted
    assert tools.delegation == accepted and retained == [accepted]
    [text] = tools.handoff_texts
    assert "已持久接收" in text and "排程尚未確認" in text
    assert len(calls) == 2


def test_official_follow_cancellation_does_not_invent_a_peer_terminal_receipt(harness, task_runs):
    from azure.ai.agentserver.core.tasks import TaskCancelled

    _, _, recipient, _, sender = sender_case()
    accepted = NativeRelayAcceptance(
        message_id="cancel-follow-only", recipient_agent_id=recipient.target.agent_id,
        conversation_id="cancel-follow-cid", submit_digest="a" * 64, status="accepted",
        state_profile=NATIVE_RESPONSES_PROFILE,
        dispatch=NativeResponseDispatch(state="not_taken", inflight_request_id="original-busy"))
    calls = []

    def relay(packet):
        calls.append(json.loads(packet.content))
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Keep original receipt.")
    assert harness.call(client.send, sender, "cancel-follow-call", message) == accepted
    assert len(task_runs) == 1
    harness.call(task_runs[0].cancel)
    with pytest.raises(TaskCancelled):
        harness.call(task_results, task_runs)
    assert accepted.status == "accepted" and accepted.response_id is None
    assert all(call == calls[0] for call in calls) and not harness.sdk.calls


def test_shutdown_recovers_original_follow_in_the_same_session_without_another_send(local_env):
    from types import SimpleNamespace
    from starlette.testclient import TestClient
    from shared_context.behavior_hosted import create_host
    from shared_context.native_turns import NativeTurnRuntime
    from test_native_turns import IDENTITY, OfflineSdk

    _, _, recipient, _, sender = sender_case()
    accepted = NativeRelayAcceptance(
        message_id="recover-original-follow", recipient_agent_id=recipient.target.agent_id,
        conversation_id="recover-peer-cid", submit_digest="b" * 64, status="accepted",
        state_profile=NATIVE_RESPONSES_PROFILE,
        dispatch=NativeResponseDispatch(state="not_taken", inflight_request_id="original-busy"))
    calls, taken, ready = [], threading.Event(), False

    def relay(packet):
        calls.append(bytes(packet.content))
        if ready:
            taken.set()
            value = accepted.model_copy(update={"status": "started", "response_id": "resp-recovered", "dispatch": None})
        else:
            value = accepted
        return httpx.Response(202, content=value.model_dump_json().encode())

    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    sender_client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original retained input.")
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        assert client.portal.call(sender_client.send, sender, "retained-call", message) == accepted
    count = len(calls)
    ready = True
    current = NativeTurnRuntime(IDENTITY, OfflineSdk())
    restored = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    assert restored is not sender_client
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=current)):
        assert taken.wait(5), "the original official task must recover without calling send again"
    assert len(calls) == count + 1 and len(set(calls)) == 1
    assert not old.executor.calls and not current.executor.calls


def test_environment_wait_recovers_the_same_command_and_never_reexecutes_the_sender_model(local_env):
    from types import SimpleNamespace
    from azure.ai.agentserver.core.tasks import TaskDeferred
    from starlette.testclient import TestClient
    from shared_context.behavior_hosted import create_host
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView
    from shared_context.native_turns import NativeTurnRuntime
    from test_native_turns import IDENTITY, OfflineSdk

    _, _, recipient, _, sender = sender_case()
    calls, waiting, delivered, ready = [], threading.Event(), threading.Event(), [False]

    def relay(packet):
        calls.append((packet.url.path, bytes(packet.content)))
        if "/environment" in packet.url.path:
            waiting.set()
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id=recipient.target.agent_id, request_id="same-follow",
                previous_hosted_session_id="old-peer", previous_agent_version="41",
                hosted_session_id="new-peer", hosted_agent_version="42",
                state="ready" if ready[0] else "pending", reason=None if ready[0] else "scan_pending",
                progress_token="a" * 64).model_dump_json().encode())
        if not ready[0]:
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        delivered.set()
        return httpx.Response(202, content=NativeRelayAcceptance(
            message_id="same-recovered-message", recipient_agent_id=recipient.target.agent_id,
            conversation_id="same-peer-cid", submit_digest="c" * 64, status="accepted").model_dump_json().encode())

    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    original = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    message = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original queued brief.")
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        result = client.portal.start_task_soon(original.send, sender, "same-queued-call", message)
        assert waiting.wait(3)
        assert not result.done() and not delivered.is_set()
    with pytest.raises(TaskDeferred):
        result.result(timeout=3)
    ready[0] = True
    current = NativeTurnRuntime(IDENTITY, OfflineSdk())
    NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=current)):
        assert delivered.wait(5), "SDK recovery must continue the persisted environment wait without portal input"
    assert len({body for _path, body in calls}) == 1
    assert len([path for path, _ in calls if path.endswith("/send")]) == 2
    assert not old.executor.calls and not current.executor.calls


@pytest.mark.parametrize("recipient_profile", ["foundry-native-v1", NATIVE_RESPONSES_PROFILE])
def test_recovery_still_reads_the_pre_responses_follow_task_input(local_env, fast_follow, recipient_profile):
    from types import SimpleNamespace
    from azure.ai.agentserver.core.tasks import TaskContext, task
    from starlette.testclient import TestClient
    from shared_context.behavior_hosted import create_host
    from shared_context.behavior_native_agent_wire import NativeRelaySend
    from shared_context.native_turns import NativeTurnRuntime
    from test_native_turns import IDENTITY, OfflineSdk

    _, _, recipient, _, sender = sender_case()
    original = NativeRelaySend(
        conversation_id=sender.conversation_id, request_id=sender.request_id, tool_call_id="old-follow-call",
        message=NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Old original input."))
    entered = threading.Event()

    @task(name="fa-native-version-message-v1")
    async def old_shape(ctx: TaskContext[NativeRelaySend]) -> NativeRelayAcceptance:
        entered.set()
        await ctx.shutdown.wait()
        return await ctx.exit_for_recovery()

    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        async def start():
            await old_shape.start(task_id="old-persisted-follow", input=original)
        client.portal.call(start)
        assert entered.wait(3)

    calls, resumed = [], threading.Event()
    accepted = NativeRelayAcceptance(
        message_id="old-original-message", recipient_agent_id=recipient.target.agent_id,
        conversation_id="old-original-peer-cid", submit_digest="c" * 64, status="accepted",
        state_profile=recipient_profile,
        dispatch=NativeResponseDispatch(state="needs_response", inflight_request_id="old-original-message")
        if recipient_profile == NATIVE_RESPONSES_PROFILE else None)

    def relay(packet):
        calls.append(json.loads(packet.content))
        if recipient_profile == NATIVE_RESPONSES_PROFILE:
            if len(calls) == 1:
                return httpx.Response(202, content=accepted.model_dump_json().encode())
            queued = accepted.model_copy(update={"dispatch": NativeResponseDispatch(
                state="already_queued", inflight_request_id=accepted.message_id)})
            resumed.set()
            return httpx.Response(202, content=queued.model_dump_json().encode())
        resumed.set()
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    current = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=current)):
        assert resumed.wait(5)
    expected = 2 if recipient_profile == NATIVE_RESPONSES_PROFILE else 1
    assert calls == [original.model_dump(mode="json")] * expected
    assert not current.executor.calls
