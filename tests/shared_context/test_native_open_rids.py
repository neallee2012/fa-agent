"""ADR-0009 §5 open-RID set: a version follow reads the open receipts, not every receipt ever written (audit 8).

One shared ``fa/v1/turns`` store; three Hosted sessions of the same Agent (versions 1, 2, 3) follow one another through
the real invocation surface. Version 1 plays an image before the set: its store has history and no set, so the first
follow scans every receipt once (the backfill) and marks the set complete for version 2; version 2's follow then reads
only what the set lists.
"""

import asyncio
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context import native_left_receipts, native_open_rids
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSessionRead, NativeSubmit
from shared_context.errors import Problem
from shared_context.native_turns import TURNS_STORE, NativeTurnRuntime, _changed, _decode, _json, _Rid
from test_native_environment import invoke
from test_native_session_bootstrap import prepare
from test_native_turn_wire import submit
from test_native_turns import IDENTITY, Harness, OfflineSdk, local_env  # noqa: F401 - fixture

HISTORY = 40
V2 = IDENTITY.model_copy(update={"session_id": "offline-v2-session", "agent_version": "2"})
V3 = IDENTITY.model_copy(update={"session_id": "offline-v3-session", "agent_version": "3"})
GHOST = "offline-retired-session"


class Quiet:
    async def quiet(self, sid, *, task_id=None, include_responses=False):
        return True

    async def owned_input(self, *, task_id=None):
        return None


async def _inactive(sid):
    return True


def _runtime(identity, monkeypatch, sdk=None):
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    return NativeTurnRuntime(identity, sdk or OfflineSdk(), bootstrap_source_inactive=_inactive, task_observer=Quiet())


def _clone(sample, rid, **receipt):
    submitted = NativeSubmit(command=sample.receipt.command.model_copy(update={"request_id": rid}),
                             capture=sample.receipt.capture)
    return _changed(sample, receipt=_changed(sample.receipt, request_id=rid, command=submitted.command,
                                             submit_digest=submitted.sha256, **receipt), **(
        {"run_token": None} if receipt.get("status") == "accepted" else {}))


@pytest.fixture
def reads(monkeypatch):
    """Every ``rid:<RID>`` read, in order."""
    seen, original = [], FoundryStateStore.get_item

    async def counted(store, key, **kwargs):
        if store._name == TURNS_STORE and key.startswith("rid:"):
            seen.append(key[4:])
        return await original(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "get_item", counted)
    return seen


@pytest.fixture
def history(local_env, monkeypatch):  # noqa: F811
    """Version 1 with one real turn and ``HISTORY`` more finished receipts, plus one receipt a retired SID left
    accepted; no open-RID set (an image before it)."""
    sdk = OfflineSdk()
    old = _runtime(IDENTITY, monkeypatch, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old)) as client:
        h = Harness(old, sdk, client)
        prepare(h)
        assert h.invoke(submit("seed-turn")).status_code == 202
        h.wait("seed-turn", "completed")
        sample = h.call(old._rid, "seed-turn").value

        async def written(operation, *args):
            # The local backend rewrites one file per item; Windows can briefly deny the rename (test-only retry).
            for attempt in range(10):
                try:
                    return await operation(*args)
                except PermissionError:
                    if attempt == 9:
                        raise
                    await asyncio.sleep(0.05)

        async def seed():
            async with FoundryStateStore(TURNS_STORE) as store:
                for index in range(HISTORY):
                    rid = f"history-{index:03d}"
                    await written(store.create_item, "rid:" + rid,
                                  _json(_clone(sample, rid, status="unknown", result=None)))
                ghost = _clone(sample, "ghost-accepted", status="accepted", result=None, hosted_session_id=GHOST)
                await written(store.create_item, "rid:ghost-accepted", _json(ghost))
                await written(store.delete_item, native_open_rids.OPEN_KEY)

        h.call(seed)
        etag = invoke(client, IDENTITY, NativeSessionRead()).json()["etag"]
    return SimpleNamespace(etag=etag)


