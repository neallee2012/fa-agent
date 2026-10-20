"""ADR-0009 M1 2d-i step 2 (P5): the API's background cut after a turn end.

Real source and destination Hosted runtimes over one shared ``fa/v1/turns`` store behind the real API (``file_api``);
only the platform (session metadata, version definitions, Session Files) is an offline double. The source is version
41 and the selector names 42, so the binding row's V_rec differs from the selected version.
"""

from datetime import timedelta
import hashlib
import logging
import time
from types import SimpleNamespace

import pytest

from shared_context import behavior_native_sessions, native_cut
from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_native_sessions import NativeSessions, native_sessions
from shared_context.behavior_native_turn_api import agent_client
from shared_context.behavior_native_turn_wire import NativeEnvironmentView, NativeSessionRead
from shared_context.native_binding import NativeBindingStore
from shared_context.native_turns import TURNS_STORE
from test_native_abandon_source import binding_rows  # noqa: F401
import test_native_abandon_source as abandon
from test_native_binding import bound, declared  # noqa: F401
from test_native_environment import invoke
from test_native_files_api import (  # noqa: F401
    PREFIX, file_api, file_loops, instruction_models, jwk_client, local_env, native_api, signing_key,
    synthetic_standing_v1, token,
)
from test_native_follow_no_wake import not_read_after_the_stop, source_calls


_FOLLOW, _CONFIRM = NativeSessions.follow, NativeSessions._confirm_environment
_CREATE = FoundryHttpClient.create_session  # the real one: ``file_api`` replaces it with its own double


@pytest.fixture(autouse=True)
def replace_retried(monkeypatch):
    """Windows: the SDK's offline State store replaces its JSON file (``os.replace``), which fails with WinError 5
    while another handle (a virus scanner) still holds the file just written; that turned random store writes in this
    file into NATIVE_STATE_STORE_UNKNOWN. Foundry's store has no such window, so the double retries the replace."""
    from azure.ai.agentserver.core.storage import _local_state

    write = _local_state.LocalStateStoreBackend._write

    def retried(self, document):
        for _ in range(100):
            try:
                return write(self, document)
            except PermissionError:
                time.sleep(0.01)
        return write(self, document)

    monkeypatch.setattr(_local_state.LocalStateStoreBackend, "_write", retried)


class Crash(BaseException):
    """The API process dies here: no handler of the cut runs."""


def ready(fixture, monkeypatch, *, files=True, idle=False):
    """The Agent bound to the source (row claimed), the platform double wired, the selector on 42."""
    abandon.platform(fixture, monkeypatch)
    abandon.claim(fixture)
    if not files:
        for child in fixture.workspace.root.iterdir():
            _remove(child)
    if idle:
        fixture.files.stopped.add(fixture.source.session_id)
    return fixture


def on_source_version(fixture, monkeypatch):
    """The selector names the row's version: reads go to the source (before P6 a moved selector refuses them)."""
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: fixture.source.agent_version)


def _remove(path):
    if path.is_dir():
        for child in path.iterdir():
            _remove(child)
        path.rmdir()
    else:
        path.unlink()


def sessions(fixture):
    return native_sessions(fixture.case.composition)


def control(fixture):
    return fixture.case.composition.runtime.work_policy


def cut(fixture):
    composition = fixture.case.composition
    return sessions(fixture).cut(agent_client(composition, control(fixture)))


def state(fixture):
    return invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()


def operations(fixture, since=0):
    return [(command.get("operation"), command.get("mode")) for command, _ in fixture.controls[since:]]


def set_admitting(fixture, value):
    async def write():
        async with fixture.runtime._store(TURNS_STORE) as store:
            item = await store.get_item("agent")
            record = dict(item.value)
            if value is None:
                record.pop("admitting", None)
            else:
                record["admitting"] = {"request_id": value, "submit_digest": fixture.saved.submit_digest}
            await store.set_item("agent", record, if_match=item.etag)
    fixture.hosted.portal.call(write)


def submit_on_source(fixture, rid):
    return fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json={
        "request_id": rid, "content": "A message after the withdraw"})


def finished(driver, agent_id, timeout=30):
    deadline = time.monotonic() + timeout
    while agent_id in driver._running:
        assert time.monotonic() < deadline, "the background attempt did not finish"
        time.sleep(0.02)


# --- the turn-end hook ---------------------------------------------------------------------------------------------

