"""ADR-0009 M1 2d-i ``NativeSessionCut``: the destination session takes the Agent only while the shared store is quiet;
``NativeSessionWithdraw`` puts back what a ``pending`` replaced.

Real Hosted runtimes over one local ``fa/v1/turns`` store; only the SDK is offline. The source is a prepared session
with one finished turn (``source``) or the open-RID history of ``test_native_open_rids`` (``history``); the
destination either runs behind its own host (``_cut``, through the invocation surface with the binding-row locator)
or is called in the source's loop (a probe, as ``test_native_idle_waits`` does), so a test can admit work on the
source in the middle of the cut.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import jsonschema
import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from pydantic import ValidationError
from starlette.testclient import TestClient

from shared_context import native_environment, native_left_receipts
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import (
    NATIVE_LOCATOR_BINDING_ROW, NATIVE_LOCATOR_HEADER, NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER,
    NativeContinuationRead, NativeContinuationSubmit, NativeEnvironmentView, NativeSessionCut, NativeSessionFollow,
    NativeSessionRead, NativeSessionWithdraw, material_digest)
from shared_context.errors import Problem
from shared_context.native_environment import follow_session
from shared_context.native_environment_cut import cut_session, withdraw_session
from shared_context.native_turns import _RECORD_READS, TURNS_STORE, NativeTurnRuntime, _json
from shared_context.native_workspace_files import NativeWorkspaceFiles
from test_native_open_rids import (  # noqa: F401 - fixtures
    GHOST, HISTORY, V2, V3, Quiet, _clone, _follow, _inactive, _open_set, _runtime, history, reads)
from test_native_profile_snapshot import H0_FIXTURE
from test_native_responses import response_submit
from test_native_routine_turns import routine_request
from test_native_session_bootstrap import prepare
from test_native_turn_wire import agent_receive, submit
from test_native_turns import IDENTITY, Harness, OfflineSdk, local_env  # noqa: F401 - fixture

SOURCE = IDENTITY.session_id
H0 = json.loads(H0_FIXTURE.read_text(encoding="utf-8"))


def h0_decodes(model, value):
    """H0 (release 30) reads ``value`` as ``model``: its frozen JSON schema, ``additionalProperties: false`` included."""
    jsonschema.validate(value, {**H0["models"][model]["schema"], "$defs": H0["defs"]})


def following_key(session_id):
    return native_environment._key(None, session_id=session_id)


def items():
    """Every item of ``fa/v1/turns``: (value, ETag) by key."""
    async def read():
        found, after = {}, None
        async with FoundryStateStore(TURNS_STORE) as store:
            while True:
                page = await store.list_keys(limit=100, after=after)
                for key in page.keys:
                    item = await store.get_item(key.key)
                    found[key.key] = (item.value, item.etag)
                if not page.has_more:
                    return found
                after = page.last_id
    return asyncio.run(read())


def cut_lines(caplog):
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith("NATIVE_UPGRADE_CUT")]


def _cut(identity, monkeypatch, *, request_id, source=SOURCE, runtime=None, **fields):
    """The cut through the destination's own host, located by the binding row (a record read would be refused)."""
    runtime = runtime or _runtime(identity, monkeypatch)
    body = {"operation": "session_cut", "request_id": request_id, "expected_session_id": source, **fields}
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        response = client.post("/invocations", json=body, headers={
            NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, NATIVE_LOCATOR_HEADER: NATIVE_LOCATOR_BINDING_ROW,
            "x-agent-session-id": identity.session_id, "x-agent-invocation-id": "offline-cut",
            "x-agent-foundry-call-id": "offline-cut-caller"})
    assert response.status_code == 200, response.text
    return response.json()


def probe(identity=V2, **options):
    return NativeTurnRuntime(identity, OfflineSdk(), task_observer=options.pop("task_observer", Quiet()), **options)


@pytest.fixture
def source(local_env, monkeypatch):  # noqa: F811
    """The Agent's current session: prepared, one finished turn, no open-RID marker (it was never followed to)."""
    sdk = OfflineSdk()
    runtime = _runtime(IDENTITY, monkeypatch, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, sdk, client)
        prepare(h)
        assert h.invoke(submit("base-turn")).status_code == 202
        h.base = h.wait("base-turn", "completed")
        client.portal.call(runtime.drain_open_removals)
        yield h


