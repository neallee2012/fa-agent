"""Explicit Agent-wide version following through the real N1 invocation surface."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.core.exceptions import ServiceResponseError

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_agent_wire import NativePeerRequest, NativeRelayAcceptance
from shared_context.behavior_native_turn_wire import NativeConversationRead, NativeHistory, NativeRead, NativeSessionRead
from shared_context.errors import Problem
from shared_context.native_agent_tools import NativeAgentRelayClient
from shared_context.native_turns import NativeTurnRuntime
from test_native_agent_relay import sender_case
from test_native_session_bootstrap import prepare
from test_native_turn_wire import agent_receive, submit
from test_native_turns import IDENTITY, Harness, OfflineSdk, local_env


def invoke(client, identity, command):
    body = command.model_dump(mode="json") if hasattr(command, "model_dump") else command
    return client.post("/invocations", json=body, headers={
        "x-client-fa-state-profile": "foundry-native-v1",
        "x-agent-session-id": identity.session_id,
        "x-agent-invocation-id": "offline-environment",
        "x-agent-foundry-call-id": "offline-original-caller",
    })


@pytest.mark.parametrize("timeout_stage", [None, "prepare", "commit"],
                         ids=["within-deadline", "draining-timeout", "confirming-timeout"])
def test_follow_scans_multiple_pages_and_confirms_again_from_the_start(local_env, monkeypatch, caplog, timeout_stage):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    keys = {f"progress:offline-{number:03d}" for number in range(40)}
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, old.executor, client)
        prepare(h)
        original = invoke(client, IDENTITY, NativeSessionRead()).json()

        async def seed():
            async with FoundryStateStore("fa/v1/turns") as store:
                for key in sorted(keys):
                    await store.create_item(key, {"offline": True})

        h.call(seed)
    identity = IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    pages, page_requests, source_reads = [], [], []
    list_keys = FoundryStateStore.list_keys
    stage, timed_out = "prepare", False

    async def observed_page(store, **kwargs):
        nonlocal timed_out
        page_requests.append(kwargs)
        if stage == timeout_stage and kwargs["after"] is not None and not timed_out:
            timed_out = True
            await asyncio.sleep(30)
        page = await list_keys(store, **kwargs)
        pages.append((kwargs, [key.key for key in page.keys], page.last_id))
        return page

    async def inactive(sid):
        source_reads.append(sid)
        return True

    monkeypatch.setattr(FoundryStateStore, "list_keys", observed_page)
    runtime = NativeTurnRuntime(identity, OfflineSdk(), bootstrap_source_inactive=inactive)
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        command = {
            "operation": "session_follow", "request_id": "large-follow-original",
            "expected_session_id": IDENTITY.session_id, "expected_etag": original["etag"], "stage": "prepare",
        }
        pending = invoke(client, identity, command)
        assert pending.status_code == 200, pending.text
        if timeout_stage == "prepare":
            assert timed_out and pending.json()["reason"] == "scan_pending"
            cursor, requests_before = pages[-1][2], len(page_requests)
            pending = invoke(client, identity, command)
            assert page_requests[requests_before]["after"] == cursor
        assert pending.json()["reason"] == "source_stop_required", pending.text
        drained = len(pages)
        assert drained >= 3 and pages[0][0]["after"] is None
        assert keys <= {key for _, page, _ in pages for key in page}
        assert source_reads == []
        stage = "commit"
        ready = invoke(client, identity, {**command, "stage": "commit"})
        if timeout_stage == "commit":
            assert timed_out and ready.json()["reason"] == "scan_pending"
            cursor, requests_before = pages[-1][2], len(page_requests)
            ready = invoke(client, identity, {**command, "stage": "commit"})
            assert page_requests[requests_before]["after"] == cursor
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
        assert len(pages) >= drained + 3 and pages[drained][0]["after"] is None
        assert keys <= {key for _, page, _ in pages[drained:] for key in page}
        assert all(options["limit"] == 16 for options, _, _ in pages)
        assert ready.json()["request_id"] == command["request_id"]
        assert source_reads == [IDENTITY.session_id] * (2 if timeout_stage == "commit" else 1)
        assert not runtime.executor.calls
        if timeout_stage is not None:
            assert "FA_NATIVE_FOLLOW_SCAN_PENDING reason=read_deadline" in caplog.text


def test_explicit_follow_pauses_new_work_until_original_compute_is_inactive(local_env, monkeypatch):
    old_sdk = OfflineSdk()
    old = NativeTurnRuntime(IDENTITY, old_sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        prepared = prepare(Harness(old, old_sdk, client)).json()
        original = invoke(client, IDENTITY, NativeSessionRead()).json()
    identity = IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    inactive = False

    async def source_inactive(sid):
        assert sid == IDENTITY.session_id
        return inactive

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(identity, sdk, bootstrap_source_inactive=source_inactive)
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        command = {
            "operation": "session_follow", "request_id": "follow-original",
            "expected_session_id": IDENTITY.session_id, "expected_etag": original["etag"],
            "stage": "prepare",
        }
        pending = invoke(client, identity, command)
        assert pending.status_code == 200, pending.text
        assert pending.json()["state"] == "pending"
        assert pending.json()["reason"] == "source_stop_required"
        rejected = invoke(client, identity, submit("new-must-wait"))
        assert rejected.status_code == 503
        assert rejected.json()["error"]["code"] == "AGENT_VERSION_FOLLOWING"
        assert not sdk.calls
        command["stage"] = "commit"
        assert invoke(client, identity, command).json()["reason"] == "source_active"
        inactive = True
        ready = invoke(client, identity, command)
        assert ready.status_code == 200, ready.text
        assert ready.json()["state"] == "ready"
        assert ready.json()["hosted_session_id"] == identity.session_id
        assert ready.json()["previous_hosted_session_id"] == IDENTITY.session_id
        assert ready.json()["model_started"] is False
        state = invoke(client, identity, NativeSessionRead()).json()
        assert state["preparation"] == prepared
        assert state["environment"] == ready.json()
        assert invoke(client, identity, command).json() == ready.json()
        assert not sdk.calls
        accepted = invoke(client, identity, submit("new-after-follow"))
        assert accepted.status_code == 202, accepted.text
        result = Harness(runtime, sdk, client).wait("new-after-follow", "completed")
        assert result.hosted_session_id == identity.session_id and len(sdk.calls) == 1


def test_follow_retains_original_cid_receipts_and_last_completed_checkpoint(local_env, monkeypatch):
    old_sdk = OfflineSdk()
    old = NativeTurnRuntime(IDENTITY, old_sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        old_h = Harness(old, old_sdk, client)
        prepare(old_h)
        assert old_h.invoke(submit()).status_code == 202
        completed = old_h.wait("rid-1", "completed")
        state = invoke(client, IDENTITY, NativeSessionRead()).json()
    identity = IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)

    async def inactive(_sid, *, task_id=None):
        return True

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(identity, sdk, bootstrap_source_inactive=inactive,
                                task_observer=SimpleNamespace(quiet=inactive))
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        command = {
            "operation": "session_follow", "request_id": "follow-original",
            "expected_session_id": IDENTITY.session_id, "expected_etag": state["etag"], "stage": "prepare",
        }
        assert invoke(client, identity, command).status_code == 200
        assert invoke(client, identity, {**command, "stage": "commit"}).json()["state"] == "ready"
        prior = invoke(client, identity, NativeRead(request_id="rid-1"))
        assert prior.json() == completed.model_dump(mode="json")
        conversation = invoke(client, identity, NativeConversationRead(conversation_id=completed.conversation_id)).json()
        assert conversation["hosted_session_id"] == IDENTITY.session_id
        assert conversation["execution_hosted_session_id"] == identity.session_id
        following = invoke(client, identity, submit("rid-2", completed.conversation_id))
        assert following.status_code == 202, following.text
        next_turn = Harness(runtime, sdk, client).wait("rid-2", "completed")
        assert next_turn.conversation_id == completed.conversation_id and next_turn.turn_number == 2
        assert next_turn.hosted_session_id == identity.session_id
        assert sdk.calls[0][2] is not None and len(sdk.calls) == 1
        assert invoke(client, identity, NativeRead(request_id="rid-1")).json() == completed.model_dump(mode="json")
        history = invoke(client, identity, NativeHistory(conversation_id=completed.conversation_id)).json()
        assert [turn["request_id"] for turn in history["turns"]] == ["rid-1", "rid-2"]


def test_t5_refused_while_following_retries_same_input_after_ready_without_loss(local_env, monkeypatch):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        prepare(Harness(old, old.executor, client))
        original = invoke(client, IDENTITY, NativeSessionRead()).json()
    identity = IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)

    async def inactive(_sid):
        return True

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(identity, sdk, bootstrap_source_inactive=inactive)
    app = create_host(SimpleNamespace(identity=identity), native_runtime=runtime)
    with TestClient(app) as client:
        follow = {
            "operation": "session_follow", "request_id": "follow-original",
            "expected_session_id": IDENTITY.session_id, "expected_etag": original["etag"], "stage": "prepare",
        }
        assert invoke(client, identity, follow).json()["state"] == "pending"
        received = agent_receive("one-original-peer-message")
        calls, responses = [], []

        async def relay(packet):
            calls.append(bytes(packet.content))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as hosted:
                headers = {
                    "x-client-fa-state-profile": "foundry-native-v1",
                    "x-agent-session-id": identity.session_id, "x-agent-invocation-id": "offline-peer",
                    "x-agent-foundry-call-id": "offline-original-peer-caller",
                }
                result = await hosted.post("/invocations", json=received.model_dump(mode="json"), headers=headers)
                responses.append(result.status_code)
                if result.status_code == 503:
                    assert result.json()["error"]["code"] == "AGENT_VERSION_FOLLOWING"
                    absent = await hosted.post("/invocations", json={
                        "operation": "read", "request_id": received.command.request_id}, headers=headers)
                    assert absent.status_code == 404 and not sdk.calls
                    if len(calls) == 2:
                        ready = await hosted.post("/invocations", json={**follow, "stage": "commit"}, headers=headers)
                        assert ready.json()["state"] == "ready"
                    return httpx.Response(503, json=result.json())
                assert result.status_code == 202
                receipt = result.json()
                value = NativeRelayAcceptance(
                    message_id=receipt["request_id"], recipient_agent_id=identity.agent_id,
                    conversation_id=receipt["conversation_id"], submit_digest=receipt["submit_digest"],
                    status=receipt["status"])
                return httpx.Response(202, content=value.model_dump_json().encode())

        async def token():
            return "offline-sender-token"

        _, _, _, _, sender = sender_case()
        sender_client = NativeAgentRelayClient(
            "https://api.invalid", token, transport=httpx.MockTransport(relay))
        message = NativePeerRequest(recipient_agent_id=identity.agent_id, kind="request", content="Original peer input")
        accepted = client.portal.call(sender_client.send, sender, "original-tool-call", message)
        assert accepted.message_id == received.command.request_id
        assert responses == [503, 503, 202] and calls[0] == calls[1] == calls[2]
        completed = Harness(runtime, sdk, client).wait(received.command.request_id, "completed")
        assert completed.command == received.command and len(sdk.calls) == 1


def test_terminal_rid_does_not_hide_an_official_notification_still_retrying(local_env, monkeypatch):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, old.executor, client)
        prepare(h)
        h.invoke(submit())
        completed = h.wait("rid-1", "completed")
        original = invoke(client, IDENTITY, NativeSessionRead()).json()
    identity = IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    pending, task_reads, inactive_reads = [True], [], []

    async def quiet(sid, *, task_id=None):
        task_reads.append(sid)
        return not pending[0]

    async def inactive(sid):
        inactive_reads.append(sid)
        return True

    runtime = NativeTurnRuntime(identity, OfflineSdk(), bootstrap_source_inactive=inactive)
    runtime.task_observer = SimpleNamespace(quiet=quiet)
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        command = {
            "operation": "session_follow", "request_id": "follow-original",
            "expected_session_id": IDENTITY.session_id, "expected_etag": original["etag"], "stage": "prepare",
        }
        response = invoke(client, identity, command)
        assert response.status_code == 200
        assert response.json()["reason"] == "task_active"
        assert task_reads == [IDENTITY.session_id] and inactive_reads == []
        assert invoke(client, identity, NativeRead(request_id="rid-1")).json() == completed.model_dump(mode="json")
        pending[0] = False
        assert invoke(client, identity, command).json()["reason"] == "source_stop_required"
        assert invoke(client, identity, {**command, "stage": "commit"}).json()["state"] == "ready"
        assert not runtime.executor.calls


def test_new_sid_restart_reenters_only_its_original_started_input_without_model_replay(local_env, monkeypatch):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, old.executor, client)
        prepare(h)
        h.invoke(submit())
        completed = h.wait("rid-1", "completed")
        original = invoke(client, IDENTITY, NativeSessionRead()).json()
    identity = IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)

    async def inactive(_sid, *, task_id=None):
        return True

    sdk = OfflineSdk()
    sdk.block = True
    runtime = NativeTurnRuntime(identity, sdk, bootstrap_source_inactive=inactive,
                                task_observer=SimpleNamespace(quiet=inactive))
    inputs = []
    start = runtime.chain.start

    async def observed_start(*args, **kwargs):
        inputs.append(kwargs["input"])
        return await start(*args, **kwargs)

    monkeypatch.setattr(runtime.chain, "start", observed_start)
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        command = {
            "operation": "session_follow", "request_id": "follow-original",
            "expected_session_id": IDENTITY.session_id, "expected_etag": original["etag"], "stage": "prepare",
        }
        assert invoke(client, identity, command).json()["reason"] == "source_stop_required"
        assert invoke(client, identity, {**command, "stage": "commit"}).json()["state"] == "ready"
        assert invoke(client, identity, submit("rid-2", completed.conversation_id)).status_code == 202
        started = Harness(runtime, sdk, client).wait("rid-2", "started")
    assert len(inputs) == 1 and inputs[0].request_id == started.request_id

    async def owned(*, task_id=None):
        return inputs[0].model_dump(mode="json")

    restored_sdk = OfflineSdk()
    restored = NativeTurnRuntime(identity, restored_sdk, task_observer=SimpleNamespace(owned_input=owned))
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=restored)) as client:
        receipt = Harness(restored, restored_sdk, client).wait("rid-2", "unknown")
        assert receipt.command == started.command and receipt.conversation_id == completed.conversation_id
        assert receipt.hosted_session_id == identity.session_id and not restored_sdk.calls
        assert invoke(client, identity, NativeRead(request_id="rid-1")).json() == completed.model_dump(mode="json")


def test_unconfirmed_admission_cannot_be_mistaken_for_an_empty_agent(local_env, monkeypatch):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    create = FoundryStateStore.create_item

    async def lost(store, key, value, **kwargs):
        if key == "rid:unconfirmed-original":
            raise ServiceResponseError("synthetic receipt write has no visible acknowledgment")
        return await create(store, key, value, **kwargs)

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        prepare(Harness(old, old.executor, client))
        monkeypatch.setattr(FoundryStateStore, "create_item", lost)
        response = invoke(client, IDENTITY, submit("unconfirmed-original"))
        assert response.status_code == 503
        original = invoke(client, IDENTITY, NativeSessionRead()).json()
        assert not old.executor.calls
    monkeypatch.setattr(FoundryStateStore, "create_item", create)
    identity = IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)

    async def inactive(_sid, *, task_id=None):
        return True

    runtime = NativeTurnRuntime(identity, OfflineSdk(), bootstrap_source_inactive=inactive,
                                task_observer=SimpleNamespace(quiet=inactive))
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        response = invoke(client, identity, {
            "operation": "session_follow", "request_id": "follow-original",
            "expected_session_id": IDENTITY.session_id, "expected_etag": original["etag"], "stage": "prepare",
        })
        assert response.status_code == 200 and response.json()["reason"] == "admission_pending"
        assert invoke(client, identity, NativeSessionRead()).json()["hosted_session_id"] == IDENTITY.session_id
        assert not runtime.executor.calls


def test_follow_preserves_unknown_without_restoring_an_empty_context(local_env, monkeypatch):
    async def failed(*_args):
        raise RuntimeError("synthetic original SDK outcome is unknown")

    old = NativeTurnRuntime(IDENTITY, failed)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, OfflineSdk(), client)
        prepare(h)
        h.invoke(submit())
        unknown = h.wait("rid-1", "unknown")
        state = invoke(client, IDENTITY, NativeSessionRead()).json()
    identity = IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)

    async def inactive(_sid, *, task_id=None):
        return True

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(identity, sdk, bootstrap_source_inactive=inactive,
                                task_observer=SimpleNamespace(quiet=inactive))
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        command = {
            "operation": "session_follow", "request_id": "follow-original",
            "expected_session_id": IDENTITY.session_id, "expected_etag": state["etag"], "stage": "prepare",
        }
        assert invoke(client, identity, command).json()["reason"] == "source_stop_required"
        assert invoke(client, identity, {**command, "stage": "commit"}).json()["state"] == "ready"
        assert invoke(client, identity, NativeRead(request_id="rid-1")).json() == unknown.model_dump(mode="json")
        refused = invoke(client, identity, submit("new-in-old-cid", unknown.conversation_id))
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE"
        assert not sdk.calls


def test_pending_follow_fences_new_receipts_but_preserves_already_accepted_work(local_env, monkeypatch):
    from shared_context.behavior_native_turn_wire import NativeSessionFollow
    from test_native_turns import accept_without_chain

    sdk = OfflineSdk()
    old = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, sdk, client)
        prepare(h)
        accepted = accept_without_chain(h).value.receipt
        state = invoke(client, IDENTITY, NativeSessionRead()).json()
        next_identity = IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"})
        candidate = NativeTurnRuntime(next_identity, OfflineSdk())
        pending = client.portal.call(candidate.dispatch, NativeSessionFollow(
            request_id="follow-with-queue", expected_session_id=IDENTITY.session_id,
            expected_etag=state["etag"], stage="prepare"))
        assert pending.state == "pending" and pending.reason == "accepted_or_started"
        # The source runtime remains the only task manager; a preparation reader has no task execution.
        rejected = invoke(client, IDENTITY, submit("must-not-join-during-follow"))
        assert rejected.status_code == 503 and rejected.json()["error"]["code"] == "AGENT_VERSION_FOLLOWING"
        assert invoke(client, IDENTITY, NativeRead(request_id=accepted.request_id)).json() == accepted.model_dump(mode="json")
        assert not sdk.calls


def test_delayed_original_commit_cannot_replace_a_later_published_environment(local_env, monkeypatch):
    from shared_context.behavior_native_turn_wire import NativeSessionFollow

    original = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=original)) as client:
        prepare(Harness(original, original.executor, client))
        first = invoke(client, IDENTITY, NativeSessionRead()).json()
    second_id = IDENTITY.model_copy(update={"session_id": "offline-second-session", "agent_version": "2"})
    third_id = IDENTITY.model_copy(update={"session_id": "offline-third-session", "agent_version": "3"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", second_id.session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def inactive(sid):
        calls.append(sid)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return True

    second = NativeTurnRuntime(second_id, OfflineSdk(), bootstrap_source_inactive=inactive)
    with TestClient(create_host(SimpleNamespace(identity=second_id), native_runtime=second)) as client:
        start = NativeSessionFollow(request_id="follow-second", expected_session_id=IDENTITY.session_id,
                                    expected_etag=first["etag"], stage="prepare")
        assert client.portal.call(second.dispatch, start).state == "pending"
        commit = start.model_copy(update={"stage": "commit"})
        delayed = client.portal.start_task_soon(second.dispatch, commit)
        client.portal.call(entered.wait)
        try:
            assert client.portal.call(second.dispatch, commit).state == "ready"
            state = client.portal.call(second.session_state)

            async def already_inactive(_sid):
                return True

            third = NativeTurnRuntime(third_id, OfflineSdk(), bootstrap_source_inactive=already_inactive)
            next_ = NativeSessionFollow(request_id="follow-third", expected_session_id=second_id.session_id,
                                        expected_etag=state.etag, stage="prepare")
            assert client.portal.call(third.dispatch, next_).state == "pending"
            assert client.portal.call(third.dispatch, next_.model_copy(update={"stage": "commit"})).state == "ready"
        finally:
            client.portal.call(release.set)
        failure = None
        try:
            delayed.result(timeout=5)
        except Problem as error:
            failure = error.body["error"]["code"]
        assert client.portal.call(second.session_state).hosted_session_id == third_id.session_id
        assert failure == "NATIVE_ENVIRONMENT_CONFLICT"
        assert not second.executor.calls and not third.executor.calls