def test_the_turn_end_hook_adds_no_reads_to_the_request_that_saw_it(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    on_source_version(fixture, monkeypatch)
    sessions(fixture).guard.enforce = True
    attempts, reads = [], []
    monkeypatch.setattr(NativeSessions, "cut", lambda self, client, **_: attempts.append(client.target.agent_id) or (
        "current", None, None))
    original_read = NativeBindingStore._read
    monkeypatch.setattr(NativeBindingStore, "_read", lambda self, agent_id: reads.append(agent_id) or original_read(
        self, agent_id))

    def read():
        before = len(fixture.controls), len(fixture.project_calls), len(reads), len(fixture.files.calls)
        response = abandon.read_request(fixture, "original-rid")
        assert response.status_code == 200 and response.json()["status"] == "completed", response.text
        return (len(fixture.controls) - before[0], len(fixture.project_calls) - before[1], len(reads) - before[2],
                len(fixture.files.calls) - before[3])

    read()  # warm: the reader, selector and metadata reads are kept for the next two
    hook = native_cut.turn_ended
    monkeypatch.setattr(native_cut, "turn_ended", lambda *_args: None)
    without = read()
    monkeypatch.setattr(native_cut, "turn_ended", hook)
    driver = native_cut.native_cut_driver(fixture.case.composition)
    with_hook = read()
    finished(driver, fixture.source.agent_id)
    assert with_hook == without
    assert attempts == [fixture.source.agent_id]
    read()  # the same terminal receipt polled again triggers nothing more
    finished(driver, fixture.source.agent_id)
    assert attempts == [fixture.source.agent_id]


def test_the_turn_end_hook_never_raises_into_the_turn(monkeypatch, caplog):
    def broken(_composition):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(behavior_native_sessions, "native_sessions", broken)
    caplog.set_level(logging.WARNING)
    native_cut.turn_ended(SimpleNamespace(), None, SimpleNamespace(agent_id="agent", request_id="rid"))
    assert "NATIVE_UPGRADE_CUT_HOOK_FAILED code=RuntimeError agent_id=agent" in caplog.text


def test_no_cut_with_the_flag_off_or_when_v_rec_is_the_selected_version(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    on_source_version(fixture, monkeypatch)
    triggered = []
    monkeypatch.setattr(native_cut.CutDriver, "turn_ended", lambda self, *args: triggered.append(args))
    assert sessions(fixture).guard.enforce is False
    assert abandon.read_request(fixture, "original-rid").status_code == 200
    assert triggered == [] and "_native_cut" not in vars(fixture.case.composition)
    before = len(fixture.controls), len(fixture.files.calls)
    assert cut(fixture) == ("current", None, None)
    assert (len(fixture.controls), len(fixture.files.calls)) == before
    assert abandon.row(fixture).hosted_session_id == fixture.source.session_id


def test_busy_waits_for_the_next_turn_end_with_no_calls_meanwhile(file_api, monkeypatch, caplog):
    fixture = ready(file_api, monkeypatch, files=False)
    sessions(fixture).guard.enforce = True
    quiet = [False]

    async def task_quiet(sid, *, task_id=None, include_responses=False):
        return quiet[0]

    fixture.runtime.task_observer = SimpleNamespace(quiet=task_quiet)
    driver = native_cut.native_cut_driver(fixture.case.composition)
    caplog.set_level(logging.WARNING)
    agent_id = fixture.source.agent_id
    assert driver.turn_ended(control(fixture), agent_id, "turn-1")
    finished(driver, agent_id)
    assert "NATIVE_UPGRADE_CUT_API outcome=busy reason=task_active" in caplog.text
    calls = len(fixture.controls), len(fixture.files.calls), len(fixture.project_calls)
    time.sleep(0.5)
    assert (len(fixture.controls), len(fixture.files.calls), len(fixture.project_calls)) == calls
    assert state(fixture)["hosted_session_id"] == fixture.source.session_id
    quiet[0] = True
    assert driver.turn_ended(control(fixture), agent_id, "turn-2")
    finished(driver, agent_id)
    assert "NATIVE_UPGRADE_CUT_API outcome=ready" in caplog.text
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id


# --- probe, list, cut ----------------------------------------------------------------------------------------------

def test_a_turn_admitted_after_the_probe_makes_the_cut_busy_and_its_files_still_move(file_api, monkeypatch):
    fixture = file_api
    listed = fixture.files.list_session_files
    seen = []

    def admitted_while_listing(name, sid, *, path, limit):
        if not seen:
            # The order is probe, list, cut: only the probe went out before this listing.
            seen.append(operations(fixture))
            set_admitting(fixture, "late-rid")
            (fixture.workspace.root / "late.txt").write_bytes(b"written by the late turn\n")
            set_admitting(fixture, None)
        return listed(name, sid, path=path, limit=limit)

    fixture.files.list_session_files = admitted_while_listing
    ready(fixture, monkeypatch)
    start = len(fixture.controls)
    assert cut(fixture) == ("busy", "admission_pending", None)
    assert [item for item in seen[0][start:] if item[0] == "session_cut"] == [("session_cut", "probe")]
    assert [item for item in operations(fixture, start) if item[0] == "session_cut"][-1] == ("session_cut", None)
    current = state(fixture)
    assert current["hosted_session_id"] == fixture.source.session_id and current.get("environment") is None
    assert abandon.row(fixture).upgrade is None and not fixture.files.stopped
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None
    assert (fixture.target_workspace.root / "late.txt").read_bytes() == b"written by the late turn\n"


def test_files_mode_moves_the_workspace_and_notices_it(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and 0 <= fence < behavior_native_sessions.CUT_FENCE_SECONDS
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id
    assert switched.last_switch.files_migrated is True and switched.switch_notice == "updated"
    assert switched.upgrade is None
    assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    assert fixture.files.stopped == {fixture.source.session_id}
    assert not_read_after_the_stop(fixture)  # ADR-0009 §10 check ②: a stopped source is never read (or woken)
    current = state(fixture)
    assert current["hosted_session_id"] == fixture.destination.session_id
    assert current["environment"]["files_migrated"] is True


def test_an_idle_source_is_left_for_its_next_turn_end(file_api, monkeypatch):
    """Coordinator 2026-10-16: an idle source's files can not be copied without waking it, so the cut (here from a
    stale terminal-receipt read) does nothing; the Agent moves, files included, at its next real turn end."""
    fixture = ready(file_api, monkeypatch, idle=True)
    calls, controls = len(fixture.files.calls), len(fixture.controls)
    assert cut(fixture) == ("source_idle", None, None)
    assert fixture.files.calls[calls:] == [] and fixture.files.woke == []  # no session created, no Files call
    assert fixture.destination.session_id not in fixture.files.targets
    assert len(fixture.controls) == controls and source_calls(fixture) == ([], [], [])
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.source.session_id and row.upgrade is None and row.abandon is None
    fixture.files.stopped.discard(fixture.source.session_id)  # the next turn resumed it
    assert cut(fixture)[:2] == ("ready", None)
    _moved_with_files(fixture)


def _drifted(fixture, *, recorded):
    """The platform re-created the source SID (created on 41) on 43; ``recorded``: the row's drift CAS saw it."""
    sid = fixture.source.session_id
    fixture.files.targets[sid] = fixture.source.model_copy(update={"agent_version": "43"})
    fixture.original.identity = fixture.original.identity.model_copy(update={"agent_version": "43"})
    if recorded:
        row = abandon.store(fixture).record_observed(
            fixture.source.agent_id, hosted_session_id=sid, agent_version="43", source="vm")
        assert (row.created_agent_version, row.observed.agent_version, row.drift) == ("41", "43", True)


def test_a_drift_the_row_recorded_is_the_same_source_and_its_files_move(file_api, monkeypatch):
    """Dev 2026-10-18: a source the row records as drifted (created 114, observed 116) was refused on every turn end
    with NATIVE_FILES_SOURCE_UNAVAILABLE, because the files steps compared its platform version with the created one."""
    fixture = ready(file_api, monkeypatch)
    _drifted(fixture, recorded=True)
    outcome, reason, _ = cut(fixture)
    assert (outcome, reason) == ("ready", None)
    _moved_with_files(fixture)
    assert abandon.row(fixture).previous_sessions[-1].hosted_session_id == fixture.source.session_id


def test_a_drift_the_row_never_recorded_is_refused_without_invoking_the_source(file_api, monkeypatch):
    """ADR-0009 §10: a version FA has not recorded for the source SID is never invoked for a file snapshot."""
    fixture = ready(file_api, monkeypatch)
    _drifted(fixture, recorded=False)
    controls = len(fixture.control_versions)
    outcome, reason, _ = cut(fixture)
    assert (outcome, reason) == ("withdrawn", "NATIVE_FILES_SOURCE_UNAVAILABLE")
    # Only the cut's ``has_files`` listing of the active source reached it: no invocation and no stop.
    assert source_calls(fixture, since=(controls, 0)) == ([], ["list", "page"], [])
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.source.session_id and row.upgrade is None


# --- fence bound, crashes ------------------------------------------------------------------------------------------

def test_a_fence_past_its_bound_is_withdrawn_and_the_old_sid_admits_again(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", -1)
    outcome, reason, _ = cut(fixture)
    assert (outcome, reason) == ("withdrawn", "fence_overrun")
    current = state(fixture)
    # The environment before the fence (none: the source is the prepared session) is put back.
    assert current["hosted_session_id"] == fixture.source.session_id and current.get("environment") is None
    assert abandon.row(fixture).hosted_session_id == fixture.source.session_id
    assert abandon.row(fixture).upgrade is None
    on_source_version(fixture, monkeypatch)  # P6 sends by the row whatever the selector names; before it, by 41
    accepted = submit_on_source(fixture, "after-withdraw")
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["hosted_session_id"] == fixture.source.session_id


def test_the_dev_fault_hook_holds_the_fence_until_its_bound_withdraws_it(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    monkeypatch.setenv(behavior_native_sessions.CUT_FAULT_ENV, "after_pending")
    monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", 0.2)
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("withdrawn", "fence_overrun") and fence >= 0.2
    assert state(fixture)["hosted_session_id"] == fixture.source.session_id
    assert not [call for call in fixture.files.calls if call[0] in {"get", "put", "stop"}]


def test_a_crash_after_pending_is_withdrawn_only_once_the_fence_is_past_its_bound(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)

    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(NativeSessions, "follow", crash)
    with pytest.raises(Crash):
        cut(fixture)
    assert state(fixture)["environment"]["state"] == "pending"
    fence = abandon.row(fixture).upgrade
    assert fence.hosted_session_id == fixture.destination.session_id
    monkeypatch.setattr(NativeSessions, "follow", _FOLLOW)
    # Young: another replica's job may still be running it.
    assert cut(fixture) == ("fence_live", None, None)
    assert state(fixture)["environment"]["state"] == "pending" and abandon.row(fixture).upgrade == fence
    # A pending the row does not record as its fence belongs to the follow that wrote it, whatever its age.
    store = abandon.store(fixture)
    store.record_upgrade(fixture.source.agent_id, hosted_session_id=fixture.source.session_id, upgrade=None)
    monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", -1)
    assert cut(fixture) == ("pending_other", None, None)
    assert state(fixture)["environment"]["state"] == "pending"
    store.record_upgrade(fixture.source.agent_id, hosted_session_id=fixture.source.session_id, upgrade=fence)
    # Its own fence past the bound, with no stop of the source recorded, is withdrawn.
    assert cut(fixture) == ("withdrawn", None, None)
    monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", 120)
    current = state(fixture)
    assert current["hosted_session_id"] == fixture.source.session_id and current.get("environment") is None
    assert abandon.row(fixture).upgrade is None
    assert cut(fixture)[0] == "ready"
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id


def test_an_overrun_after_fas_stop_never_withdraws_and_the_files_arrive(file_api, monkeypatch):
    from shared_context.behavior_session_files import FollowFiles

    fixture = ready(file_api, monkeypatch)
    now = abandon.clock(monkeypatch)
    fixture.files.on_stop = lambda: now.__setitem__(0, now[0] + behavior_native_sessions.CUT_FENCE_SECONDS + 1)
    stopped_step, steps = FollowFiles.stopped_step, []

    def one_step_short(self):
        steps.append(1)
        return False if len(steps) == 1 else stopped_step(self)  # the stopped copy needs another step past the bound

    monkeypatch.setattr(FollowFiles, "stopped_step", one_step_short)
    assert cut(fixture)[0] == "ready" and len(steps) == 2
    assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    assert fixture.files.stopped == {fixture.source.session_id}
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id


def test_a_crash_after_fas_stop_is_driven_forward_by_the_next_attempt(file_api, monkeypatch):
    from shared_context.behavior_session_files import FollowFiles

    fixture = ready(file_api, monkeypatch)
    stopped_step = FollowFiles.stopped_step

    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(FollowFiles, "stopped_step", crash)
    with pytest.raises(Crash):
        cut(fixture)
    assert fixture.files.stopped == {fixture.source.session_id}
    assert state(fixture)["environment"]["state"] == "pending"
    monkeypatch.setattr(FollowFiles, "stopped_step", stopped_step)
    monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", -1)  # even past its bound: never withdrawn
    assert cut(fixture) == ("ready", None, None)
    assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.upgrade is None
    assert switched.last_switch.files_migrated is True
    # The crash fell between FA's stop and the stopped step records: published from the complete, verified staging
    # with only those records, no read (or wake) of the stopped source.
    assert not_read_after_the_stop(fixture)


def _one_stopped_step_short(monkeypatch):
    from shared_context.behavior_session_files import FollowFiles

    stopped_step, steps = FollowFiles.stopped_step, []

    def one_step_short(self):
        steps.append(1)
        return False if len(steps) == 1 else stopped_step(self)

    monkeypatch.setattr(FollowFiles, "stopped_step", one_step_short)


def _crash_after_fas_stop(fixture, monkeypatch):
    from shared_context.behavior_session_files import FollowFiles

    stopped_step = FollowFiles.stopped_step

    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(FollowFiles, "stopped_step", crash)
    try:
        outcome = cut(fixture)
    except Crash:
        outcome = None
    assert outcome is None, outcome
    assert fixture.files.stopped == {fixture.source.session_id}
    assert state(fixture)["environment"]["state"] == "pending" and abandon.row(fixture).upgrade.stop_started
    monkeypatch.setattr(FollowFiles, "stopped_step", stopped_step)


def _moved_with_files(fixture):
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.upgrade is None
    assert switched.last_switch.files_migrated is True
    assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    assert state(fixture)["hosted_session_id"] == fixture.destination.session_id


def _sends_admit(fixture, monkeypatch, rid):
    # P6 sends by the row whatever the selector names; before it, by the selector, so it names the row's version.
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: fixture.destination.agent_version)
    accepted = submit_on_source(fixture, rid)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["hosted_session_id"] == fixture.destination.session_id


def test_a_selector_rolled_back_during_the_fence_after_fas_stop_still_commits(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _one_stopped_step_short(monkeypatch)  # the commit takes another follow pass after the stop
    fixture.files.on_stop = lambda: on_source_version(fixture, monkeypatch)  # the selector rolls back to 41
    outcome, reason, _ = cut(fixture)
    assert (outcome, reason) == ("ready", None)
    _moved_with_files(fixture)
    _sends_admit(fixture, monkeypatch, "after-rolled-back")


def test_a_selector_rolled_back_after_a_crash_past_fas_stop_still_commits(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    on_source_version(fixture, monkeypatch)  # the row's V_rec is the selected version again
    assert cut(fixture) == ("ready", None, None)
    _moved_with_files(fixture)
    _sends_admit(fixture, monkeypatch, "after-rolled-back")


def test_a_selector_moved_forward_after_fas_stop_commits_and_the_next_cut_moves_on(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: "43")
    assert cut(fixture) == ("ready", None, None)
    _moved_with_files(fixture)
    seen = []

    def destination_43(self, client, version, before_send, **kwargs):
        seen.append((version, kwargs["row"].hosted_session_id))
        raise Crash()  # the offline platform has no version 43 session to create

    monkeypatch.setattr(NativeSessions, "_destination", destination_43)
    with pytest.raises(Crash):
        cut(fixture)
    assert seen == [("43", fixture.destination.session_id)]


def test_the_stop_reserve_is_checked_before_every_stop_until_it_is_marked(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    now = abandon.clock(monkeypatch)
    status, first = behavior_native_sessions._source_session_status, []

    def settling(source, phase, **kwargs):
        if phase == "source_status" and not first:
            first.append(phase)
            now[0] += behavior_native_sessions.CUT_FENCE_SECONDS - 5  # inside the bound, under the stop reserve
            return "updating"  # no stop is sent on this pass
        return status(source, phase, **kwargs)

    monkeypatch.setattr(behavior_native_sessions, "_source_session_status", settling)
    outcome, reason, _ = cut(fixture)
    assert (outcome, reason) == ("withdrawn", "fence_overrun")
    assert not fixture.files.stopped and abandon.row(fixture).upgrade is None
    current = state(fixture)
    assert current["hosted_session_id"] == fixture.source.session_id and current.get("environment") is None


def test_a_stop_that_never_settles_is_capped_with_a_warning_and_left_for_the_next_trigger(
        file_api, monkeypatch, caplog):
    fixture = ready(file_api, monkeypatch)
    now = abandon.clock(monkeypatch)
    late = behavior_native_sessions.CUT_FENCE_SECONDS + behavior_native_sessions.CUT_DRIVE_SECONDS // 2
    fixture.files.on_stop = lambda: now.__setitem__(0, now[0] + late)
    status = behavior_native_sessions._source_session_status
    monkeypatch.setattr(behavior_native_sessions, "_source_session_status", lambda *_a, **_k: "active")
    caplog.set_level(logging.WARNING)
    outcome, reason, _ = cut(fixture)
    assert (outcome, reason) == ("stopping", "drive_capped")
    assert caplog.text.count("NATIVE_UPGRADE_CUT_OVERDUE") == 1
    assert [call for call in fixture.files.calls if call[0] == "stop"] == [("stop", fixture.source.session_id)] * 2
    assert state(fixture)["environment"]["state"] == "pending" and abandon.row(fixture).upgrade.stop_started
    fixture.files.on_stop = None
    monkeypatch.setattr(behavior_native_sessions, "_source_session_status", status)
    assert cut(fixture) == ("ready", None, None)  # the next trigger drives it on
    _moved_with_files(fixture)


def test_a_stop_whose_record_was_lost_is_never_withdrawn_and_its_files_arrive(file_api, monkeypatch):
    from shared_context.behavior_session_files import FollowFiles
    from shared_context.errors import Problem

    fixture = ready(file_api, monkeypatch)
    record, lost = FollowFiles.record_stop, []

    def lost_once(self):
        if not lost:
            lost.append(1)
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE the stop record was lost")
        return record(self)

    monkeypatch.setattr(FollowFiles, "record_stop", lost_once)
    outcome, _, _ = cut(fixture)
    assert outcome == "ready" and lost == [1]
    assert fixture.files.stopped == {fixture.source.session_id}
    _moved_with_files(fixture)


def test_a_crash_between_the_marker_and_fas_stop_is_driven_forward(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    stopping = behavior_native_sessions._CutFence.stopping

    def crash_after_marker(self):
        stopping(self)
        raise Crash()

    monkeypatch.setattr(behavior_native_sessions._CutFence, "stopping", crash_after_marker)
    with pytest.raises(Crash):
        cut(fixture)
    assert abandon.row(fixture).upgrade.stop_started and not fixture.files.stopped
    monkeypatch.setattr(behavior_native_sessions._CutFence, "stopping", stopping)
    monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", -1)  # a marked fence is never withdrawn
    assert cut(fixture) == ("ready", None, None)
    assert fixture.files.stopped == {fixture.source.session_id}
    _moved_with_files(fixture)


def _following(response):
    assert response.status_code in {409, 503}, response.text
    error = response.json()["error"]
    assert error.get("not_dispatched") is True and error.get("server_following") is True, response.text


def test_a_send_after_a_lost_stop_record_drives_the_fence_and_the_files_arrive(file_api, monkeypatch):
    from shared_context.behavior_session_files import FollowFiles

    fixture = ready(file_api, monkeypatch)
    record = FollowFiles.record_stop

    def crash(_self):
        raise Crash()

    monkeypatch.setattr(FollowFiles, "record_stop", crash)  # the platform accepted FA's stop; the API died here
    with pytest.raises(Crash):
        cut(fixture)
    assert fixture.files.stopped == {fixture.source.session_id} and abandon.row(fixture).upgrade.stop_started
    monkeypatch.setattr(FollowFiles, "record_stop", record)
    without_files = []
    monkeypatch.setattr(NativeSessions, "without_files", lambda *args, **kwargs: without_files.append(1))
    # The first send meets the fence: the send path drives it (no no-files commit) and asks for the same RID again.
    _following(submit_on_source(fixture, "after-lost-record"))
    assert without_files == []
    _moved_with_files(fixture)
    _sends_admit(fixture, monkeypatch, "after-lost-record")


def test_a_resend_after_a_capped_drive_with_the_selector_rolled_back_commits(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    now = abandon.clock(monkeypatch)
    late = behavior_native_sessions.CUT_FENCE_SECONDS + behavior_native_sessions.CUT_DRIVE_SECONDS // 2
    fixture.files.on_stop = lambda: now.__setitem__(0, now[0] + late)
    status = behavior_native_sessions._source_session_status
    monkeypatch.setattr(behavior_native_sessions, "_source_session_status", lambda *_a, **_k: "active")
    assert cut(fixture)[:2] == ("stopping", "drive_capped")
    fixture.files.on_stop = None
    monkeypatch.setattr(behavior_native_sessions, "_source_session_status", status)
    on_source_version(fixture, monkeypatch)  # V_rec is the selected version again: no turn end comes, only resends
    _following(submit_on_source(fixture, "after-capped"))
    _moved_with_files(fixture)
    _sends_admit(fixture, monkeypatch, "after-capped")


def test_a_source_deleted_after_fas_stop_switches_without_files_and_says_so(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    abandon.platform(fixture, monkeypatch, status="missing")
    downloads = len([call for call in fixture.files.calls if call[0] == "get"])
    assert cut(fixture) == ("stopping", None, None)  # one 404 is not a confirmation (§7a (m))
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
    assert cut(fixture) == ("abandoned", None, None)
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.upgrade is None
    assert switched.switch_notice == "source_retired" and switched.last_switch.files_migrated is False
    assert (switched.last_switch.basis, switched.last_switch.source_status) == ("retired", "missing")
    assert len([call for call in fixture.files.calls if call[0] == "get"]) == downloads
    assert state(fixture)["hosted_session_id"] == fixture.destination.session_id


def _failing_after_fas_stop(fixture, monkeypatch, status, code, *, times=None, wake=False):
    """A crash after FA's stop, then the stopped copy fails with ``code`` (``times`` times, or always); ``wake``: the
    source is awake again when it fails."""
    from shared_context.behavior_session_files import FollowFiles
    from shared_context.errors import Problem

    _crash_after_fas_stop(fixture, monkeypatch)
    stopped_step, failed = FollowFiles.stopped_step, []

    def failing(self):
        if times is None or len(failed) < times:
            failed.append(code)
            if wake:
                fixture.files.stopped.discard(fixture.source.session_id)
            raise Problem(status, code, "OFFLINE the stopped copy failed")
        return stopped_step(self)

    monkeypatch.setattr(FollowFiles, "stopped_step", failing)
    return failed


def _moved_without_files(fixture, basis):
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.upgrade is None
    assert switched.last_switch.files_migrated is False
    assert (switched.last_switch.basis, switched.last_switch.source_status) == (basis, "idle")
    assert switched.switch_notice == "without_files" and not switched.retired(fixture.source.session_id)
    current = state(fixture)
    assert current["hosted_session_id"] == fixture.destination.session_id
    assert current["environment"]["files_migrated"] is False
    assert not (fixture.target_workspace.root / "nested" / "draft.txt").exists()


@pytest.mark.parametrize("status, code", [
    (409, "NATIVE_FILES_CONFLICT"), (413, "NATIVE_FILES_LIMIT"), (409, "NATIVE_FILES_UNSAFE"),
    (409, "NATIVE_FILES_SOURCE_CHANGED"), (409, "NATIVE_FILES_UNSUPPORTED"),
])
def test_an_unmovable_error_is_confirmed_once_then_commits_without_files(file_api, monkeypatch, caplog, status, code):
    fixture = ready(file_api, monkeypatch)
    failed = _failing_after_fas_stop(fixture, monkeypatch, status, code)
    now = abandon.clock(monkeypatch)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("stopping", None, None)  # one read is not a confirmation
    assert "NATIVE_UPGRADE_CUT_FILES_UNMOVABLE_UNCONFIRMED code=" + code in caplog.text
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS - 1
    assert cut(fixture) == ("stopping", None, None)
    now[0] += 1
    assert cut(fixture) == ("abandoned", None, None)
    assert failed == [code] * 3
    assert "NATIVE_UPGRADE_CUT_FILES_UNMOVABLE code=%s basis=files_unmovable" % code in caplog.text
    _moved_without_files(fixture, "files_unmovable")
    _sends_admit(fixture, monkeypatch, "after-unmovable")


@pytest.mark.parametrize("code", ["NATIVE_FILES_SOURCE_UNAVAILABLE", "NATIVE_FILES_UNAVAILABLE"])
def test_a_woken_source_after_fas_stop_waits_and_never_commits_without_files(file_api, monkeypatch, caplog, code):
    """After FA's stop with the staging verified, and the source's metadata then reading it active, these codes only
    say the source is awake again: not permanent, no clock, never ``_without_moved_files``; the next pass stops it
    again and publishes from the staging."""
    fixture = ready(file_api, monkeypatch)
    failed = _failing_after_fas_stop(fixture, monkeypatch, 409, code, times=3, wake=True)
    without = []
    monkeypatch.setattr(NativeSessions, "_without_moved_files", lambda *args: without.append(1) or "abandoned")
    now = abandon.clock(monkeypatch)
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        assert cut(fixture) == ("stopping", None, None)
        now[0] += behavior_native_sessions.CUT_TRANSIENT_SECONDS + 1
    assert "NATIVE_UPGRADE_CUT_SOURCE_WOKEN code=" + code in caplog.text
    assert "NATIVE_UPGRADE_CUT_FILES_UNMOVABLE" not in caplog.text and without == [] and failed == [code] * 3
    assert cut(fixture) == ("ready", None, None)
    _moved_with_files(fixture)


def test_b_a_fence_whose_stopped_source_woke_on_the_selected_version_commits_with_its_files(file_api, monkeypatch):
    """Dev 2026-10-16, Agent B's exact state: FA's stop recorded, the staging verified, the fence's ``stop_started``
    marked, one drive pass already failed on the woken source, and the source now active on another version (the
    selected one, not its pinned one). The next pass stops it again and commits with the files from the staging."""
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 409, "NATIVE_FILES_SOURCE_UNAVAILABLE", times=1)
    enforcing(fixture, monkeypatch)  # B ran with 2d-i on: its clocks are durable on the row
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("stopping", None, None)
    sid = fixture.source.session_id
    fixture.files.stopped.discard(sid)  # woken (by the old code's stopped-session Files GET) on the selected version
    fixture.files.targets[sid] = fixture.source.model_copy(update={"agent_version": fixture.destination.agent_version})
    assert abandon.row(fixture).upgrade.stop_started and state(fixture)["environment"]["state"] == "pending"
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
    restarted(fixture)  # the next deploy
    stops, mark = sum(call[0] == "stop" for call in fixture.files.calls), len(fixture.files.calls)
    assert cut(fixture) == ("ready", None, None)
    _moved_with_files(fixture)
    assert sum(call[0] == "stop" for call in fixture.files.calls) == stops + 1  # stopped again, once
    assert source_calls(fixture, since=(0, mark))[1] == [] and fixture.files.woke == []


@pytest.mark.parametrize("status, code", [
    (409, "NATIVE_FILES_UNAVAILABLE"),  # the destination's own `admitting` or execution lock is not idle
    (409, "NATIVE_FILES_SOURCE_UNAVAILABLE"),
])
def test_a_woken_code_while_the_source_stays_stopped_exits_through_the_bounded_pair(
        file_api, monkeypatch, caplog, status, code):
    """Review P2: the code alone is not a wake. With the source still idle it is the destination's own failure, so it
    is confirmed like any unmovable error and the fence commits without files, instead of waiting forever."""
    fixture = ready(file_api, monkeypatch)
    failed = _failing_after_fas_stop(fixture, monkeypatch, status, code)
    now = abandon.clock(monkeypatch)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("stopping", None, None)
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS
    assert cut(fixture) == ("abandoned", None, None)
    assert failed == [code] * 2 and "NATIVE_UPGRADE_CUT_SOURCE_WOKEN" not in caplog.text
    _moved_without_files(fixture, "files_unmovable")


def test_a_transient_destination_oserror_503_does_not_commit_without_files_after_five_seconds(file_api, monkeypatch):
    """Review: a 503 ``NATIVE_FILES_UNAVAILABLE`` is the destination's staging OSError, which may clear. It waits under
    the 30-minute drive bound instead of the 5-second unmovable pair, and the files arrive once it clears."""
    fixture = ready(file_api, monkeypatch)
    failed = _failing_after_fas_stop(fixture, monkeypatch, 503, "NATIVE_FILES_UNAVAILABLE", times=2)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("stopping", None, None)
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
    assert cut(fixture) == ("stopping", None, None)
    assert cut(fixture) == ("ready", None, None)
    assert len(failed) == 2
    _moved_with_files(fixture)


def test_a_persistent_destination_oserror_503_exits_by_the_drive_bound(file_api, monkeypatch, caplog):
    """The same 503 that never clears still ends: past ``CUT_TRANSIENT_SECONDS`` the fence commits without files
    (``files_overdue``), so no Agent waits on it forever."""
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 503, "NATIVE_FILES_UNAVAILABLE")
    now = abandon.clock(monkeypatch)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("stopping", None, None)
    now[0] += behavior_native_sessions.CUT_TRANSIENT_SECONDS
    assert cut(fixture) == ("stopping", None, None)
    now[0] += 1
    assert cut(fixture) == ("abandoned", None, None)
    assert "basis=files_overdue" in caplog.text and "NATIVE_UPGRADE_CUT_SOURCE_WOKEN" not in caplog.text
    _moved_without_files(fixture, "files_overdue")


def _woken_on(fixture, version):
    """The stopped source is awake again on ``version`` (dev 2026-10-16: B's source on the selected version)."""
    sid = fixture.source.session_id
    fixture.files.stopped.discard(sid)
    fixture.files.targets[sid] = fixture.source.model_copy(update={"agent_version": version})


def test_errors_before_the_source_woke_on_another_version_do_not_count_toward_the_bound(file_api, monkeypatch):
    """Review P1: the first pass that sees the source awake on another version clears the drive clocks, so a
    transient error after the wake waits its own 30 minutes instead of committing without files at once."""
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 503, "NATIVE_FILES_UNKNOWN", times=3)
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    without = []
    monkeypatch.setattr(NativeSessions, "_without_moved_files", lambda *args: without.append(1) or "abandoned")
    assert cut(fixture) == ("stopping", None, None)
    first = abandon.row(fixture).clock("drive").since
    now[0] += behavior_native_sessions.CUT_TRANSIENT_SECONDS - 100
    _woken_on(fixture, fixture.destination.agent_version)
    assert cut(fixture) == ("stopping", None, None)  # cleared by the wake, started again by this pass's error
    assert abandon.row(fixture).clock("drive").since != first
    now[0] += 200  # past 30 minutes from the first error, not from the wake
    assert cut(fixture) == ("stopping", None, None) and without == []
    assert cut(fixture) == ("ready", None, None)
    _moved_with_files(fixture)


def test_a_source_that_stays_on_another_version_still_reaches_the_bound(file_api, monkeypatch, caplog):
    """The wake clears the clocks once: transient errors that go on for 30 minutes after it still commit without
    files, so no Agent waits on the fence forever."""
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 503, "NATIVE_FILES_UNKNOWN")
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    caplog.set_level(logging.WARNING)
    _woken_on(fixture, fixture.destination.agent_version)
    assert cut(fixture) == ("stopping", None, None)
    now[0] += behavior_native_sessions.CUT_TRANSIENT_SECONDS
    assert cut(fixture) == ("stopping", None, None)
    now[0] += 1
    assert cut(fixture) == ("abandoned", None, None)
    assert "NATIVE_UPGRADE_CUT_FILES_UNMOVABLE code=NATIVE_FILES_UNKNOWN basis=files_overdue" in caplog.text


def test_b_an_old_drive_clock_then_a_metadata_5xx_then_the_woken_source_commits_with_the_same_files(
        file_api, monkeypatch):
    """Agent B: its durable drive clock started at 03:02 and no pass ran since (frozen, then the deploy). The first
    pass after that gap meets a transient 5xx on the source's metadata read before any staging check: the clock starts
    again instead of committing without files; the next pass finds the source awake on the selected version and
    commits with the files of the same manifest."""
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 503, "NATIVE_FILES_UNKNOWN", times=1)
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("stopping", None, None)
    assert abandon.row(fixture).clock("drive") is not None
    digest = hashlib.sha256((fixture.workspace.root / "nested" / "draft.txt").read_bytes()).hexdigest()
    now[0] += 3 * 3600
    restarted(fixture)  # the deploy
    abandon.platform(fixture, monkeypatch, status="unavailable")
    assert cut(fixture) == ("stopping", None, None)
    assert abandon.row(fixture).upgrade.stop_started and state(fixture)["environment"]["state"] == "pending"
    abandon.platform(fixture, monkeypatch)
    _woken_on(fixture, fixture.destination.agent_version)
    assert cut(fixture) == ("ready", None, None)
    _moved_with_files(fixture)
    assert abandon.row(fixture).clocks == ()
    moved = fixture.target_workspace.root / "nested" / "draft.txt"
    assert hashlib.sha256(moved.read_bytes()).hexdigest() == digest


def test_b_an_old_unmovable_clock_and_a_destination_read_error_on_the_woken_pass_keep_the_files(
        file_api, monkeypatch):
    """Agent B may also hold an ``unmovable`` clock from 03:02. On the first pass after the deploy the source is seen
    on another version before the destination's files progress is read, so a destination-side unmovable code from
    that read starts a new pair instead of meeting the old clock; the next pass commits with the files."""
    from shared_context.behavior_session_files import FollowFiles
    from shared_context.errors import Problem

    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 409, "NATIVE_FILES_CONFLICT", times=1)
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("stopping", None, None)
    assert abandon.row(fixture).clock("unmovable") is not None
    now[0] += 3 * 3600
    restarted(fixture)
    _woken_on(fixture, fixture.destination.agent_version)
    step, failed = FollowFiles.step, []

    def busy_once(self, stage, **kwargs):
        if stage == "read" and not failed:
            failed.append(stage)
            raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "OFFLINE original admission is not idle")
        return step(self, stage, **kwargs)

    monkeypatch.setattr(FollowFiles, "step", busy_once)
    assert cut(fixture) == ("stopping", None, None)
    assert failed == ["read"]
    assert cut(fixture) == ("ready", None, None)
    _moved_with_files(fixture)


def test_b_a_wake_clear_whose_table_write_failed_is_retried_before_any_clock_is_read(file_api, monkeypatch):
    """Review: the wake's clear goes through ``_binding``, which swallows a Table failure. Counted as done anyway, the
    pass's unmovable code would meet B's old 03:02 clock and commit without files at once. Unwritten, the pass waits
    and the next pass clears again before the destination is read, so the code starts a new pair."""
    from shared_context.behavior_session_files import FollowFiles
    from shared_context.errors import Problem

    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 409, "NATIVE_FILES_CONFLICT", times=1)
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("stopping", None, None)
    old = abandon.row(fixture).clock("unmovable").since
    now[0] += 3 * 3600
    restarted(fixture)
    _woken_on(fixture, fixture.destination.agent_version)
    record_clock, clears = NativeBindingStore.record_clock, []

    def failing_once(self, agent_id, *, kind, subject, **kwargs):
        if kind == "unmovable" and subject is None:
            clears.append(kind)
            if len(clears) == 1:
                raise Problem(503, "TABLE_UNAVAILABLE", "OFFLINE the clear's write failed")
        return record_clock(self, agent_id, kind=kind, subject=subject, **kwargs)

    step, failed = FollowFiles.step, []

    def conflict_once(self, stage, **kwargs):
        if stage == "read" and not failed:
            failed.append(stage)
            raise Problem(409, "NATIVE_FILES_CONFLICT", "OFFLINE the destination's read failed")
        return step(self, stage, **kwargs)

    monkeypatch.setattr(NativeBindingStore, "record_clock", failing_once)
    monkeypatch.setattr(FollowFiles, "step", conflict_once)
    without = []
    monkeypatch.setattr(NativeSessions, "_without_moved_files", lambda *args: without.append(1) or "abandoned")
    assert cut(fixture) == ("stopping", None, None)  # the clear failed: nothing read, no clock read
    assert failed == [] and abandon.row(fixture).clock("unmovable").since == old
    assert cut(fixture) == ("stopping", None, None)  # cleared again, then the conflict starts a new pair
    assert len(clears) >= 2 and failed == ["read"] and without == []
    assert abandon.row(fixture).clock("unmovable").since != old
    assert cut(fixture) == ("ready", None, None)
    _moved_with_files(fixture)


def test_a_send_confirms_an_unmovable_error_and_the_resend_is_admitted(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 409, "NATIVE_FILES_CONFLICT")
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("stopping", None, None)
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS
    _following(submit_on_source(fixture, "after-conflict"))  # the send's own pass commits the fence without files
    _moved_without_files(fixture, "files_unmovable")
    _sends_admit(fixture, monkeypatch, "after-conflict")


@pytest.mark.parametrize("status, code", [
    (503, "NATIVE_FILES_UNKNOWN"), (503, "NATIVE_FILES_TIMEOUT"), (403, "NATIVE_FILES_ACCESS_DENIED")])
def test_a_transient_error_keeps_the_fence_and_the_next_trigger_moves_the_files(file_api, monkeypatch, status, code):
    fixture = ready(file_api, monkeypatch)
    failed = _failing_after_fas_stop(fixture, monkeypatch, status, code, times=2)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("stopping", None, None)
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1  # a transient error is never "confirmed"
    assert cut(fixture) == ("stopping", None, None)
    assert state(fixture)["environment"]["state"] == "pending" and abandon.row(fixture).upgrade.stop_started
    assert cut(fixture) == ("ready", None, None)
    assert len(failed) == 2
    _moved_with_files(fixture)


def test_transient_errors_past_thirty_minutes_commit_without_files(file_api, monkeypatch, caplog):
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 503, "NATIVE_FILES_UNKNOWN")
    now = abandon.clock(monkeypatch)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("stopping", None, None)
    now[0] += behavior_native_sessions.CUT_TRANSIENT_SECONDS
    assert cut(fixture) == ("stopping", None, None)
    now[0] += 1
    assert cut(fixture) == ("abandoned", None, None)
    assert "NATIVE_UPGRADE_CUT_FILES_UNMOVABLE code=NATIVE_FILES_UNKNOWN basis=files_overdue" in caplog.text
    _moved_without_files(fixture, "files_overdue")
    _sends_admit(fixture, monkeypatch, "after-overdue")


def test_a_send_meeting_a_slow_download_after_fas_stop_is_admitted_once_its_own_resend_moves_the_files(
        file_api, monkeypatch):
    """P6: the send's first pass meets the slow download and keeps the fence (no no-files commit); the in-request
    wait resends the same RID, whose pass moves the files and commits, and the message is admitted on the new SID in
    that same request."""
    fixture = ready(file_api, monkeypatch)
    sessions(fixture).guard.enforce = True
    failed = _failing_after_fas_stop(fixture, monkeypatch, 503, "NATIVE_FILES_TIMEOUT", times=1)
    without_files = []
    monkeypatch.setattr(NativeSessions, "without_files", lambda *args, **kwargs: without_files.append(1))
    sent = submit_on_source(fixture, "after-slow")
    assert sent.status_code == 202 and sent.json()["hosted_session_id"] == fixture.destination.session_id, sent.text
    assert without_files == [] and failed == ["NATIVE_FILES_TIMEOUT"]
    _moved_with_files(fixture)


def _slow_download_once(monkeypatch):
    from shared_context.behavior_session_files import SessionFiles
    from shared_context.errors import Problem

    download, slow = SessionFiles.download, []

    def slow_once(self, *args, **kwargs):
        if not slow:
            slow.append(1)
            raise Problem(503, "NATIVE_FILES_TIMEOUT", "OFFLINE the file stream ran past its step deadline")
        return download(self, *args, **kwargs)

    monkeypatch.setattr(SessionFiles, "download", slow_once)
    return slow


def _rowless(fixture, monkeypatch):
    """2d-i turns every follow entry off for a bound Agent (P6); the automatic follow stays only for an Agent whose
    binding row can not be written (here every claim fails), so its slow-download rule is exercised on that one."""
    from shared_context.errors import Problem

    def unwritable(*_args, **_kwargs):
        raise Problem(503, "TABLE_UNAVAILABLE", "OFFLINE the binding row can not be written")

    abandon.platform(fixture, monkeypatch)
    monkeypatch.setattr(NativeBindingStore, "claim", unwritable)
    sessions(fixture).guard.enforce = True
    return fixture


def test_with_the_flag_on_a_sends_own_follow_keeps_a_slow_download_pending(file_api, monkeypatch):
    """No row, so no fence: the automatic follow (``advance``; here through its own endpoint) moves the files itself.
    A slow download keeps its pending (no no-files commit) and answers that pending view, never 503
    NATIVE_FILES_TIMEOUT (P6: a send answers ``server_following``); the next one moves them."""
    from test_native_automatic_environment import advance

    fixture = _rowless(file_api, monkeypatch)
    slow = _slow_download_once(monkeypatch)
    waiting = advance(fixture)
    assert waiting.status_code == 200, waiting.text
    assert (waiting.json()["state"], waiting.json()["reason"]) == ("pending", "files_pending")
    assert slow == [1] and abandon.row(fixture) is None
    assert state(fixture)["environment"]["state"] == "pending"
    moved = advance(fixture)
    assert moved.status_code == 200 and moved.json()["state"] == "ready", moved.text
    _moved_with_files(fixture)


def _resent_until_admitted(fixture, rid):
    """The workbench resends the same RID while the server follows."""
    for _ in range(3):
        sent = submit_on_source(fixture, rid)
        if sent.status_code == 202:
            assert sent.json()["hosted_session_id"] == fixture.destination.session_id
            return
        _following(sent)
    raise AssertionError(sent.text)


def test_with_the_flag_on_a_follows_slow_downloads_past_thirty_minutes_commit_without_files(file_api, monkeypatch):
    from shared_context.behavior_session_files import SessionFiles
    from shared_context.errors import Problem
    from test_native_automatic_environment import advance

    fixture = _rowless(file_api, monkeypatch)
    now = abandon.clock(monkeypatch)

    def slow(*_args, **_kwargs):
        raise Problem(503, "NATIVE_FILES_TIMEOUT", "OFFLINE the file stream ran past its step deadline")

    monkeypatch.setattr(SessionFiles, "download", slow)
    assert advance(fixture).json()["state"] == "pending"
    now[0] += behavior_native_sessions.CUT_TRANSIENT_SECONDS
    assert advance(fixture).json()["state"] == "pending"
    now[0] += 1
    moved = advance(fixture)
    assert moved.status_code == 200 and moved.json()["state"] == "ready", moved.text
    assert moved.json()["files_migrated"] is False
    assert sessions(fixture)._drive_failing == {}  # a later follow of the same RID starts its own clock


def test_with_the_flag_off_a_sends_slow_download_still_commits_without_files(file_api, monkeypatch):
    """Today's follow, unchanged by 2d-i while it is off (main raised NATIVE_FILES_CONFLICT here, the same path)."""
    fixture = ready(file_api, monkeypatch)
    _slow_download_once(monkeypatch)
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: fixture.destination.agent_version)
    _resent_until_admitted(fixture, "slow-send")
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id
    assert switched.last_switch.files_migrated is False