def _follow(identity, source, etag, monkeypatch, *, request_id, settled=(), runtime=None):
    runtime = runtime or _runtime(identity, monkeypatch)
    command = {"operation": "session_follow", "request_id": request_id, "expected_session_id": source,
               "expected_etag": etag, "stage": "prepare", "idle_only": True,
               **({"settled_session_ids": list(settled)} if settled else {})}
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        prepared = invoke(client, identity, command).json()
        if prepared.get("reason") != "source_stop_required":
            return prepared, None, None
        marked = client.portal.call(native_open_rids.read, runtime)
        ready = invoke(client, identity, {**command, "stage": "commit"}).json()
        state = invoke(client, identity, NativeSessionRead()).json()
    return ready, state["etag"], marked


def _open_set(identity, monkeypatch):
    runtime = _runtime(identity, monkeypatch)
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        return client.portal.call(native_open_rids.read, runtime)


def test_after_one_backfill_a_follow_reads_only_the_open_receipts(history, reads, monkeypatch):
    ready, etag, before_commit = _follow(V2, IDENTITY.session_id, history.etag, monkeypatch,
                                         request_id="follow-v2", settled=(GHOST,))
    assert ready["state"] == "ready", ready
    # The legacy store has no set: both drains read every receipt (the one-time backfill).
    assert len(reads) >= 2 * (HISTORY + 2)
    # The marker is written only after the commit; before it the set names no session.
    assert before_commit is not None and before_commit.complete_for is None
    marked = _open_set(V2, monkeypatch)
    assert marked.complete_for == V2.session_id
    # The retired SID's unfinished receipt was not waited for, and stays listed so no later follow loses it.
    assert set(marked.rids) == {"ghost-accepted"}

    reads.clear()
    ready, _, _ = _follow(V3, V2.session_id, etag, monkeypatch, request_id="follow-v3", settled=(GHOST,))
    assert ready["state"] == "ready", ready
    # Two drains, each reading the one listed receipt: none of the HISTORY + 1 finished ones.
    assert reads == ["ghost-accepted", "ghost-accepted"]
    assert _open_set(V3, monkeypatch).complete_for == V3.session_id


def test_a_listed_open_receipt_still_stops_the_follow(history, reads, monkeypatch):
    _, etag, _ = _follow(V2, IDENTITY.session_id, history.etag, monkeypatch, request_id="follow-v2",
                         settled=(GHOST,))
    reads.clear()
    # The API no longer lists the retired SID as previous: its receipt is open work again.
    pending, _, _ = _follow(V3, V2.session_id, etag, monkeypatch, request_id="follow-v3")
    assert pending["state"] == "pending" and pending["reason"] == "accepted_or_started", pending
    assert reads == ["ghost-accepted"]


def test_a_marker_for_another_session_scans_every_receipt_again(history, reads, monkeypatch):
    _, etag, _ = _follow(V2, IDENTITY.session_id, history.etag, monkeypatch, request_id="follow-v2",
                         settled=(GHOST,))
    runtime = _runtime(V2, monkeypatch)
    with TestClient(create_host(SimpleNamespace(identity=V2), native_runtime=runtime)) as client:
        # As if another path (an abandon, a rollback) made a session the Agent's without marking the set for it.
        client.portal.call(native_open_rids.unmark, runtime, V2.session_id)
    reads.clear()
    ready, _, _ = _follow(V3, V2.session_id, etag, monkeypatch, request_id="follow-v3", settled=(GHOST,))
    assert ready["state"] == "ready", ready
    assert len(reads) >= 2 * (HISTORY + 2)


def _abandon(identity, source, etag, monkeypatch, *, request_id, runtime=None):
    runtime = runtime or _runtime(identity, monkeypatch)
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        taken = invoke(client, identity, {"operation": "session_abandon", "request_id": request_id,
                                          "expected_session_id": source, "expected_etag": etag,
                                          "source_status": "missing"})
        assert taken.status_code == 200 and taken.json()["state"] == "ready", taken.text
        return runtime, client.portal, invoke(client, identity, NativeSessionRead()).json()["etag"]