def test_the_cut_is_not_a_record_read():
    """It runs on a session that is not (yet) the Agent's: ``native_response`` must not refuse it as replaced."""
    assert not {"session_cut", "session_withdraw"} & _RECORD_READS


@pytest.mark.parametrize("status", ["accepted", "started"])
def test_an_open_receipt_makes_the_cut_busy_and_it_writes_nothing(source, status):
    async def seed():
        async with FoundryStateStore(TURNS_STORE) as store:
            await store.create_item("rid:open-one", _json(_clone(
                source.call(source.runtime._rid, "base-turn").value, "open-one", status=status, result=None)))

    asyncio.run(seed())
    before = items()
    view = source.call(cut_session, probe(), NativeSessionCut(
        request_id="cut-open", expected_session_id=SOURCE, expected_etag=before["agent"][1]))
    assert (view.state, view.reason) == ("current", "accepted_or_started")
    assert items() == before


@pytest.mark.parametrize("why", ["admitting", "expected_etag"])
def test_an_admission_or_a_moved_etag_makes_the_cut_busy_before_any_scan(source, why):
    if why == "admitting":
        async def reserve():
            async with FoundryStateStore(TURNS_STORE) as store:
                item = await store.get_item("agent")
                value = dict(item.value)
                value["admitting"] = {"request_id": "admitting-rid", "submit_digest": source.base.submit_digest}
                await store.set_item("agent", value, if_match=item.etag)
        asyncio.run(reserve())
    before = items()
    view = source.call(cut_session, probe(), NativeSessionCut(
        request_id="cut-early", expected_session_id=SOURCE,
        expected_etag="OFFLINE-older" if why != "admitting" else before["agent"][1]))
    assert (view.state, view.reason) == ("current", "admission_pending")
    assert items() == before


def test_a_busy_chain_task_makes_the_cut_busy_and_it_writes_nothing(source):
    class Busy(Quiet):
        async def quiet(self, sid, *, task_id=None, include_responses=False):
            return False

    before = items()
    view = source.call(cut_session, probe(task_observer=Busy()), NativeSessionCut(
        request_id="cut-task", expected_session_id=SOURCE, expected_etag=before["agent"][1]))
    assert (view.state, view.reason) == ("current", "task_active")
    assert items() == before


ENTRIES = {
    "submit": lambda h: submit("admitted-during-cut"),
    "receive": lambda h: agent_receive("admitted-during-cut"),
    "routine": lambda h: routine_request(h.base.conversation_id),
    "group": lambda h: response_submit("admitted-during-cut"),
}


def _unchanged_source(found):
    agent = found["agent"][0]
    assert agent["hosted_session_id"] == SOURCE and agent.get("environment", {}).get("state") != "pending"
    assert not any(key.startswith(("following:", "environment-before:")) for key in found)


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_an_admission_during_the_scan_makes_the_cut_busy(source, monkeypatch, entry):
    """ADR-0009 4a: a new RID admitted on the source between E0 and the commit moves the ``agent`` ETag; the cut's
    E0 CAS loses, it answers busy, and the Agent stays the source's (the admitted turn runs there)."""
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    source.runtime.responses_enabled = True
    command, original = ENTRIES[entry](source), native_environment._tasks_pending

    async def admitted(*args, **kwargs):
        response = await asyncio.to_thread(source.invoke, command)
        assert response.status_code == 202, response.text
        return await original(*args, **kwargs)

    e0 = source.invoke(NativeSessionRead()).json()["etag"]
    monkeypatch.setattr(native_environment, "_tasks_pending", admitted)
    view = source.call(cut_session, probe(), NativeSessionCut(
        request_id="cut-raced", expected_session_id=SOURCE, expected_etag=e0))
    assert (view.state, view.reason) == ("current", "admission_pending")
    _unchanged_source(items())
    assert source.call(source.runtime._rid, command.command.request_id).value.receipt.hosted_session_id == SOURCE


