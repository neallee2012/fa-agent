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
from shared_context.native_turns import NativeTurnRuntime, TURNS_STORE, EFFECTS_STORE, _changed, _json
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
        # Another VM: the Agent record is the same, only the VM's own version differs (ADR-0009 §1).
        assert host.portal.call(runtime.session_state).model_copy(update={"actual_version": IDENTITY.agent_version}) == original
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
        # Another VM: the Agent record is unchanged; only the VM's own version differs (ADR-0009 §1).
        state = host.portal.call(current.session_state)
        assert state.actual_version == "2"
        assert state.model_copy(update={"actual_version": IDENTITY.agent_version}) == original
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


def admission(rid, digest):
    """The two fields the admission mark reads from a submitted request."""
    return SimpleNamespace(command=SimpleNamespace(request_id=rid), sha256=digest)


def refusal(problem):
    return problem.value.status, problem.value.body["error"]["code"], problem.value.body["error"]["message"]


def test_the_original_session_is_unavailable_before_the_state_stores_open(local_env):
    with pytest.raises(Problem) as unavailable:
        asyncio.run(NativeTurnRuntime(IDENTITY, OfflineSdk())._require_original_session(admitting=True))
    assert refusal(unavailable) == (503, "NATIVE_EXECUTION_UNAVAILABLE", "native State stores have not initialized")


def test_a_held_admission_refuses_other_material_and_other_rids_without_writing(harness):
    """The record retains one admission intent: the same RID with another digest is 409, another RID waits (503)."""
    runtime = harness.runtime
    held = admission("held-rid", "a" * 64)
    harness.call(lambda: runtime._original_session(admitting=True, request=held))
    before = harness.call(runtime._agent_record)
    assert (before[0].admitting.request_id, before[0].admitting.submit_digest) == ("held-rid", "a" * 64)
    with pytest.raises(Problem) as conflict:
        harness.call(lambda: runtime._original_session(admitting=True, request=admission("held-rid", "b" * 64)))
    assert refusal(conflict) == (409, "NATIVE_RID_CONFLICT", "the original admission retains different submitted material")
    with pytest.raises(Problem) as pending:
        harness.call(lambda: runtime._original_session(admitting=True, request=admission("other-rid", "b" * 64)))
    assert refusal(pending) == (503, "NATIVE_ADMISSION_PENDING", "the original admission must be reconciled first")
    # The retained intent itself is admitted again as it is: no second mark.
    assert harness.call(lambda: runtime._original_session(admitting=True, request=held)) == before
    assert harness.call(runtime._agent_record) == before and not harness.sdk.calls


@pytest.mark.parametrize("failure,known", [
    ("lost", False), ("lost", True), ("precondition", False), ("applied", False), ("vanished", False),
], ids=["unapplied-uncertain", "unapplied-uncertain-known", "unapplied-conflict", "applied-uncertain",
        "unapplied-uncertain-vanished"])
def test_an_uncertain_admission_mark_is_read_back_before_it_is_trusted(harness, monkeypatch, failure, known):
    """A mark write with no answer is read back: our own mark is kept with the ETag read; anything else is 503 unless
    a conflict on a ``known`` record (the reread path); a plain transport loss never takes that path."""
    from azure.ai.agentserver.core.storage import FoundryStoragePreconditionError

    runtime, original, failed = harness.runtime, FoundryStateStore.set_item, []
    before = harness.call(runtime._agent_record)

    async def uncertain(store, key, value, **kwargs):
        if key != "agent" or failed:
            return await original(store, key, value, **kwargs)
        failed.append(key)
        if failure == "applied":
            await original(store, key, value, **kwargs)
        if failure == "precondition":
            raise FoundryStoragePreconditionError("OFFLINE refused mark", status_code=412)
        raise ServiceResponseError("synthetic lost acknowledgement")

    monkeypatch.setattr(FoundryStateStore, "set_item", uncertain)
    if failure == "vanished":  # the read-back finds no record at all
        read, gone = FoundryStateStore.get_item, []

        async def vanished(store, key, *args, **kwargs):
            if key == "agent" and failed and not gone:
                gone.append(key)
                return None
            return await read(store, key, *args, **kwargs)
        monkeypatch.setattr(FoundryStateStore, "get_item", vanished)
    mark = lambda: runtime._original_session(admitting=True, known=before if known else None)
    if failure == "applied":
        current, etag = harness.call(mark)
        assert current.admission_started and (current, etag) == harness.call(runtime._agent_record) and etag
        assert failed and runtime._agent_session == current
        return
    with pytest.raises(Problem) as unknown:
        harness.call(mark)
    assert refusal(unknown) == (503, "NATIVE_BOOTSTRAP_UNKNOWN", "original admission binding is unconfirmed")
    assert unknown.value.__suppress_context__ and failed == ["agent"]
    assert harness.call(runtime._agent_record) == before and not before[0].admission_started