def test_a_marked_fence_that_committed_unrecorded_is_repaired(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)

    def crash(*_args, **_kwargs):
        raise Crash()

    _crash_after_fas_stop(fixture, monkeypatch)
    monkeypatch.setattr(NativeSessions, "_confirm_environment", crash)
    with pytest.raises(Crash):
        cut(fixture)  # Hosted committed; the row still names the source and its marked fence
    assert state(fixture)["hosted_session_id"] == fixture.destination.session_id
    assert abandon.row(fixture).upgrade.stop_started
    monkeypatch.setattr(NativeSessions, "_confirm_environment", _CONFIRM)
    assert cut(fixture) == ("repaired", None, None)
    _moved_with_files(fixture)


def test_a_marked_fence_lost_without_a_commit_is_cleared_and_the_next_cut_starts(file_api, monkeypatch, caplog):
    from shared_context.native_binding import NativeBindingUpgrade

    fixture = ready(file_api, monkeypatch)
    store, agent_id, source_sid = abandon.store(fixture), fixture.source.agent_id, fixture.source.session_id
    fence = NativeBindingUpgrade(
        hosted_session_id=fixture.destination.session_id, agent_version=fixture.destination.agent_version,
        request_id=behavior_native_sessions.cut_request_id(agent_id, source_sid, fixture.destination.session_id),
        started_at=store._now())
    store.record_upgrade(agent_id, hosted_session_id=source_sid, upgrade=fence)
    store.mark_stop(agent_id, hosted_session_id=source_sid, upgrade=fence)
    before, start = state(fixture), len(fixture.controls)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("fence_lost", None, None)  # Hosted holds no pending and the source is still current
    assert "NATIVE_UPGRADE_CUT_FENCE_LOST session_id=" + fixture.destination.session_id in caplog.text
    row = abandon.row(fixture)
    assert row.upgrade is None and row.hosted_session_id == source_sid and row.abandon is None
    assert state(fixture) == before and ("session_withdraw", None) not in operations(fixture, start)
    assert not fixture.files.stopped
    assert cut(fixture)[0] == "ready"
    _moved_with_files(fixture)