def test_a_continuation_admitted_during_the_scan_makes_the_cut_busy(local_env, monkeypatch):  # noqa: F811
    """The fifth entry (``continue_turn``, official recovery's derived input) moves the ETag the same way."""
    from shared_context.native_effects import NativeEffects

    first_sdk = OfflineSdk()
    first = NativeTurnRuntime(IDENTITY, first_sdk)

    async def tracked(receipt, scope, previous, control):
        await NativeEffects(first, receipt, control).start_operations()
        return await first_sdk(receipt, scope, previous, control)

    first.executor = tracked
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as client:
        h = Harness(first, first_sdk, client)
        prepare(h)
        assert h.invoke(submit("base")).status_code == 202
        cid = h.wait("base", "completed").conversation_id
        first_sdk.block = True
        assert h.invoke(submit("root", cid, "Continue the original work.")).status_code == 202
        root = h.wait("root", "started")

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    outcome = []

    async def deliver(view):
        original = await runtime.read(view.original_request_id)
        request = NativeContinuationSubmit(
            command=original.command.model_copy(update={
                "request_id": view.request_id, "conversation_id": original.conversation_id}),
            capture=original.capture, state_profile=original.state_profile, continuation=view.continuation)
        admitted = []

        async def during_scan(*args, **kwargs):
            admitted.append(await runtime.continue_turn(request))
            return None

        _, e0 = await runtime._agent_record()
        with monkeypatch.context() as patch:
            patch.setattr(native_environment, "_tasks_pending", during_scan)
            outcome.append(await cut_session(probe(), NativeSessionCut(
                request_id="cut-continued", expected_session_id=SOURCE, expected_etag=e0)))
        return admitted[0]

    runtime.on_continuation = deliver
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        recovered = Harness(runtime, sdk, client)
        recovered.wait(root.request_id, "unknown")
        view = recovered.invoke(NativeContinuationRead(request_id=root.request_id)).json()
        recovered.wait(view["request_id"], "completed")
    assert [(view.state, view.reason) for view in outcome] == [("current", "admission_pending")]
    _unchanged_source(items())


@pytest.mark.parametrize("version", ["2", "1"], ids=["newer", "same-version"])
def test_a_quiet_cut_without_files_commits_e0_to_ready_with_no_pending(source, monkeypatch, caplog, version):
    """No files: one CAS E0 -> ``ready`` (as the abandon), never a ``pending``; a fresh SID of the same version too
    (M5: the destination SID differs, the version is what the API named)."""
    destination = V2.model_copy(update={"agent_version": version, "session_id": "offline-cut-" + version})
    writes, set_item = [], FoundryStateStore.set_item

    async def recorded(store, key, value, **kwargs):
        if store._name == TURNS_STORE and key == "agent":
            writes.append(value)
        return await set_item(store, key, value, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "set_item", recorded)
    e0 = source.invoke(NativeSessionRead()).json()["etag"]
    caplog.set_level(logging.WARNING)
    ready = source.call(cut_session, probe(destination), NativeSessionCut(
        request_id="cut-direct", expected_session_id=SOURCE, expected_etag=e0))
    assert (ready.state, ready.hosted_session_id, ready.previous_hosted_session_id) == (
        "ready", destination.session_id, SOURCE)
    assert ready.files_migrated is None  # nothing moved: the binding row's notice says so (record_switch)
    assert [(write["hosted_session_id"], write["environment"]["state"]) for write in writes] == [
        (destination.session_id, "ready")]
    found = items()
    h0_decodes("native_turns._AgentSession", found["agent"][0])
    h0_decodes("behavior_native_turn_wire.NativeEnvironmentView",
               found["environment:" + material_digest(ready)][0])
    assert not any(key.startswith(("following:", "environment-before:")) for key in found)
    [line] = cut_lines(caplog)
    assert "outcome=ready" in line and "scan=full" in line and "calls=1" in line
    # The source is replaced: its new work is refused before anything is admitted.
    refused = source.invoke(submit("after-cut"))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    # A rerun returns the same commit.
    again = source.call(cut_session, probe(destination), NativeSessionCut(
        request_id="cut-direct", expected_session_id=SOURCE, expected_etag=e0))
    assert again == ready


