"""A published SID binding owns one official chain; retired tasks and conversation data stay intact."""

from types import SimpleNamespace

import httpx
import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.ai.agentserver.core.tasks._client import TransportClassifiedError
from azure.ai.agentserver.core.tasks._local_provider import LocalFileTaskProvider
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeRead, NativeSessionRead
from shared_context.native_task_observer import NativeTaskObserver
from shared_context.native_turns import NativeTurnRuntime, TURNS_STORE
from test_native_environment import invoke
from test_native_session_bootstrap import prepare
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env


async def token():
    return "OFFLINE-binding-observer"


def observer(identity, provider, reads):
    async def respond(request):
        assert request.method == "GET" and not request.content
        reads.append(request.url)
        if request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        task = await provider.get(request.url.path.rsplit("/", 1)[1])
        if task is None:
            return httpx.Response(404)
        lease = None if task.lease is None else {
            key: getattr(task.lease, key) for key in ("owner", "instance_id", "generation", "expires_at")}
        return httpx.Response(200, json={
            **{key: getattr(task, key) for key in ("id", "agent_name", "session_id", "status", "payload", "source")},
            "lease": lease,
        })

    return NativeTaskObserver("https://offline.example/api/projects/offline", identity, token,
                              transport=httpx.MockTransport(respond))


async def agent_item():
    async with FoundryStateStore(TURNS_STORE) as store:
        return await store.get_item("agent")