def test_a_waiting_pass_keeps_the_transient_clock(file_api, monkeypatch):
    """Thirty minutes of failing count from the first error: a pass that only waits (the source ``updating``) does not
    restart them."""
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 503, "NATIVE_FILES_UNKNOWN")
    now = abandon.clock(monkeypatch)
    status = ["idle"]
    abandon.platform(fixture, monkeypatch, status=lambda: status[0])
    assert cut(fixture) == ("stopping", None, None)
    now[0] += behavior_native_sessions.CUT_TRANSIENT_SECONDS - 10
    status[0] = "updating"
    assert cut(fixture) == ("stopping", None, None)
    status[0] = "idle"
    now[0] += 11
    assert cut(fixture) == ("abandoned", None, None)
    _moved_without_files(fixture, "files_overdue")


def test_a_files_commit_after_a_failed_no_files_attempt_records_the_moved_files(file_api, monkeypatch):
    """``_without_moved_files`` wrote its abandon decision, then its switch failed; the fence later committed with the
    files: the row says so, not the stale decision."""
    fixture = ready(file_api, monkeypatch)
    store, agent_id = abandon.store(fixture), fixture.source.agent_id
    store.decide_abandon(agent_id, hosted_session_id=fixture.source.session_id, basis="files_unmovable",
                         source_status="idle", created_agent_version=fixture.source.agent_version)
    switched = store.record_switch(agent_id, NativeEnvironmentView(
        agent_id=agent_id, request_id="moved", state="ready", files_migrated=True, files_manifest_sha256="0" * 64,
        previous_hosted_session_id=fixture.source.session_id, previous_agent_version=fixture.source.agent_version,
        hosted_session_id=fixture.destination.session_id, hosted_agent_version=fixture.destination.agent_version))
    assert switched.last_switch.files_migrated is True and switched.last_switch.basis is None
    assert switched.switch_notice == "updated" and switched.abandon is None