def test_a_quiet_cut_with_files_writes_the_h0_shaped_fence_and_the_follow_carries_it(source, tmp_path):
    before = items()["agent"][0]
    destination = probe(workspace_files=NativeWorkspaceFiles(tmp_path / "destination-files"))
    view = source.call(cut_session, destination, NativeSessionCut(
        request_id="cut-files", expected_session_id=SOURCE, files_required=True))
    assert (view.state, view.reason, view.hosted_session_id) == ("pending", "files_pending", V2.session_id)
    found = items()
    agent, following = found["agent"][0], found[following_key(V2.session_id)][0]
    assert agent["hosted_session_id"] == SOURCE and agent["environment"]["state"] == "pending"
    assert agent["environment"]["request_id"] == "cut-files"
    assert (following["phase"], following["files_required"], following["idle_only"]) == ("files", True, True)
    assert "cut" not in following
    # Everything H0 may read during the fence: its admission check (``agent``) and its file stages (``following:``).
    h0_decodes("native_turns._AgentSession", agent)
    h0_decodes("native_environment._Following", following)
    saved = found["environment-before:cut-files"][0]
    assert (saved["hosted_session_id"], saved.get("environment")) == (SOURCE, before.get("environment"))
    refused = source.invoke(submit("during-fence"))
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "AGENT_VERSION_FOLLOWING"
    # The existing follow file stages take it from here; a rerun of the cut returns the same fence.
    resumed = source.call(follow_session, destination, NativeSessionFollow(
        request_id="cut-files", expected_session_id=SOURCE, expected_etag=found["agent"][1], stage="prepare",
        idle_only=True, files_required=True))
    assert (resumed.state, resumed.reason) == ("pending", "files_pending")
    assert source.call(cut_session, destination, NativeSessionCut(
        request_id="cut-files", expected_session_id=SOURCE, files_required=True)) == view


def test_a_probe_then_a_cut_at_its_etag_commits(source, caplog):
    """P4 review: the probe answers E0 and commits nothing; the API lists the source's files, then cuts at E0."""
    caplog.set_level(logging.WARNING)
    before = items()
    quiet = source.call(cut_session, probe(), NativeSessionCut(
        request_id="cut-probe", expected_session_id=SOURCE, mode="probe"))
    assert (quiet.state, quiet.reason, quiet.quiet_etag) == ("current", None, before["agent"][1])
    assert items() == before
    ready = source.call(cut_session, probe(), NativeSessionCut(
        request_id="cut-commit", expected_session_id=SOURCE, expected_etag=quiet.quiet_etag))
    assert (ready.state, ready.hosted_session_id, ready.quiet_etag) == ("ready", V2.session_id, None)
    probed, committed = cut_lines(caplog)
    assert "outcome=quiet" in probed and "mode=probe" in probed
    assert "outcome=ready" in committed and "mode=cut" in committed and "task_ms=" in committed


def test_a_turn_admitted_and_finished_between_the_probe_and_the_cut_makes_the_cut_busy(source):
    """P4 review (data loss): the turn may write workspace files after the API's listing and end before the cut's
    scan, which then finds the store quiet. Its admission moved the ETag past E0, so the cut commits nothing."""
    quiet = source.call(cut_session, probe(), NativeSessionCut(
        request_id="cut-probe", expected_session_id=SOURCE, mode="probe"))
    # Here the API lists the source's files (none), then a turn runs and finishes on the source.
    assert source.invoke(submit("between-probe-and-cut")).status_code == 202
    source.wait("between-probe-and-cut", "completed")
    source.call(source.runtime.drain_open_removals)
    before = items()
    view = source.call(cut_session, probe(), NativeSessionCut(
        request_id="cut-after-probe", expected_session_id=SOURCE, expected_etag=quiet.quiet_etag))
    assert (view.state, view.reason) == ("current", "admission_pending")
    assert items() == before
    _unchanged_source(before)


def test_a_cut_without_files_requires_the_probe_etag_and_no_switch_settles_its_source():
    with pytest.raises(ValidationError, match="expected_etag"):
        NativeSessionCut(request_id="cut-blind", expected_session_id=SOURCE)
    NativeSessionCut(request_id="cut-probe", expected_session_id=SOURCE, mode="probe")
    NativeSessionCut(request_id="cut-files", expected_session_id=SOURCE, files_required=True)
    for model, fields in ((NativeSessionCut, {"expected_etag": "OFFLINE-e0"}),
                          (NativeSessionFollow, {"expected_etag": "OFFLINE-e0", "stage": "prepare"})):
        with pytest.raises(ValidationError, match="settled_session_ids"):
            model(request_id="settled-source", expected_session_id=SOURCE, settled_session_ids=[GHOST, SOURCE],
                  **fields)