def test_an_abandon_back_to_a_marked_session_neither_reads_the_set_nor_trusts_its_old_marker(history, monkeypatch):
    """An abandon commits without a scan, and a SID can get the Agent back by one (a generation-0 SID the binding row
    no longer lists as previous). Its marker from the earlier hold must not hide what another image admitted in
    between, and the abandon itself never reads or writes the set, so a store fault there can not fail it."""
    _, etag, _ = _follow(V2, IDENTITY.session_id, history.etag, monkeypatch, request_id="follow-v2",
                         settled=(GHOST,))
    assert _open_set(V2, monkeypatch).complete_since == "follow-v2"
    # Adoption (M9) is not under test: keep the history's never-started receipt where it is.
    monkeypatch.setattr(native_left_receipts, "adopt_left", lambda runtime: asyncio.sleep(0))
    _, portal, etag = _abandon(IDENTITY, V2.session_id, etag, monkeypatch, request_id="abandon-to-v1")

    async def admitted_without_the_set():
        # What an image before the set leaves while it holds the Agent: an open receipt the set does not list.
        async with FoundryStateStore(TURNS_STORE) as store:
            sample = _decode((await store.get_item("rid:history-000")).value, _Rid)
            open_one = _clone(sample, "v1-open", status="started", result=None, hosted_session_id=IDENTITY.session_id)
            await store.create_item("rid:v1-open", _json(open_one))

    asyncio.run(admitted_without_the_set())
    get_item = FoundryStateStore.get_item

    async def faulty(store, key, **kwargs):
        if store._name == TURNS_STORE and key == native_open_rids.OPEN_KEY:
            raise OSError("OFFLINE open-RID set unreadable")
        return await get_item(store, key, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(FoundryStateStore, "get_item", faulty)
        _, _, etag = _abandon(V2, IDENTITY.session_id, etag, monkeypatch, request_id="abandon-to-v2")
    # The marker still names V2, but from the follow before; V3's follow scans every receipt and waits for V1's turn.
    marked = _open_set(V2, monkeypatch)
    assert (marked.complete_for, marked.complete_since) == (V2.session_id, "follow-v2")
    pending, _, _ = _follow(V3, V2.session_id, etag, monkeypatch, request_id="follow-v3", settled=(GHOST,))
    assert pending["state"] == "pending" and pending["reason"] == "accepted_or_started", pending
    assert _open_set(V3, monkeypatch).complete_for is None


def test_a_turn_enters_the_set_when_admitted_and_leaves_it_when_terminal(history, monkeypatch):
    _, etag, _ = _follow(V2, IDENTITY.session_id, history.etag, monkeypatch, request_id="follow-v2",
                         settled=(GHOST,))
    sdk = OfflineSdk()
    sdk.block = True
    runtime = _runtime(V2, monkeypatch, sdk)
    with TestClient(create_host(SimpleNamespace(identity=V2), native_runtime=runtime)) as client:
        h = Harness(runtime, sdk, client)
        assert invoke(client, V2, submit("v2-turn")).status_code == 202
        h.wait("v2-turn", "started")
        assert "v2-turn" in client.portal.call(native_open_rids.read, runtime).rids
        sdk.block = False
        h.wait("v2-turn", "completed")
        client.portal.call(runtime.drain_open_removals)
        assert set(client.portal.call(native_open_rids.read, runtime).rids) == {"ghost-accepted"}


def test_the_removal_runs_beside_the_chain_and_shutdown_waits_for_it(history, monkeypatch):
    """A terminal write does not hold the chain's next turn for the removal's round trips; the VM shutdown waits for
    the removals still in flight (bounded)."""
    _, etag, _ = _follow(V2, IDENTITY.session_id, history.etag, monkeypatch, request_id="follow-v2",
                         settled=(GHOST,))
    sdk = OfflineSdk()
    sdk.block = True
    runtime = _runtime(V2, monkeypatch, sdk)
    remove, release, held = native_open_rids.remove, asyncio.Event(), []

    async def slow(runtime_, rid):
        held.append(rid)
        await release.wait()
        await remove(runtime_, rid)

    with monkeypatch.context() as patch:
        patch.setattr(native_open_rids, "remove", slow)
        with TestClient(create_host(SimpleNamespace(identity=V2), native_runtime=runtime)) as client:
            h = Harness(runtime, sdk, client)
            assert invoke(client, V2, submit("v2-first")).status_code == 202
            h.wait("v2-first", "started")
            assert invoke(client, V2, submit("v2-second")).status_code == 202
            sdk.block = False
            # Both turns finish while the first removal is still held: the chain went on without it.
            h.wait("v2-first", "completed")
            h.wait("v2-second", "completed")
            assert "v2-first" in held
            assert {"v2-first", "v2-second"} <= set(client.portal.call(native_open_rids.read, runtime).rids)
            client.portal.call(release.set)
        # Leaving the host ran its shutdown, which waited for both removals.
        assert not runtime._open_removals
    assert set(_open_set(V2, monkeypatch).rids) == {"ghost-accepted"}


def test_shutdown_drains_a_removal_a_turn_starts_while_the_executor_closes(local_env, monkeypatch):  # noqa: F811
    """A turn the executor stops at shutdown ends after the first drain: its removal is drained too (bounded)."""
    sdk = OfflineSdk()
    runtime = _runtime(IDENTITY, monkeypatch, sdk)
    remove, removed = native_open_rids.remove, []

    async def slow(runtime_, rid):
        await asyncio.sleep(0.3)
        await remove(runtime_, rid)
        removed.append(rid)

    async def closing():
        # Stands in for a turn the executor stopped: its terminal write starts the removal.
        runtime._remove_open_later("stopped-turn")

    sdk.aclose = closing
    with monkeypatch.context() as patch:
        patch.setattr(native_open_rids, "remove", slow)
        with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
            prepare(Harness(runtime, sdk, client))
            client.portal.call(native_open_rids.add, runtime, ("stopped-turn",))
        assert removed == ["stopped-turn"] and not runtime._open_removals
    assert _open_set(IDENTITY, monkeypatch).rids == ()


def test_a_crash_between_the_terminal_write_and_the_removal_never_hides_or_blocks(history, reads, monkeypatch):
    _, etag, _ = _follow(V2, IDENTITY.session_id, history.etag, monkeypatch, request_id="follow-v2",
                         settled=(GHOST,))
    sdk = OfflineSdk()
    runtime = _runtime(V2, monkeypatch, sdk)

    async def crashed(runtime, rid):
        return None

    with monkeypatch.context() as patch:
        patch.setattr(native_open_rids, "remove", crashed)
        with TestClient(create_host(SimpleNamespace(identity=V2), native_runtime=runtime)) as client:
            h = Harness(runtime, sdk, client)
            accepted = invoke(client, V2, submit("v2-turn"))
            assert accepted.status_code == 202, accepted.text
            h.wait("v2-turn", "completed")
            assert "v2-turn" in client.portal.call(native_open_rids.read, runtime).rids
            etag = invoke(client, V2, NativeSessionRead()).json()["etag"]
    reads.clear()
    ready, _, _ = _follow(V3, V2.session_id, etag, monkeypatch, request_id="follow-v3", settled=(GHOST,))
    assert ready["state"] == "ready", ready
    # The stale entry is read, found terminal and dropped; nothing else is read.
    assert sorted(set(reads)) == ["ghost-accepted", "v2-turn"] and len(reads) <= 4
    assert set(_open_set(V3, monkeypatch).rids) == {"ghost-accepted"}


def test_a_failed_add_keeps_the_admission_open_until_the_same_rid_records_it(history, monkeypatch):
    """Both adds fail (beside the create and before the clear): 503 and nothing runs; ``admitting`` stays, so a follow
    (``idle_only``, which never settles it) waits instead of missing the receipt. The client's same-RID resend records
    it, clears ``admitting`` and runs the turn once."""
    _, etag, _ = _follow(V2, IDENTITY.session_id, history.etag, monkeypatch, request_id="follow-v2",
                         settled=(GHOST,))
    sdk = OfflineSdk()
    runtime = _runtime(V2, monkeypatch, sdk)
    add = native_open_rids.add

    async def failing(*args, **kwargs):
        raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE open-RID write failed")

    with TestClient(create_host(SimpleNamespace(identity=V2), native_runtime=runtime)) as client:
        with monkeypatch.context() as patch:
            patch.setattr(native_open_rids, "add", failing)
            refused = invoke(client, V2, submit("v2-turn"))
            assert refused.status_code == 503 and refused.json()["error"]["message"] == "OFFLINE open-RID write failed"
        assert sdk.calls == []
        state = invoke(client, V2, NativeSessionRead()).json()
        assert state.get("admission_request_id") == "v2-turn"
        assert "v2-turn" not in client.portal.call(native_open_rids.read, runtime).rids
        etag = state["etag"]
    # The receipt exists but is not listed: the follow waits on the admission instead of missing it.
    pending, _, _ = _follow(V3, V2.session_id, etag, monkeypatch, request_id="follow-v3", settled=(GHOST,))
    assert pending["reason"] == "admission_pending", pending
    assert native_open_rids.add is add
    # The same RID again: settling records it first, then the turn runs and leaves the set; the follow goes on.
    sdk = OfflineSdk()
    runtime = _runtime(V2, monkeypatch, sdk)
    with TestClient(create_host(SimpleNamespace(identity=V2), native_runtime=runtime)) as client:
        h = Harness(runtime, sdk, client)
        assert invoke(client, V2, submit("v2-turn")).status_code == 202
        h.wait("v2-turn", "completed")
        assert [call[0] for call in sdk.calls] == ["v2-turn"]
        client.portal.call(runtime.drain_open_removals)
        assert invoke(client, V2, NativeSessionRead()).json().get("admission_request_id") is None
        assert set(client.portal.call(native_open_rids.read, runtime).rids) == {"ghost-accepted"}
    ready, _, _ = _follow(V3, V2.session_id, etag, monkeypatch, request_id="follow-v3", settled=(GHOST,))
    assert ready["state"] == "ready", ready


def test_a_send_reads_the_set_with_its_first_reads_and_adds_with_one_cas(local_env, monkeypatch):  # noqa: F811
    """No extra send-path latency (Neal): the set is read together with the admission's other first reads, before the
    admission mark, so adding the RID beside the receipt create is one CAS and no read waits behind the mark."""
    sdk = OfflineSdk()
    runtime = _runtime(IDENTITY, monkeypatch, sdk)
    calls = []
    for name in ("get_item", "create_item", "set_item"):
        def recorded(store, key, *args, _name=name, _original=getattr(FoundryStateStore, name), **kwargs):
            if store._name == TURNS_STORE:
                calls.append((_name, key))
            return _original(store, key, *args, **kwargs)

        monkeypatch.setattr(FoundryStateStore, name, recorded)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, sdk, client)
        prepare(h)
        assert h.invoke(submit("first-turn")).status_code == 202
        h.wait("first-turn", "completed")
        client.portal.call(runtime.drain_open_removals)
        calls.clear()
        assert h.invoke(submit("second-turn")).status_code == 202
        # Only the admission is measured (the 202 follows the settle); how the turn ends does not matter here.
        h.wait("second-turn", "completed", "unknown")
    mark = calls.index(("set_item", "agent"))
    clear = calls.index(("set_item", "agent"), mark + 1)
    assert calls.index(("create_item", "rid:second-turn")) in range(mark, clear)
    admission = [(index, op) for index, (op, key) in enumerate(calls[:clear]) if key == native_open_rids.OPEN_KEY]
    # One read, issued with the first reads (before the mark); one write, the CAS against that read.
    assert [op for _, op in admission] == ["get_item", "set_item"], admission
    assert admission[0][0] < mark < admission[1][0]


