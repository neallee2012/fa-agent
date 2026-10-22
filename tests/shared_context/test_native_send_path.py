"""ADR-0009 phase 3: the send path locates the Agent's session by its binding row and expects V_rec.

A compatible drift is recorded and runs; a refusal of a row-located submit is resolved in the request (the same RID
at most once more, read first when it may have been taken) or handed to the client as a same-RID resend
(``server_following``), never shown as a version refusal. M5: a fresh SID of the same version. Platform and Hosted
are offline doubles (one ``NativeTransport`` per SID over one shared RID store), except the M5 case, which runs the
real source and destination Hosted runtimes behind the real API (``file_api``)."""

import json
import logging
import time
from types import SimpleNamespace

import httpx
import pytest
from azure.ai.projects.models import AgentSessionResource

from shared_context import behavior_native_sessions, behavior_native_turn_api
from shared_context.behavior_foundry import PLATFORM_NO_RETRY
from shared_context.behavior_native_sessions import (
    NativeSessions, fresh_session_id, native_session_id, native_sessions,
)
from shared_context.behavior_native_turn_wire import (
    NativeEnvironmentNotice, NativeEnvironmentView, NativeReceipt, before_dispatch,
)
from shared_context.errors import Problem
from test_native_binding import CountingRepository, bound, claim, declared, row  # noqa: F401
from test_native_turn_api import PREFIX, NativeTransport, native_api, submit  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key, token  # noqa: F401
from test_native_session_prepare_api import project_client
import test_native_abandon_source as abandon
from test_native_abandon_source import binding_rows  # noqa: F401
from test_native_automatic_environment import advance
from test_native_files_api import file_api, file_loops, local_env  # noqa: F401
from test_native_follow_no_wake import source_calls


REAL_READ_PLATFORM_SESSION = behavior_native_sessions._read_platform_session
RECORD_READS = frozenset({"read", "conversation", "history", "result", "turn_status", "progress"})


def shared(case, monkeypatch, versions=None):
    """Every SID in ``versions`` (plus the fixture's) answers from one shared RID store and one Agent record.

    ``agent.sid``/``agent.environment``: Hosted's Agent record. A submit to another SID than ``agent.sid`` is refused
    by Hosted's admission check (409 with ``not_dispatched``). ``agent.adopt``: a read through the current SID adopts
    a never-started receipt of an earlier SID (M9). ``agent.on_submit``: runs before a submit is answered. Platform
    metadata names every one of these SIDs active (``_read_platform_session``); a test overrides it after this."""
    base = case.remote
    transports = {base.target.session_id: base}
    for sid, version in (versions or {}).items():
        other = NativeTransport(base.target.model_copy(update={"session_id": sid, "agent_version": version}))
        other.records, other.preparation = base.records, base.preparation
        transports[sid] = other
    agent = SimpleNamespace(sid=base.target.session_id, environment=None, previous=set(), on_submit=None,
                            refuse={})

    def route(request):
        sid = request.url.params["agent_session_id"]
        remote = transports[sid]
        remote.session_binding, remote.environment = agent.sid, agent.environment
        command = json.loads(request.content)
        operation = command["operation"]
        expected = request.headers.get("x-client-fa-expected-version")
        version_ok = remote.vm_version is None or expected == remote.vm_version
        if operation == "submit" and version_ok:
            if agent.on_submit is not None:
                hook, agent.on_submit = agent.on_submit, None
                hook()
            if sid in agent.refuse:
                status, body, *taken = agent.refuse.pop(sid)
                if taken:
                    # The SID did take the RID; only its answer came back refusal-shaped (lost, then refused).
                    remote(request)
                    rid = command["command"]["request_id"]
                    remote.records[rid] = NativeReceipt.model_validate({
                        **remote.records[rid].model_dump(), **taken[0]})
                else:
                    remote.calls.append((request, None))
                return httpx.Response(status, json={"error": body})
            if sid != agent.sid:
                remote.calls.append((request, None))
                return httpx.Response(409, json={"error": {
                    "code": "NATIVE_SESSION_CHANGED", "message": "private", "not_dispatched": True}})
        pending_target = (agent.environment is not None and agent.environment.state == "pending"
                          and agent.environment.hosted_session_id == sid)
        # Hosted's ``replaced()``: stricter (any other SID, whatever its version) only for a row-located call.
        located = request.headers.get("x-client-fa-locator") == "binding-row"
        newer = int(transports[agent.sid].target.agent_version) > int(remote.target.agent_version)
        if operation in RECORD_READS and version_ok and sid != agent.sid and not pending_target and (located or newer):
            remote.calls.append((request, None))
            return httpx.Response(409, json={"error": {
                "code": "NATIVE_SESSION_CHANGED", "message": "private", "not_dispatched": True, "reason": "replaced",
                "actual_version": remote.target.agent_version}})
        if operation == "read" and version_ok and sid == agent.sid:
            original = remote.records.get(command["request_id"])
            if (original is not None and original.status == "accepted"
                    and original.hosted_session_id in agent.previous):
                remote.records[original.request_id] = original.model_copy(update={"hosted_session_id": sid})
        return remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(route))
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: "41")
    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session",
                        lambda client, **_: client.target if client.target.session_id in transports else None)
    case.composition._native_sessions = NativeSessions(case.composition)
    return transports, agent


def operations(remote, name, *, sid=None):
    return [request.headers.get("x-client-fa-expected-version") for request, command in remote.calls
            if json.loads(request.content)["operation"] == name]


def switched_to(case, sid, version="41", previous=None):
    """The row after a committed switch from the fixture's SID (another API process wrote it)."""
    view = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="switch", state="ready", previous_hosted_session_id=previous or case.sid,
        previous_agent_version="41", hosted_session_id=sid, hosted_agent_version=version)
    return view, case.store.record_switch(case.agent_id, view)