def test_a_rerun_after_the_commit_writes_the_marker_a_crash_left_out(source, monkeypatch):
    from shared_context import native_open_rids

    command = NativeSessionCut(request_id="cut-crash", expected_session_id=SOURCE, expected_etag=items()["agent"][1])
    mark = native_open_rids.mark_complete

    async def crashed(*args, **kwargs):
        raise RuntimeError("stopped between the commit's CAS and its mark")

    monkeypatch.setattr(native_open_rids, "mark_complete", crashed)
    with pytest.raises(RuntimeError):
        source.call(cut_session, probe(), command)
    assert items()["agent"][0]["hosted_session_id"] == V2.session_id
    monkeypatch.setattr(native_open_rids, "mark_complete", mark)
    assert source.call(cut_session, probe(), command).state == "ready"
    marked = _open_set(V2, monkeypatch)
    assert (marked.complete_for, marked.complete_since) == (V2.session_id, "cut-crash")


def test_a_files_cut_without_a_destination_helper_is_refused_and_writes_nothing(source):
    before = items()
    view = source.call(cut_session, probe(), NativeSessionCut(
        request_id="cut-no-helper", expected_session_id=SOURCE, files_required=True))
    assert (view.state, view.reason) == ("current", "files_unsupported")
    assert items() == before


def test_a_cut_reads_only_the_open_set_when_it_is_complete(history, reads, monkeypatch, caplog):
    _, etag, _ = _follow(V2, SOURCE, history.etag, monkeypatch, request_id="follow-v2", settled=(GHOST,))
    reads.clear()
    caplog.set_level(logging.WARNING)
    ready = _cut(V3, monkeypatch, request_id="cut-v3", source=V2.session_id, settled_session_ids=[GHOST],
                 expected_etag=etag)
    assert ready["state"] == "ready", ready
    # Only the one listed receipt (the retired SID's, not waited for), by the scan and by the adoption after the
    # commit, and the seed turn it follows in its conversation when the adopted turn runs; none of the HISTORY ones.
    assert reads[0] == "ghost-accepted" and set(reads) == {"ghost-accepted", "seed-turn"}
    [line] = cut_lines(caplog)
    assert all(part in line for part in ("outcome=ready", "scan=set", "receipts=1", "calls=1", "adopt_ms="))
    marked = _open_set(V3, monkeypatch)
    assert (marked.complete_for, marked.complete_since) == (V3.session_id, "cut-v3")


def test_a_full_scan_goes_on_across_calls_from_its_first_etag(history, reads, monkeypatch, caplog):
    """No complete set (an image before it): every receipt is read, over two calls; E0 is kept between them in this
    session's ``following:`` record, which the commit then leaves behind unchanged in shape."""
    monkeypatch.setattr(native_left_receipts, "adopt_left", lambda runtime, source=None, rids=None: asyncio.sleep(0))
    scan_page, pages = native_environment._scan_page, []

    async def one_page(*args, **kwargs):
        if pages:
            raise TimeoutError  # this call's scan deadline
        pages.append(1)
        return await scan_page(*args, **kwargs)

    caplog.set_level(logging.WARNING)
    with monkeypatch.context() as patch:
        patch.setattr(native_environment, "_scan_page", one_page)
        first = _cut(V2, monkeypatch, request_id="cut-v2", settled_session_ids=[GHOST], expected_etag=history.etag)
    assert (first["state"], first["reason"]) == ("current", "scan_pending"), first
    first_reads = list(reads)
    progress = items()[following_key(V2.session_id)][0]
    assert progress["cut"]["etag"] == history.etag and progress["cut"]["calls"] == 1 and progress["after"]
    reads.clear()
    ready = _cut(V2, monkeypatch, request_id="cut-v2", settled_session_ids=[GHOST], expected_etag=history.etag)
    assert ready["state"] == "ready", ready
    assert first_reads and set(first_reads).isdisjoint(reads)
    assert len(set(first_reads) | set(reads)) == HISTORY + 2  # the seed turn, the history and the retired SID's
    lines = cut_lines(caplog)
    assert "outcome=scanning" in lines[0] and "scan=full" in lines[0] and "calls=1" in lines[0]
    assert "outcome=ready" in lines[1] and "scan=full" in lines[1] and "calls=2" in lines[1]
    assert _open_set(V2, monkeypatch).complete_for == V2.session_id