def test_new_binding_creates_its_own_task_and_resumes_the_original_checkpoint(local_env, monkeypatch):
    creates, updates, runs = [], [], []
    create, update = LocalFileTaskProvider.create, LocalFileTaskProvider.update
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    start = old.chain.start
    providers = []

    async def record_create(provider, request):
        providers.append(provider)
        result = await create(provider, request)
        creates.append((result.id, result.session_id))
        return result

    async def no_cross_session_lease(provider, task_id, patch):
        original = await provider.get(task_id)
        if patch.status == "in_progress":
            updates.append((task_id, original.session_id, patch.lease_owner))
            if patch.lease_owner != IDENTITY.agent_name + "|session:" + original.session_id:
                raise TransportClassifiedError(
                    status=502, classification="transient", message="OFFLINE cross-creation-session lease failure")
        return await update(provider, task_id, patch)

    async def remember_run(**kwargs):
        result = await start(**kwargs)
        runs.append(result)
        return result

    monkeypatch.setattr(LocalFileTaskProvider, "create", record_create)
    monkeypatch.setattr(LocalFileTaskProvider, "update", no_cross_session_lease)
    monkeypatch.setattr(old.chain, "start", remember_run)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, old.executor, client)
        assert prepare(h).status_code == 200
        assert h.invoke(submit()).status_code == 202
        h.call(runs[0].result)
        completed = h.call(old.read, "rid-1")
        assert completed.status == "completed", completed.failure_code
        before = invoke(client, IDENTITY, NativeSessionRead()).json()
        old_task = h.call(providers[0].get, IDENTITY.agent_id)
        assert old_task.status == "suspended" and old_task.lease is None
    assert creates == [(IDENTITY.agent_id, IDENTITY.session_id)]

    identity = IDENTITY.model_copy(update={"session_id": "OFFLINE-new-binding", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    stopped, reads, current_runs = [], [], []
    source_inactive = [False]

    async def inactive(sid):
        assert sid == IDENTITY.session_id
        stopped.append(sid)
        return source_inactive[0]

    current = NativeTurnRuntime(identity, OfflineSdk(), bootstrap_source_inactive=inactive,
                                task_observer=observer(identity, providers[0], reads))
    current_start = current.chain.start

    async def current_run(**kwargs):
        result = await current_start(**kwargs)
        current_runs.append(result)
        return result

    monkeypatch.setattr(current.chain, "start", current_run)
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=current)) as client:
        command = {
            "operation": "session_follow", "request_id": "follow-new-binding",
            "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"], "stage": "prepare",
        }
        pending = invoke(client, identity, command)
        assert pending.status_code == 200 and pending.json()["reason"] == "source_stop_required", pending.text
        assert not stopped and len(creates) == 1
        active = invoke(client, identity, {**command, "stage": "commit"})
        assert active.status_code == 200 and active.json()["reason"] == "source_active", active.text
        still_old = client.portal.call(agent_item).value
        assert still_old["hosted_session_id"] == IDENTITY.session_id and "chain_task_id" not in still_old
        source_inactive[0] = True
        ready = invoke(client, identity, {**command, "stage": "commit"})
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
        assert stopped == [IDENTITY.session_id, IDENTITY.session_id] and len(creates) == 1
        published = client.portal.call(agent_item)
        assert invoke(client, identity, {**command, "stage": "commit"}).json() == ready.json()
        replayed = client.portal.call(agent_item)
        assert (replayed.etag, replayed.value) == (published.etag, published.value)
        response = invoke(client, identity, submit("new-followup", completed.conversation_id))
        assert response.status_code == 202, response.text
        finished = Harness(current, current.executor, client).wait("new-followup", "completed")
        client.portal.call(current_runs[-1].result)
        assert finished.turn_number == 2 and finished.conversation_id == completed.conversation_id
        binding = client.portal.call(agent_item).value
        assert binding["chain_task_id"] != IDENTITY.agent_id
        assert creates == [(IDENTITY.agent_id, IDENTITY.session_id), (binding["chain_task_id"], identity.session_id)]
        assert not updates
        assert len(current.executor.calls) == 1 and current.executor.calls[0][2] is not None
        assert invoke(client, identity, NativeRead(request_id="rid-1")).json() == completed.model_dump(mode="json")
        retained = client.portal.call(providers[0].get, IDENTITY.agent_id)
        assert (retained.etag, retained.status, retained.lease, retained.payload) == (
            old_task.etag, old_task.status, old_task.lease, old_task.payload)
        assert all(url.path.endswith(("/tasks", "/" + IDENTITY.agent_id)) for url in reads)
        # All conversations on this published binding still share one serial official chain.
        current.executor.block = True
        assert invoke(client, identity, submit("other-cid-one")).status_code == 202
        Harness(current, current.executor, client).wait("other-cid-one", "started")
        assert invoke(client, identity, submit("other-cid-two")).json()["status"] == "accepted"
        assert len(creates) == 2 and current.executor.max_active == 1
        current.executor.block = False
        client.portal.call(current_runs[-1].result)
        Harness(current, current.executor, client).wait("other-cid-two", "completed")
        assert current.executor.max_active == 1 and len(creates) == 2
        second_state = invoke(client, identity, NativeSessionRead()).json()
        second_task = client.portal.call(providers[0].get, binding["chain_task_id"])
        assert second_task.status == "suspended" and second_task.lease is None

    third_identity = identity.model_copy(update={"session_id": "OFFLINE-third-binding", "agent_version": "3"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", third_identity.session_id)
    third_reads, third_runs = [], []

    async def second_inactive(sid):
        assert sid == identity.session_id
        return True

    third = NativeTurnRuntime(third_identity, OfflineSdk(), bootstrap_source_inactive=second_inactive,
                              task_observer=observer(third_identity, providers[0], third_reads))
    third_start = third.chain.start

    async def third_run(**kwargs):
        result = await third_start(**kwargs)
        third_runs.append(result)
        return result

    monkeypatch.setattr(third.chain, "start", third_run)
    with TestClient(create_host(SimpleNamespace(identity=third_identity), native_runtime=third)) as client:
        following = {
            "operation": "session_follow", "request_id": "follow-third-binding",
            "expected_session_id": identity.session_id, "expected_etag": second_state["etag"], "stage": "prepare",
        }
        assert invoke(client, third_identity, following).json()["reason"] == "source_stop_required"
        assert invoke(client, third_identity, {**following, "stage": "commit"}).json()["state"] == "ready"
        third_binding = client.portal.call(agent_item).value
        assert third_binding["chain_task_id"] not in (IDENTITY.agent_id, binding["chain_task_id"])
        assert all(url.path.endswith(("/tasks", "/" + binding["chain_task_id"])) for url in third_reads)
        assert invoke(client, third_identity, submit("third-followup", completed.conversation_id)).status_code == 202
        client.portal.call(third_runs[-1].result)
        final = Harness(third, third.executor, client).wait("third-followup", "completed")
        assert final.turn_number == 3 and final.conversation_id == completed.conversation_id
        assert creates[-1] == (third_binding["chain_task_id"], third_identity.session_id)
        retained = client.portal.call(providers[0].get, binding["chain_task_id"])
        assert (retained.etag, retained.status, retained.lease, retained.payload) == (
            second_task.etag, second_task.status, second_task.lease, second_task.payload)
        assert len(third.executor.calls) == 1 and third.executor.calls[0][2] is not None

    # Restarting the current SID reuses the published task, rather than creating a new generation.
    restarted = NativeTurnRuntime(third_identity, OfflineSdk())
    restart_runs = []
    restart_start = restarted.chain.start

    async def restart_run(**kwargs):
        result = await restart_start(**kwargs)
        restart_runs.append(result)
        return result

    monkeypatch.setattr(restarted.chain, "start", restart_run)
    with TestClient(create_host(SimpleNamespace(identity=third_identity), native_runtime=restarted)) as client:
        assert invoke(client, third_identity, submit("after-restart", completed.conversation_id)).status_code == 202
        client.portal.call(restart_runs[-1].result)
        assert Harness(restarted, restarted.executor, client).wait("after-restart", "completed").turn_number == 4
        assert len(creates) == 3
        assert client.portal.call(agent_item).value["chain_task_id"] == third_binding["chain_task_id"]


@pytest.mark.parametrize("status,lease", [
    ("in_progress", {"owner": "OFFLINE-source-owner"}),
    ("suspended", {"owner": "OFFLINE-source-owner"}),
])
def test_source_task_is_not_mistaken_for_an_empty_destination(local_env, monkeypatch, status, lease):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, old.executor, client)
        assert prepare(h).status_code == 200
        assert h.invoke(submit()).status_code == 202
        h.wait("rid-1", "completed")
        state = invoke(client, IDENTITY, NativeSessionRead()).json()
    identity = IDENTITY.model_copy(update={"session_id": "OFFLINE-new-binding", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    paths, stopped = [], []

    def respond(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/" + IDENTITY.agent_id):
            return httpx.Response(200, json={
                "id": IDENTITY.agent_id, "agent_name": identity.agent_name, "session_id": IDENTITY.session_id,
                "status": status, "lease": lease, "payload": {},
            })
        return httpx.Response(404)

    async def inactive(sid):
        stopped.append(sid)
        return True

    task_observer = NativeTaskObserver("https://offline.example/api/projects/offline", identity, token,
                                       transport=httpx.MockTransport(respond))
    current = NativeTurnRuntime(identity, OfflineSdk(), task_observer=task_observer, bootstrap_source_inactive=inactive)
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=current)) as client:
        result = invoke(client, identity, {
            "operation": "session_follow", "request_id": "must-wait",
            "expected_session_id": IDENTITY.session_id, "expected_etag": state["etag"], "stage": "prepare",
        })
        assert result.status_code == 200 and result.json()["reason"] == "task_active", result.text
        item = client.portal.call(agent_item).value
        assert item["hosted_session_id"] == IDENTITY.session_id and "chain_task_id" not in item
        assert paths == ["/api/projects/offline/tasks/" + IDENTITY.agent_id]
        assert not stopped and not current.executor.calls