def metadata(monkeypatch, versions, status="active"):
    def get_session(name, sid):
        return AgentSessionResource({"agent_session_id": sid, "status": status,
                                     "version_indicator": {"type": "version_ref", "agent_version": versions[sid]}})

    def get_version(name, version):
        return SimpleNamespace(name=name, version=version, definition={
            "kind": "hosted", "environment_variables": {"FA_NATIVE_TURNS": "foundry-native-v2"}})

    project_client(monkeypatch, get_session=get_session, get_version=get_version)


def next_tick(monkeypatch):
    """The next request starts on a later ``monotonic()`` tick than the last one's binding row read, as on Linux.

    Windows ticks every 15.6 ms, so two back-to-back submits can share a tick and the second reuses the first's row
    read (``bound_row`` keeps a read started at or after the submit's start), missing a row written between them.
    One second later stays far inside ``SELECTOR_CHECK_SECONDS`` and ``METADATA_REUSE_SECONDS``; the clock still runs,
    so the in-request wait ends."""
    real = time.monotonic
    # Both modules: shifting only the sessions clock would date the kept read after every submit start, so it would
    # always be reused. One fixed shift covers one more submit; a third submit in the same test needs another tick.
    for module in (behavior_native_sessions, behavior_native_turn_api):  # the kept row's tick and the submit's start
        monkeypatch.setattr(module, "monotonic", lambda: real() + 1)


# --- §4 step 6, phase 3 test column: a different compatible version -------------------------------------------------

def test_woken_at_another_compatible_version_sends_the_same_rid_once_and_records_it(bound, monkeypatch):
    case = bound
    claim(case)
    transports, _agent = shared(case, monkeypatch)
    metadata(monkeypatch, {case.sid: "40"})
    case.remote.vm_version = "40"  # the platform woke the same session object on 40 (a newer image says so)
    accepted = submit(case)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["hosted_session_id"] == case.sid
    # The drift is found on the session read (no RID yet); the submit itself expects 40 and is admitted once.
    assert operations(case.remote, "submit") == ["40"]
    assert list(case.remote.records) == ["native-rid"]
    recorded = row(case)
    assert (recorded.observed.agent_version, recorded.observed.source) == ("40", "vm")
    # The drift notice went out with this owner response and was cleared once it was written (phase 2c).
    assert recorded.drift and recorded.notice() is None and recorded.notice_to_version == "40"
    assert accepted.json()["environment_notice"] == NativeEnvironmentNotice(
        from_version="41", to_version="40").model_dump(mode="json", exclude_none=True)
    # The next send expects the recorded actual version directly: no refusal in between.
    case.remote.completed("native-rid")
    again = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert again.status_code == 202, again.text
    assert operations(case.remote, "session_read")[-1] == "40"


@pytest.mark.parametrize("old_image", [False, True])
def test_a_drift_between_the_session_read_and_the_submit_dispatches_the_same_rid_once(bound, monkeypatch, old_image):
    case = bound
    claim(case)
    transports, agent = shared(case, monkeypatch)
    metadata(monkeypatch, {case.sid: "40"})
    case.remote.old_image = old_image

    def woke_on_40():
        case.remote.vm_version = "40"

    agent.on_submit = woke_on_40
    accepted = submit(case)
    assert accepted.status_code == 202, accepted.text
    # Refused at 41 before dispatch; one resend at 40 (an old image's refusal is read first: missing, then sent).
    assert operations(case.remote, "submit") == ["41", "40"]
    assert operations(case.remote, "read") == ([] if not old_image else ["40"])
    assert list(case.remote.records) == ["native-rid"]
    assert (row(case).observed.agent_version, row(case).observed.source) == (
        "40", "metadata-active" if old_image else "vm")


def test_an_old_images_metadata_guess_writes_nothing_until_the_vm_accepts_it(bound, monkeypatch):
    case = bound
    claim(case)
    transports, agent = shared(case, monkeypatch)
    metadata(monkeypatch, {case.sid: "39"})  # the metadata names 39, but the VM really runs 40
    case.remote.old_image = True
    case.remote.vm_version = "40"
    before = row(case)
    refused = submit(case)
    assert refused.status_code == 409, refused.text
    assert row(case) == before  # neither 39 nor a drift flag: nothing the VM confirmed
    assert operations(case.remote, "submit") == []


# --- §4 step 6 "SID changed": 409 without not_dispatched -------------------------------------------------------------

def test_a_refusal_without_not_dispatched_reads_the_row_then_the_rid_before_any_resend(bound, monkeypatch):
    case = bound
    claim(case)
    new_sid = native_session_id(case.remote.target, "unused", version="42")
    transports, agent = shared(case, monkeypatch, {new_sid: "42"})

    def moved():
        switched_to(case, new_sid, "42")
        agent.sid = new_sid
        agent.refuse[case.sid] = (409, {"code": "NATIVE_SESSION_CHANGED", "message": "private"})  # an old image

    agent.on_submit = moved
    accepted = submit(case)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["hosted_session_id"] == new_sid
    new = transports[new_sid]
    names = [json.loads(request.content)["operation"] for request, _ in new.calls]
    assert names.index("read") < names.index("submit")  # the RID was read through the new SID first
    assert operations(new, "submit") == ["42"] and list(case.remote.records) == ["native-rid"]


def test_a_finished_original_found_through_the_new_sid_is_returned_not_resent(bound, monkeypatch):
    case = bound
    claim(case)
    new_sid = native_session_id(case.remote.target, "unused", version="42")
    transports, agent = shared(case, monkeypatch, {new_sid: "42"})

    def taken_then_moved():
        switched_to(case, new_sid, "42")
        agent.sid = new_sid
        # The old SID took the RID and finished it; its answer came back as a bare refusal.
        agent.refuse[case.sid] = (409, {"code": "NATIVE_SESSION_CHANGED", "message": "private"},
                                  {"status": "completed", "turn_number": 1, "result": _result()})

    agent.on_submit = taken_then_moved
    found = submit(case)
    assert found.status_code == 200 and found.json()["status"] == "completed", found.text
    assert operations(transports[new_sid], "submit") == []