def _withdraw(etag, pending_request_id, pending_session_id=V2.session_id, *, request_id="withdraw-one"):
    return NativeSessionWithdraw(request_id=request_id, expected_etag=etag, pending_request_id=pending_request_id,
                                 pending_session_id=pending_session_id)


def test_a_withdraw_restores_what_the_cut_fence_replaced(source, tmp_path):
    before = items()["agent"]
    destination = probe(workspace_files=NativeWorkspaceFiles(tmp_path / "destination-files"))
    source.call(cut_session, destination, NativeSessionCut(
        request_id="cut-files", expected_session_id=SOURCE, files_required=True))
    fenced = items()["agent"]
    # Any H2d session runs it (here a third one): H0 has no withdraw.
    state = source.call(withdraw_session, probe(V3), _withdraw(fenced[1], "cut-files"))
    assert state.hosted_session_id == SOURCE and state.environment == before[0].get("environment")
    restored = items()["agent"]
    assert restored[0] == before[0] and state.etag == restored[1]
    h0_decodes("native_turns._AgentSession", restored[0])
    assert source.invoke(submit("after-withdraw")).status_code == 202
    source.wait("after-withdraw", "completed")
    # Sent again (a lost answer): the same state, nothing written.
    again = source.call(withdraw_session, probe(V3), _withdraw(fenced[1], "cut-files"))
    assert (again.hosted_session_id, again.environment) == (SOURCE, state.environment)


def test_a_withdraw_after_fas_recorded_stop_is_refused_and_answers_the_pending(source, tmp_path, monkeypatch, caplog):
    """Whichever API replica asks: once the fence's files progress records FA's stop of the source, Hosted keeps the
    ``pending`` and answers it, so the caller drives it to its commit."""
    destination = probe(workspace_files=NativeWorkspaceFiles(tmp_path / "destination-files"))
    source.call(cut_session, destination, NativeSessionCut(
        request_id="cut-files", expected_session_id=SOURCE, files_required=True))
    fenced = items()
    original = native_environment._original_progress

    async def stopped(runtime, view):
        record, etag, key = await original(runtime, view)
        assert key == following_key(V2.session_id)  # the fence's own record, read by a third session
        return SimpleNamespace(files=SimpleNamespace(stop_started=True)), etag, key

    monkeypatch.setattr(native_environment, "_original_progress", stopped)
    caplog.set_level(logging.WARNING)
    state = source.call(withdraw_session, probe(V3), _withdraw(fenced["agent"][1], "cut-files"))
    assert (state.hosted_session_id, state.environment.state, state.environment.request_id) == (
        SOURCE, "pending", "cut-files")
    assert items() == fenced and "NATIVE_UPGRADE_WITHDRAW_REFUSED" in caplog.text


def test_a_retarget_record_naming_another_pending_never_blocks_withdrawing_an_unmarked_fence(source, tmp_path,
                                                                                            monkeypatch):
    """#858 review P3: ``_original_progress`` answers 409 ``NATIVE_ENVIRONMENT_CONFLICT`` when a retained retarget names
    another pending; that says nothing about FA stopping this fence's source, so the withdraw goes on."""
    before = items()["agent"]
    destination = probe(workspace_files=NativeWorkspaceFiles(tmp_path / "destination-files"))
    source.call(cut_session, destination, NativeSessionCut(
        request_id="cut-files", expected_session_id=SOURCE, files_required=True))
    fenced = items()["agent"]

    async def conflicting(runtime, view):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the retained file operation differs")

    monkeypatch.setattr(native_environment, "_original_progress", conflicting)
    state = source.call(withdraw_session, probe(V3), _withdraw(fenced[1], "cut-files"))
    assert state.hosted_session_id == SOURCE and items()["agent"][0] == before[0]


