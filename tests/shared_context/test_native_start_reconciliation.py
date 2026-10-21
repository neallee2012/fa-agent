"""Explicit environment changes settle an original unstarted input, never execute it on another SID."""

from types import SimpleNamespace

import httpx
import pytest
from azure.ai.agentserver.core.tasks import TaskConflictError
from azure.ai.agentserver.core.tasks._local_provider import LocalFileTaskProvider
from azure.ai.agentserver.core.storage import FoundryStateStore
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeRead, NativeSessionRead
from shared_context.native_task_observer import NativeTaskObserver
from shared_context.native_turns import NativeTurnRuntime, TURNS_STORE
from test_native_environment import invoke
from test_native_session_bootstrap import prepare
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env


async def inactive(_sid):
    return True


async def token():
    return "OFFLINE-observer-token"


@pytest.fixture
def stalled_input(local_env, monkeypatch, request):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    runs, providers = [], []
    start = old.chain.start

    async def capture(**kwargs):
        run = await start(**kwargs)
        runs.append(run)
        return run

    monkeypatch.setattr(old.chain, "start", capture)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, old.executor, client)
        assert prepare(h).status_code == 200
        assert h.invoke(submit()).status_code == 202
        h.call(runs[0].result)
        completed = h.call(old.read, "rid-1")
        update = LocalFileTaskProvider.update

        async def leave_input(provider, task_id, patch):
            if task_id == IDENTITY.agent_id and patch.status == "in_progress":
                providers.append(provider)
                # Seed the old accepted gap without invoking the newly fixed transport-error branch.
                raise TaskConflictError("OFFLINE old start did not acquire its lease")
            return await update(provider, task_id, patch)

        with monkeypatch.context() as old_gap:
            old_gap.setattr(LocalFileTaskProvider, "update", leave_input)
            retained_response = h.invoke(submit("stalled-rid", completed.conversation_id))
            assert retained_response.status_code == 409, retained_response.text
        retained = h.call(old.read, "stalled-rid")
        assert retained.status == "accepted" and retained.turn_number is None
        original_task = h.call(providers[0].get, IDENTITY.agent_id)
        assert original_task.status == "suspended" and original_task.lease is None
        assert original_task.payload["input"]["request_id"] == "stalled-rid"
        source = invoke(client, IDENTITY, NativeSessionRead()).json()

    pending_id = None
    if getattr(request, "param", False):
        pending_id = IDENTITY.model_copy(update={"session_id": "OFFLINE-pending-session", "agent_version": "2"})
        monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", pending_id.session_id)
        pending = NativeTurnRuntime(pending_id, OfflineSdk())
        with TestClient(create_host(SimpleNamespace(identity=pending_id), native_runtime=pending)) as client:
            response = invoke(client, pending_id, {
                "operation": "session_follow", "request_id": "older-pending-operation",
                "expected_session_id": IDENTITY.session_id, "expected_etag": source["etag"], "stage": "prepare",
            })
            assert response.status_code == 200 and response.json()["reason"] == "accepted_or_started"
    identity = IDENTITY.model_copy(update={
        "session_id": "OFFLINE-next-session", "agent_version": "3" if pending_id is not None else "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    gets, mutation = [], [lambda body: body]

    async def original_task_get(request):
        assert request.method == "GET" and not request.content
        gets.append(request.url.path)
        if request.url.path.endswith("/" + IDENTITY.agent_id):
            task = await providers[0].get(IDENTITY.agent_id)
            return httpx.Response(200, json=mutation[0]({
                key: getattr(task, key) for key in ("id", "agent_name", "session_id", "status", "lease", "payload", "source")
            }))
        return httpx.Response(200, json={"data": [], "has_more": False})

    observer = NativeTaskObserver("https://offline.example/api/projects/offline", identity, token,
                                  transport=httpx.MockTransport(original_task_get))
    current = NativeTurnRuntime(identity, OfflineSdk(), bootstrap_source_inactive=inactive, task_observer=observer)
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=current)) as client:
        yield SimpleNamespace(client=client, current=current, identity=identity, before=source, receipt=retained,
                              completed=completed, provider=providers[0], original_task=original_task, gets=gets,
                              mutation=mutation, pending_id=pending_id)