@pytest.mark.parametrize("status", ["accepted", "started"])
def test_an_unfinished_rid_of_the_old_sid_is_never_resent_to_the_new_sid(bound, monkeypatch, status):
    """§7a (e)/M9: a never-started one is adopted by the new SID on the read (it runs there once, by Hosted's own
    create-or-read); a started one is reported as it is. Neither is admitted again."""
    case = bound
    claim(case)
    new_sid = native_session_id(case.remote.target, "unused", version="42")
    transports, agent = shared(case, monkeypatch, {new_sid: "42"})

    def left_then_moved():
        switched_to(case, new_sid, "42")
        agent.sid, agent.previous = new_sid, {case.sid}
        agent.refuse[case.sid] = (409, {"code": "NATIVE_SESSION_CHANGED", "message": "private"}, {"status": status})

    agent.on_submit = left_then_moved
    reported = submit(case)
    assert reported.status_code in {200, 202}, reported.text
    receipt = reported.json()
    assert receipt["request_id"] == "native-rid" and receipt["status"] == status
    assert list(case.remote.records) == ["native-rid"]
    if status == "accepted":
        assert receipt["hosted_session_id"] == new_sid  # adopted; the same RID's create-or-read only reads it back
    else:
        assert receipt["hosted_session_id"] == case.sid
        assert operations(transports[new_sid], "submit") == []


# --- §4 step 6 "inconsistent" (M3): V_obs = V_rec --------------------------------------------------------------------

def test_a_409_at_the_recorded_version_repairs_the_row_and_sends_the_same_rid_to_the_new_sid(bound, monkeypatch, caplog):
    case = bound
    claim(case)
    new_sid = native_session_id(case.remote.target, "unused", version="42")
    transports, agent = shared(case, monkeypatch, {new_sid: "42"})
    view = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="switch", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id=new_sid, hosted_agent_version="42")

    def committed_without_the_row():
        # A follow committed in Hosted; its API crashed before the row write.
        agent.sid, agent.environment = new_sid, view

    agent.on_submit = committed_without_the_row
    before = row(case)
    with caplog.at_level(logging.WARNING):
        accepted = submit(case)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["hosted_session_id"] == new_sid
    repaired = row(case)
    assert (repaired.hosted_session_id, repaired.created_agent_version) == (new_sid, "42")
    assert repaired.earlier(case.sid)
    assert before.observed.agent_version == "41" and not repaired.drift  # not drift: no drift CAS
    assert "NATIVE_VERSION_OBSERVED" not in caplog.text and "NATIVE_BINDING_DIVERGED" in caplog.text
    assert operations(case.remote, "submit") == ["41"] and operations(transports[new_sid], "submit") == ["42"]
    assert list(case.remote.records) == ["native-rid"]


def test_an_unreadable_definition_still_repairs_the_row_with_the_send_profile_and_sends_the_same_rid(bound, monkeypatch, caplog):
    """M2: before 2d-i the repair wrote the row and resent; a transient ``get_version`` failure must not turn that
    into 409. The row records v1, as before; the deploy gate reports it when the version declared otherwise."""
    case = bound
    claim(case)
    new_sid = native_session_id(case.remote.target, "unused", version="42")
    transports, agent = shared(case, monkeypatch, {new_sid: "42"})
    declared(case, monkeypatch, **{"42": Problem(503, "AGENT_VERSION_FOLLOWING", "unreadable")})
    view = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="switch", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id=new_sid, hosted_agent_version="42")

    def committed_without_the_row():
        agent.sid, agent.environment = new_sid, view

    agent.on_submit = committed_without_the_row
    with caplog.at_level(logging.WARNING):
        accepted = submit(case)
    assert accepted.status_code == 202 and accepted.json()["hosted_session_id"] == new_sid, accepted.text
    repaired = row(case)
    assert (repaired.hosted_session_id, repaired.state_profile) == (new_sid, "foundry-native-v2")  # the send profile
    assert "NATIVE_BINDING_SWITCH_PROFILE_UNREAD code=AGENT_VERSION_FOLLOWING" in caplog.text
    assert operations(case.remote, "submit") == ["41"] and operations(transports[new_sid], "submit") == ["42"]
    assert list(case.remote.records) == ["native-rid"]


def test_a_row_behind_the_agent_record_is_repaired_on_the_session_read(bound, monkeypatch):
    """A same-version switch (M5) committed in Hosted whose row write was lost: the selector still names the version
    the row's SID was created on, so that SID may be read, and its session read names the new SID. (When the
    selector moved, the row's SID is not called at all: ``test_a_moved_selector_never_calls_the_rows_sid_*``.)"""
    case = bound
    claim(case)
    real_id = case.composition.config.identity.managed_identity_client_id
    new_sid = fresh_session_id(case.remote.target, real_id, current_session_id=case.sid, version="41")
    transports, agent = shared(case, monkeypatch, {new_sid: "41"})
    agent.sid = new_sid
    agent.environment = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="switch", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id=new_sid, hosted_agent_version="41")
    accepted = submit(case)
    assert accepted.status_code == 202 and accepted.json()["hosted_session_id"] == new_sid, accepted.text
    assert operations(case.remote, "submit") == [] and row(case).hosted_session_id == new_sid


@pytest.mark.parametrize("found", ["submit_refusal", "session_read"])
def test_a_switch_repaired_inside_the_request_answers_with_the_post_switch_rows_notice(bound, monkeypatch, found):
    """The repair writes the switch with ``record_switch``, which sets the phase 2c switch notice (M3 found it
    either on the submit's refusal or on the session read). The owner's response is built from the client that
    answered (the new SID) and its post-switch row, so it carries that notice, and the route clears it once
    delivered; the row located at the start of the request (the old SID, no notice) is never the source."""
    case = bound
    claim(case)
    real_id = case.composition.config.identity.managed_identity_client_id
    new_sid = fresh_session_id(case.remote.target, real_id, current_session_id=case.sid, version="41")
    transports, agent = shared(case, monkeypatch, {new_sid: "41"})
    view = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="switch", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id=new_sid, hosted_agent_version="41")

    def committed_without_the_row():
        agent.sid, agent.environment = new_sid, view

    if found == "session_read":
        committed_without_the_row()
    else:
        agent.on_submit = committed_without_the_row
    accepted = submit(case)
    assert accepted.status_code == 202 and accepted.json()["hosted_session_id"] == new_sid, accepted.text
    assert accepted.json()["environment_notice"] == {"kind": "environment_switched_without_files"}
    assert row(case).hosted_session_id == new_sid and row(case).notice() is None  # delivered, then cleared