def test_an_ordinary_follow_saves_what_its_pending_replaces_and_never_commits_over_a_withdraw(source):
    e0 = source.invoke(NativeSessionRead()).json()["etag"]
    before = items()["agent"][0]
    destination = probe(bootstrap_source_inactive=_inactive)
    follow = NativeSessionFollow(request_id="follow-one", expected_session_id=SOURCE, expected_etag=e0,
                                 stage="prepare", idle_only=True)
    pending = source.call(follow_session, destination, follow)
    assert pending.state == "pending"
    found = items()
    assert found["environment-before:follow-one"][0] == {
        "schema_version": "fa-native-environment-before-v1", "hosted_session_id": SOURCE,
        **({"environment": before["environment"]} if "environment" in before else {})}
    state = source.call(withdraw_session, destination, _withdraw(found["agent"][1], "follow-one"))
    assert state.hosted_session_id == SOURCE and items()["agent"][0] == before
    with pytest.raises(Problem) as refused:
        source.call(follow_session, destination, follow.model_copy(update={"stage": "commit"}))
    assert (refused.value.status, refused.value.body["error"]["code"]) == (409, "NATIVE_ENVIRONMENT_CONFLICT")
    assert items()["agent"][0] == before


def test_a_withdraw_of_a_pending_an_older_follow_left_rebuilds_a_decodable_ready_view(history, monkeypatch):
    """No ``environment-before:`` (an image before it wrote the ``pending``): the ready view comes back from what the
    ``pending`` kept of it. Sent through the invocation surface to a session that is not the Agent's."""
    legacy = NativeEnvironmentView(
        agent_id=IDENTITY.agent_id, request_id="legacy-follow", previous_hosted_session_id=SOURCE,
        previous_agent_version=IDENTITY.agent_version, hosted_session_id="offline-legacy-target",
        hosted_agent_version="2", state="pending", reason="source_stop_required")

    async def left_by_an_older_follow():
        async with FoundryStateStore(TURNS_STORE) as store:
            item = await store.get_item("agent")
            await store.set_item("agent", {**item.value, "environment": _json(legacy)}, if_match=item.etag)
            return (await store.get_item("agent")).etag

    etag = asyncio.run(left_by_an_older_follow())
    runtime = _runtime(V2, monkeypatch)
    with TestClient(create_host(SimpleNamespace(identity=V2), native_runtime=runtime)) as client:
        response = client.post("/invocations", json=_withdraw(
            etag, "legacy-follow", "offline-legacy-target", request_id="withdraw-legacy").model_dump(mode="json"),
            headers={NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, NATIVE_LOCATOR_HEADER: NATIVE_LOCATOR_BINDING_ROW,
                     "x-agent-session-id": V2.session_id, "x-agent-invocation-id": "offline-withdraw",
                     "x-agent-foundry-call-id": "offline-withdraw-caller"})
    assert response.status_code == 200, response.text
    view = response.json()["environment"]
    assert (view["state"], view["hosted_session_id"], view["hosted_agent_version"], view["request_id"],
            view["previous_hosted_session_id"]) == ("ready", SOURCE, "1", "withdraw-legacy", "offline-legacy-target")
    agent = items()["agent"][0]
    h0_decodes("native_turns._AgentSession", agent)
    assert agent["environment"] == view
    sdk = OfflineSdk()
    runtime = _runtime(IDENTITY, monkeypatch, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, sdk, client)
        assert h.invoke(submit("after-legacy-withdraw")).status_code == 202
        h.wait("after-legacy-withdraw", "completed")


def test_a_withdraw_never_rebuilds_a_ready_view_for_another_source(source):
    """A legacy ``pending`` whose ``previous_*`` names another session than the Agent's: refused, nothing written.
    The record decode refuses such a store already; the withdraw is given one in memory to check its own guard."""
    stray = NativeEnvironmentView(
        agent_id=IDENTITY.agent_id, request_id="legacy-stray", previous_hosted_session_id="offline-other-source",
        previous_agent_version=IDENTITY.agent_version, hosted_session_id="offline-legacy-target",
        hosted_agent_version="2", state="pending", reason="source_stop_required")
    runtime, read = probe(V3), None

    async def strayed():
        current, etag = await read()
        return current.model_copy(update={"environment": stray}), etag

    read, runtime._agent_record = runtime._agent_record, strayed
    before = items()
    with pytest.raises(Problem) as refused:
        source.call(withdraw_session, runtime, _withdraw(before["agent"][1], "legacy-stray", "offline-legacy-target"))
    assert (refused.value.status, refused.value.body["error"]["code"]) == (409, "NATIVE_ENVIRONMENT_UNAVAILABLE")
    assert items() == before


