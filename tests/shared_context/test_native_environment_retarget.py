"""Explicitly supersede an obsolete pending operation without rewriting its records."""

import asyncio
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeRead, NativeSessionFollow, NativeSessionRead
from shared_context.errors import Problem
from shared_context.native_turns import NativeTurnRuntime
from test_native_environment import invoke
from test_native_session_bootstrap import prepare
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env, accept_without_chain


async def inactive(_sid, *, task_id=None):
    return True


async def no_suspended_input(*, task_id=None):
    return None


@pytest.fixture
def pending_retarget(local_env, monkeypatch, request):
    kind = getattr(request, "param", None)
    sdk = OfflineSdk()
    old = NativeTurnRuntime(IDENTITY, sdk)
    receipt = None
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, sdk, client)
        prepared = prepare(h)
        assert prepared.status_code == 200, prepared.text
        if kind == "accepted":
            receipt = accept_without_chain(h).value.receipt
        elif kind == "started":
            sdk.block = True
            response = h.invoke(submit())
            assert response.status_code == 202, response.text
            receipt = h.wait("rid-1", "started")
        elif kind in {"notification", "task-denied", "unknown"}:
            if kind == "unknown":
                async def failed(*_args):
                    raise RuntimeError("synthetic original unconfirmed outcome")
                old.executor = failed
            response = h.invoke(submit())
            assert response.status_code == 202, response.text
            receipt = h.wait("rid-1", "unknown" if kind == "unknown" else "completed")
        elif kind == "admission":
            write = FoundryStateStore.create_item
            failed = []
            async def unavailable(store, key, value, **kwargs):
                if key == "rid:rid-1":
                    failed.append(True)
                    raise ServiceResponseError("synthetic original admission write is unconfirmed")
                return await write(store, key, value, **kwargs)
            with monkeypatch.context() as writes:
                writes.setattr(FoundryStateStore, "create_item", unavailable)
                response = h.invoke(submit())
                assert response.status_code == 503 and failed, response.text
                assert response.json()["error"]["code"] == "NATIVE_STATE_STORE_UNKNOWN"
        source = invoke(client, IDENTITY, NativeSessionRead()).json()
        if kind == "admission":
            assert source["admission_request_id"] == "rid-1"
    pending_id = IDENTITY.model_copy(update={"session_id": "offline-pending-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", pending_id.session_id)
    pending = NativeTurnRuntime(pending_id, OfflineSdk(),
                                task_observer=SimpleNamespace(quiet=inactive, suspended_input=no_suspended_input))
    with TestClient(create_host(SimpleNamespace(identity=pending_id), native_runtime=pending)) as client:
        response = invoke(client, pending_id, {
            "operation": "session_follow", "request_id": "original-pending",
            "expected_session_id": IDENTITY.session_id, "expected_etag": source["etag"], "stage": "prepare",
        })
        assert response.status_code == 200, response.text
        old_view = invoke(client, pending_id, NativeSessionRead()).json()["following"]
    current_id = IDENTITY.model_copy(update={"session_id": "offline-current-session", "agent_version": "3"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", current_id.session_id)

    async def quiet(_sid, *, task_id=None):
        if kind == "task-denied":
            raise Problem(403, "NATIVE_ENVIRONMENT_UNAVAILABLE", "original task access is denied")
        return kind != "notification"

    current = NativeTurnRuntime(current_id, OfflineSdk(), bootstrap_source_inactive=inactive,
                                task_observer=SimpleNamespace(quiet=quiet, suspended_input=no_suspended_input))
    with TestClient(create_host(SimpleNamespace(identity=current_id), native_runtime=current)) as client:
        before = invoke(client, current_id, NativeSessionRead()).json()
        command = {
            "operation": "session_retarget", "request_id": "new-explicit-operation",
            "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"],
            "expected_agent_version": current_id.agent_version,
            "pending_request_id": "original-pending", "pending_session_id": pending_id.session_id,
        }
        yield SimpleNamespace(client=client, current=current, identity=current_id, pending=pending,
                              command=command, before=before, old_view=old_view, receipt=receipt)


def test_explicit_retarget_preserves_old_pending_and_continues_original_conversation(local_env, monkeypatch):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, old.executor, client)
        prepare(h)
        assert h.invoke(submit()).status_code == 202
        receipt = h.wait("rid-1", "completed")
        initial = invoke(client, IDENTITY, NativeSessionRead()).json()
    pending_identity = IDENTITY.model_copy(update={"session_id": "offline-pending-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", pending_identity.session_id)
    pending = NativeTurnRuntime(pending_identity, OfflineSdk(), task_observer=SimpleNamespace(quiet=inactive))
    old_command = {
        "operation": "session_follow", "request_id": "original-pending",
        "expected_session_id": IDENTITY.session_id, "expected_etag": initial["etag"], "stage": "prepare",
    }
    with TestClient(create_host(SimpleNamespace(identity=pending_identity), native_runtime=pending)) as client:
        response = invoke(client, pending_identity, old_command)
        assert response.status_code == 200 and response.json()["reason"] == "source_stop_required"
        old_view = invoke(client, pending_identity, NativeSessionRead()).json()["following"]
    current_identity = IDENTITY.model_copy(update={"session_id": "offline-current-session", "agent_version": "3"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", current_identity.session_id)
    sdk = OfflineSdk()
    current = NativeTurnRuntime(current_identity, sdk, bootstrap_source_inactive=inactive,
                                task_observer=SimpleNamespace(quiet=inactive))
    with TestClient(create_host(SimpleNamespace(identity=current_identity), native_runtime=current)) as client:
        before = invoke(client, current_identity, NativeSessionRead()).json()
        command = {
            "operation": "session_retarget", "request_id": "new-explicit-operation",
            "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"],
            "expected_agent_version": current_identity.agent_version,
            "pending_request_id": "original-pending", "pending_session_id": pending_identity.session_id,
        }
        retargeted = invoke(client, current_identity, command)
        assert retargeted.status_code == 200, retargeted.text
        planned = retargeted.json()
        assert planned["state"] == "pending" and planned["reason"] == "source_stop_required"
        assert planned["request_id"] == command["request_id"]
        assert planned["hosted_session_id"] == current_identity.session_id
        assert planned["previous_hosted_session_id"] == IDENTITY.session_id
        assert client.portal.call(pending.session_state).following.model_dump(mode="json") == old_view
        state = invoke(client, current_identity, NativeSessionRead()).json()
        assert state["environment"] == planned and state["hosted_session_id"] == IDENTITY.session_id
        committed = invoke(client, current_identity, {
            "operation": "session_follow", "request_id": command["request_id"],
            "expected_session_id": IDENTITY.session_id, "expected_etag": state["etag"], "stage": "commit",
        })
        assert committed.status_code == 200 and committed.json()["state"] == "ready"
        assert invoke(client, current_identity, command).json() == committed.json()
        assert client.portal.call(pending.session_state).following.model_dump(mode="json") == old_view
        assert invoke(client, current_identity, NativeRead(request_id="rid-1")).json() == receipt.model_dump(mode="json")
        assert not sdk.calls and not pending.executor.calls
        response = invoke(client, current_identity, submit("new-followup", receipt.conversation_id))
        assert response.status_code == 202
        finished = Harness(current, sdk, client).wait("new-followup", "completed")
        assert finished.conversation_id == receipt.conversation_id and finished.turn_number == 2
        assert sdk.calls[0][2] is not None and len(sdk.calls) == 1


@pytest.mark.parametrize("pending_retarget,reason", [
    ("accepted", "accepted_or_started"), ("started", "accepted_or_started"), ("admission", "admission_pending"),
    ("notification", "task_active"), ("task-denied", "task_access_denied"),
], indirect=["pending_retarget"])
def test_pending_work_prevents_switching_the_operation_pointer(pending_retarget, reason):
    case = pending_retarget
    response = invoke(case.client, case.identity, case.command)
    assert response.status_code == 200 and response.json()["reason"] == reason, response.text
    after = invoke(case.client, case.identity, NativeSessionRead()).json()
    assert after["environment"] == case.before["environment"]
    assert after["hosted_session_id"] == case.before["hosted_session_id"]
    assert case.client.portal.call(case.pending.session_state).following.model_dump(mode="json") == case.old_view
    if case.receipt is not None:
        # ADR-0009 §2: a call not located by a binding row (this one sends no locator header) keeps phase 2a's rule,
        # so this retarget candidate on the record's version still serves record reads.
        assert invoke(case.client, case.identity, NativeRead(request_id="rid-1")).json() == case.receipt.model_dump(mode="json")
    assert not case.current.executor.calls


@pytest.mark.parametrize("changes", [
    {"request_id": "original-pending"}, {"pending_request_id": "different-pending"},
    {"pending_session_id": "different-pending-session"}, {"expected_session_id": "another-source"},
    {"expected_etag": "stale"}, {"expected_agent_version": "4"},
])
def test_retarget_requires_the_original_pending_and_current_destination(pending_retarget, changes):
    case = pending_retarget
    response = invoke(case.client, case.identity, {**case.command, **changes})
    assert response.status_code in {409, 422}
    assert invoke(case.client, case.identity, NativeSessionRead()).json() == case.before
    assert not case.current.executor.calls


@pytest.mark.parametrize("pending_retarget", ["unknown"], indirect=True)
def test_terminal_unknown_is_preserved_during_explicit_retarget(pending_retarget):
    case = pending_retarget
    response = invoke(case.client, case.identity, case.command)
    assert response.status_code == 200 and response.json()["reason"] == "source_stop_required"
    assert invoke(case.client, case.identity, NativeRead(request_id="rid-1")).json() == case.receipt.model_dump(mode="json")
    assert case.receipt.status == "unknown" and not case.current.executor.calls


def test_lost_pointer_ack_replays_the_new_operation_without_rewriting_old_pending(pending_retarget, monkeypatch):
    case = pending_retarget
    write = FoundryStateStore.set_item
    lost = []

    async def applied(store, key, value, **kwargs):
        result = await write(store, key, value, **kwargs)
        if key == "agent" and value.get("environment", {}).get("request_id") == case.command["request_id"] and not lost:
            lost.append(True)
            raise ServiceResponseError("synthetic pointer acknowledgment lost")
        return result

    monkeypatch.setattr(FoundryStateStore, "set_item", applied)
    response = invoke(case.client, case.identity, case.command)
    assert response.status_code == 200 and lost
    saved = invoke(case.client, case.identity, NativeSessionRead()).json()
    repeated = invoke(case.client, case.identity, case.command)
    assert repeated.status_code == 200 and repeated.json() == response.json()
    assert invoke(case.client, case.identity, NativeSessionRead()).json() == saved
    assert case.client.portal.call(case.pending.session_state).following.model_dump(mode="json") == case.old_view
    assert not case.current.executor.calls


def test_saved_retarget_identity_cannot_be_reused_for_another_original_operation(pending_retarget):
    case = pending_retarget
    initial = invoke(case.client, case.identity, case.command)
    assert initial.status_code == 200, initial.text
    saved = invoke(case.client, case.identity, NativeSessionRead()).json()
    for changes in ({"request_id": "another-new-id"}, {"pending_request_id": "different-original"},
                    {"pending_session_id": "different-original-session"}):
        response = invoke(case.client, case.identity, {**case.command, **changes})
        assert response.status_code == 409
        assert invoke(case.client, case.identity, NativeSessionRead()).json() == saved
    assert case.client.portal.call(case.pending.session_state).following.model_dump(mode="json") == case.old_view


@pytest.mark.parametrize("agent_marker", [False, True])
def test_saved_retarget_keeps_later_refusal_when_progress_publish_is_unconfirmed(pending_retarget, monkeypatch, agent_marker):
    from shared_context import native_environment as environment
    from shared_context.native_turns import TURNS_STORE, _changed, _json

    case = pending_retarget
    drain, progress = environment._drain_pages, environment._progress
    intent_key, progress_key = environment._retarget_keys(case.command["request_id"])

    async def scan_pending(*_args, **_kwargs):
        return "scan_pending", None, False

    async def original_intent():
        async with case.current._store(TURNS_STORE) as store:
            return (await store.get_item(intent_key)).value

    monkeypatch.setattr(environment, "_drain_pages", scan_pending)
    first = invoke(case.client, case.identity, case.command)
    assert first.status_code == 200 and first.json()["reason"] == "scan_pending", first.text
    saved_intent = case.client.portal.call(original_intent)

    async def mark_source():
        old, etag = await environment._read(case.pending)
        await progress(case.pending, _changed(old, view=_changed(old.view, source_invocation_refused=True)), etag)
        if agent_marker:
            current, etag = await case.current._agent_record()
            async with case.current._store(TURNS_STORE) as store:
                await store.set_item("agent", _json(_changed(
                    current, environment=_changed(current.environment, source_invocation_refused=True))), if_match=etag)

    async def lost_publish(runtime, record, etag, *, key=None):
        if key == progress_key and record.view.source_invocation_refused:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "OFFLINE final progress CAS did not commit")
        return await progress(runtime, record, etag, key=key)

    case.client.portal.call(mark_source)
    monkeypatch.setattr(environment, "_drain_pages", drain)
    monkeypatch.setattr(environment, "_progress", lost_publish)
    resumed = invoke(case.client, case.identity, case.command)
    assert resumed.status_code == 503, resumed.text
    state = invoke(case.client, case.identity, NativeSessionRead()).json()
    assert state["source_invocation_refused"] is True
    assert state["environment"]["source_invocation_refused"] is True
    assert not state["following"].get("source_invocation_refused", False)
    monkeypatch.setattr(environment, "_progress", progress)
    repeated = invoke(case.client, case.identity, case.command)
    assert repeated.status_code == 200 and repeated.json()["source_invocation_refused"] is True, repeated.text
    continued = invoke(case.client, case.identity, NativeSessionFollow(
        request_id=case.command["request_id"], expected_session_id=case.command["expected_session_id"],
        expected_etag=state["etag"], stage="prepare"))
    assert continued.status_code == 200 and continued.json()["source_invocation_refused"] is True, continued.text
    assert case.client.portal.call(original_intent) == saved_intent
    assert not case.current.executor.calls and not case.pending.executor.calls


@pytest.mark.parametrize("pending_retarget", [None, "notification"], indirect=True)
def test_same_retarget_completes_following_publication_after_pointer_write(pending_retarget, monkeypatch):
    case = pending_retarget
    if case.receipt is not None:
        busy = invoke(case.client, case.identity, case.command)
        assert busy.status_code == 200 and busy.json()["reason"] == "task_active"
        case.current.task_observer = SimpleNamespace(quiet=inactive)
    write = FoundryStateStore.set_item
    failed = []

    async def unavailable(store, key, value, **kwargs):
        if key.startswith("retarget-progress:") and value["phase"] == "stopping" and not failed:
            agent = await store.get_item("agent")
            assert agent.value["environment"]["request_id"] == case.command["request_id"]
            failed.append(True)
            raise ServiceResponseError("synthetic selected following phase not confirmed")
        return await write(store, key, value, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "set_item", unavailable)
    first = invoke(case.client, case.identity, case.command)
    assert first.status_code == 503 and failed
    saved = invoke(case.client, case.identity, NativeSessionRead()).json()
    assert saved["hosted_session_id"] == IDENTITY.session_id
    assert saved["environment"]["request_id"] == saved["following"]["request_id"] == case.command["request_id"]
    repeated = invoke(case.client, case.identity, case.command)
    assert repeated.status_code == 200 and repeated.json()["reason"] == "source_stop_required", repeated.text
    follow = {
        "operation": "session_follow", "request_id": case.command["request_id"],
        "expected_session_id": IDENTITY.session_id, "expected_etag": saved["etag"], "stage": "prepare",
    }
    prepared = invoke(case.client, case.identity, follow)
    assert prepared.status_code == 200 and prepared.json()["reason"] == "source_stop_required", prepared.text
    assert invoke(case.client, case.identity, case.command).json()["reason"] == "source_stop_required"
    ready = invoke(case.client, case.identity, {**follow, "stage": "commit"})
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert case.client.portal.call(case.pending.session_state).following.model_dump(mode="json") == case.old_view
    assert not case.current.executor.calls


@pytest.mark.parametrize("pending_retarget", ["notification"], indirect=True)
def test_same_retarget_id_cannot_select_another_build(pending_retarget, monkeypatch):
    case = pending_retarget
    first = invoke(case.client, case.identity, case.command)
    assert first.status_code == 200 and first.json()["reason"] == "task_active"
    next_identity = case.identity.model_copy(update={"session_id": "offline-later-session", "agent_version": "4"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", next_identity.session_id)
    later = NativeTurnRuntime(next_identity, OfflineSdk(), task_observer=SimpleNamespace(quiet=inactive))
    with TestClient(create_host(SimpleNamespace(identity=next_identity), native_runtime=later)) as client:
        before = invoke(client, next_identity, NativeSessionRead()).json()
        response = invoke(client, next_identity, {
            **case.command, "expected_agent_version": next_identity.agent_version, "expected_etag": before["etag"],
        })
        assert response.status_code == 409, response.text
        assert invoke(client, next_identity, NativeSessionRead()).json() == before
        assert not later.executor.calls


def test_original_operation_finishing_wins_over_a_delayed_retarget(pending_retarget, monkeypatch):
    case = pending_retarget
    write = FoundryStateStore.set_item
    raced = []
    case.pending.bootstrap_source_inactive = inactive

    async def racing(store, key, value, **kwargs):
        if key == "agent" and value.get("environment", {}).get("request_id") == case.command["request_id"] and not raced:
            raced.append(True)
            ready = await case.pending.dispatch(NativeSessionFollow(
                request_id="original-pending", expected_session_id=IDENTITY.session_id,
                expected_etag=case.command["expected_etag"], stage="commit"))
            assert ready.state == "ready"
        return await write(store, key, value, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "set_item", racing)
    response = invoke(case.client, case.identity, case.command)
    assert response.status_code in {409, 503} and raced
    state = invoke(case.client, case.identity, NativeSessionRead()).json()
    assert state["hosted_session_id"] == case.pending.identity.session_id
    assert state["environment"]["request_id"] == "original-pending" and state["environment"]["state"] == "ready"
    assert not case.current.executor.calls
    assert invoke(case.client, case.identity, case.command).status_code == 409
    following = {
        "operation": "session_follow", "request_id": "new-follow-from-winner",
        "expected_session_id": case.pending.identity.session_id, "expected_etag": state["etag"], "stage": "prepare",
    }
    prepared = invoke(case.client, case.identity, following)
    assert prepared.status_code == 200 and prepared.json()["reason"] == "source_stop_required", prepared.text
    ready = invoke(case.client, case.identity, {**following, "stage": "commit"})
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["previous_hosted_session_id"] == case.pending.identity.session_id
    assert invoke(case.client, case.identity, case.command).status_code == 409
    assert invoke(case.client, case.identity, submit("new-work-after-race")).status_code == 202
    result = Harness(case.current, case.current.executor, case.client).wait("new-work-after-race", "completed")
    assert result.hosted_session_id == case.identity.session_id and len(case.current.executor.calls) == 1


def test_conflicting_destination_following_does_not_change_the_current_operation(local_env, monkeypatch):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        prepared = prepare(Harness(old, old.executor, client))
        assert prepared.status_code == 200, prepared.text
        source = invoke(client, IDENTITY, NativeSessionRead()).json()
    pending_id = IDENTITY.model_copy(update={"session_id": "offline-pending-session", "agent_version": "2"})
    current_id = IDENTITY.model_copy(update={"session_id": "offline-current-session", "agent_version": "3"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", current_id.session_id)
    pending = NativeTurnRuntime(pending_id, OfflineSdk(), bootstrap_source_inactive=inactive)
    current = NativeTurnRuntime(current_id, OfflineSdk(), bootstrap_source_inactive=inactive)
    create = FoundryStateStore.create_item
    raced = []
    original = NativeSessionFollow(
        request_id="original-pending", expected_session_id=IDENTITY.session_id,
        expected_etag=source["etag"], stage="prepare")

    async def select_original_first(store, key, value, **kwargs):
        if (key.startswith("following:") and value["view"]["hosted_session_id"] == current_id.session_id
                and not raced):
            raced.append(True)
            await pending.initialize()
            selected = await pending.dispatch(original)
            assert selected.state == "pending" and selected.reason == "source_stop_required"
        return await create(store, key, value, **kwargs)

    with TestClient(create_host(SimpleNamespace(identity=current_id), native_runtime=current)) as client:
        with monkeypatch.context() as competing:
            competing.setattr(FoundryStateStore, "create_item", select_original_first)
            losing = invoke(client, current_id, {
                "operation": "session_follow", "request_id": "earlier-current-follow",
                "expected_session_id": IDENTITY.session_id, "expected_etag": source["etag"], "stage": "prepare",
            })
        assert losing.status_code in {409, 503} and raced
        before = invoke(client, current_id, NativeSessionRead()).json()
        assert before["environment"]["request_id"] == original.request_id
        assert before["following"]["request_id"] == "earlier-current-follow"
        command = {
            "operation": "session_retarget", "request_id": "new-retarget-with-conflicting-destination",
            "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"],
            "expected_agent_version": current_id.agent_version,
            "pending_request_id": original.request_id, "pending_session_id": pending_id.session_id,
        }
        rejected = invoke(client, current_id, command)
        assert rejected.status_code == 409, rejected.text
        assert invoke(client, current_id, NativeSessionRead()).json() == before
        repeated = invoke(client, current_id, command)
        assert repeated.status_code == 409
        assert invoke(client, current_id, NativeSessionRead()).json() == before
        ready = client.portal.call(pending.dispatch, original.model_copy(update={"stage": "commit"}))
        assert ready.state == "ready" and ready.hosted_session_id == pending_id.session_id
        assert not current.executor.calls and not pending.executor.calls


def test_delayed_following_cannot_poison_a_retarget_after_its_last_read(local_env, monkeypatch):
    from shared_context import native_environment

    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        prepared = prepare(Harness(old, old.executor, client))
        assert prepared.status_code == 200, prepared.text
        source = invoke(client, IDENTITY, NativeSessionRead()).json()
    pending_id = IDENTITY.model_copy(update={"session_id": "offline-pending-session", "agent_version": "2"})
    current_id = IDENTITY.model_copy(update={"session_id": "offline-current-session", "agent_version": "3"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", current_id.session_id)
    pending = NativeTurnRuntime(pending_id, OfflineSdk(), bootstrap_source_inactive=inactive)
    current = NativeTurnRuntime(current_id, OfflineSdk(), bootstrap_source_inactive=inactive)
    create = FoundryStateStore.create_item
    check = native_environment._compatible_retarget_following
    entered, release = asyncio.Event(), asyncio.Event()
    checked = []

    async def delayed_create(store, key, value, **kwargs):
        if key.startswith("following:") and value["view"]["request_id"] == "delayed-follow":
            entered.set()
            await release.wait()
        return await create(store, key, value, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "create_item", delayed_create)
    with TestClient(create_host(SimpleNamespace(identity=current_id), native_runtime=current)) as client:
        delayed = client.portal.start_task_soon(current.dispatch, NativeSessionFollow(
            request_id="delayed-follow", expected_session_id=IDENTITY.session_id,
            expected_etag=source["etag"], stage="prepare"))
        client.portal.call(asyncio.wait_for, entered.wait(), 5)
        try:
            client.portal.call(pending.initialize)
            original = client.portal.call(pending.dispatch, NativeSessionFollow(
                request_id="original-pending", expected_session_id=IDENTITY.session_id,
                expected_etag=source["etag"], stage="prepare"))
            assert original.reason == "source_stop_required"
            before = invoke(client, current_id, NativeSessionRead()).json()

            async def publish_loser_after_read(runtime, target):
                result = await check(runtime, target)
                if not checked:
                    assert result is None
                    checked.append(True)
                    release.set()
                    with pytest.raises(Problem):
                        await asyncio.wrap_future(delayed)
                return result

            monkeypatch.setattr(native_environment, "_compatible_retarget_following", publish_loser_after_read)
            command = {
                "operation": "session_retarget", "request_id": "retarget-wins-the-pointer",
                "expected_session_id": IDENTITY.session_id, "expected_etag": before["etag"],
                "expected_agent_version": current_id.agent_version,
                "pending_request_id": original.request_id, "pending_session_id": pending_id.session_id,
            }
            selected = invoke(client, current_id, command)
            assert selected.status_code == 200 and selected.json()["reason"] == "source_stop_required", selected.text
            state = invoke(client, current_id, NativeSessionRead()).json()
            assert state["following"]["request_id"] == command["request_id"]
            assert invoke(client, current_id, command).json() == selected.json()
            ready = invoke(client, current_id, {
                "operation": "session_follow", "request_id": command["request_id"],
                "expected_session_id": IDENTITY.session_id, "expected_etag": state["etag"], "stage": "commit",
            })
            assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
            retained, _ = client.portal.call(native_environment._read, current)
            assert retained.view.request_id == "delayed-follow"
            assert invoke(client, current_id, submit("after-delayed-follow")).status_code == 202
            result = Harness(current, current.executor, client).wait("after-delayed-follow", "completed")
            assert result.hosted_session_id == current_id.session_id and len(current.executor.calls) == 1
        finally:
            client.portal.call(release.set)
            try:
                delayed.result(timeout=5)
            except Problem:
                pass


def test_unsupported_source_is_not_marked_pending_before_its_accepted_work_drains(local_env):
    from shared_context.native_turns import TURNS_STORE

    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, old.executor, client)
        prepared = prepare(h)
        assert prepared.status_code == 200, prepared.text
        accepted = accept_without_chain(h).value.receipt

        async def legacy_binding():
            async with FoundryStateStore(TURNS_STORE) as store:
                item = await store.get_item("agent")
                value = dict(item.value)
                value.pop("following_supported")
                await store.set_item("agent", value, if_match=item.etag)

        client.portal.call(legacy_binding)
        before = invoke(client, IDENTITY, NativeSessionRead()).json()
        destination = NativeTurnRuntime(
            IDENTITY.model_copy(update={"session_id": "offline-next-session", "agent_version": "2"}), OfflineSdk())
        pending = client.portal.call(destination.dispatch, NativeSessionFollow(
            request_id="follow-legacy-source", expected_session_id=IDENTITY.session_id,
            expected_etag=before["etag"], stage="prepare"))
        assert pending.state == "pending" and pending.reason == "accepted_or_started"
        after = invoke(client, IDENTITY, NativeSessionRead()).json()
        assert "environment" not in after and after["hosted_session_id"] == before["hosted_session_id"]
        assert invoke(client, IDENTITY, NativeRead(request_id=accepted.request_id)).json() == accepted.model_dump(mode="json")
        assert not old.executor.calls and not destination.executor.calls


def test_retarget_reads_all_pages_before_switching_the_original_operation(pending_retarget):
    from shared_context.native_turns import TURNS_STORE

    case = pending_retarget

    async def retained_metadata():
        async with FoundryStateStore(TURNS_STORE) as store:
            for index in range(20):
                await store.create_item(f"retained:{index}", {"value": "completed metadata"})

    case.client.portal.call(retained_metadata)
    first = invoke(case.client, case.identity, case.command)
    assert first.status_code == 200 and first.json()["reason"] == "source_stop_required"
    assert invoke(case.client, case.identity, NativeSessionRead()).json()["environment"] == first.json()
    second = invoke(case.client, case.identity, case.command)
    assert second.status_code == 200 and second.json() == first.json()
    assert case.client.portal.call(case.pending.session_state).following.model_dump(mode="json") == case.old_view
    assert not case.current.executor.calls