def test_the_stricter_replaced_check_never_reaches_the_owner_on_a_read(bound, monkeypatch):
    """ADR-0009 §2 phase 3: Hosted now refuses record reads through any SID that is neither current nor the pending
    target, whatever its version. Reads locate by the binding row, so only a row behind Hosted's record (a commit
    whose row write was lost) meets the refusal, and it is repaired from that SID's session read: no 409 is shown."""
    case = bound
    claim(case)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    real_id = case.composition.config.identity.managed_identity_client_id
    fresh = fresh_session_id(case.remote.target, real_id, current_session_id=case.sid, version="41")
    transports, agent = shared(case, monkeypatch, {fresh: "41"})
    agent.sid = fresh  # committed in Hosted (same version, M5); the row still names the old SID
    agent.environment = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="switch", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id=fresh, hosted_agent_version="41")
    read = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert read.status_code == 200 and read.json()["status"] == "completed", read.text
    assert row(case).hosted_session_id == fresh and row(case).earlier(case.sid)
    again = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert again.status_code == 200
    refused_reads = [request for request, command in case.remote.calls if command is None]
    assert len(refused_reads) == 1  # the stale row met the refusal once; the repaired row reads the fresh SID


# --- ADR-0009 §10 phase 0 guarantee on the row-located path ---------------------------------------------------------

def _following(case, monkeypatch, state="pending", hosted_session_id="fa-native-destination"):
    """``advance`` replaced by a recorder that calls no session; the view it returns."""
    followed = []

    def advance(self, client, command, *, before_send):
        followed.append(command.expected_agent_version)
        return NativeEnvironmentView(
            agent_id=case.agent_id, request_id=command.request_id, state=state,
            reason="source_active" if state == "pending" else None, previous_hosted_session_id=case.sid,
            previous_agent_version="41", hosted_session_id=hosted_session_id, hosted_agent_version="41")

    monkeypatch.setattr(NativeSessions, "advance", advance)
    return followed


def test_a_moved_selector_never_calls_the_rows_sid_before_the_follow(bound, monkeypatch):
    """Review probe: the row's SID (created on 41, idle) is not the version the selector now picks (42). The
    follow-up's conversation prefetch (removed 2026-10-22) and the session read both used to reach it first,
    which wakes an idle source on the platform; the retained follow decides now, and nothing reaches that SID."""
    case = bound
    assert submit(case).status_code == 202  # claims the row at the fixture's SID
    case.remote.completed("native-rid")
    transports, agent = shared(case, monkeypatch)
    metadata(monkeypatch, {case.sid: "41"}, status="idle")
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: "42")
    followed = _following(case, monkeypatch)
    before = len(case.remote.calls)
    answer = submit(case, rid="native-rid-2", conversation_id="native-cid")
    error = answer.json()["error"]
    assert error["not_dispatched"] is True and error["server_following"] is True, answer.text
    assert followed == ["42"]
    assert [json.loads(request.content)["operation"] for request, _ in case.remote.calls[before:]] == []


@pytest.mark.parametrize("selected", ["41", "42"])
def test_with_2di_on_a_follow_up_reads_its_conversation_only_after_the_rows_session_read(bound, monkeypatch, selected):
    """Dev 2026-10-22: with FA_NATIVE_SESSION_GUARD_ENFORCE the submit's conversation prefetch (#942) and the session
    read reached the row's SID within 2-32 ms, and the platform held one of them ~2 s (agentsv2
    ``platform_preprocessing_ms`` 2,064-2,123) on every warm submit after 07:10Z; sequential reads were never held.
    The conversation is now read after the session read, whatever version the selector names (2d-i P7)."""
    from test_native_turn_api import in_flight

    case = bound
    monkeypatch.setenv("FA_NATIVE_SESSION_GUARD_ENFORCE", "1")
    claim(case)
    shared(case, monkeypatch)
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: selected)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    most = in_flight(monkeypatch)
    before = len(case.remote.calls)
    answer = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert answer.status_code == 202, answer.text
    operations = [json.loads(request.content)["operation"] for request, _ in case.remote.calls[before:]]
    assert operations == ["session_read", "conversation", "submit"]
    assert {request.url.params["agent_session_id"] for request, _ in case.remote.calls} == {case.sid}
    assert most == {case.sid: 1}


def test_with_2di_on_a_follow_up_resent_in_its_request_after_a_switch_invokes_its_sid_one_call_at_a_time(
        bound, monkeypatch):
    """The first attempt meets AGENT_VERSION_FOLLOWING (the in-request switch runs); the same RID resent in this
    request (``wait_in_request``) also reads its session and then its conversation, never two calls at once."""
    from test_native_turn_api import environment_view, in_flight

    case = bound
    monkeypatch.setenv("FA_NATIVE_SESSION_GUARD_ENFORCE", "1")
    claim(case)
    shared(case, monkeypatch)
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: "41")
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    admission, switched = NativeSessions.admission, []

    def following_once(sessions, client, **options):
        if not switched:
            raise Problem(503, "AGENT_VERSION_FOLLOWING", "OFFLINE the Agent is changing environments")
        return admission(sessions, client, **options)

    monkeypatch.setattr(NativeSessions, "admission", following_once)
    monkeypatch.setattr(NativeSessions, "advance", lambda _s, client, command, **_: switched.append(1) or
                        environment_view(client, command, state="ready", hosted_session_id=case.sid))
    monkeypatch.setattr(behavior_native_turn_api.NativeTurnGateway, "_ready_handoffs", lambda *_: None)
    monkeypatch.setattr(behavior_native_turn_api, "_resend_sleep", lambda _seconds: None)
    most = in_flight(monkeypatch)
    before = len(case.remote.calls)
    answer = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert answer.status_code == 202, answer.text
    operations = [json.loads(request.content)["operation"] for request, _ in case.remote.calls[before:]]
    assert most == {case.sid: 1}
    # First attempt: session read, the RID's read (not taken), the switch; then the resend.
    assert switched == [1] and operations == ["session_read", "read", "session_read", "conversation", "submit"]