def test_a_withdraw_of_a_committed_switch_answers_the_commit(source):
    ready = source.call(cut_session, probe(), NativeSessionCut(
        request_id="cut-direct", expected_session_id=SOURCE, expected_etag=items()["agent"][1]))
    committed = items()["agent"]
    state = source.call(withdraw_session, probe(V3), _withdraw("OFFLINE-pending-etag", "cut-direct"))
    assert (state.hosted_session_id, state.environment, state.etag) == (V2.session_id, ready, committed[1])
    assert items()["agent"] == committed


@pytest.mark.parametrize("first", ["withdraw", "commit"])
def test_a_withdraw_and_a_commit_of_the_same_pending_exactly_one_wins(source, monkeypatch, first):
    """Both CAS the same ``pending`` ETag. ``first`` reads it, then the other one runs to its end inside ``first``'s
    CAS window and wins; ``first`` loses its CAS and reads back the winner (a withdraw answers the commit)."""
    e0 = source.invoke(NativeSessionRead()).json()["etag"]
    before = items()["agent"][0]
    destination = probe(bootstrap_source_inactive=_inactive)
    follow = NativeSessionFollow(request_id="follow-raced", expected_session_id=SOURCE, expected_etag=e0,
                                 stage="prepare", idle_only=True)
    source.call(follow_session, destination, follow)
    withdraw, commit = _withdraw(items()["agent"][1], "follow-raced"), follow.model_copy(update={"stage": "commit"})
    save_agent, armed, outcome = native_environment._save_agent, [True], {}

    async def run(name):
        try:
            outcome[name] = await (withdraw_session(probe(V3), withdraw) if name == "withdraw"
                                   else follow_session(destination, commit))
        except Problem as error:
            outcome[name] = error.body["error"]["code"]

    async def racing(*args, **kwargs):
        if armed:
            armed.clear()
            await run("commit" if first == "withdraw" else "withdraw")
        return await save_agent(*args, **kwargs)

    monkeypatch.setattr(native_environment, "_save_agent", racing)
    source.call(run, first)
    agent = items()["agent"][0]
    if first == "withdraw":  # the commit won
        assert outcome["commit"].state == "ready" and agent["hosted_session_id"] == V2.session_id
        assert outcome["withdraw"].hosted_session_id == V2.session_id
    else:  # the withdraw won
        assert outcome["commit"] == "NATIVE_ENVIRONMENT_UNKNOWN"
        assert outcome["withdraw"].hosted_session_id == SOURCE and agent == before


def test_a_follow_to_a_fresh_session_of_the_same_version_commits(source):
    """ADR-0009 4a: "destination SID != current SID", no longer "a newer version"."""
    destination = probe(V2.model_copy(update={"agent_version": IDENTITY.agent_version}),
                        bootstrap_source_inactive=_inactive)
    follow = NativeSessionFollow(request_id="follow-same-version", expected_session_id=SOURCE,
                                 expected_etag=source.invoke(NativeSessionRead()).json()["etag"], stage="prepare",
                                 idle_only=True)
    assert source.call(follow_session, destination, follow).state == "pending"
    ready = source.call(follow_session, destination, follow.model_copy(update={"stage": "commit"}))
    assert (ready.state, ready.hosted_session_id, ready.hosted_agent_version) == (
        "ready", V2.session_id, IDENTITY.agent_version)


def test_the_commit_adopts_what_a_previous_sid_never_started(history, monkeypatch):
    """M9 as the abandon: the retired SID's never-started receipt (not waited for) becomes this session's."""
    ready = _cut(V2, monkeypatch, request_id="cut-adopt", settled_session_ids=[GHOST], expected_etag=history.etag)
    assert ready["state"] == "ready", ready
    adopted = items()["rid:ghost-accepted"][0]
    assert adopted["adopted_from"] == GHOST and adopted["receipt"]["hosted_session_id"] == V2.session_id
    assert _open_set(V2, monkeypatch).complete_for == V2.session_id
