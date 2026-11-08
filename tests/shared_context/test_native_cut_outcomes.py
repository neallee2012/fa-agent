"""ADR-0009 M1 2d-i step 2: background cut outcomes ``test_native_cut.py`` does not reach otherwise.

Same harness as ``test_native_cut.py``: real source and destination Hosted runtimes behind the real API, with the
platform as an offline double. Each test moves one platform answer and checks the outcome the cut job logs and the
binding row it leaves.
"""

import pytest

from shared_context import behavior_native_sessions
from shared_context.behavior_native_sessions import NativeSessions, _target
from shared_context.behavior_session_files import SessionFiles
from shared_context.errors import Problem
from shared_context.native_binding import NativeBindingSession, NativeBindingStore
import test_native_abandon_source as abandon
from test_native_abandon_source import binding_rows  # noqa: F401 (autouse fixture)
from test_native_cut import cut, enforcing, operations, ready, sessions, replace_retried, set_admitting, state  # noqa: F401 (autouse fixture)
from test_native_follow_no_wake import source_calls
from test_native_files_api import (  # noqa: F401 (fixtures)
    file_api, file_loops, instruction_models, jwk_client, local_env, native_api, signing_key, synthetic_standing_v1,
    token,
)


def _unmoved(fixture):
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.source.session_id and row.upgrade is None


def _after_probe(monkeypatch):
    """True once the cut's quiet probe was answered."""
    probed = []
    scanned = behavior_native_sessions._scanned

    def recorded(invoke, command, bound, **options):
        value = scanned(invoke, command, bound, **options)
        probed.append(command.mode)
        return value

    monkeypatch.setattr(behavior_native_sessions, "_scanned", recorded)
    return probed


def test_an_agent_with_no_binding_row_is_unbound_and_nothing_is_called(file_api, monkeypatch):
    fixture = file_api
    abandon.platform(fixture, monkeypatch)
    assert abandon.row(fixture) is None
    calls, start = len(fixture.files.calls), len(fixture.controls)
    assert cut(fixture) == ("unbound", None, None)
    assert fixture.files.calls[calls:] == [] and operations(fixture, start) == []
    assert abandon.row(fixture) is None


def test_an_incompatible_claim_with_its_abandon_decided_is_switched_not_unbound(file_api, monkeypatch):
    fixture = file_api
    abandon.incompatible_idle(fixture, monkeypatch)
    assert abandon.store(fixture).decide_abandon(
        fixture.source.agent_id, hosted_session_id=fixture.source.session_id, basis="incompatible",
        source_status="idle", created_agent_version=fixture.source.agent_version,
        state_profile=abandon.OLD_PROFILE) is not None
    assert not abandon.row(fixture).supported()
    assert cut(fixture) == ("abandoned", None, None)
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.abandon is None