@pytest.mark.parametrize("status", ["failed", "deleted", "expired", "missing"])
def test_a_retired_or_deleted_rows_sid_is_never_called_and_the_follow_runs(bound, monkeypatch, status):
    """ADR-0009 §9: a cold process reads the row's SID's platform metadata (no invocation) before calling it, as
    main's reader did; a 404 or a retired session is never invoked, by a submit or a read."""
    case = bound
    claim(case)
    transports, agent = shared(case, monkeypatch)

    def platform(client, **_):
        if status == "missing":
            return None
        raise behavior_native_sessions._RetiredSession(503, "NATIVE_BOOTSTRAP_UNKNOWN", "retired")

    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", platform)
    followed = _following(case, monkeypatch)
    answer = submit(case)
    error = answer.json()["error"]
    assert error["not_dispatched"] is True and error["server_following"] is True, answer.text
    assert followed == ["41"]
    read = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert read.status_code == 409 and read.json()["error"]["code"] == "NATIVE_SESSION_CHANGED", read.text
    assert case.remote.calls == []


def _metadata_reads(monkeypatch, deleted=()):
    """``_read_platform_session`` recording each SID it reads; a SID in ``deleted`` is a 404 (``delete_session``)."""
    looked = []

    def platform(client, **_):
        looked.append(client.target.session_id)
        return None if client.target.session_id in deleted else client.target

    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", platform)
    return looked


def _later(monkeypatch, seconds=behavior_native_sessions.METADATA_REUSE_SECONDS + 1):
    """Past the work-delivery metadata window, far inside ``READER_IDLE_SECONDS``."""
    now = behavior_native_sessions.monotonic() + seconds
    monkeypatch.setattr(behavior_native_sessions, "monotonic", lambda: now)


def test_a_warm_process_never_calls_a_row_sid_deleted_since_its_last_use(bound, monkeypatch):
    """Dev 2026-10-15 M5: the row's SID answered a submit and a status read, then the platform deleted it. The next
    submit used to skip the metadata for a day and invoke the SID, which the platform re-created empty; a submit reads
    the metadata in its own request, so the 404 runs the follow instead and the SID is not called (ADR-0009 §10 /
    phase 3; §7a's second read then abandons it for a fresh SID of 41)."""
    case = bound
    claim(case)
    shared(case, monkeypatch)
    deleted = set()
    looked = _metadata_reads(monkeypatch, deleted)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    assert looked == [case.sid]
    _later(monkeypatch)
    assert case.client.get(PREFIX + "/requests/native-rid", headers=case.headers).status_code == 200
    assert looked == [case.sid] * 2  # a read that invokes the SID reads its metadata too, once per window
    _later(monkeypatch)
    deleted.add(case.sid)
    followed = _following(case, monkeypatch)
    before = len(case.remote.calls)
    answer = submit(case, rid="native-rid-2", conversation_id="native-cid")
    error = answer.json()["error"]
    assert error["not_dispatched"] is True and error["server_following"] is True, answer.text
    assert followed == ["41"] and looked[1:] and set(looked) == {case.sid}
    assert case.remote.calls[before:] == []


def test_a_burst_of_submits_reads_the_row_sids_metadata_once_per_window(bound, monkeypatch):
    """One submit and a follow-up inside the window share one metadata read;
    the next submit after the window reads it again."""
    case = bound
    claim(case)
    shared(case, monkeypatch)
    looked = _metadata_reads(monkeypatch)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    assert submit(case, rid="native-rid-2", conversation_id="native-cid").status_code == 202
    case.remote.completed("native-rid-2")
    assert looked == [case.sid]
    _later(monkeypatch)
    assert submit(case, rid="native-rid-3", conversation_id="native-cid").status_code == 202
    assert looked == [case.sid] * 2


def test_a_warm_submits_metadata_read_runs_alongside_its_selector_read(bound, monkeypatch):
    """Latency: the submit's serial platform depth before the session call stays one round trip. Before this read
    was added a warm submit made only the selector read; now the selector and metadata reads each wait for the other
    to have started, which only completes when they run at the same time (a serial pair times out and fails)."""
    from threading import Event

    case = bound
    claim(case)
    shared(case, monkeypatch)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    _later(monkeypatch)
    started, overlapped = {"selector": Event(), "metadata": Event()}, []

    def both(name, other, value):
        started[name].set()
        overlapped.append((name, started[other].wait(timeout=5), len(case.remote.calls)))
        return value

    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: both("selector", "metadata", "41"))
    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session",
                        lambda client, **_: both("metadata", "selector", client.target))
    before = len(case.remote.calls)
    assert submit(case, rid="native-rid-2", conversation_id="native-cid").status_code == 202
    # One selector read and one metadata read, concurrent, both before the first call to the session.
    assert sorted(overlapped) == [("metadata", True, before), ("selector", True, before)]


def _warm(case, monkeypatch):
    """A warm process: the row's SID answered a submit, then the metadata window passed (well inside the day)."""
    claim(case)
    shared(case, monkeypatch)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    _later(monkeypatch)


def _budget():
    return {**PLATFORM_NO_RETRY, "connection_timeout": behavior_native_sessions.METADATA_BUDGET_SECONDS,
            "read_timeout": behavior_native_sessions.METADATA_BUDGET_SECONDS}


