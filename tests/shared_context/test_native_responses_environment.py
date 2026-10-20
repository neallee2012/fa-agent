"""Public follow/retarget with fixed official local tasks/store and synthetic SDK/HTTP."""

from types import SimpleNamespace

import httpx
import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context import native_responses_wire
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeConversationRead, NativeRead, NativeReceipt, NativeSessionRead
from shared_context.native_responses import native_responses_handler_factory
from shared_context.native_task_observer import NativeTaskObserver
from shared_context.native_turns import TURNS_STORE, NativeTurnRuntime
from test_native_environment import invoke
from test_native_responses import ResponsesHarness, response_submit
from test_native_session_bootstrap import prepare
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env


def responses_host(identity, runtime, root):
    root.mkdir(exist_ok=True)
    hosted = SimpleNamespace(identity=identity, supervisor=SimpleNamespace(journal=SimpleNamespace(root=root)))
    return create_host(hosted, native_runtime=runtime, responses_handler_factory=native_responses_handler_factory)


class OriginalTasks:
    def __init__(self, identity, *, blocked=None):
        self.identity, self.blocked, self.calls = identity, blocked, []
        self.error = None

    async def token(self):
        return "offline-task-token"

    def server(self, request):
        self.calls.append(request)
        assert request.method == "GET" and not request.content
        if not request.url.path.endswith("/tasks"):
            return httpx.Response(404)
        assert request.url.params["session_id"] == IDENTITY.session_id
        name = request.url.params["tag.task_name"]
        if name.startswith("responses_") and self.error is not None:
            return httpx.Response(self.error, json={"error": {"code": "offline-refusal"}})
        rows = [] if name != self.blocked else [{
            "id": "opaque-original-response-task", "agent_name": IDENTITY.agent_name,
            "session_id": IDENTITY.session_id, "status": "in_progress", "tags": {"task_name": name},
        }]
        return httpx.Response(200, json={"data": rows, "has_more": False})

    def observer(self):
        return NativeTaskObserver("https://offline.invalid/api/projects/original", self.identity, self.token,
                                  transport=httpx.MockTransport(self.server))


@pytest.fixture
def original_response(local_env, monkeypatch, tmp_path, request):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    root = tmp_path / "journal"
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(responses_host(IDENTITY, runtime, root)) as client:
        harness = ResponsesHarness(runtime, sdk, client)
        assert prepare(harness).status_code == 200
        admission = harness.invoke(response_submit("original-response"))
        assert admission.status_code == 202, admission.text
        accepted = harness.call(runtime.read, "original-response")
        receipt = accepted
        if getattr(request, "param", "completed") == "completed":
            started = harness.respond(accepted)
            assert started.status_code == 200, started.text
            receipt = harness.wait("original-response", "completed")
            assert harness.response_result(started.json()["id"])["status"] == "completed"
        state = invoke(client, IDENTITY, NativeSessionRead()).json()
    return SimpleNamespace(root=root, receipt=receipt, state=state, sdk=sdk)


