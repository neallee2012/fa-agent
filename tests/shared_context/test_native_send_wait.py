"""ADR-0009 2d-i P6: the send path under FA_NATIVE_SESSION_GUARD_ENFORCE.

A message that meets a switch waits in its own request (W = 10 s, the same RID resent), then is answered
``server_following`` for the workbench's same-RID resend; never a refusal (M2). Every follow entry is off: a selector
move is served by the bound SID, a routine never follows on its own, and the in-request switch only carries out what
the ADR names (a retired SID after two reads at least 5 s apart, an incompatible SID at once, an older follow's
``pending`` withdrawn, a stuck admission past its durable bound). Real API and source/destination Hosted runtimes over
one shared store (``file_api``); the platform is an offline double. Without the flag nothing changes."""

import logging
import threading
import time

import pytest

from shared_context import behavior_native_sessions, behavior_native_turn_api
from shared_context.behavior_native_sessions import NativeSessions
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import NOTICE_KINDS_HEADER, NOTICE_KINDS_VERSION, NativeSessionFollow
from shared_context.native_binding import NativeBindingStore
from test_native_abandon_source import OLD_PROFILE, binding_rows  # noqa: F401
import test_native_abandon_source as abandon
from test_native_binding import bound, declared  # noqa: F401
from test_native_cut import (  # noqa: F401
    Crash, _FOLLOW, control, cut, enforcing, operations, ready, replace_retried, restarted, sessions, set_admitting,
    state, submit_on_source,
)
from test_native_environment import invoke
from test_native_files_api import (  # noqa: F401
    PREFIX, file_api, file_loops, instruction_models, jwk_client, local_env, native_api, signing_key,
    synthetic_standing_v1, token,
)
from test_native_follow_no_wake import source_calls


def fake_time(monkeypatch):
    """One fake clock for this process's bounds, the durable bounds and the in-request wait, which sleeps by moving it."""
    now = abandon.clock(monkeypatch)
    slept = []
    monkeypatch.setattr(behavior_native_turn_api, "monotonic", lambda: now[0])
    monkeypatch.setattr(behavior_native_turn_api, "_resend_sleep",
                        lambda seconds: slept.append(seconds) or now.__setitem__(0, now[0] + seconds))
    return now, slept


def warm_metadata(fixture, monkeypatch):
    """The fixture's platform double for ``_read_platform_session``, also taking a warm process's no-retry options."""
    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session",
                        lambda client, **_options: fixture.files.targets.get(client.target.session_id))


def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(behavior_native_turn_api, "_resend_sleep", slept.append)
    return slept


def following(response, rid):
    assert response.status_code in {409, 503}, response.text
    error = response.json()["error"]
    assert (error.get("not_dispatched"), error.get("server_following"), error.get("request_id")) == (
        True, True, rid), response.text


def admitted(response, sid):
    assert response.status_code == 202, response.text
    assert response.json()["hosted_session_id"] == sid, response.text
    return response.json()


def crashed_fence(fixture, monkeypatch):
    """The background cut wrote its files fence (row ``upgrade`` and Hosted's ``pending``), then the API died."""
    def crash(*_args, **_kwargs):
        raise Crash()

    monkeypatch.setattr(NativeSessions, "follow", crash)
    with pytest.raises(Crash):
        cut(fixture)
    monkeypatch.setattr(NativeSessions, "follow", _FOLLOW)
    assert state(fixture)["environment"]["state"] == "pending" and abandon.row(fixture).upgrade is not None


def follow_operations(fixture, since=0):
    return [operation for operation in operations(fixture, since) if operation[0] == "session_follow"]


# --- flag-off parity and the fence-less send -----------------------------------------------------------------------