def test_a_decided_abandon_not_carried_out_yet_is_pending(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    assert abandon.store(fixture).decide_abandon(
        fixture.source.agent_id, hosted_session_id=fixture.source.session_id, basis="retired",
        source_status="failed", created_agent_version=fixture.source.agent_version) is not None
    monkeypatch.setattr(NativeSessions, "_carry_out", lambda self, client, version, before_send: None)
    assert cut(fixture) == ("abandon_pending", None, None)
    assert abandon.row(fixture).abandon is not None


def test_an_idle_source_whose_candidate_is_its_own_sid_is_never_read(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch, idle=True)
    source = fixture.source
    monkeypatch.setattr(NativeSessions, "candidate", lambda self, client, **_: client.for_target(_target(
        client.target, session_id=source.session_id, agent_version=source.agent_version)))
    assert cut(fixture) == ("source_idle", None, None)
    assert source_calls(fixture) == ([], [], [])
    _unmoved(fixture)


@pytest.mark.parametrize(("settled", "finished"), [("withdrawn", False), ("fence_live", False), ("repaired", True)])
def test_with_the_idle_flag_only_a_recorded_missed_commit_is_finished(file_api, monkeypatch, settled, finished):
    fixture = ready(file_api, monkeypatch)
    sessions(fixture).idle_upgrade = True
    finishes = []
    monkeypatch.setattr(NativeSessions, "_settle", lambda self, client, destination, state, row, **_: settled)
    monkeypatch.setattr(NativeSessions, "_finish", lambda self, source, destination, *, bound: finishes.append(
        (source.target.session_id, destination.target.session_id)))
    assert cut(fixture) == (settled, None, None)
    assert finishes == ([(fixture.source.session_id, fixture.destination.session_id)] if finished else [])


def test_a_destination_on_the_rows_own_sid_is_the_drift_path(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    source = fixture.source
    monkeypatch.setattr(NativeSessions, "_destination", lambda self, client, *_, **__: client.for_target(_target(
        client.target, session_id=source.session_id, agent_version=source.agent_version)))
    start = len(fixture.controls)
    assert cut(fixture) == ("same_session", None, None)
    assert operations(fixture, start) == []
    _unmoved(fixture)


def test_a_stuck_admission_whose_source_failed_is_not_abandoned(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    now = abandon.clock(monkeypatch)
    set_admitting(fixture, "stuck-rid")
    assert cut(fixture) == ("busy", "admission_pending", None)
    now[0] += behavior_native_sessions.STUCK_ADMISSION_SECONDS + 1
    abandon.platform(fixture, monkeypatch, status="failed")
    assert cut(fixture) == ("source_unavailable", "failed", None)
    _unmoved(fixture)
    assert abandon.row(fixture).abandon is None


def test_a_source_idle_by_the_quiet_point_is_left_for_its_next_turn_end(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    probed = _after_probe(monkeypatch)
    abandon.platform(fixture, monkeypatch, status=lambda: "idle" if probed else "active")
    calls = len(fixture.files.calls)
    assert cut(fixture) == ("source_idle", None, None)
    assert probed == ["probe"] and [call for call in fixture.files.calls[calls:] if call[0] != "create"] == []
    _unmoved(fixture)


def test_an_unlisted_source_workspace_is_files_unknown(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    _unlisted(monkeypatch)
    start = len(fixture.controls)
    assert cut(fixture) == ("files_unknown", "SESSION_FILES_UNAVAILABLE", None)
    assert operations(fixture, start)[-1] == ("session_cut", "probe")
    _unmoved(fixture)


def _unlisted(monkeypatch):
    def unlisted(self, sid):
        raise Problem(503, "SESSION_FILES_UNAVAILABLE", "offline listing failure")

    monkeypatch.setattr(SessionFiles, "has_files", unlisted)


@pytest.mark.parametrize("durable", [False, True])
def test_a_cleared_admission_restarts_its_stuck_bound(file_api, monkeypatch, durable):
    """Each ``admitting`` RID seen anew waits its own ``STUCK_ADMISSION_SECONDS``: the bound kept in this process, or
    with the guard enforced on the binding row, is cleared once a cut finds no admission."""
    fixture = ready(file_api, monkeypatch)
    if durable:
        enforcing(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    set_admitting(fixture, "stuck-rid")
    assert cut(fixture) == ("busy", "admission_pending", None)
    if durable:
        assert abandon.row(fixture).clock("admitting").subject == "stuck-rid"
    set_admitting(fixture, None)
    _unlisted(monkeypatch)
    assert cut(fixture) == ("files_unknown", "SESSION_FILES_UNAVAILABLE", None)
    assert abandon.row(fixture).clock("admitting") is None
    now[0] += behavior_native_sessions.STUCK_ADMISSION_SECONDS + 1
    set_admitting(fixture, "stuck-rid")
    assert cut(fixture) == ("busy", "admission_pending", None)
    _unmoved(fixture)


def test_without_the_idle_flag_a_fenced_switch_sends_no_prewarm(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    start = len(fixture.controls)
    assert cut(fixture)[:2] == ("ready", None)
    assert ("prewarm", None) not in operations(fixture, start)


def test_a_source_the_platform_deleted_is_unavailable_not_idle(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    abandon.platform(fixture, monkeypatch, status="missing")
    start = len(fixture.controls)
    assert cut(fixture) == ("source_unavailable", "missing", None)
    assert ("session_cut", "probe") in operations(fixture, start)
    _unmoved(fixture)


def test_the_probe_names_the_rows_previous_sids_as_settled(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    store, row = abandon.store(fixture), abandon.row(fixture)
    record = store.repository.get(store.partition_key, store.row_key(row.agent_id))
    store.repository.replace(store.partition_key, store.row_key(row.agent_id), row.model_copy(update={
        "previous_sessions": (NativeBindingSession(hosted_session_id="fa-native-earlier", agent_version="40"),),
    }).model_dump(mode="json"), record.etag)
    start = len(fixture.controls)
    cut(fixture)
    probes = [command for command, _ in fixture.controls[start:] if command.get("mode") == "probe"]
    assert probes and all(command["settled_session_ids"] == ["fa-native-earlier"] for command in probes)


def test_a_failed_switch_without_a_snapshot_raises_for_the_cut_job(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch, files=False)
    probed = _after_probe(monkeypatch)
    scanned = behavior_native_sessions._scanned

    def refused(invoke, command, bound, **options):
        if probed:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "offline switch refusal")
        return scanned(invoke, command, bound, **options)

    monkeypatch.setattr(behavior_native_sessions, "_scanned", refused)
    with pytest.raises(Problem) as raised:
        cut(fixture)
    assert raised.value.body["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT" and probed == ["probe"]
    _unmoved(fixture)


@pytest.mark.parametrize("row", ["unreadable", "missing", "another-marked"])
def test_a_fence_whose_row_is_unreadable_right_before_its_withdraw_is_left_to_its_job(file_api, monkeypatch, row):
    """Past its bound the fence is withdrawn only after one more row read; an unreadable Table, no row, or another
    job's marked fence there leaves the pending in place ("fence_live"), never withdrawn or driven blind."""
    fixture = ready(file_api, monkeypatch)
    monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", -1)
    read, after = NativeBindingStore.read, []

    def unreadable_after_the_fence(self, agent_id):
        if ("session_cut", None) not in operations(fixture):
            return read(self, agent_id)
        after.append(1)
        if row == "missing":
            return None
        if row == "another-marked":
            found = read(self, agent_id)
            # The cut's own re-read sees its fence; the one right before the withdraw sees another job's, marked.
            return found if len(after) == 1 else found.model_copy(update={"upgrade": found.upgrade.model_copy(
                update={"request_id": "another-cut-request", "stop_started": True})})
        raise Problem(503, "OFFLINE_TABLE_UNAVAILABLE", "OFFLINE the binding Table is not readable")

    monkeypatch.setattr(NativeBindingStore, "read", unreadable_after_the_fence)
    outcome, reason, _ = cut(fixture)
    assert (outcome, reason) == ("fence_live", "fence_overrun")
    assert ("session_withdraw", None) not in operations(fixture)
    pending = state(fixture)["environment"]
    assert pending["state"] == "pending" and pending["hosted_session_id"] == fixture.destination.session_id


def test_an_unmarked_fence_whose_pending_is_gone_after_an_earlier_follow_is_cleared_and_the_cut_goes_on(
        file_api, monkeypatch):
    """The source became the Agent's by an earlier, committed follow (its ``environment`` is ``ready``): a recorded
    fence with no pending and no stop is only cleared, not taken for another follow's pending."""
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView
    from shared_context.native_binding import NativeBindingUpgrade
    from shared_context.native_turns import TURNS_STORE, _changed, _json
    from test_native_cut import _moved_with_files

    fixture = ready(file_api, monkeypatch)

    async def earlier_follow():
        current, etag = await fixture.original._agent_record()
        view = NativeEnvironmentView(
            agent_id=fixture.source.agent_id, request_id="earlier-follow",
            previous_hosted_session_id="OFFLINE-before-original", previous_agent_version="40",
            hosted_session_id=fixture.source.session_id, hosted_agent_version=fixture.source.agent_version,
            state="ready", files_profile=current.preparation.files_profile)
        async with fixture.original._store(TURNS_STORE) as store:
            await store.set_item("agent", _json(_changed(current, environment=view, following_supported=True)),
                                 if_match=etag)

    fixture.hosted.portal.call(earlier_follow)
    store, agent_id, source_sid = abandon.store(fixture), fixture.source.agent_id, fixture.source.session_id
    store.record_upgrade(agent_id, hosted_session_id=source_sid, upgrade=NativeBindingUpgrade(
        hosted_session_id=fixture.destination.session_id, agent_version=fixture.destination.agent_version,
        request_id=behavior_native_sessions.cut_request_id(agent_id, source_sid, fixture.destination.session_id),
        started_at=store._now()))
    assert cut(fixture)[0] == "ready"
    _moved_with_files(fixture)


def test_a_pending_the_rows_fence_does_not_name_is_left_to_the_follow_that_wrote_it(file_api, monkeypatch):
    """A fence on the row for another request than the pending's is not this pending's fence: ``pending_other``,
    never withdrawn, whatever its age."""
    from test_native_cut import Crash

    fixture = ready(file_api, monkeypatch)
    follow = NativeSessions.follow

    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(NativeSessions, "follow", crash)
    with pytest.raises(Crash):
        cut(fixture)
    monkeypatch.setattr(NativeSessions, "follow", follow)
    fence = abandon.row(fixture).upgrade
    store, agent_id, source_sid = abandon.store(fixture), fixture.source.agent_id, fixture.source.session_id
    store.record_upgrade(agent_id, hosted_session_id=source_sid, upgrade=None)
    store.record_upgrade(agent_id, hosted_session_id=source_sid,
                         upgrade=fence.model_copy(update={"request_id": "another-cut-request"}))
    monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", -1)
    assert cut(fixture) == ("pending_other", None, None)
    pending = state(fixture)["environment"]
    assert pending["state"] == "pending" and pending["request_id"] == fence.request_id


def _settle_args(fixture, fence):
    """The direct ``_settle`` caller's pieces (as in ``test_native_cut.py``'s replica races): the sessions, the
    Agent's client and the fence's own destination."""
    from shared_context.behavior_native_turn_api import agent_client
    from test_native_cut import control

    session = sessions(fixture)
    client = agent_client(fixture.case.composition, control(fixture))
    return session, client, session._fence_reader(client, fence)


def _unmarked(fixture):
    """The binding row as a replica that read it before FA's stop was marked sees it."""
    row = abandon.row(fixture)
    return row.model_copy(update={"upgrade": row.upgrade.model_copy(update={"stop_started": False})})


def _write_row(fixture, row):
    store = abandon.store(fixture)
    record = store.repository.get(store.partition_key, store.row_key(row.agent_id))
    store.repository.replace(store.partition_key, store.row_key(row.agent_id), row.model_dump(mode="json"),
                             record.etag)


def _committed_unrecorded(fixture, monkeypatch):
    """Replica B drives its marked fence to Hosted's commit and dies before the binding row records it."""
    from test_native_cut import _CONFIRM, Crash

    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(NativeSessions, "_confirm_environment", crash)
    with pytest.raises(Crash):
        cut(fixture)
    monkeypatch.setattr(NativeSessions, "_confirm_environment", _CONFIRM)
    assert state(fixture)["hosted_session_id"] == fixture.destination.session_id
    assert abandon.row(fixture).hosted_session_id == fixture.source.session_id


def _progress_conflict(_destination, _pending):
    raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "OFFLINE the files progress names no pending any more")


@pytest.mark.parametrize("raced", ["progress", "withdraw"])
def test_a_stale_settle_of_a_fence_another_replica_committed_records_the_commit(file_api, monkeypatch, raced):
    """B stopped the source and committed; A read the row (no marker) and the ``pending`` before both and settles its
    own fence. A's files-progress read meets the commit (409, then a fresh read), or that read came before B's stop
    and Hosted answers A's withdraw with the committed state: either way the commit is recorded ("repaired"), never
    taken for a withdraw or for the same pending. (This offline Hosted still answers the progress read of a committed
    fence; the 409 is the double's, as in ``test_native_cut.py``'s files-progress conflict.)"""
    from test_native_cut import _crash_after_fas_stop, _moved_with_files

    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    stale = _unmarked(fixture)
    session, client, reader = _settle_args(fixture, stale.upgrade)
    before = session._state(reader)
    assert before.environment.state == "pending"
    _committed_unrecorded(fixture, monkeypatch)
    store = abandon.store(fixture)
    monkeypatch.setattr(NativeSessions, "_binding_row", lambda self, agent: (store, stale))
    monkeypatch.setattr(NativeSessions, "_stop_started", staticmethod(
        _progress_conflict if raced == "progress" else lambda destination, pending: False))
    start = len(fixture.controls)
    assert session._settle(client, reader, before, stale, own=True) == "repaired"
    assert (("session_withdraw", None) in operations(fixture, start)) is (raced == "withdraw")
    _moved_with_files(fixture)


def test_a_stale_settle_of_a_legacy_pending_another_replica_committed_records_the_commit(file_api, monkeypatch):
    """The same race for an older follow's ``pending`` (no fence on the row, guard enforced)."""
    from test_native_cut import _crash_after_fas_stop, _moved_with_files, restarted

    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    store, agent_id, source_sid = abandon.store(fixture), fixture.source.agent_id, fixture.source.session_id
    fence = abandon.row(fixture).upgrade
    store.record_upgrade(agent_id, hosted_session_id=source_sid, upgrade=None, lost=fence)
    restarted(fixture)
    row = abandon.row(fixture)
    assert row.upgrade is None
    session, client, reader = _settle_args(fixture, fence)
    before = session._state(reader)
    assert before.environment.state == "pending"
    _committed_unrecorded(fixture, monkeypatch)
    monkeypatch.setattr(NativeSessions, "_stop_started", staticmethod(_progress_conflict))
    assert session._settle(client, reader, before, row) == "repaired"
    _moved_with_files(fixture)


def test_a_young_fence_whose_files_progress_records_fas_stop_is_driven_without_the_rows_marker(
        file_api, monkeypatch):
    """The row read misses FA's marker (another replica's stop) but Hosted's files progress records it: a cut drives
    the fence to its commit at once, never leaves it to a job as "fence_live" nor asks Hosted to withdraw it."""
    from test_native_cut import _crash_after_fas_stop, _moved_with_files

    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    _write_row(fixture, _unmarked(fixture))
    assert not abandon.row(fixture).upgrade.stop_started
    start = len(fixture.controls)
    assert cut(fixture) == ("ready", None, None)
    assert ("session_withdraw", None) not in operations(fixture, start)
    _moved_with_files(fixture)


@pytest.mark.parametrize("pending", [True, False])
def test_a_fence_the_job_marked_is_never_withdrawn_even_when_its_row_read_misses_the_marker(
        file_api, monkeypatch, caplog, pending):
    """The job's own fence after its stop was marked (``marked``), with a row read that does not show the marker and
    no stop in the files progress yet: its ``pending`` is driven to the commit with the files; a ``pending`` already
    gone is the "fence_lost" anomaly, its record cleared, never taken for a fence that is simply gone."""
    import logging

    from shared_context.behavior_native_turn_api import NativeTurnGateway
    from shared_context.behavior_native_turn_wire import NativeSessionRead, NativeSessionWithdraw
    from test_native_cut import _crashed_fence, _moved_with_files

    fixture = ready(file_api, monkeypatch)
    _crashed_fence(fixture, monkeypatch)
    row = abandon.row(fixture)
    assert not row.upgrade.stop_started
    session, client, reader = _settle_args(fixture, row.upgrade)
    if not pending:
        read = NativeTurnGateway._invoke(reader, NativeSessionRead())[0]
        NativeTurnGateway._invoke(reader, NativeSessionWithdraw(
            request_id="another-path-withdraw", expected_etag=read.etag,
            pending_request_id=read.environment.request_id, pending_session_id=read.environment.hosted_session_id))
    caplog.set_level(logging.WARNING)
    start = len(fixture.controls)
    settled = session._settle(client, reader, session._state(reader), row, own=True, marked=True)
    assert ("session_withdraw", None) not in operations(fixture, start)
    if pending:
        assert settled == "ready"
        _moved_with_files(fixture)
    else:
        assert settled == "fence_lost" and "NATIVE_UPGRADE_CUT_FENCE_LOST" in caplog.text
        kept = abandon.row(fixture)
        assert kept.upgrade is None and kept.hosted_session_id == fixture.source.session_id


@pytest.mark.parametrize("stop", ["marked-meanwhile", "files-progress"])
def test_limit_4_a_broken_fence_whose_stop_fa_recorded_is_left_without_files_never_driven_by_a_fresh_sid(
        file_api, monkeypatch, stop):
    """Past limit ④ an unmarked fence is withdrawn by a fresh SID; when FA's stop shows up on the way (another
    replica marks the row before the withdraw, or the files progress records it and Hosted refuses the withdraw) that
    SID never drives the fence: the withdraw is refused and the fence takes the no-files abandon path."""
    from test_native_cut import _broken_destination, _crash_after_fas_stop, _crashed_fence

    fixture = ready(file_api, monkeypatch)
    if stop == "files-progress":
        _crash_after_fas_stop(fixture, monkeypatch)
        _write_row(fixture, _unmarked(fixture))
    else:
        _crashed_fence(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    _broken_destination(fixture, monkeypatch)
    now = abandon.clock(monkeypatch)
    assert cut(fixture) == ("fence_broken", None, None)
    now[0] += behavior_native_sessions.FENCE_BROKEN_SECONDS
    known = {fixture.source.session_id, fixture.destination.session_id}
    if stop == "marked-meanwhile":
        read_state, fence = NativeSessions._state, abandon.row(fixture).upgrade

        def marked_meanwhile(client, **options):
            if client.target.session_id not in known:  # the fresh SID's read, right before the withdraw
                abandon.store(fixture).mark_stop(fixture.source.agent_id, hosted_session_id=fixture.source.session_id,
                                                 upgrade=fence)
            return read_state(client, **options)

        monkeypatch.setattr(NativeSessions, "_state", staticmethod(marked_meanwhile))
    monkeypatch.setattr(NativeSessions, "_carry_out", lambda *_args: "switched")
    start = len(fixture.controls)
    assert cut(fixture) == ("abandoned", None, None)
    assert [op for op in operations(fixture, start) if op[0] == "session_follow"] == []
    assert abandon.row(fixture).abandon.basis == "files_overdue"


def test_a_sends_pass_over_a_marked_fence_takes_one_step_and_leaves_the_rest_to_the_next(file_api, monkeypatch):
    """A send drives a marked fence one pass only (``once``): files that need another step leave the send waiting
    for its resend, never held until the commit."""
    from shared_context import behavior_native_turn_api
    from shared_context.behavior_session_files import FollowFiles
    from test_native_cut import _crash_after_fas_stop, _following, submit_on_source

    fixture = ready(file_api, monkeypatch)
    _crash_after_fas_stop(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    monkeypatch.setattr(behavior_native_turn_api, "IN_REQUEST_WAIT_SECONDS", 0)
    stopped_step, steps = FollowFiles.stopped_step, []

    def one_step_short(self):
        steps.append(1)
        return False if len(steps) == 1 else stopped_step(self)

    monkeypatch.setattr(FollowFiles, "stopped_step", one_step_short)
    _following(submit_on_source(fixture, "behind-the-fence"))
    assert steps == [1] and state(fixture)["environment"]["state"] == "pending"
    assert abandon.row(fixture).hosted_session_id == fixture.source.session_id