def test_two_replicas_cut_the_same_rid_and_only_one_drives(file_api, monkeypatch):
    from shared_context.behavior_native_turn_wire import NativeSessionCut

    fixture = ready(file_api, monkeypatch)

    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(NativeSessions, "follow", crash)
    with pytest.raises(Crash):
        cut(fixture)  # replica A wrote its fence and the pending, and is still copying
    fence = abandon.row(fixture).upgrade
    monkeypatch.setattr(NativeSessions, "follow", _FOLLOW)
    session, row = sessions(fixture), abandon.row(fixture)
    client = agent_client(fixture.case.composition, control(fixture))
    destination = session._destination(client, fixture.destination.agent_version, lambda: None, mark=False, row=row)
    # Replica B derives the same destination SID and cut RID as A's fence, so the row's CAS is what parts them.
    sid = destination.target.session_id
    assert sid == fence.hosted_session_id and fence.request_id == behavior_native_sessions.cut_request_id(
        fixture.source.agent_id, fixture.source.session_id, sid)
    command = NativeSessionCut(request_id=fence.request_id, expected_session_id=fixture.source.session_id,
                               expected_etag="0x0", files_required=True)
    runtime, start = fixture.case.composition.runtime, len(fixture.controls)
    earlier = runtime._now
    monkeypatch.setattr(runtime, "_now", lambda: earlier() + timedelta(seconds=1))  # B builds its record later
    # Replica B, the same RID: A's record stands, so B sends nothing and leaves the fence to A.
    assert session._fenced(client, destination, command, row, fixture.destination.agent_version, None) == (
        "fence_live", None, None)
    assert operations(fixture, start) == [] and abandon.row(fixture).upgrade == fence
    assert state(fixture)["environment"]["state"] == "pending"


def test_a_late_withdraw_after_another_replicas_stop_is_refused_by_hosted(file_api, monkeypatch, caplog):
    """B stopped the source (row marker and Hosted's files progress); A read the row before that and judges the fence
    its own to withdraw. Hosted refuses, A drives the fence instead, and the files arrive."""
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)  # replica B
    store, agent_id, source_sid = abandon.store(fixture), fixture.source.agent_id, fixture.source.session_id
    marked = abandon.row(fixture)
    stale = marked.model_copy(update={"upgrade": marked.upgrade.model_copy(update={"stop_started": False})})
    # Neither clearing nor the same fence again replaces B's marked record.
    store.record_upgrade(agent_id, hosted_session_id=source_sid, upgrade=None)
    store.record_upgrade(agent_id, hosted_session_id=source_sid, upgrade=stale.upgrade)
    assert abandon.row(fixture) == marked
    session = sessions(fixture)
    reads = [stale]
    binding_row = NativeSessions._binding_row
    # A's reads raced B's stop: its re-read and its files-progress read both come before B's marker and record.
    monkeypatch.setattr(NativeSessions, "_binding_row", lambda self, agent: (
        (store, reads.pop()) if reads else binding_row(self, agent)))
    monkeypatch.setattr(NativeSessions, "_stop_started", staticmethod(lambda destination, pending: False))
    client = agent_client(fixture.case.composition, control(fixture))
    reader = session._fence_reader(client, stale.upgrade)
    caplog.set_level(logging.WARNING)
    start = len(fixture.controls)
    assert session._settle(client, reader, session._state(reader), stale, own=True) == "ready"
    assert ("session_withdraw", None) in operations(fixture, start)
    assert "NATIVE_UPGRADE_WITHDRAW_REFUSED" in caplog.text
    _moved_with_files(fixture)


def test_a_send_whose_fence_pass_meets_a_concurrent_withdraw_is_delivered_once_to_the_source(
        file_api, monkeypatch, caplog):
    """Dev 2026-10-18, Agent B (2d-i, flag on): a send's pass over the young fence read the fence's ``pending``, then
    the background job withdrew it at its bound before the pass read the fence's files progress, so that read met
    409 ``NATIVE_ENVIRONMENT_CONFLICT`` (no pending any more). The row still recorded the fence (the job clears it
    after Hosted's withdraw). The send must not be refused: it goes to the source, once."""
    from shared_context.behavior_native_turn_api import NativeTurnGateway
    from shared_context.behavior_native_turn_wire import NativeSessionWithdraw

    fixture = ready(file_api, monkeypatch)
    _crashed_fence(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)  # the selector stays on the fence's version, as on dev
    stop_started, raced = NativeSessions._stop_started, []

    def withdrawn_meanwhile(destination, pending):
        if not raced:
            raced.append(pending.request_id)
            read = NativeTurnGateway._invoke(destination, NativeSessionRead())[0]
            NativeTurnGateway._invoke(destination, NativeSessionWithdraw(
                request_id="background-job-withdraw", expected_etag=read.etag,
                pending_request_id=pending.request_id, pending_session_id=pending.hosted_session_id))
        return stop_started(destination, pending)

    monkeypatch.setattr(NativeSessions, "_stop_started", staticmethod(withdrawn_meanwhile))
    caplog.set_level(logging.WARNING)
    start = len(fixture.controls)
    accepted = submit_on_source(fixture, "during-the-withdraw")
    assert raced, "the send's pass never read the fence's files progress"
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["hosted_session_id"] == fixture.source.session_id
    assert [op for op in operations(fixture, start) if op[0] == "submit"] == [("submit", None)]
    assert abandon.row(fixture).upgrade is None
    again = submit_on_source(fixture, "during-the-withdraw")
    # The same receipt (it has started meanwhile), not a second turn.
    assert again.status_code == 202 and again.json()["status"] == "started", again.text
    assert {key: value for key, value in again.json().items() if key not in {"status", "turn_number"}} == {
        key: value for key, value in accepted.json().items() if key not in {"status", "turn_number"}}
    assert [record for record in caplog.messages if "NATIVE_TURN_REFUSED" in record] == []
    assert "NATIVE_UPGRADE_CUT_FENCE_SETTLED_MEANWHILE" in caplog.text