def test_without_the_flag_a_send_meeting_a_fence_makes_one_attempt_as_on_main(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    crashed_fence(fixture, monkeypatch)
    slept, attempts = no_sleep(monkeypatch), []
    original = NativeTurnGateway._submit
    monkeypatch.setattr(NativeTurnGateway, "_submit", lambda self, *a, **k: attempts.append(1) or original(self, *a, **k))
    assert sessions(fixture).guard.enforce is False
    following(submit_on_source(fixture, "flag-off"), "flag-off")
    assert attempts == [1] and slept == []


def test_with_the_flag_a_send_that_meets_no_switch_makes_the_same_calls_as_one_attempt(file_api, monkeypatch):
    """No fence, no refusal: exactly one attempt, no wait, and the same session, platform, Table and Files calls as
    calling the attempt itself (``_submit``) without the wait around it."""
    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    abandon.platform(fixture, monkeypatch)
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: fixture.source.agent_version)
    slept, reads = no_sleep(monkeypatch), []
    read = NativeBindingStore._read
    monkeypatch.setattr(NativeBindingStore, "_read", lambda self, agent_id: reads.append(agent_id) or read(self, agent_id))

    def counted(rid):
        before = len(fixture.controls), len(fixture.project_calls), len(reads), len(fixture.files.calls)
        admitted(submit_on_source(fixture, rid), fixture.source.session_id)
        return (len(fixture.controls) - before[0], len(fixture.project_calls) - before[1], len(reads) - before[2],
                len(fixture.files.calls) - before[3])

    counted("warm")  # the reader, selector, metadata and guard reads are kept for the next two
    with_wait = counted("with-wait")
    monkeypatch.setattr(NativeTurnGateway, "submit", lambda self, principal, command, **options: self._submit(
        principal, command, **options))
    assert with_wait == counted("one-attempt") and slept == []


# --- fences: wait in-request, never queue behind a fence step --------------------------------------------------------