def _failing_platform(monkeypatch, failure):
    """The real ``_read_platform_session`` over a ``get_session`` that fails as ``failure`` says: an HTTP status, a
    transport timeout, or a session status. Returns (AIProjectClient constructions, get_session SIDs)."""
    from azure.core.exceptions import HttpResponseError, ServiceResponseTimeoutError
    gets = []

    def get_session(name, sid):
        gets.append(sid)
        if failure == "timeout":
            raise ServiceResponseTimeoutError("the platform did not answer")
        if isinstance(failure, int):
            error = HttpResponseError("platform")
            error.status_code = failure
            raise error
        return AgentSessionResource({"agent_session_id": sid, "status": failure,
                                     "version_indicator": {"type": "version_ref", "agent_version": "41"}})

    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", REAL_READ_PLATFORM_SESSION)
    return project_client(monkeypatch, get_session=get_session), gets


@pytest.mark.parametrize("failure", [503, 500, 429, "timeout", "creating", "updating"])
def test_a_warm_submit_whose_metadata_read_fails_is_sent_as_on_main(bound, monkeypatch, caplog, failure):
    """Review probe: a warm process (main skipped this read for a day after a use) whose metadata read fails (5xx,
    429, timeout) or finds the SID creating or updating sends anyway: one read, no retry, one warning (was 503)."""
    case = bound
    _warm(case, monkeypatch)
    constructed, gets = _failing_platform(monkeypatch, failure)
    with caplog.at_level(logging.WARNING, logger=behavior_native_sessions.__name__):
        sent = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert sent.status_code == 202, sent.text
    assert gets == [case.sid] and [kwargs for *_, kwargs in constructed] == [_budget()]
    assert [record.getMessage().split()[0] for record in caplog.records
            if "METADATA" in record.getMessage()] == ["NATIVE_BINDING_METADATA_UNCONFIRMED"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_warm_submit_whose_metadata_read_is_denied_calls_nothing(bound, monkeypatch, status):
    """401/403 stay errors on a warm process too: the follow-up's conversation read never reaches the SID the send
    could not check."""
    case = bound
    _warm(case, monkeypatch)
    _failing_platform(monkeypatch, status)
    before = len(case.remote.calls)
    refused = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert refused.status_code == status, refused.text
    assert case.remote.calls[before:] == []


def test_a_cold_submit_whose_metadata_read_fails_is_refused_as_on_main(bound, monkeypatch):
    """A cold process (here: right after ``forget``) has not used the SID, so a failed read stays 503 as on main,
    with main's retrying read (no budget), and nothing is called."""
    case = bound
    _warm(case, monkeypatch)
    native_sessions(case.composition).forget()
    constructed, gets = _failing_platform(monkeypatch, 503)
    before = len(case.remote.calls)
    refused = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "NATIVE_BOOTSTRAP_UNKNOWN", refused.text
    # Only the session read asks (main's retrying read), and nothing is called.
    assert gets == [case.sid] and all(kwargs["retry_total"] == 1 and kwargs["read_timeout"] == 10
                                           for *_, kwargs in constructed)
    assert case.remote.calls[before:] == []


def test_a_hung_metadata_read_holds_a_warm_submit_no_longer_than_its_budget(bound, monkeypatch):
    """Neal: no extra send latency. A metadata read that never answers holds the send for the budget only (the wait
    stops there; azure-core's transport timeouts end the read itself), and the send goes on."""
    from threading import Event
    from time import perf_counter

    case = bound
    _warm(case, monkeypatch)
    monkeypatch.setattr(behavior_native_sessions, "METADATA_BUDGET_SECONDS", 0.5)
    release, options = Event(), []

    def hung(client, **kwargs):
        options.append(kwargs)
        release.wait(timeout=10)
        return client.target

    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", hung)
    try:
        started = perf_counter()
        sent = submit(case, rid="native-rid-2", conversation_id="native-cid")
        held = perf_counter() - started
        assert not release.is_set()
    finally:
        release.set()
    assert sent.status_code == 202, sent.text
    assert held < 0.5 + 2.5, held  # the budget plus the offline submit itself, far below the hung read
    assert options == [_budget()]


def test_a_history_read_never_calls_a_row_sid_deleted_since_its_last_use(bound, monkeypatch):
    """A read that invokes the SID (history, status) reads its metadata as a send does, so a SID deleted since its
    last use is not called (and not re-created empty): the read is refused without invoking anything."""
    case = bound
    claim(case)
    shared(case, monkeypatch)
    deleted = set()
    looked = _metadata_reads(monkeypatch, deleted)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    deleted.add(case.sid)
    _later(monkeypatch)
    before = len(case.remote.calls)
    history = case.client.get(PREFIX + "/conversations/native-cid", headers=case.headers)
    status = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    for read in (history, status):
        assert read.status_code == 409 and read.json()["error"]["code"] == "NATIVE_SESSION_CHANGED", read.text
    assert case.remote.calls[before:] == []
    assert looked == [case.sid] * 3  # a 404 keeps nothing, so each read asks again


def test_status_polls_every_second_read_the_row_sids_metadata_once_per_window(bound, monkeypatch):
    """The workbench polls about once a second: at most one ``get_session`` per SID per ``METADATA_REUSE_SECONDS``."""
    case = bound
    claim(case)
    shared(case, monkeypatch)
    now = abandon.clock(monkeypatch)
    looked = _metadata_reads(monkeypatch)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    for _ in range(30):
        now[0] += 1
        assert case.client.get(PREFIX + "/requests/native-rid", headers=case.headers).status_code == 200
    assert looked == [case.sid] * 4  # the submit at 0 s, then polls at 10, 20 and 30 s


def test_only_a_row_located_call_asks_hosted_for_the_stricter_replaced_check(bound, monkeypatch):
    """An Agent without a usable row is still read through the selector's candidate SID, which may be neither the
    current SID nor the pending target during a retarget or a rolling deploy: Hosted keeps phase 2a's rule for it."""
    case = bound
    assert submit(case).status_code == 202  # no row yet: the legacy locator, which claims the row
    first = len(case.remote.calls)
    case.remote.completed("native-rid")
    next_tick(monkeypatch)  # else a tied tick reuses the first submit's "no row" read, from before its claim
    assert submit(case, rid="native-rid-2", conversation_id="native-cid").status_code == 202
    locators = [request.headers.get("x-client-fa-locator") for request, _ in case.remote.calls]
    assert set(locators[:first]) == {None}
    assert set(locators[first:]) == {"binding-row"}


# --- §4 step 5 / §9: 403 NATIVE_TURNS_DISABLED on the same SID -------------------------------------------------------

def test_turns_disabled_on_the_same_sid_is_a_same_rid_resend_not_a_refusal(bound, monkeypatch, caplog):
    """Before 2d-i there is no in-request switch, so the API can not make a VM of another profile run the turn
    (ADR-0009 §9: the plan never produces one). No refusal is added: nothing was dispatched (the profile check runs
    before the body is read), the drift is recorded, the SID is marked, the retained follow runs once, and the
    client resends the same RID (``server_following``), which the workbench does automatically."""
    case = bound
    claim(case)
    transports, agent = shared(case, monkeypatch)
    refusal = before_dispatch(Problem(403, "NATIVE_TURNS_DISABLED", "private"), "profile", "40")
    agent.refuse[case.sid] = (403, refusal.body["error"])
    followed = []

    def advance(self, client, command, *, before_send):
        followed.append(command.expected_agent_version)
        return NativeEnvironmentView(
            agent_id=case.agent_id, request_id=command.request_id, state="pending", reason="source_active",
            previous_hosted_session_id=case.sid, previous_agent_version="41",
            hosted_session_id="fa-native-destination", hosted_agent_version="41")

    monkeypatch.setattr(NativeSessions, "advance", advance)
    with caplog.at_level(logging.WARNING):
        answer = submit(case)
    error = answer.json()["error"]
    assert answer.status_code == 503 and error["code"] == "AGENT_VERSION_FOLLOWING", answer.text
    assert error["not_dispatched"] is True and error["server_following"] is True
    assert error["request_id"] == "native-rid" and followed == ["41"]
    assert "NATIVE_SESSION_CALL_INCOMPATIBLE operation=submit" in caplog.text
    assert (row(case).observed.agent_version, row(case).drift) == ("40", True)
    assert case.remote.records == {}


@pytest.mark.parametrize("refusal", ["profile", "version"])
def test_a_definite_refusal_whose_follow_keeps_the_same_sid_asks_for_no_resend(bound, monkeypatch, refusal):
    """The phase 3 row's "keep the current response": a 403 on the same SID, or a definite refusal nothing resolves
    (here a version refusal of a SID whose metadata is not active, so no V_obs), runs the retained follow; when that
    follow leaves the Agent on the refusing SID, ``server_following`` is not set, so the workbench does not resend
    and wake the VM for ten minutes."""
    case = bound
    claim(case)
    transports, agent = shared(case, monkeypatch)
    metadata(monkeypatch, {case.sid: "41"}, status="idle")
    if refusal == "profile":
        refused = before_dispatch(Problem(403, "NATIVE_TURNS_DISABLED", "private"), "profile", "41")
        agent.refuse[case.sid] = (403, refused.body["error"])
    else:
        agent.refuse[case.sid] = (409, {"code": "NATIVE_SESSION_CHANGED", "message": "private",
                                        "not_dispatched": True, "reason": "version"})
    followed = _following(case, monkeypatch, state="current", hosted_session_id=case.sid)
    answer = submit(case)
    error = answer.json()["error"]
    assert error["not_dispatched"] is True and error["request_id"] == "native-rid", answer.text
    assert "server_following" not in error
    assert followed == ["41"] and case.remote.records == {}


def test_a_retired_generation_0_destination_raises_the_attempt_so_the_next_one_is_fresh(bound, monkeypatch):
    """M5: generation 0 of version 42 is neither the row's current nor a recorded previous SID (trimmed, or never
    recorded), so it is the first destination for 42; when it reads back retired, the next destination is fresh."""
    case = bound
    claimed = claim(case)
    sessions, client = native_sessions(case.composition), case.foundry.for_target(case.remote.target)
    real_id = case.composition.config.identity.managed_identity_client_id
    generation0 = native_session_id(case.remote.target, real_id, version="42")
    assert sessions.destination_session(client, "42", claimed) == (generation0, None)

    def platform(selected):
        assert selected.target.session_id == generation0
        raise behavior_native_sessions._RetiredSession(503, "NATIVE_BOOTSTRAP_UNKNOWN", "retired")

    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", platform)
    monkeypatch.setattr(NativeSessions, "selected_version", lambda self, client: "42")
    with pytest.raises(behavior_native_sessions._RetiredSession):
        sessions._destination(client, "42", lambda: pytest.fail("nothing is created"))
    raised = row(case)
    assert raised.attempt("42") == 1
    assert sessions.destination_session(client, "42", raised) == (
        fresh_session_id(case.remote.target, real_id, current_session_id=case.sid, version="42", attempt=1), 1)


# --- after a follow, existing conversations continue on the new SID; M5 ----------------------------------------------

def test_fresh_sids_follow_the_m5_algorithm_and_keep_generation_0_valid(bound):
    case = bound
    target, client_id = case.remote.target, "client"
    generation0 = native_session_id(target, client_id, version="41")
    fresh = fresh_session_id(target, client_id, current_session_id=generation0, version="41")
    assert fresh.startswith("fa-native-") and len(fresh) == len(generation0) and fresh != generation0
    assert fresh == fresh_session_id(target, client_id, current_session_id=generation0, version="41", attempt=0)
    assert fresh != fresh_session_id(target, client_id, current_session_id=generation0, version="41", attempt=1)
    assert fresh != fresh_session_id(target, client_id, current_session_id=fresh, version="41")
    sessions, client = native_sessions(case.composition), case.foundry.for_target(target)
    real_id = case.composition.config.identity.managed_identity_client_id
    claimed = claim(case)
    assert sessions.destination_session(client, "41", None) == (case.sid, None)  # no row: generation 0
    assert sessions.destination_session(client, "41", claimed) == (case.sid, None)  # the current SID's version
    assert sessions.destination_session(client, "42", claimed) == (
        native_session_id(target, real_id, version="42"), None)  # a first visit keeps generation 0
    decided = case.store.decide_abandon(case.agent_id, hosted_session_id=case.sid, basis="retired",
                                        source_status="missing", created_agent_version="41")
    assert decided is not None
    abandoned = row(case)
    expected = fresh_session_id(target, real_id, current_session_id=case.sid, version="41")
    assert sessions.destination_session(client, "41", abandoned) == (expected, 0)
    raised = case.store.raise_attempt(case.agent_id, hosted_session_id=case.sid, agent_version="41", attempt=0)
    assert sessions.destination_session(client, "41", raised) == (
        fresh_session_id(target, real_id, current_session_id=case.sid, version="41", attempt=1), 1)


def test_after_a_same_version_switch_existing_conversations_continue_on_the_fresh_sid(bound, monkeypatch):
    case = bound
    claim(case)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    real_id = case.composition.config.identity.managed_identity_client_id
    fresh = fresh_session_id(case.remote.target, real_id, current_session_id=case.sid, version="41")
    transports, agent = shared(case, monkeypatch, {fresh: "41"})
    view, _ = switched_to(case, fresh, "41")
    agent.sid, agent.environment = fresh, view
    sent_before = len(operations(case.remote, "submit"))
    follow_up = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert follow_up.status_code == 202, follow_up.text
    assert follow_up.json()["hosted_session_id"] == fresh
    assert operations(transports[fresh], "submit") == ["41"]
    assert len(operations(case.remote, "submit")) == sent_before  # nothing more went to the replaced SID


def _result():
    from shared_context.behavior_native_turn_wire import NativeResultReference

    return NativeResultReference(turn_number=1, turn_key="turn:1", result_digest="a" * 64, text_sha256="b" * 64,
                                 text_bytes=1, text_parts=1, checkpoint_sha256="c" * 64)


@pytest.mark.parametrize("file_api", [{"lane": "foundry-native-v1", "target_version": "41", "fresh": True}],
                         indirect=True)
def test_a_retired_sid_of_the_selected_version_gets_a_fresh_session_of_the_same_version(
        file_api, binding_rows, monkeypatch):
    """M5 against the real runtimes: the Agent's SID is generation 0 of the selected version 41 and the platform
    retired it. Before phase 3 no follow could make another session for 41 (same SID, and a follow needs a newer
    version); now the confirmed abandon decision gives a fresh SID of 41, and the owner's existing conversation
    continues there from its checkpoint. The retired SID is never invoked, read or stopped."""
    fixture = file_api
    now = abandon.clock(monkeypatch)
    abandon.claim(fixture)
    abandon.platform(fixture, monkeypatch, status="missing")
    first = advance(fixture)
    assert first.json().get("state") != "ready", first.text
    assert abandon.row(fixture).abandon is None  # one 404 of a compatible SID decides nothing (§7a (m))
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
    view = abandon.assert_abandoned(advance(fixture), fixture)
    assert (view["hosted_session_id"], view["hosted_agent_version"]) == (fixture.destination.session_id, "41")
    assert fixture.destination.session_id != fixture.source.session_id
    switched = abandon.row(fixture)
    assert (switched.hosted_session_id, switched.created_agent_version) == (fixture.destination.session_id, "41")
    assert switched.earlier(fixture.source.session_id) and switched.last_switch.basis == "retired"
    body = {"request_id": "after-the-fresh-session", "content": "Continue, please",
            "conversation_id": fixture.saved.conversation_id}
    sent = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json=body)
    assert sent.status_code == 202, sent.text
    assert sent.json()["hosted_session_id"] == fixture.destination.session_id
    assert source_calls(fixture) == ([], [], [])