@pytest.mark.parametrize("task_name", ["responses_resilient_one_shot", "responses_resilient_multi_turn"])
def test_terminal_response_does_not_hide_its_original_official_task(original_response, monkeypatch, task_name):
    source = original_response
    identity = IDENTITY.model_copy(update={"session_id": "offline-follow-destination", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", "retain-cid", raising=False)
    tasks, inactive_reads = OriginalTasks(identity, blocked=task_name), []

    async def inactive(sid):
        inactive_reads.append(sid)
        return True

    runtime = NativeTurnRuntime(identity, OfflineSdk(), task_observer=tasks.observer(),
                                bootstrap_source_inactive=inactive)
    with TestClient(responses_host(identity, runtime, source.root)) as client:
        command = {
            "operation": "session_follow", "request_id": "explicit-response-follow",
            "expected_session_id": IDENTITY.session_id, "expected_etag": source.state["etag"], "stage": "prepare",
        }
        pending = invoke(client, identity, command)
        assert pending.status_code == 200, pending.text
        assert pending.json()["reason"] == "task_active"
        assert inactive_reads == [] and not runtime.executor.calls
        original = invoke(client, identity, NativeRead(request_id=source.receipt.request_id))
        assert original.json() == source.receipt.model_dump(mode="json")
        tasks.blocked = None
        assert invoke(client, identity, command).json()["reason"] == "source_stop_required"
        ready = invoke(client, identity, {**command, "stage": "commit"})
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
        assert inactive_reads == [IDENTITY.session_id] and not runtime.executor.calls


@pytest.mark.parametrize("original_response", ["accepted"], indirect=True)
def test_explicit_follow_fences_an_unstarted_response_without_dispatching_it(original_response, monkeypatch):
    source = original_response
    identity = IDENTITY.model_copy(update={"session_id": "offline-follow-destination", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", "retain-cid", raising=False)
    tasks, notices = OriginalTasks(identity), []
    stopped = False

    async def inactive(sid):
        assert sid == IDENTITY.session_id
        return stopped

    async def notified(receipt):
        notices.append(receipt)

    runtime = NativeTurnRuntime(identity, OfflineSdk(), task_observer=tasks.observer(),
                                bootstrap_source_inactive=inactive, on_unconfirmed=notified)
    with TestClient(responses_host(identity, runtime, source.root)) as client:
        command = {
            "operation": "session_follow", "request_id": "explicit-response-follow",
            "expected_session_id": IDENTITY.session_id, "expected_etag": source.state["etag"], "stage": "prepare",
        }
        pending = invoke(client, identity, command)
        assert pending.status_code == 200 and pending.json()["reason"] == "source_stop_required", pending.text
        closed = NativeReceipt.model_validate_json(invoke(
            client, identity, NativeRead(request_id=source.receipt.request_id)).content)
        assert closed.status == "unknown" and closed.failure_code == "NATIVE_TURN_NOT_STARTED"
        assert closed.command == source.receipt.command and closed.submit_digest == source.receipt.submit_digest
        assert closed.hosted_session_id == IDENTITY.session_id and closed.response_id is None
        assert closed.turn_number is None and not runtime.executor.calls and not source.sdk.calls
        assert invoke(client, identity, {**command, "stage": "commit"}).json()["reason"] == "source_active"
        stopped = True
        ready = invoke(client, identity, {**command, "stage": "commit"})
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
        assert invoke(client, identity, response_submit("original-response")).json() == closed.model_dump(mode="json")
        assert notices and all(item.request_id == closed.request_id for item in notices)
        following = invoke(client, identity, response_submit("new-rid", closed.conversation_id))
        assert following.status_code == 202, following.text
        assert following.json()["dispatch"]["inflight_request_id"] == "new-rid"
        assert not runtime.executor.calls
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", IDENTITY.session_id)
    late_sdk = OfflineSdk()
    late_runtime = NativeTurnRuntime(IDENTITY, late_sdk)
    with TestClient(responses_host(IDENTITY, late_runtime, source.root)) as client:
        late = ResponsesHarness(late_runtime, late_sdk, client)
        response = late.respond(source.receipt)
        assert response.status_code == 200, response.text
        assert late.response_result(response.json()["id"])["error"]["code"] == "NATIVE_TURN_NOT_STARTED"
        assert not late_sdk.calls
        # The replaced session refuses record reads; the stored receipt itself is unchanged.
        stale = invoke(client, IDENTITY, NativeRead(request_id=closed.request_id))
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
        assert late.call(late_runtime.read, closed.request_id) == closed


@pytest.mark.parametrize("original_response", ["accepted"], indirect=True)
def test_idle_only_follow_preserves_the_original_unstarted_response(original_response, monkeypatch):
    source = original_response
    identity = IDENTITY.model_copy(update={"session_id": "offline-idle-follow", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    tasks, inactive_reads = OriginalTasks(identity), []

    async def inactive(sid):
        inactive_reads.append(sid)
        return True

    runtime = NativeTurnRuntime(identity, OfflineSdk(), task_observer=tasks.observer(),
                                bootstrap_source_inactive=inactive)
    with TestClient(responses_host(identity, runtime, source.root)) as client:
        command = {
            "operation": "session_follow", "request_id": "idle-follow-original",
            "expected_session_id": IDENTITY.session_id, "expected_etag": source.state["etag"],
            "stage": "prepare", "idle_only": True,
        }
        response = invoke(client, identity, command)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "pending"
        assert response.json()["reason"] == "accepted_or_started"
        original = invoke(client, identity, NativeRead(request_id=source.receipt.request_id))
        assert original.json() == source.receipt.model_dump(mode="json")
        assert invoke(client, identity, NativeSessionRead()).json()["hosted_session_id"] == IDENTITY.session_id
        assert not inactive_reads and not runtime.executor.calls and not source.sdk.calls


def test_old_follow_progress_without_a_profile_marker_cannot_skip_responses_tasks(original_response, monkeypatch):
    source = original_response
    identity = IDENTITY.model_copy(update={"session_id": "offline-follow-destination", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", "retain-cid", raising=False)
    tasks = OriginalTasks(identity)
    runtime = NativeTurnRuntime(identity, OfflineSdk(), task_observer=tasks.observer())
    with TestClient(responses_host(identity, runtime, source.root)) as client:
        command = {
            "operation": "session_follow", "request_id": "explicit-response-follow",
            "expected_session_id": IDENTITY.session_id, "expected_etag": source.state["etag"], "stage": "prepare",
        }
        assert invoke(client, identity, command).json()["reason"] == "source_stop_required"

        async def retain_older_progress():
            async with FoundryStateStore(TURNS_STORE) as store:
                page = await store.list_keys(limit=16)
                for entry in page.keys:
                    if entry.key.startswith("following:"):
                        item = await store.get_item(entry.key)
                        value = dict(item.value)
                        value.pop("responses", None)
                        value["after"] = page.last_id
                        await store.set_item(entry.key, value, if_match=item.etag)

        client.portal.call(retain_older_progress)
        tasks.blocked = "responses_resilient_multi_turn"
        pending = invoke(client, identity, command)
        assert pending.status_code == 200 and pending.json()["reason"] == "task_active", pending.text
        state = invoke(client, identity, NativeSessionRead()).json()
        assert state["hosted_session_id"] == IDENTITY.session_id and not runtime.executor.calls


def test_paged_follow_remembers_responses_after_their_receipt_page(original_response, monkeypatch):
    import asyncio

    source = original_response
    identity = IDENTITY.model_copy(update={"session_id": "offline-follow-destination", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", "retain-cid")
    tasks = OriginalTasks(identity, blocked="responses_resilient_multi_turn")
    runtime = NativeTurnRuntime(identity, OfflineSdk(), task_observer=tasks.observer())
    with TestClient(responses_host(identity, runtime, source.root)) as client:
        async def retained_metadata():
            async with FoundryStateStore(TURNS_STORE) as store:
                for index in range(20):
                    await store.create_item(f"zz-retained:{index}", {"completed": True})

        client.portal.call(retained_metadata)
        list_keys, paused = FoundryStateStore.list_keys, []

        async def delayed_page(store, **kwargs):
            if kwargs.get("after") is not None and not paused:
                paused.append(kwargs["after"])
                await asyncio.sleep(30)
            return await list_keys(store, **kwargs)

        monkeypatch.setattr(FoundryStateStore, "list_keys", delayed_page)
        command = {
            "operation": "session_follow", "request_id": "explicit-response-follow",
            "expected_session_id": IDENTITY.session_id, "expected_etag": source.state["etag"], "stage": "prepare",
        }
        first = invoke(client, identity, command)
        assert first.status_code == 200 and first.json()["reason"] == "scan_pending", first.text
        assert paused
        assert tasks.calls == []
        second = invoke(client, identity, command)
        assert second.status_code == 200 and second.json()["reason"] == "task_active", second.text
        assert not runtime.executor.calls


def test_completed_checkpoint_continues_only_with_the_selected_environment_policy(original_response, monkeypatch):
    selected_mode = native_responses_wire.RESPONSES_ENVIRONMENT_MODE
    assert selected_mode == "retain-cid"
    monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", None)
    source = original_response
    identity = IDENTITY.model_copy(update={"session_id": "offline-follow-destination", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    tasks = OriginalTasks(identity)

    async def inactive(sid):
        assert sid == IDENTITY.session_id
        return True

    runtime = NativeTurnRuntime(identity, OfflineSdk(), task_observer=tasks.observer(),
                                bootstrap_source_inactive=inactive)
    with TestClient(responses_host(identity, runtime, source.root)) as client:
        command = {
            "operation": "session_follow", "request_id": "explicit-response-follow",
            "expected_session_id": IDENTITY.session_id, "expected_etag": source.state["etag"], "stage": "prepare",
        }
        blocked = invoke(client, identity, command)
        assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "NATIVE_ENVIRONMENT_UNAVAILABLE"
        assert not tasks.calls and not runtime.executor.calls
        monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", selected_mode)
        assert invoke(client, identity, command).json()["reason"] == "source_stop_required"
        assert invoke(client, identity, {**command, "stage": "commit"}).json()["state"] == "ready"
        monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", None)
        conversation = invoke(client, identity, NativeConversationRead(conversation_id=source.receipt.conversation_id))
        assert conversation.status_code == 200
        assert conversation.json()["continuation_blocker"] == "NATIVE_ENVIRONMENT_UNAVAILABLE"
        following = response_submit("next-response", source.receipt.conversation_id)
        assert invoke(client, identity, following).status_code == 409
        assert invoke(client, identity, NativeRead(request_id="next-response")).status_code == 404
        monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", selected_mode)
        admitted = invoke(client, identity, following)
        assert admitted.status_code == 202, admitted.text
        accepted = NativeReceipt.model_validate_json(admitted.content)
        assert accepted.dispatch.previous_response_id == source.receipt.response_id
        response = client.post("/responses", json={
            "input": accepted.command.content, "background": True, "store": True, "stream": False,
            "agent_session_id": identity.session_id, "previous_response_id": source.receipt.response_id,
        }, headers={
            "x-agent-session-id": identity.session_id, "x-agent-foundry-call-id": "OFFLINE-follow-call",
            "x-client-fa-rid": accepted.request_id, "x-client-fa-cid": accepted.conversation_id,
            "x-client-fa-submit-digest": accepted.submit_digest,
        })
        assert response.status_code == 200, response.text
        completed = Harness(runtime, runtime.executor, client).wait("next-response", "completed")
        assert completed.turn_number == 2 and completed.hosted_session_id == identity.session_id
        assert completed.capture == source.receipt.capture and completed.conversation_id == source.receipt.conversation_id
        assert len(runtime.executor.calls) == 1 and runtime.executor.calls[0][2] is not None
        original = invoke(client, identity, NativeRead(request_id=source.receipt.request_id))
        assert original.json() == source.receipt.model_dump(mode="json")


@pytest.mark.parametrize("original_response", ["accepted"], indirect=True)
@pytest.mark.parametrize("fault", ["started-wins", "close-ack-lost"])
def test_response_fence_never_overwrites_a_start_or_retries_with_a_new_etag(original_response, monkeypatch, fault):
    source = original_response
    identity = IDENTITY.model_copy(update={"session_id": "offline-follow-destination", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", "retain-cid")
    runtime = NativeTurnRuntime(identity, OfflineSdk(), task_observer=OriginalTasks(identity).observer())
    writes = []
    set_item = FoundryStateStore.set_item

    async def racing_write(store, key, value, **options):
        if (key == "rid:original-response" and value["receipt"]["failure_code"] == "NATIVE_TURN_NOT_STARTED"
                and not writes):
            writes.append(options["if_match"])
            if fault == "started-wins":
                current = await store.get_item(key)
                started = {**current.value, "run_token": "original-winner-token",
                           "receipt": {**current.value["receipt"], "status": "started", "response_id": "resp-winner"}}
                await set_item(store, key, started, if_match=current.etag)
            else:
                await set_item(store, key, value, **options)
                raise ServiceResponseError("OFFLINE close committed; ACK lost")
        return await set_item(store, key, value, **options)

    with TestClient(responses_host(identity, runtime, source.root)) as client:
        monkeypatch.setattr(FoundryStateStore, "set_item", racing_write)
        command = {
            "operation": "session_follow", "request_id": "explicit-response-follow",
            "expected_session_id": IDENTITY.session_id, "expected_etag": source.state["etag"], "stage": "prepare",
        }
        response = invoke(client, identity, command)
        assert response.status_code == 200, response.text
        if fault == "started-wins":
            # The started winner is kept (not closed as not-started) and the follow waits for it.
            assert response.json()["state"] == "pending" and response.json()["reason"] == "accepted_or_started"
        observed = invoke(client, identity, NativeRead(request_id=source.receipt.request_id)).json()
        assert observed["status"] == ("started" if fault == "started-wins" else "unknown")
        if fault == "started-wins":
            assert observed["response_id"] == "resp-winner" and observed["failure_code"] is None
        else:
            assert observed["failure_code"] == "NATIVE_TURN_NOT_STARTED"
            assert invoke(client, identity, command).json() == response.json()
        assert len(writes) == 1 and not runtime.executor.calls and not source.sdk.calls


@pytest.mark.parametrize("original_response", ["completed", "accepted"], indirect=True)
def test_retarget_keeps_old_pending_and_responses_checkpoint_until_source_tasks_are_quiet(original_response, monkeypatch):
    source = original_response
    unfinished = source.receipt.status == "accepted"
    monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", None if unfinished else "retain-cid")
    pending_id = IDENTITY.model_copy(update={"session_id": "offline-pending-destination", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", pending_id.session_id)
    pending = NativeTurnRuntime(pending_id, OfflineSdk(), task_observer=OriginalTasks(pending_id).observer())
    old_command = {
        "operation": "session_follow", "request_id": "original-pending",
        "expected_session_id": IDENTITY.session_id, "expected_etag": source.state["etag"], "stage": "prepare",
    }
    with TestClient(responses_host(pending_id, pending, source.root)) as client:
        initial = invoke(client, pending_id, old_command)
        if unfinished:
            assert initial.status_code == 503 and initial.json()["error"]["code"] == "NATIVE_ENVIRONMENT_UNAVAILABLE"
        else:
            assert initial.status_code == 200 and initial.json()["reason"] == "source_stop_required", initial.text
        old_view = invoke(client, pending_id, NativeSessionRead()).json()["following"]
    monkeypatch.setattr(native_responses_wire, "RESPONSES_ENVIRONMENT_MODE", "retain-cid")
    identity = IDENTITY.model_copy(update={"session_id": "offline-current-destination", "agent_version": "3"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    tasks = OriginalTasks(identity, blocked="responses_resilient_multi_turn")
    tasks.error = 403
    stopped = False

    async def inactive(sid):
        assert sid == IDENTITY.session_id
        return stopped

    runtime = NativeTurnRuntime(identity, OfflineSdk(), task_observer=tasks.observer(),
                                bootstrap_source_inactive=inactive)
    with TestClient(responses_host(identity, runtime, source.root)) as client:
        original = invoke(client, identity, NativeSessionRead()).json()
        command = {
            "operation": "session_retarget", "request_id": "retarget-responses",
            "expected_session_id": IDENTITY.session_id, "expected_etag": original["etag"],
            "expected_agent_version": identity.agent_version,
            "pending_request_id": old_command["request_id"], "pending_session_id": pending_id.session_id,
        }
        denied = invoke(client, identity, command)
        assert denied.status_code == 200 and denied.json()["reason"] == "task_access_denied", denied.text
        tasks.error = None
        assert invoke(client, identity, command).json()["reason"] == "task_active"
        before = invoke(client, identity, NativeSessionRead()).json()
        assert before["environment"] == original["environment"]
        tasks.blocked = None
        retargeted = invoke(client, identity, command)
        assert retargeted.status_code == 200 and retargeted.json()["reason"] == "source_stop_required", retargeted.text
        follow = {"operation": "session_follow", "request_id": command["request_id"],
                  "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"], "stage": "commit"}
        assert invoke(client, identity, follow).json()["reason"] == "source_active"
        stopped = True
        assert invoke(client, identity, follow).json()["state"] == "ready"
        assert client.portal.call(pending.session_state).following.model_dump(mode="json") == old_view
        retained = invoke(client, identity, NativeRead(request_id=source.receipt.request_id)).json()
        if unfinished:
            assert retained["status"] == "unknown" and retained["failure_code"] == "NATIVE_TURN_NOT_STARTED"
            assert retained["submit_digest"] == source.receipt.submit_digest
        else:
            assert retained == source.receipt.model_dump(mode="json")
        conversation = invoke(client, identity, NativeConversationRead(conversation_id=source.receipt.conversation_id)).json()
        assert conversation.get("last_response_id") == source.receipt.response_id
        assert conversation["capture"] == source.receipt.capture.model_dump(mode="json")
        assert conversation["execution_hosted_session_id"] == identity.session_id
        accepted = invoke(client, identity, response_submit("after-retarget", source.receipt.conversation_id))
        assert accepted.status_code == 202 and accepted.json()["dispatch"]["state"] == "needs_response", accepted.text
        assert not runtime.executor.calls and not pending.executor.calls