def test_a_send_meeting_a_files_fence_waits_in_its_request_and_is_admitted_once_the_fence_ends(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    crashed_fence(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    resends = []

    def withdraw_on_the_third_wait(seconds):
        resends.append(seconds)
        if len(resends) == 3:  # the fence's bound passes while the message waits: the background job withdraws it
            monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", -1)

    monkeypatch.setattr(behavior_native_turn_api, "_resend_sleep", withdraw_on_the_third_wait)
    sent = submit_on_source(fixture, "during-fence")
    admitted(sent, fixture.source.session_id)  # the withdraw put the old SID back: the message runs there, once
    assert len(resends) >= 3 and all(seconds == behavior_native_turn_api.RESEND_SECONDS for seconds in resends)
    assert abandon.row(fixture).upgrade is None


def test_past_w_a_send_meeting_a_fence_is_answered_server_following_with_its_rid(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    crashed_fence(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    _, slept = fake_time(monkeypatch)
    following(submit_on_source(fixture, "past-w"), "past-w")
    assert sum(slept) <= behavior_native_turn_api.IN_REQUEST_WAIT_SECONDS
    assert len(slept) == behavior_native_turn_api.IN_REQUEST_WAIT_SECONDS / behavior_native_turn_api.RESEND_SECONDS
    assert state(fixture)["environment"]["state"] == "pending"  # a young fence: only its own job settles it


def test_at_w_a_hosted_task_recovery_refusal_is_answered_server_following_too(monkeypatch):
    """The stated cost (ADR-0009 2d-i P6): a Hosted 503 for its own task recovery, which one attempt answers without
    ``server_following``, is resent for W in this request, then answered with it (the workbench resends that RID)."""
    from shared_context.behavior_native_turn_api import _BeforeAdmission, wait_in_request
    from shared_context.errors import Problem

    _, slept = fake_time(monkeypatch)
    attempts = []

    def recovering():
        attempts.append(1)
        raise _BeforeAdmission(Problem(503, "AGENT_VERSION_FOLLOWING", "OFFLINE task recovery"), "rid-recovery")

    with pytest.raises(_BeforeAdmission) as once:
        wait_in_request(recovering, enforce=False)
    assert once.value.server_following is False and attempts == [1] and slept == []
    with pytest.raises(_BeforeAdmission) as waited:
        wait_in_request(recovering, enforce=True)
    assert waited.value.server_following is True and waited.value.request_id == "rid-recovery"
    wait, resend = behavior_native_turn_api.IN_REQUEST_WAIT_SECONDS, behavior_native_turn_api.RESEND_SECONDS
    assert len(attempts) == 2 + wait / resend and sum(slept) == wait


def test_only_the_first_attempt_prefetches_the_capture(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    crashed_fence(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    fake_time(monkeypatch)
    early = []
    original = NativeTurnGateway._submit
    monkeypatch.setattr(NativeTurnGateway, "_submit", lambda self, *a, **k: early.append(k["early"]) or original(
        self, *a, **k))
    following(submit_on_source(fixture, "no-prefetch-resend"), "no-prefetch-resend")
    assert early[0] is True and len(early) > 1 and not any(early[1:])


def test_a_send_never_queues_behind_a_fence_step_that_holds_the_sessions_lock(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    crashed_fence(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    monkeypatch.setattr(behavior_native_turn_api, "IN_REQUEST_WAIT_SECONDS", 1)
    held, release = threading.Event(), threading.Event()

    def fence_step():
        with sessions(fixture).lock:  # a background fence step (one follow call, up to ~10 s)
            held.set()
            release.wait(30)

    worker = threading.Thread(target=fence_step)
    worker.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        following(submit_on_source(fixture, "behind-step"), "behind-step")
        assert time.monotonic() - started < 10  # answered within its own wait, not after the step
    finally:
        release.set()
        worker.join()


# --- follow entries off --------------------------------------------------------------------------------------------

def test_a_selector_moved_submit_is_served_by_the_bound_sid_and_starts_no_follow(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)  # the row is on 41, the selector names 42
    enforcing(fixture, monkeypatch)
    start = len(fixture.controls)
    admitted(submit_on_source(fixture, "moved-selector"), fixture.source.session_id)
    assert follow_operations(fixture, start) == [] and abandon.row(fixture).hosted_session_id == fixture.source.session_id


def test_an_older_follows_pending_is_withdrawn_in_request_and_the_message_is_admitted(file_api, monkeypatch, caplog):
    """ADR M1 "舊式 follow 留下的 pending": no fence of this API recorded it and nothing moves it on any more, so the
    send's in-request switch withdraws it by an H2d session (the selected version's) and resends the same RID."""
    fixture = ready(file_api, monkeypatch)
    etag = state(fixture)["etag"]
    left = invoke(fixture.hosted, fixture.identity, NativeSessionFollow(
        request_id="an-older-follow", expected_session_id=fixture.source.session_id, expected_etag=etag,
        stage="prepare", idle_only=True, files_required=True))
    assert left.status_code == 200 and left.json()["state"] == "pending", left.text
    enforcing(fixture, monkeypatch)
    fake_time(monkeypatch)  # the send resends once; a slow machine must not use up the real 10 s wait
    caplog.set_level(logging.WARNING)
    start = len(fixture.controls)
    admitted(submit_on_source(fixture, "after-older-follow"), fixture.source.session_id)
    assert "NATIVE_UPGRADE_LEGACY_PENDING_WITHDRAWN" in caplog.text
    assert ("session_withdraw", None) in operations(fixture, start) and follow_operations(fixture, start) == []
    assert state(fixture).get("environment") is None and not fixture.files.stopped


def test_an_older_follows_pending_with_fas_stop_recorded_is_driven_not_withdrawn(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    etag = state(fixture)["etag"]
    invoke(fixture.hosted, fixture.identity, NativeSessionFollow(
        request_id="an-older-follow", expected_session_id=fixture.source.session_id, expected_etag=etag,
        stage="prepare", idle_only=True, files_required=True))
    enforcing(fixture, monkeypatch)
    driven = []
    monkeypatch.setattr(NativeSessions, "_stop_started", staticmethod(lambda destination, pending: True))
    monkeypatch.setattr(NativeSessions, "_drive", lambda self, client, destination, pending, stop, **_: driven.append(
        (destination.target.session_id, pending.request_id)) or "stopping")
    monkeypatch.setattr(behavior_native_turn_api, "IN_REQUEST_WAIT_SECONDS", 0)
    start = len(fixture.controls)
    following(submit_on_source(fixture, "after-stopped-follow"), "after-stopped-follow")
    assert driven == [(fixture.destination.session_id, "an-older-follow")]
    assert ("session_withdraw", None) not in operations(fixture, start)


def _routine_trigger(fixture, monkeypatch):
    """A routine occurrence's trigger with its definition and clock stubbed (the in-request switch runs for real)."""
    from datetime import datetime, timezone

    from shared_context.native_routine_wire import NativeRoutineTrigger
    from shared_context.native_routines import NativeRoutineService

    service = NativeRoutineService(fixture.case.composition, enabled=True)
    monkeypatch.setattr(service, "_clock", lambda clock: None)
    monkeypatch.setattr(service, "_current", lambda control: None)
    monkeypatch.setattr(service, "_intent", lambda control, agent_id, rid: None)
    definition = type("D", (), {"kind": "routine", "conversation_id": "native-cid"})()
    monkeypatch.setattr(service, "_current_definition", lambda control, agent_id, routine_id: definition)
    monkeypatch.setattr(service, "_active_definition", lambda control, agent_id, value: None)
    return service, lambda: service.trigger(None, fixture.source.agent_id, NativeRoutineTrigger(
        routine_id="daily-summary", scheduled_for=datetime(2026, 10, 16, 9, tzinfo=timezone.utc)))


def test_a_routine_never_follows_and_ends_unconfirmed_with_its_occurrence_rid(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    del fixture.files.targets[fixture.source.session_id]  # its SID reads 404: the second read is due after the wait
    monkeypatch.setattr(behavior_native_turn_api, "IN_REQUEST_WAIT_SECONDS", 1)
    destinations, advanced = [], []
    monkeypatch.setattr(NativeSessions, "_destination", lambda *a, **k: destinations.append(1) or pytest.fail(
        "a routine never prepares its own destination"))
    advance = NativeSessions.advance
    monkeypatch.setattr(NativeSessions, "advance", lambda self, *a, **k: advanced.append(1) or advance(self, *a, **k))
    _, trigger = _routine_trigger(fixture, monkeypatch)
    start = len(fixture.controls)
    with pytest.raises(Exception) as ended:
        trigger()
    error = ended.value.body["error"]
    assert (ended.value.status, error["code"]) == (503, "NATIVE_ROUTINE_UNCONFIRMED")
    assert error["request_id"] and error["environment_code"] == "AGENT_VERSION_FOLLOWING"
    assert destinations == [] and advanced and follow_operations(fixture, start) == []


# --- retired, incompatible, stuck: the in-request switch -----------------------------------------------------------

def test_a_retired_bound_sid_is_switched_in_request_after_two_reads_five_seconds_apart(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    _, slept = fake_time(monkeypatch)
    fixture.case.headers = {**fixture.case.headers, NOTICE_KINDS_HEADER: NOTICE_KINDS_VERSION}
    del fixture.files.targets[fixture.source.session_id]  # the platform deleted it (404)
    body = admitted(submit_on_source(fixture, "after-retired"), fixture.destination.session_id)
    assert body["environment_notice"] == {"kind": "environment_source_retired"}
    assert slept[0] == behavior_native_sessions.RETIRED_CONFIRM_SECONDS and sum(slept) <= 10
    switched = abandon.row(fixture)
    assert (switched.last_switch.basis, switched.last_switch.source_status) == ("retired", "missing")
    assert source_calls(fixture) == ([], [], [])


def test_a_404_then_active_bound_sid_is_never_abandoned(file_api, monkeypatch):
    """§7a (m): one 404 of a compatible SID decides nothing; it reads active again and the message runs there."""
    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    fake_time(monkeypatch)
    missing = [True]
    read = behavior_native_sessions._read_platform_session

    def once_missing(client, **options):
        if client.target.session_id == fixture.source.session_id and missing:
            missing.clear()
            return None
        return read(client, **options)

    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", once_missing)
    admitted(submit_on_source(fixture, "after-one-404"), fixture.source.session_id)
    kept = abandon.row(fixture)
    assert kept.hosted_session_id == fixture.source.session_id and kept.abandon is None


@pytest.mark.parametrize("entry", ["submit", "advance"])
def test_k_an_incompatible_sid_is_never_called_and_every_entry_switches_or_waits(file_api, monkeypatch, entry):
    """(k) with enforcement: the binding row's profile is not supported. Before the switch, a poll and a typing
    prewarm call nothing on that SID; the submit (and the automatic environment entry) switch it at once by the §7a
    path (M1 "收斂後還剩的情況") and the message is admitted on the new SID. Zero invoke, Files and stop calls to it."""
    from test_native_automatic_environment import advance

    fixture = ready(file_api, monkeypatch)
    abandon.store(fixture)._update(fixture.source.agent_id, lambda row: row.model_copy(update={
        "state_profile": OLD_PROFILE}))
    abandon.platform(fixture, monkeypatch, status="active", profile=OLD_PROFILE)
    enforcing(fixture, monkeypatch)
    mark = len(fixture.control_versions), len(fixture.files.calls)
    polled = abandon.read_request(fixture, "original-rid")
    assert polled.status_code < 500 or polled.json()["error"].get("not_dispatched"), polled.text
    fixture.case.client.post(PREFIX + "/prewarm", headers=fixture.case.headers, json={})
    assert source_calls(fixture, since=mark) == ([], [], [])
    if entry == "submit":
        admitted(submit_on_source(fixture, "k-submit"), fixture.destination.session_id)
    else:
        moved = advance(fixture)
        assert moved.status_code == 200 and moved.json()["hosted_session_id"] == fixture.destination.session_id
        admitted(submit_on_source(fixture, "k-after-advance"), fixture.destination.session_id)
    assert abandon.row(fixture).last_switch.basis == "incompatible"
    assert source_calls(fixture, since=mark) == ([], [], [])


def test_k_a_routine_of_an_incompatible_sid_switches_in_request_and_never_calls_it(file_api, monkeypatch):
    """The routine's in-request switch reads the raw binding row: an unsupported profile has no locator row
    (``bound_row`` None), yet it is exactly the SID the switch leaves; without that the routine never switched."""
    fixture = ready(file_api, monkeypatch)
    abandon.store(fixture)._update(fixture.source.agent_id, lambda row: row.model_copy(update={
        "state_profile": OLD_PROFILE}))
    abandon.platform(fixture, monkeypatch, status="active", profile=OLD_PROFILE)
    enforcing(fixture, monkeypatch)
    fake_time(monkeypatch)
    service, trigger = _routine_trigger(fixture, monkeypatch)
    reached = []

    class Reached(Exception):
        pass

    def original(client, rid):
        reached.append(client.target.session_id)
        raise Reached()

    monkeypatch.setattr(service, "_original", original)
    mark = len(fixture.control_versions), len(fixture.files.calls)
    with pytest.raises(Reached):
        trigger()
    assert reached == [fixture.destination.session_id]  # the occurrence goes on, on the new SID
    assert abandon.row(fixture).last_switch.basis == "incompatible"
    assert source_calls(fixture, since=mark) == ([], [], [])


def test_a_stuck_admission_meeting_sends_is_abandoned_in_request_from_a_durable_start(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    now, _ = fake_time(monkeypatch)
    warm_metadata(fixture, monkeypatch)
    set_admitting(fixture, "stuck-rid")
    following(submit_on_source(fixture, "behind-stuck"), "behind-stuck")
    assert abandon.row(fixture).clock("admitting").subject == "stuck-rid"  # from the first blocked send
    restarted(fixture)  # another replica takes the next send: the bound still holds
    now[0] += behavior_native_sessions.STUCK_ADMISSION_SECONDS
    admitted(submit_on_source(fixture, "behind-stuck"), fixture.destination.session_id)
    switched = abandon.row(fixture)
    assert (switched.last_switch.basis, switched.last_switch.source_status) == ("stuck_admission", "active")
    assert source_calls(fixture)[1:] == ([], [])  # the switch never read its files or stopped it