def test_an_unconfirmed_set_read_is_retried_like_an_unconfirmed_write(local_env, monkeypatch):  # noqa: F811
    """A lost read (first or after a lost write) is read again within the bounded attempts, not the end of the update;
    when every read is lost the update ends as the 503 the best-effort callers already absorb."""
    sdk = OfflineSdk()
    runtime = _runtime(IDENTITY, monkeypatch, sdk)
    lost = {"get_item": 0, "set_item": 0}
    for name in lost:
        def flaky(store, key, *args, _name=name, _original=getattr(FoundryStateStore, name), **kwargs):
            if key == native_open_rids.OPEN_KEY and lost[_name]:
                lost[_name] -= 1
                raise ServiceResponseError(f"OFFLINE {_name} lost")
            return _original(store, key, *args, **kwargs)

        monkeypatch.setattr(FoundryStateStore, name, flaky)

    async def scenario():
        await native_open_rids.add(runtime, ("first",))
        # Read lost, then read and write lost, then read and write.
        lost.update(get_item=1, set_item=1)
        await native_open_rids.add(runtime, ("second",))
        assert (await native_open_rids.read(runtime)).rids == ("first", "second")
        lost["get_item"] = 100
        with pytest.raises(Problem) as refused:
            await native_open_rids.add(runtime, ("third",))
        assert (refused.value.status, refused.value.body["error"]["code"]) == (503, "NATIVE_STATE_STORE_UNKNOWN")
        await native_open_rids.remove(runtime, "first")
        await native_open_rids.mark_complete(runtime, "offline-sid", "offline-follow")
        lost["get_item"] = 0
        assert (await native_open_rids.read(runtime)).rids == ("first", "second")

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        prepare(Harness(runtime, sdk, client))
        client.portal.call(scenario)