@pytest.mark.parametrize("file_api", [{"lane": "foundry-native-v1", "target_version": "41", "fresh": True}],
                         indirect=True)
def test_a_warm_process_moves_a_session_deleted_since_its_last_use_to_a_fresh_sid_by_resending(
        file_api, binding_rows, monkeypatch):
    """The dev M5 sequence on a warm API process: the owner used the session, the platform deleted it, and the
    owner sends. The send never reaches the deleted SID: its 404 runs the follow, the workbench's same-RID resend
    after ``RETIRED_CONFIRM_SECONDS`` reads the second 404, which abandons it for a fresh SID of the same version,
    and the next resend lands there, continuing the conversation."""
    fixture = file_api
    now = abandon.clock(monkeypatch)
    abandon.claim(fixture)
    # The fixture's setup kept "no row" on the real clock; the row claimed just now is the locator from here on.
    native_sessions(fixture.case.composition).forget()
    status = ["active"]
    # One platform for both metadata reads: the send path's (``_read_platform_session``) and the follow's.
    abandon.platform(fixture, monkeypatch, status=lambda: status[0])
    listed = behavior_native_sessions._read_platform_session
    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", lambda client, **options: None if (
        status[0] == "missing" and client.target.session_id == fixture.source.session_id)
        else listed(client, **options))
    assert fixture.case.client.get(PREFIX + "/requests/original-rid", headers=fixture.case.headers).status_code == 200
    since = (len(fixture.control_versions), len(fixture.files.calls))
    status[0] = "missing"
    now[0] += behavior_native_sessions.METADATA_REUSE_SECONDS + 1
    body = {"request_id": "after-the-delete", "content": "Continue, please",
            "conversation_id": fixture.saved.conversation_id}
    for _ in range(2):  # the first 404, then the second at least RETIRED_CONFIRM_SECONDS later: abandoned, switched
        waiting = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json=body)
        error = waiting.json()["error"]
        assert error["not_dispatched"] is True and error["server_following"] is True, waiting.text
        now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
    switched = abandon.row(fixture)
    assert (switched.hosted_session_id, switched.created_agent_version) == (fixture.destination.session_id, "41")
    assert switched.earlier(fixture.source.session_id) and switched.last_switch.basis == "retired"
    sent = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json=body)
    assert sent.status_code == 202, sent.text
    assert sent.json()["hosted_session_id"] == fixture.destination.session_id != fixture.source.session_id
    assert source_calls(fixture, since=since) == ([], [], [])