def test_a_files_progress_conflict_while_the_same_pending_stays_is_still_refused(file_api, monkeypatch):
    from shared_context.errors import Problem

    fixture = ready(file_api, monkeypatch)
    _crashed_fence(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)

    def conflict(_destination, _pending):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original file operation or reader changed")

    monkeypatch.setattr(NativeSessions, "_stop_started", staticmethod(conflict))
    refused = submit_on_source(fixture, "genuine-conflict")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT", refused.text
    assert state(fixture)["environment"]["state"] == "pending" and abandon.row(fixture).upgrade is not None


def test_a_cut_trigger_that_was_never_scheduled_can_come_again():
    driver = native_cut.CutDriver(SimpleNamespace())

    def refused(*_args, **_kwargs):
        raise RuntimeError("cannot schedule new futures after shutdown")

    driver._pool.shutdown()
    driver._pool.submit = refused
    assert driver.turn_ended(None, "agent", "rid") is False
    assert driver._seen == {} and driver._running == set()


def test_a_slow_download_is_transient_not_a_conflict(monkeypatch):
    from contextlib import contextmanager
    import hashlib

    from shared_context import behavior_session_files
    from shared_context.errors import Problem

    now = [0.0]
    monkeypatch.setattr(behavior_session_files, "monotonic", lambda: now[0])

    def stream():
        yield b"a"
        now[0] += behavior_session_files.FILE_STEP_SECONDS + 1
        yield b"b"

    agents = SimpleNamespace(download_session_file=lambda name, sid, path: stream())
    monkeypatch.setattr(behavior_session_files.SessionFiles, "_project",
                        contextmanager(lambda self, sid: (yield agents)))
    files = behavior_session_files.SessionFiles(SimpleNamespace(target=SimpleNamespace(agent_name="agent")))
    entry = SimpleNamespace(byte_length=2, sha256=hashlib.sha256(b"ab").hexdigest())
    with pytest.raises(Problem) as slow:
        files.download("sid", "path", entry)
    assert (slow.value.status, slow.value.body["error"]["code"]) == (503, "NATIVE_FILES_TIMEOUT")
    assert "NATIVE_FILES_TIMEOUT" not in behavior_native_sessions._UNMOVABLE


def test_the_stopped_listing_pages_from_the_manifest_pass_the_hosted_gate():
    """``list_stopped`` pages built from the manifest (no Files call on the stopped source), more than one page per
    directory included, complete Hosted's ``advance_listing`` exactly as a real listing would."""
    from shared_context.behavior_session_files import _manifest_page
    from shared_context.native_files_wire import (
        NativeFileEntry, NativeFilesListing, NativeFilesManifest, advance_listing,
    )

    entries = [NativeFileEntry(path=name, kind="directory", mode=0o700) for name in ("d", "e")]
    entries += [NativeFileEntry(path="d/f%03d" % index, kind="file", mode=0o600, sha256="0" * 64)
                for index in range(230)]
    manifest = NativeFilesManifest(
        agent_id="agent", request_id="rid", source_session_id="s", source_agent_version="41",
        target_session_id="t", target_agent_version="42", root_mode=0o700,
        entries=tuple(sorted(entries, key=lambda entry: entry.path)))
    listing, pages = NativeFilesListing(), 0
    while not listing.complete:
        listing, pages = advance_listing(manifest, listing, _manifest_page(manifest, listing)), pages + 1
    assert pages == 1 + 3 + 1  # the root, "d" in three pages, the empty "e"


def test_j_a_crash_after_the_commit_is_recorded_by_the_rerun(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch, files=False)  # the direct commit (an idle source is not cut)

    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(NativeSessions, "_confirm_environment", crash)
    with pytest.raises(Crash):
        cut(fixture)
    assert state(fixture)["hosted_session_id"] == fixture.destination.session_id
    assert abandon.row(fixture).hosted_session_id == fixture.source.session_id
    monkeypatch.setattr(NativeSessions, "_confirm_environment", _CONFIRM)
    assert cut(fixture) == ("repaired", None, None)
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.earlier(fixture.source.session_id)


def test_j_a_commit_the_row_missed_is_recorded_while_the_source_is_idle(file_api, monkeypatch):
    """Review P3: an idle source returns ``source_idle`` only after a commit the row missed is settled (§7a (j))."""
    fixture = ready(file_api, monkeypatch, files=False)

    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(NativeSessions, "_confirm_environment", crash)
    with pytest.raises(Crash):
        cut(fixture)
    monkeypatch.setattr(NativeSessions, "_confirm_environment", _CONFIRM)
    fixture.files.stopped.add(fixture.source.session_id)  # reclaimed meanwhile
    calls = len(fixture.files.calls)
    assert cut(fixture) == ("repaired", None, None)
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.earlier(fixture.source.session_id)
    assert fixture.files.calls[calls:] == [] and fixture.files.woke == []


def test_a_legacy_pending_with_fas_stop_is_driven_while_the_source_is_idle(file_api, monkeypatch, caplog):
    """Review P3: with every follow entry off (2d-i), an older follow's pending whose files progress records FA's stop
    is driven to its commit with the files by the cut even though the source is idle."""
    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    # An older follow's pending: no fence on the row (``lost`` is the one way to clear a marked record).
    abandon.store(fixture).record_upgrade(fixture.source.agent_id, hosted_session_id=fixture.source.session_id,
                                          upgrade=None, lost=abandon.row(fixture).upgrade)
    assert abandon.row(fixture).upgrade is None and state(fixture)["environment"]["state"] == "pending"
    restarted(fixture)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("ready", None, None)
    assert "NATIVE_UPGRADE_LEGACY_PENDING_DRIVEN" in caplog.text
    _moved_with_files(fixture)
    assert fixture.files.woke == []


