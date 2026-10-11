"""Fixed task GET/list transport used by the N1 environment/reentry interfaces."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from shared_context.errors import Problem
from shared_context.native_task_observer import NativeTaskObserver
from test_native_turns import IDENTITY


async def token():
    return "offline-agent-token"


def task_record(**changes):
    return {
        "id": IDENTITY.agent_id, "agent_name": IDENTITY.agent_name,
        "session_id": "original-creation-session", "status": "suspended", "lease": None,
        "payload": {"input": {"request_id": "original-rid", "accepted_etag": "original-etag", "call_id": "opaque"}},
        **changes,
    }


@pytest.mark.parametrize("blocked", [
    None, "fa-native-terminal-notification-v1", "fa-native-agent-enqueue-v1", "fa-native-version-message-v1",
])
@pytest.mark.parametrize("original_present", [True, False], ids=["original-chain", "no-original-chain"])
def test_quiet_uses_bounded_server_filters_without_task_or_lease_mutation(blocked, original_present):
    calls = []

    def server(request):
        calls.append(request)
        assert request.method == "GET" and not request.content
        if request.url.path.endswith("/" + IDENTITY.agent_id):
            return httpx.Response(200, json=task_record()) if original_present else httpx.Response(404)
        assert dict(request.url.params) == {
            "api-version": "v1", "agent_name": IDENTITY.agent_name,
            "session_id": IDENTITY.session_id, "status": "in_progress", "source_type": "agentserver.task",
            "tag.task_name": request.url.params["tag.task_name"], "omit_attachment_values": "true", "limit": "1",
        }
        name = request.url.params["tag.task_name"]
        rows = [] if name != blocked else [task_record(
            id="original-notification", status="in_progress", session_id=IDENTITY.session_id,
            tags={"task_name": name})]
        return httpx.Response(200, json={"data": rows, "has_more": False})

    observer = NativeTaskObserver("https://offline.example/api/projects/offline", IDENTITY, token,
                                  transport=httpx.MockTransport(server))
    assert asyncio.run(observer.quiet(IDENTITY.session_id)) is (blocked is None)
    assert len(calls) <= 4


@pytest.mark.parametrize("status", [403, 500])
def test_task_read_denial_or_uncertainty_is_never_an_empty_chain(status):
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "PRIVATE-NOT-FOR-LOGS"}})

    observer = NativeTaskObserver("https://offline.example/api/projects/offline", IDENTITY, token,
                                  transport=httpx.MockTransport(server))
    with pytest.raises(Problem) as error:
        asyncio.run(observer.quiet(IDENTITY.session_id))
    assert len(calls) == 1 and "PRIVATE" not in str(error.value.body)
    assert error.value.status == (403 if status == 403 else 503)


@pytest.mark.parametrize("owner,expired,creation,expected", [
    (IDENTITY.session_id, True, "old-creation-session", "resume"),
    ("other-session", True, "old-creation-session", "pending"),
    (IDENTITY.session_id, False, "old-creation-session", "pending"),
    (IDENTITY.session_id, True, IDENTITY.session_id, "skip"),
])
def test_only_the_current_sid_expired_lease_exposes_its_original_input(owner, expired, creation, expected):
    original = task_record(status="in_progress", session_id=creation, lease={
        "owner": IDENTITY.agent_name + "|session:" + owner, "instance_id": "old-instance", "generation": 1,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)).isoformat(),
    })
    calls = []

    def server(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=original)

    observer = NativeTaskObserver("https://offline.example/api/projects/offline", IDENTITY, token,
                                  transport=httpx.MockTransport(server))
    if expected == "pending":
        with pytest.raises(Problem) as refused:
            asyncio.run(observer.owned_input())
        assert refused.value.status == 503
    else:
        observed = asyncio.run(observer.owned_input())
        assert observed == (original["payload"]["input"] if expected == "resume" else None)
    assert len(calls) == 1


def test_suspended_sequence_metadata_is_not_pending_work():
    original = task_record(payload={"last_input_id": "completed-rid", "steering": {
        "pending_inputs": [], "cancel_requested": False, "drain_in_progress": False, "next_input_seq": 3}})

    def server(request):
        return httpx.Response(200, json=(original if request.url.path.endswith("/" + IDENTITY.agent_id)
                                         else {"data": [], "has_more": False}))

    observer = NativeTaskObserver("https://offline.example/api/projects/offline", IDENTITY, token,
                                  transport=httpx.MockTransport(server))
    assert asyncio.run(observer.quiet(IDENTITY.session_id)) is True


@pytest.mark.parametrize("method", ["quiet", "suspended_input", "owned_input"])
def test_observation_uses_the_persisted_binding_task_id_without_touching_the_legacy_task(method):
    selected_task = "fa-chain-offline-published-binding"
    original = task_record(id=selected_task, source={"type": "agentserver.task", "name": "fa-native-agent-turn-v1"})
    original["payload"]["last_input_id"] = "original-rid"
    if method == "owned_input":
        original.update(status="in_progress", lease={
            "owner": IDENTITY.agent_name + "|session:" + IDENTITY.session_id,
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        })
    paths = []

    def server(request):
        assert request.method == "GET" and not request.content
        paths.append(request.url.path)
        assert not request.url.path.endswith("/" + IDENTITY.agent_id)
        if request.url.path.endswith("/" + selected_task):
            return httpx.Response(200, json=original)
        assert request.url.path.endswith("/tasks")
        return httpx.Response(200, json={"data": [], "has_more": False})

    observer = NativeTaskObserver("https://offline.example/api/projects/offline", IDENTITY, token,
                                  transport=httpx.MockTransport(server))
    call = (observer.quiet(IDENTITY.session_id, task_id=selected_task) if method == "quiet"
            else getattr(observer, method)(task_id=selected_task))
    result = asyncio.run(call)
    assert result == (True if method == "quiet" else original["payload"]["input"])
    assert paths[0] == "/api/projects/offline/tasks/" + selected_task


@pytest.mark.parametrize("draining", [False, True])
def test_recovery_uses_fixed_sdk_drain_choice_not_a_truthy_active_input(draining):
    ordinary = {"request_id": "original-input", "accepted_etag": "etag-A", "call_id": "opaque-A"}
    active = {"request_id": "draining-input", "accepted_etag": "etag-B", "call_id": "opaque-B"}
    original = task_record(status="in_progress", lease={
        "owner": IDENTITY.agent_name + "|session:" + IDENTITY.session_id,
        "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }, payload={"input": ordinary, "last_input_id": "newer-queued-id",
                "steering": {"active_input": active, "drain_in_progress": draining, "pending_inputs": []}})
    observer = NativeTaskObserver("https://offline.example/api/projects/offline", IDENTITY, token,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=original)))
    assert asyncio.run(observer.owned_input()) == (active if draining else ordinary)