def change_agent(runtime, record, etag):
    """Write another field of the Agent record behind ``record`` (as an environment write would)."""
    changed = _changed(record, following_supported=not record.following_supported)

    async def write():
        async with runtime._store(TURNS_STORE) as store:
            await store.set_item("agent", _json(changed), if_match=etag)
    return changed, write


def test_a_known_record_is_used_as_is_unless_the_mark_needs_the_etag_it_lacks(harness):
    """``known`` saves the read for a check that writes nothing; a mark without its ETag reads the record again, so a
    change written since is kept, never overwritten by an unconditional write."""
    runtime = harness.runtime
    stale, etag = harness.call(runtime._agent_record)
    changed, write = change_agent(runtime, stale, etag)
    harness.call(write)
    assert harness.call(lambda: runtime._original_session(known=(stale, None))) == (stale, None)
    marked = harness.call(lambda: runtime._original_session(admitting=True, known=(stale, None)))
    assert marked[0] == _changed(changed, admission_started=True) and marked == harness.call(runtime._agent_record)


def test_a_known_record_changed_before_the_mark_is_read_again_and_marked(harness, caplog):
    """The mark's CAS on a stale ``known`` ETag conflicts: the record is read again and marked from that read."""
    import logging

    runtime = harness.runtime
    stale = harness.call(runtime._agent_record)
    changed, write = change_agent(runtime, *stale)
    harness.call(write)
    caplog.set_level(logging.INFO, logger="shared_context.native_turns")
    marked = harness.call(lambda: runtime._original_session(admitting=True, known=stale))
    assert marked[0] == _changed(changed, admission_started=True) and marked == harness.call(runtime._agent_record)
    assert caplog.text.count("FA_NATIVE_ADMISSION_MARK_REREAD") == 1


@pytest.mark.parametrize("cause", ["recovery", "environment"])
def test_new_work_waits_while_the_agent_follows_but_a_receipt_goes_on(harness, monkeypatch, cause):
    """Both refusals are 503 for new work only: a receipt's own work goes on; a retained admission intent goes on
    through an environment change but not past an unconfirmed owned task."""
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView
    from shared_context.native_turns import _Admission

    runtime = harness.runtime
    record, etag = harness.call(runtime._agent_record)
    held = admission("held-rid", "a" * 64)
    record = record.model_copy(update={"admitting": _Admission(request_id="held-rid", submit_digest="a" * 64)})
    if cause == "recovery":
        monkeypatch.setattr(runtime, "_recovery_error", Problem(503, "OFFLINE_RECOVERY", "unconfirmed"))
        message = "the original owned task is not yet confirmed"
    else:
        record = record.model_copy(update={"environment": NativeEnvironmentView(
            agent_id=record.agent_id, request_id="OFFLINE-follow", previous_hosted_session_id=record.hosted_session_id,
            previous_agent_version="1", hosted_session_id="OFFLINE-next-session", hosted_agent_version="2",
            state="pending")})
        message = "this Agent is changing environments; no new work was accepted"
    known = (record, etag)
    with pytest.raises(Problem) as following:
        harness.call(lambda: runtime._original_session(known=known))
    assert refusal(following) == (503, "AGENT_VERSION_FOLLOWING", message)
    own = SimpleNamespace(hosted_session_id=IDENTITY.session_id)
    assert harness.call(lambda: runtime._original_session(receipt=own, known=known)) == known
    if cause == "recovery":
        with pytest.raises(Problem) as retained:
            harness.call(lambda: runtime._original_session(request=held, known=known))
        assert refusal(retained) == (503, "AGENT_VERSION_FOLLOWING", message)
    else:
        assert harness.call(lambda: runtime._original_session(request=held, known=known)) == known
