"""Explicit bootstrap before native work; original stores and task IDs stay in place."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSessionPrepare, NativeSessionRead
from shared_context.errors import Problem
from shared_context.native_sdk import NativeBootstrapSource
from shared_context.native_turns import NativeTurnRuntime, TURNS_STORE, EFFECTS_STORE
from test_native_turns import IDENTITY, OfflineSdk, Harness, harness, local_env, native_retry_policy, accept_without_chain
from test_native_turn_wire import submit


def prepare(h, rid="original-prepare"):
    state = h.call(h.runtime.session_state)
    return h.invoke(NativeSessionPrepare(request_id=rid, expected_session_id=state.hosted_session_id,
                                        expected_etag=state.etag))


def test_first_prepare_is_cas_only_and_same_agent_replays_original(harness):
    first = prepare(harness)
    assert first.status_code == 200
    value = first.json()
    assert value["hosted_session_id"] == IDENTITY.session_id
    assert value["previous_hosted_session_id"] is None and not value["model_started"]
    snapshot = harness.call(harness.runtime.session_state)
    assert prepare(harness, rid="another-prepare-request").json() == value
    assert harness.call(harness.runtime.session_state) == snapshot
    assert not harness.sdk.calls
    assert harness.invoke(submit()).status_code == 202
    harness.wait("rid-1", "completed")
    assert prepare(harness).json() == value
    assert len(harness.sdk.calls) == 1


def test_bootstrap_never_changes_an_agent_with_accepted_work(harness):
    accepted = accept_without_chain(harness)
    before = accepted.value.receipt
    reply = prepare(harness)
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "NATIVE_BOOTSTRAP_HAS_WORK"
    assert harness.call(harness.runtime.read, "rid-1") == before
    assert not harness.sdk.calls


def test_refused_profile_does_not_claim_a_native_admission(harness):
    def refused(_):
        raise Problem(422, "NATIVE_PROFILE_UNSUPPORTED", "unsupported before admission")
    harness.runtime.preflight = refused
    response = harness.invoke(submit())
    assert response.status_code == 422
    assert prepare(harness).status_code == 200
    assert not harness.sdk.calls


def test_original_unknown_material_is_not_deleted_or_reinterpreted(harness):
    original = {"retained_unknown": "not a new instruction"}
    async def insert():
        async with harness.runtime._store(TURNS_STORE) as store:
            await store.create_item("rid:original-unknown", original)
    harness.call(insert)
    assert prepare(harness).json()["error"]["code"] == "NATIVE_BOOTSTRAP_HAS_WORK"
    async def read():
        async with harness.runtime._store(TURNS_STORE) as store:
            return (await store.get_item("rid:original-unknown")).value
    assert harness.call(read) == original


def test_empty_native_bootstrap_does_not_probe_or_stop_its_active_legacy_source(local_env, monkeypatch):
    old_sdk = OfflineSdk()
    old = NativeTurnRuntime(IDENTITY, old_sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as host:
        original = host.portal.call(old.session_state)
    new_identity = IDENTITY.model_copy(update={"session_id": "OFFLINE-api-created-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", new_identity.session_id)
    checks = []
    async def verified(sid):
        checks.append(sid)
        return False
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(new_identity, sdk, bootstrap_source_inactive=verified)
    with TestClient(create_host(SimpleNamespace(identity=new_identity), native_runtime=runtime)) as host:
        h = Harness(runtime, sdk, host)
        assert host.portal.call(runtime.session_state) == original
        prepared = prepare(h)
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["previous_hosted_session_id"] == IDENTITY.session_id
        assert prepared.json()["hosted_session_id"] == new_identity.session_id
        assert checks == []
        # An old object's cached binding is not a grant after the new CAS.
        with pytest.raises(Problem) as stale:
            host.portal.call(old.submit, submit("old-must-not-start"))
        assert stale.value.body["error"]["code"] == "NATIVE_SESSION_CHANGED"
        assert not old_sdk.calls
        with pytest.raises(Problem) as missing:
            host.portal.call(runtime.read, "old-must-not-start")
        assert missing.value.body["error"]["code"] == "NATIVE_RID_MISSING"
        assert h.invoke(submit("new-api-owned-turn")).status_code == 202
        h.wait("new-api-owned-turn", "completed")
        assert len(sdk.calls) == 1


@pytest.mark.parametrize("retained", ["preparation", "admission", "rid", "effect"])
def test_active_legacy_bootstrap_correction_cannot_replace_any_native_work(local_env, monkeypatch, retained):
    old = NativeTurnRuntime(IDENTITY, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as host:
        h = Harness(old, old.executor, host)
        if retained == "preparation":
            assert prepare(h).status_code == 200
        elif retained == "admission":
            h.call(lambda: old._require_original_session(admitting=True))
        else:
            async def save():
                async with old._store(TURNS_STORE if retained == "rid" else EFFECTS_STORE) as store:
                    await store.create_item("rid:original" if retained == "rid" else "original-effect",
                                            {"original": "retained"})
            h.call(save)
        original = h.call(old.session_state)
    identity = IDENTITY.model_copy(update={"session_id": "OFFLINE-api-created-session", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    current = NativeTurnRuntime(identity, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=current)) as host:
        response = prepare(Harness(current, current.executor, host))
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == (
            "NATIVE_SESSION_CHANGED" if retained == "preparation" else "NATIVE_BOOTSTRAP_HAS_WORK")
        assert host.portal.call(current.session_state) == original
        assert not old.executor.calls and not current.executor.calls


def test_first_admission_intent_and_prepare_compete_on_same_agent_etag(harness):
    state = harness.call(harness.runtime.session_state)
    harness.call(lambda: harness.runtime._require_original_session(admitting=True))
    response = harness.invoke(NativeSessionPrepare(request_id="stale-prepare",
        expected_session_id=state.hosted_session_id, expected_etag=state.etag))
    assert response.status_code == 409 and response.json()["error"]["code"] == "NATIVE_BOOTSTRAP_CONFLICT"
    assert harness.call(harness.runtime.session_state).preparation is None and not harness.sdk.calls


def test_preparation_lost_ack_reconciles_only_original_agent_item(harness, monkeypatch):
    original = FoundryStateStore.set_item
    lost = False
    async def uncertain(store, key, value, **kwargs):
        nonlocal lost
        result = await original(store, key, value, **kwargs)
        if key == "agent" and value.get("preparation") and not lost:
            lost = True
            raise ServiceResponseError("synthetic lost acknowledgement")
        return result
    monkeypatch.setattr(FoundryStateStore, "set_item", uncertain)
    saved = prepare(harness)
    assert saved.status_code == 200 and lost
    current = harness.call(harness.runtime.session_state)
    assert prepare(harness).json() == saved.json()
    assert harness.call(harness.runtime.session_state) == current and not harness.sdk.calls


def test_admission_between_empty_read_and_prepare_cas_cannot_be_rebound(harness, monkeypatch):
    original = FoundryStateStore.set_item
    raced = False
    async def race(store, key, value, **kwargs):
        nonlocal raced
        if key == "agent" and value.get("preparation") and not raced:
            raced = True
            await harness.runtime._require_original_session(admitting=True)
        return await original(store, key, value, **kwargs)
    monkeypatch.setattr(FoundryStateStore, "set_item", race)
    response = prepare(harness)
    assert response.status_code == 409 and response.json()["error"]["code"] == "NATIVE_BOOTSTRAP_CONFLICT"
    assert raced and harness.call(harness.runtime.session_state).preparation is None
    assert not harness.sdk.calls


@pytest.mark.parametrize("state,inactive", [("active", False), ("idle", True), ("creating", False), ("expired", True)])
def test_previous_session_status_is_a_real_read_not_a_body_flag(state, inactive):
    from azure.ai.projects.operations._operations import build_agents_get_session_request

    endpoint = "https://synthetic.example/api/projects/project"
    expected = httpx.URL(endpoint + build_agents_get_session_request("agent", "original").url)
    calls = []
    async def token():
        return "synthetic-token"
    def transport(request):
        calls.append(request)
        assert request.method == "GET" and request.url == expected
        return httpx.Response(200, json={"agent_session_id": "original", "status": state})
    reader = NativeBootstrapSource(endpoint, "agent", token,
                                    transport=httpx.MockTransport(transport))
    assert asyncio.run(reader("original")) is inactive
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 503])
def test_missing_or_refused_old_session_is_not_assumed_inactive(status):
    calls = []
    async def token():
        return "synthetic-token"
    def transport(request):
        calls.append(request)
        return httpx.Response(status)
    reader = NativeBootstrapSource("https://synthetic.example/api/projects/project", "agent", token,
                                    transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as refusal:
        asyncio.run(reader("original"))
    assert refusal.value.body["error"]["code"] == "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE"
    assert len(calls) == 1