@pytest.mark.parametrize("stalled_input", [False, True], indirect=True, ids=["follow", "retarget"])
def test_explicit_follow_closes_stalled_input_and_keeps_original_cid_checkpoint(stalled_input):
    case = stalled_input
    assert invoke(case.client, case.identity, NativeRead(request_id="stalled-rid")).json()["status"] == "accepted"
    before = invoke(case.client, case.identity, NativeSessionRead()).json()
    command = {
        "operation": "session_follow", "request_id": "explicit-environment-follow",
        "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"], "stage": "prepare",
    }
    retarget = None if case.pending_id is None else {
        "operation": "session_retarget", "request_id": command["request_id"],
        "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"],
        "expected_agent_version": case.identity.agent_version, "pending_request_id": "older-pending-operation",
        "pending_session_id": case.pending_id.session_id,
    }
    response = invoke(case.client, case.identity, command if retarget is None else retarget)
    assert response.status_code == 200 and response.json()["reason"] == "source_stop_required", response.text
    closed = invoke(case.client, case.identity, NativeRead(request_id="stalled-rid")).json()
    assert closed["status"] == "unknown" and closed["failure_code"] == "NATIVE_TURN_NOT_STARTED"
    assert closed["turn_number"] is None and closed["command"] == case.receipt.command.model_dump(mode="json")
    task = case.client.portal.call(case.provider.get, IDENTITY.agent_id)
    assert (task.etag, task.payload, task.session_id) == (
        case.original_task.etag, case.original_task.payload, case.original_task.session_id)
    assert not case.current.executor.calls
    ready = invoke(case.client, case.identity, {**command, "stage": "commit"})
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert invoke(case.client, case.identity, NativeRead(request_id="rid-1")).json() == case.completed.model_dump(mode="json")
    accepted = invoke(case.client, case.identity, submit("after-follow", case.completed.conversation_id))
    assert accepted.status_code == 202, accepted.text
    finished = Harness(case.current, case.current.executor, case.client).wait("after-follow", "completed")
    assert finished.turn_number == 2 and finished.conversation_id == case.completed.conversation_id
    assert len(case.current.executor.calls) == 1 and case.current.executor.calls[0][2] is not None
    assert invoke(case.client, case.identity, NativeRead(request_id="stalled-rid")).json() == closed


@pytest.mark.parametrize("change", [
    "in-progress", "leased", "different-agent", "different-task", "different-source",
    "different-rid", "different-etag", "different-call", "different-last-input", "attachment", "queued", "active",
])
def test_follow_does_not_close_an_unconfirmed_or_different_input(stalled_input, change):
    case = stalled_input

    def alter(body):
        if change == "in-progress":
            body["status"] = "in_progress"
        elif change == "leased":
            body["lease"] = {"owner": "OFFLINE-owner", "expires_at": "2000-01-01T00:00:00Z"}
        elif change == "different-agent":
            body["agent_name"] = "OFFLINE-another-agent"
        elif change == "different-task":
            body["id"] = "OFFLINE-another-task"
        elif change == "different-source":
            body["source"] = {"type": "agentserver.task", "name": "another-handler"}
        elif change == "different-rid":
            body["payload"]["input"]["request_id"] = body["payload"]["last_input_id"] = "another-rid"
        elif change == "different-etag":
            body["payload"]["input"]["accepted_etag"] = "another-etag"
        elif change == "different-call":
            body["payload"]["input"]["call_id"] = "another-original-call"
        elif change == "different-last-input":
            body["payload"]["last_input_id"] = "another-rid"
        elif change == "attachment":
            body["payload"]["input"] = {"__attachment_ref__": {"key": "input", "hash": "sha256:" + "a" * 64}}
        else:
            body["payload"]["steering"] = (
                {"pending_inputs": [{"request_id": "queued-rid"}]} if change == "queued"
                else {"pending_inputs": [], "active_input": {"request_id": "active-rid"}})
        return body

    case.mutation[0] = alter
    before = invoke(case.client, case.identity, NativeSessionRead()).json()
    response = invoke(case.client, case.identity, {
        "operation": "session_follow", "request_id": "explicit-environment-follow",
        "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"], "stage": "prepare",
    })
    assert response.status_code in {200, 409, 503}, response.text
    if response.status_code == 200:
        assert response.json()["state"] == "pending" and response.json()["reason"] == "accepted_or_started"
    assert invoke(case.client, case.identity, NativeRead(request_id="stalled-rid")).json() == case.receipt.model_dump(mode="json")
    assert not case.current.executor.calls
    retained = case.client.portal.call(case.provider.get, IDENTITY.agent_id)
    assert (retained.etag, retained.payload) == (case.original_task.etag, case.original_task.payload)


@pytest.mark.parametrize("winner", ["started", "unknown"])
def test_follow_close_cas_preserves_a_winning_started_or_terminal_record(stalled_input, monkeypatch, winner):
    case = stalled_input
    write, won = FoundryStateStore.set_item, []

    async def competing(store, key, value, **kwargs):
        if key == "rid:stalled-rid" and value["receipt"]["failure_code"] == "NATIVE_TURN_NOT_STARTED" and not won:
            original = await store.get_item(key)
            changed = {**original.value, "run_token": "original-winning-run", "receipt": {
                **original.value["receipt"], "status": winner,
                "failure_code": "NATIVE_TURN_UNKNOWN" if winner == "unknown" else None}}
            await write(store, key, changed, if_match=original.etag)
            won.append(changed)
        return await write(store, key, value, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "set_item", competing)
    before = invoke(case.client, case.identity, NativeSessionRead()).json()
    response = invoke(case.client, case.identity, {
        "operation": "session_follow", "request_id": "explicit-environment-follow",
        "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"], "stage": "prepare",
    })
    assert won
    if winner == "started":
        # The read-back started winner is confirmed (#905), so the follow waits for it instead of reporting unknown.
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "pending" and response.json()["reason"] == "accepted_or_started"
    else:
        assert response.status_code == 200 and response.json()["reason"] == "source_stop_required", response.text
    assert invoke(case.client, case.identity, NativeRead(request_id="stalled-rid")).json() == won[0]["receipt"]
    assert not case.current.executor.calls