def test_an_admission_stuck_past_five_minutes_takes_the_abandon_path(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    now = abandon.clock(monkeypatch)
    set_admitting(fixture, "stuck-rid")
    assert cut(fixture) == ("busy", "admission_pending", None)
    now[0] += behavior_native_sessions.STUCK_ADMISSION_SECONDS - 1
    assert cut(fixture) == ("busy", "admission_pending", None)
    now[0] += 2
    assert cut(fixture) == ("abandoned", "stuck_admission", None)
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id
    assert (switched.last_switch.basis, switched.last_switch.source_status) == ("stuck_admission", "active")
    assert switched.switch_notice == "without_files" and not switched.retired(fixture.source.session_id)
    assert source_calls(fixture) == ([], [], [])
    assert "admission_request_id" not in state(fixture)


# --- durable bounds (2d-i P6) --------------------------------------------------------------------------------------

def restarted(fixture):
    """Another API replica, or this one after a restart: no in-process memory of the earlier attempts."""
    fixture.case.composition.__dict__.pop("_native_sessions", None)


def enforcing(fixture, monkeypatch):
    monkeypatch.setenv("FA_NATIVE_SESSION_GUARD_ENFORCE", "1")
    restarted(fixture)


def test_a_stuck_admission_bound_holds_across_a_restart(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    set_admitting(fixture, "stuck-rid")
    assert cut(fixture) == ("busy", "admission_pending", None)
    assert abandon.row(fixture).clock("admitting").subject == "stuck-rid"
    restarted(fixture)
    now[0] += behavior_native_sessions.STUCK_ADMISSION_SECONDS + 1
    assert cut(fixture) == ("abandoned", "stuck_admission", None)
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.clocks == ()


def test_the_transient_files_bound_holds_across_a_restart(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 503, "NATIVE_FILES_UNKNOWN")
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("stopping", None, None)
    restarted(fixture)
    now[0] += behavior_native_sessions.CUT_TRANSIENT_SECONDS + 1
    assert cut(fixture) == ("abandoned", None, None)
    _moved_without_files(fixture, "files_overdue")


def test_the_unmovable_confirmation_holds_across_a_restart(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _failing_after_fas_stop(fixture, monkeypatch, 409, "NATIVE_FILES_CONFLICT")
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("stopping", None, None)
    restarted(fixture)
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS
    assert cut(fixture) == ("abandoned", None, None)
    _moved_without_files(fixture, "files_unmovable")


def test_the_retired_pair_holds_across_a_restart(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    abandon.platform(fixture, monkeypatch, status="missing")
    assert cut(fixture) == ("stopping", None, None)
    assert abandon.row(fixture).clock("retired").subject == fixture.source.session_id
    restarted(fixture)
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
    assert cut(fixture) == ("abandoned", None, None)
    assert abandon.row(fixture).switch_notice == "source_retired"


def test_without_the_flag_the_bounds_stay_in_memory_and_the_row_has_no_clocks(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    now = abandon.clock(monkeypatch)
    set_admitting(fixture, "stuck-rid")
    assert cut(fixture) == ("busy", "admission_pending", None)
    assert "clocks" not in abandon.row(fixture).model_dump(mode="json")
    restarted(fixture)  # main's memory: a restart starts the bound again
    now[0] += behavior_native_sessions.STUCK_ADMISSION_SECONDS + 1
    assert cut(fixture) == ("busy", "admission_pending", None)


# --- P9 hard gates, the P8 gap, the deploy hook (2d-i P6) ----------------------------------------------------------

def _crashed_fence(fixture, monkeypatch):
    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(NativeSessions, "follow", crash)
    with pytest.raises(Crash):
        cut(fixture)
    monkeypatch.setattr(NativeSessions, "follow", _FOLLOW)
    assert state(fixture)["environment"]["state"] == "pending" and abandon.row(fixture).upgrade is not None


@pytest.mark.parametrize("status, notice", [
    ("updating", "without_files"), ("unavailable", "without_files"), ("deleting", "source_retired")])
def test_limit_3_a_marked_fence_whose_source_stays_unsettled_commits_without_files_past_the_bound(
        file_api, monkeypatch, caplog, status, notice):
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    abandon.platform(fixture, monkeypatch, status=status)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("stopping", None, None)
    assert abandon.row(fixture).clock("drive") is not None  # bounded, and durable
    now[0] += behavior_native_sessions.CUT_TRANSIENT_SECONDS
    assert cut(fixture) == ("stopping", None, None)
    now[0] += 1
    assert cut(fixture) == ("abandoned", None, None)
    assert "NATIVE_UPGRADE_CUT_SOURCE_UNSETTLED_OVERDUE agent_id=" + fixture.source.agent_id in caplog.text
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.upgrade is None
    assert switched.last_switch.files_migrated is False and switched.switch_notice == notice
    assert not (fixture.target_workspace.root / "nested" / "draft.txt").exists()


def _broken_destination(fixture, monkeypatch, *, retired=True):
    """The fence's destination answers no invocation (state, Files progress, withdraw). ``retired``: its platform
    metadata reads 404 too (a definite failure); otherwise it reads active, as in a platform 5xx outage. Any other
    session of the selected version (a fresh M5 SID) is created on the platform double and answered by the same
    image over the same shared store (the offline Hosted runtime)."""
    from shared_context.behavior_foundry import FoundryHttpClient
    from shared_context.behavior_native_turn_api import NativeTurnGateway
    from shared_context.errors import Problem

    invoke_, create_ = NativeTurnGateway._invoke, FoundryHttpClient.create_session
    broken_sid, known = fixture.destination.session_id, {fixture.destination.session_id, fixture.source.session_id}

    def broken(client, command):
        if client.target.session_id == broken_sid:
            raise Problem(503, "FOUNDRY_INVOCATION_UNKNOWN", "OFFLINE the fence's destination does not answer")
        if client.target.session_id not in known:
            client = client.for_target(client.target.model_copy(update={"session_id": broken_sid}))
        return invoke_(client, command)

    def create(client, *, before_send):
        if client.target.session_id in known:
            return create_(client, before_send=before_send)
        before_send()
        fixture.files.targets[client.target.session_id] = client.target

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(broken))
    monkeypatch.setattr(FoundryHttpClient, "create_session", create)
    if retired:
        fixture.files.targets.pop(broken_sid, None)
    return broken_sid


def test_limit_4_sends_wait_while_the_fence_destination_does_not_answer(file_api, monkeypatch, caplog):
    from shared_context import behavior_native_turn_api

    fixture = ready(file_api, monkeypatch)
    _crashed_fence(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    _broken_destination(fixture, monkeypatch)
    monkeypatch.setattr(behavior_native_turn_api, "IN_REQUEST_WAIT_SECONDS", 0)
    caplog.set_level(logging.WARNING)
    _following(submit_on_source(fixture, "behind-broken"))  # never a refusal
    assert "NATIVE_UPGRADE_CUT_FENCE_BROKEN agent_id=" + fixture.source.agent_id in caplog.text
    row = abandon.row(fixture)
    assert row.clock("fence_broken").subject == row.upgrade.request_id


def test_limit_4_past_its_bound_an_unmarked_fence_is_withdrawn_by_a_fresh_sid_and_keeps_its_files(
        file_api, monkeypatch, caplog):
    """FA never stopped the source, so its files are movable: the fence is withdrawn (never the no-files abandon) by
    a fresh session of the selected version, never the broken one, and the old SID is current again."""
    fixture = ready(file_api, monkeypatch)
    _crashed_fence(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    broken_sid = _broken_destination(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("fence_broken", None, None)
    now[0] += behavior_native_sessions.FENCE_BROKEN_SECONDS
    assert cut(fixture) == ("withdrawn", None, None)
    assert "NATIVE_UPGRADE_CUT_FENCE_BROKEN_WITHDRAWN agent_id=" + fixture.source.agent_id in caplog.text
    kept = abandon.row(fixture)
    assert (kept.hosted_session_id, kept.upgrade, kept.abandon) == (fixture.source.session_id, None, None)
    assert kept.attempt(fixture.destination.agent_version) == 1  # the next destination is never the broken SID
    current = state(fixture)
    assert current["hosted_session_id"] == fixture.source.session_id and current.get("environment") is None
    assert not fixture.files.stopped and (fixture.workspace.root / "nested" / "draft.txt").exists()
    assert broken_sid != sessions(fixture).destination_session(
        agent_client(fixture.case.composition, control(fixture)), fixture.destination.agent_version, kept)[0]


def test_limit_4_past_its_bound_a_marked_fence_is_left_without_files_for_a_fresh_sid(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    broken_sid = _broken_destination(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("fence_broken", None, None)
    now[0] += behavior_native_sessions.FENCE_BROKEN_SECONDS
    carried = []

    def carry_out(self, client, version, before_send):
        # The offline Hosted runtime answers as one SID, so a real fresh-SID commit is not modelled here; the §7a
        # carry-out with basis files_overdue (the ④ "without files" notice) is covered by the abandon-path tests.
        carried.append((version, self.destination_session(client, version, abandon.row(fixture))[0]))
        return "switched"

    monkeypatch.setattr(NativeSessions, "_carry_out", carry_out)
    assert cut(fixture) == ("abandoned", None, None)
    decided = abandon.row(fixture)
    assert (decided.abandon.basis, decided.abandon.source_status) == ("files_overdue", "idle")  # FA stopped it
    assert decided.attempt(fixture.destination.agent_version) == 1
    assert carried and carried[0][0] == fixture.destination.agent_version and carried[0][1] != broken_sid


def test_limit_4_raises_the_attempt_of_the_version_the_carry_out_goes_to_never_a_vetoed_one(file_api, monkeypatch):
    from shared_context.native_binding import NativeBindingIdleUpgrade

    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    _broken_destination(fixture, monkeypatch)
    store, row = abandon.store(fixture), abandon.row(fixture)
    record = store.repository.get(store.partition_key, store.row_key(row.agent_id))
    store.repository.replace(store.partition_key, store.row_key(row.agent_id), row.model_copy(update={
        "idle_upgrade": NativeBindingIdleUpgrade(vetoed_versions=("42",))}).model_dump(mode="json"), record.etag)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("fence_broken", None, None)
    now[0] += behavior_native_sessions.FENCE_BROKEN_SECONDS
    monkeypatch.setattr(NativeSessions, "_carry_out", lambda *_args: "switched")
    assert cut(fixture) == ("abandoned", None, None)
    decided = abandon.row(fixture)
    # This Agent vetoed the selector's 42, so the carry-out goes to a fresh 41 SID, never the broken 42 one.
    assert behavior_native_sessions._unvetoed(decided, "42") == "41"
    assert (decided.attempt("42"), decided.attempt("41")) == (0, 0)


def test_limit_4_a_platform_5xx_outage_never_counts_toward_the_bound(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    _broken_destination(fixture, monkeypatch, retired=False)  # its metadata still reads active
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("fence_broken", None, None)
    now[0] += behavior_native_sessions.FENCE_BROKEN_SECONDS * 10
    assert cut(fixture) == ("fence_broken", None, None)
    waiting = abandon.row(fixture)
    assert waiting.abandon is None and waiting.upgrade.stop_started and waiting.clock("fence_broken") is None


def test_the_operator_inspects_without_a_call_and_clears_an_unmarked_fence_at_once(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _crashed_fence(fixture, monkeypatch)
    composition, agent_id = fixture.case.composition, fixture.source.agent_id
    start = len(fixture.controls), len(fixture.project_calls)
    seen = native_cut.fence_command(composition, agent_id)
    assert seen["row"]["fence"]["hosted_session_id"] == fixture.destination.session_id and "outcome" not in seen
    assert (len(fixture.controls), len(fixture.project_calls)) == start  # the row only
    cleared = native_cut.fence_command(composition, agent_id, clear=True)
    assert cleared["outcome"] == "withdrawn" and cleared["after"]["fence"] is None  # a young fence too
    assert state(fixture)["hosted_session_id"] == fixture.source.session_id and state(fixture).get("environment") is None


def test_the_operator_commits_a_marked_fence_without_files_and_says_so(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    abandon.platform(fixture, monkeypatch, status="updating")
    slept = []
    monkeypatch.setattr(behavior_native_sessions, "_clear_sleep", slept.append)
    cleared = native_cut.fence_command(fixture.case.composition, fixture.source.agent_id, clear=True)
    assert cleared["outcome"] == "abandoned", cleared
    # Driven first: every pass the bound allows, only then the no-files commit.
    assert slept == [behavior_native_sessions.CLEAR_PASS_SECONDS] * (behavior_native_sessions.CLEAR_DRIVE_PASSES - 1)
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.upgrade is None
    assert (switched.last_switch.basis, switched.switch_notice) == ("files_overdue", "without_files")


def test_the_operator_drives_a_marked_fence_to_its_commit_with_files_when_a_later_pass_can(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    slept = []
    abandon.platform(fixture, monkeypatch, status=lambda: "updating" if not slept else "idle")
    monkeypatch.setattr(behavior_native_sessions, "_clear_sleep", slept.append)
    cleared = native_cut.fence_command(fixture.case.composition, fixture.source.agent_id, clear=True)
    assert cleared["outcome"] == "ready" and len(slept) == 1, cleared
    _moved_with_files(fixture)


def test_the_fence_cli_prints_the_inspection(monkeypatch, capsys):
    from contextlib import contextmanager
    import json

    from shared_context import behavior_agent_versions

    seen = []

    @contextmanager
    def server(_configuration):
        yield SimpleNamespace(installation=lambda: None)

    monkeypatch.setattr("shared_context.behavior_server.build_server", server)
    monkeypatch.setattr("shared_context.behavior_server_config.load_configuration", lambda path: path)
    monkeypatch.setattr(native_cut, "fence_command", lambda composition, agent_id, clear: seen.append(
        (agent_id, clear)) or {"agent_id": agent_id})
    for confirm in ([], ["--confirm", "agent-2"]):  # --clear changes the Agent: never without its ID again
        with pytest.raises(SystemExit):
            behavior_agent_versions.main(["fence", "--agent-id", "agent-1", "--clear", *confirm])
    assert seen == []
    assert behavior_agent_versions.main(["fence", "--agent-id", "agent-1", "--clear", "--confirm", "agent-1"]) == 0
    assert seen == [("agent-1", True)] and json.loads(capsys.readouterr().out) == {"agent_id": "agent-1"}


def test_p8_a_decided_agent_with_no_sends_is_switched_by_the_background_cut(file_api, monkeypatch):
    """ADR M1 step 4(b): the patrol's decision (retired, read twice) is carried out by the cut worker, so the Agent
    leaves its pre-2d-i SID without a send and never holds the 2d-ii gate."""
    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    del fixture.files.targets[fixture.source.session_id]
    abandon.store(fixture).decide_abandon(
        fixture.source.agent_id, hosted_session_id=fixture.source.session_id, basis="retired",
        source_status="missing", created_agent_version=fixture.source.agent_version)
    assert cut(fixture) == ("abandoned", None, None)
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id
    assert switched.switch_notice == "source_retired" and switched.retired(fixture.source.session_id)
    assert source_calls(fixture) == ([], [], [])


def test_the_deploy_follow_hook_verifies_only_and_calls_no_session(file_api, monkeypatch):
    from shared_context.native_environment_sync import follow_all

    fixture = ready(file_api, monkeypatch)
    advanced = []
    monkeypatch.setattr(NativeSessions, "advance", lambda *a, **k: advanced.append(1))
    start = len(fixture.controls)
    results = follow_all(fixture.case.composition, agent_id=fixture.source.agent_id, verify_only=True)
    assert [(item["agent_id"], item.get("verify_only")) for item in results] == [(fixture.source.agent_id, True)]
    assert results[0]["state"] in {"ok", "pending"} and advanced == [] and len(fixture.controls) == start


# --- repair-fallback profile ---------------------------------------------------------------------------------------

def test_a_repair_fallback_profile_is_corrected_by_the_next_definition_read(bound, monkeypatch):
    from shared_context.errors import Problem

    case = bound
    sessions_ = native_sessions(case.composition)
    client = case.foundry.for_target(case.remote.target)
    declared(case, monkeypatch, **{"42": Problem(503, "AGENT_VERSION_FOLLOWING", "unreadable")})
    view = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="switch", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id="fa-native-next", hosted_agent_version="42")
    sessions_._record_switch(client, view, repairing=True)
    # Flag off: no new row field, so an older API revision still decodes the row.
    assert "profile_unread" not in case.store.read(case.agent_id).model_dump(mode="json")
    sessions_.guard.enforce = True
    sessions_._record_switch(client, view.model_copy(update={
        "request_id": "switch-2", "previous_hosted_session_id": "fa-native-next", "hosted_session_id": "fa-native-third"}),
        repairing=True)
    written = case.store.read(case.agent_id)
    assert (written.state_profile, written.profile_unread) == ("foundry-native-v1", True)
    declared(case, monkeypatch, **{"42": "foundry-native-v2"})
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: "42")
    assert sessions_.cut(client) == ("current", None, None)
    corrected = case.store.read(case.agent_id)
    assert (corrected.state_profile, corrected.profile_unread) == ("foundry-native-v2", False)
    assert "profile_unread" not in corrected.model_dump(mode="json")


# --- the destination create ------------------------------------------------------------------------------------------

def real_create(fixture, monkeypatch, create, status=None):
    """The real ``create_session`` over the platform double: ``create(sid)`` answers the create call (raise to fail
    it); ``status(sid)``, when given, answers every later ``get_session`` of the destination."""
    import azure.ai.projects

    base = azure.ai.projects.AIProjectClient

    class Project(base):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            read = self.agents.get_session
            self.agents.create_session = lambda name, *, version_indicator, agent_session_id: create(agent_session_id)
            if status is not None:
                self.agents.get_session = lambda name, sid: (
                    status(sid) if sid == fixture.destination.session_id else read(name, sid))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", Project)
    monkeypatch.setattr(FoundryHttpClient, "create_session", _CREATE)


@pytest.mark.parametrize("answer", [403, 400, 500, None])
def test_a_refused_destination_create_is_created_again_and_an_unanswered_one_is_not(file_api, monkeypatch, answer):
    """A create the platform answered with a refusal (a 403 before the admin adds the role, another 4xx, a 5xx) made no
    session by that call, so the next attempt creates the same SID again without an API restart; a create with no
    answer (a timeout) keeps its latch: no second create is sent (AGENTS.md rule 7)."""
    from azure.core.exceptions import HttpResponseError, ServiceRequestError
    from shared_context.errors import Problem

    fixture = ready(file_api, monkeypatch)
    sid, creates = fixture.destination.session_id, []

    def create(agent_session_id):
        creates.append(agent_session_id)
        if len(creates) == 1:
            if answer is None:
                raise ServiceRequestError("OFFLINE no answer")
            error = HttpResponseError("OFFLINE refused")
            error.status_code = answer
            raise error
        fixture.files.targets[sid] = fixture.destination
        return fixture.files.get_session(fixture.destination.agent_name, sid)

    real_create(fixture, monkeypatch, create)
    session, client = sessions(fixture), agent_client(fixture.case.composition, control(fixture))

    def destination():
        return session._destination(client, fixture.destination.agent_version, lambda: None, mark=False,
                                    row=abandon.row(fixture))

    with pytest.raises(Problem) as first:
        destination()
    assert first.value.status == (403 if answer == 403 else 503)
    if answer is None:
        with pytest.raises(Problem) as again:
            destination()
        assert again.value.body["error"]["code"] == "NATIVE_ENVIRONMENT_UNKNOWN" and creates == [sid]
        return
    assert destination().target.session_id == sid and creates == [sid, sid]


@pytest.mark.parametrize("ending", ["deadline", "shutdown"])
def test_the_cut_stops_waiting_for_a_destination_left_updating(file_api, monkeypatch, caplog, ending):
    """The platform leaves the new session ``updating``: the cut's wait for it ends at ``CUT_DESTINATION_SECONDS`` or on API
    shutdown (``_CutFence``) instead of polling forever, and the attempt logs why; the next turn end tries again."""
    from azure.ai.projects.models import AgentSessionResource

    fixture = ready(file_api, monkeypatch)
    now = abandon.clock(monkeypatch)
    driver = native_cut.native_cut_driver(fixture.case.composition)
    polls = []

    def updating(sid):
        polls.append(sid)
        assert len(polls) < 5, "the destination wait has no bound"
        if ending == "deadline":
            now[0] += 61
        else:
            driver._stop.set()
        return AgentSessionResource({"agent_session_id": sid, "status": "updating", "version_indicator": {
            "type": "version_ref", "agent_version": fixture.destination.agent_version}})

    real_create(fixture, monkeypatch, updating, status=updating)
    caplog.set_level(logging.WARNING)
    assert driver._attempt(control(fixture), fixture.source.agent_id) == "destination_unready"
    assert polls == [fixture.destination.session_id] * (2 if ending == "deadline" else 1)
    assert "NATIVE_UPGRADE_CUT_API outcome=destination_unready reason=" + (
        "fence_overrun" if ending == "deadline" else "shutdown") in caplog.text


# --- ADR-0009 phase 4b-1: U0 conditions with FA_NATIVE_IDLE_UPGRADE ------------------------------------------------

def _agent_name(fixture):
    return agent_client(fixture.case.composition, control(fixture)).target.agent_name


def _vetoed(fixture, where):
    """Version 42 (the selector's) vetoed for this Agent on its row, or project-wide by the veto row."""
    from shared_context.native_binding import NativeBindingIdleUpgrade
    from shared_context.native_version_veto import veto_store

    if where == "project":
        veto_store(fixture.case.composition).veto(_agent_name(fixture), "42")
        return
    store, row = abandon.store(fixture), abandon.row(fixture)
    record = store.repository.get(store.partition_key, store.row_key(row.agent_id))
    store.repository.replace(store.partition_key, store.row_key(row.agent_id), row.model_copy(update={
        "idle_upgrade": NativeBindingIdleUpgrade(vetoed_versions=("42",))}).model_dump(mode="json"), record.etag)


def _switched_at_only(fixture):
    kept = abandon.row(fixture).idle_upgrade
    assert kept.switched_at is not None and kept.target_version is None and kept.deferred_since is None


@pytest.mark.parametrize("where", ["agent", "project"])
def test_without_the_idle_flag_a_vetoed_target_still_cuts_as_before(file_api, monkeypatch, where):
    """FA_NATIVE_IDLE_UPGRADE=0 (the default): 2d-i's cut is unchanged, vetoes are not read and nothing is recorded."""
    fixture = ready(file_api, monkeypatch)
    _vetoed(fixture, where)
    assert sessions(fixture).idle_upgrade is False
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None
    _moved_with_files(fixture)
    assert abandon.row(fixture).idle_upgrade is None


def test_without_the_idle_flag_a_failed_source_is_still_probed_first(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    abandon.platform(fixture, monkeypatch, status="failed")
    start = len(fixture.controls)
    assert cut(fixture) == ("source_unavailable", "failed", None)
    assert ("session_cut", "probe") in operations(fixture, start)
    assert abandon.row(fixture).idle_upgrade is None


@pytest.mark.parametrize("where", ["agent", "project"])
def test_with_the_idle_flag_a_vetoed_target_is_never_cut(file_api, monkeypatch, where):
    fixture = ready(file_api, monkeypatch)
    _vetoed(fixture, where)
    sessions(fixture).idle_upgrade = True
    before, start, calls = abandon.row(fixture), len(fixture.controls), len(fixture.files.calls)
    assert cut(fixture) == ("vetoed", where, None)
    assert len(fixture.controls) == start and fixture.files.calls[calls:] == []
    assert fixture.destination.session_id not in fixture.files.targets
    assert abandon.row(fixture) == before, "a vetoed target is not due: no deferral start"
    assert state(fixture)["hosted_session_id"] == fixture.source.session_id


def test_with_the_idle_flag_a_compatible_idle_source_is_left_and_records_nothing(file_api, monkeypatch):
    """U0: the deferral start is written only once every condition holds; an idle source is not one."""
    fixture = ready(file_api, monkeypatch, idle=True)
    sessions(fixture).idle_upgrade = True
    before, start, calls = abandon.row(fixture), len(fixture.controls), len(fixture.files.calls)
    assert cut(fixture) == ("source_idle", None, None)
    assert len(fixture.controls) == start and fixture.files.calls[calls:] == []
    assert fixture.destination.session_id not in fixture.files.targets
    assert abandon.row(fixture) == before and before.idle_upgrade is None
    fixture.files.stopped.discard(fixture.source.session_id)  # the next turn resumed it
    assert cut(fixture)[:2] == ("ready", None)
    _moved_with_files(fixture)
    _switched_at_only(fixture)  # U8: the switch rebuilt the row with its switch time


def test_with_the_idle_flag_an_incompatible_target_records_nothing(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    sessions(fixture).idle_upgrade = True
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", lambda client: "fa-native-unknown")
    before = abandon.row(fixture)
    assert cut(fixture) == ("target_incompatible", "fa-native-unknown", None)
    assert abandon.row(fixture) == before and before.idle_upgrade is None


def test_with_the_idle_flag_the_deferral_starts_once_when_u0_holds(file_api, monkeypatch):
    """A compatible target over an active source that is busy: the deferral start is written once for that target."""
    fixture = ready(file_api, monkeypatch, files=False)
    sessions(fixture).idle_upgrade = True
    quiet = [False]

    async def task_quiet(sid, *, task_id=None, include_responses=False):
        return quiet[0]

    fixture.runtime.task_observer = SimpleNamespace(quiet=task_quiet)
    assert cut(fixture)[0] == "busy"
    first = abandon.row(fixture)
    assert first.hosted_session_id == fixture.source.session_id
    assert first.idle_upgrade.target_version == "42" and first.idle_upgrade.deferred_since is not None
    store = abandon.store(fixture)
    etag = store.repository.get(store.partition_key, store.row_key(first.agent_id)).etag
    assert cut(fixture)[0] == "busy"
    assert store.repository.get(store.partition_key, store.row_key(first.agent_id)).etag == etag, "written once"
    quiet[0] = True
    assert cut(fixture)[:2] == ("ready", None)
    assert state(fixture)["hosted_session_id"] == fixture.destination.session_id
    _switched_at_only(fixture)  # U8: the switch rebuilt the row with its switch time


def test_with_the_idle_flag_an_unread_veto_row_never_cuts(file_api, monkeypatch):
    """A Table read failure (the store wraps Azure errors as 502 UPSTREAM_ERROR) is fail-closed: veto_unread."""
    from shared_context.errors import Problem
    from shared_context.native_version_veto import NativeVersionVetoStore

    fixture = ready(file_api, monkeypatch)
    sessions(fixture).idle_upgrade = True

    def unavailable(self, agent_name, agent_version):
        raise Problem(502, "UPSTREAM_ERROR", "table timeout", service="table")

    monkeypatch.setattr(NativeVersionVetoStore, "read", unavailable)
    before, start = abandon.row(fixture), len(fixture.controls)
    assert cut(fixture) == ("veto_unread", "UPSTREAM_ERROR", None)
    assert len(fixture.controls) == start and fixture.destination.session_id not in fixture.files.targets
    assert abandon.row(fixture) == before


@pytest.mark.parametrize("where", ["read", "partition"])
def test_with_the_idle_flag_a_code_bug_in_the_veto_read_is_not_a_silent_veto(file_api, monkeypatch, where):
    """A programming error (or a misconfigured store, the partition collision) leaves ``cut``: the driver logs
    outcome=error, never a permanent veto_unread."""
    from shared_context import native_version_veto
    from shared_context.errors import Problem

    fixture = ready(file_api, monkeypatch)
    sessions(fixture).idle_upgrade = True
    if where == "read":
        def broken(*_args, **_kw):
            raise TypeError("a bug")

        monkeypatch.setattr(native_version_veto.NativeVersionVetoStore, "read", broken)
    else:
        legacy = fixture.case.composition.config.table.scope.partition_key
        monkeypatch.setattr(native_version_veto, "VETO_PARTITION", legacy)
    before, start = abandon.row(fixture), len(fixture.controls)
    with pytest.raises(TypeError if where == "read" else Problem, match="a bug" if where == "read" else "legacy"):
        cut(fixture)
    assert len(fixture.controls) == start and abandon.row(fixture) == before


def test_with_the_idle_flag_a_source_that_is_not_active_creates_no_destination(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    abandon.platform(fixture, monkeypatch, status="failed")
    sessions(fixture).idle_upgrade = True
    start = len(fixture.controls)
    assert cut(fixture) == ("source_unavailable", "failed", None)
    assert operations(fixture, start) == [] and fixture.destination.session_id not in fixture.files.targets