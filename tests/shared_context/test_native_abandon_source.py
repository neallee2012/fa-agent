"""ADR-0009 §7a abandon-source switch: acceptance rows (a)-(q), each against the real API, follow and Hosted runtimes.

The ``file_api`` fixture serves a real source and destination Hosted runtime over one shared ``fa/v1/turns`` store
behind the real API; only the platform (session metadata, version definitions, Session Files) is an offline double.
Before the G profile bump (phase 2d) every deployed profile is ``foundry-native-v1``, so a test makes the source
incompatible through its version definition's ``FA_NATIVE_TURNS`` (what ``get_version`` reports) or, for the
zero-call rows, by writing a binding row with another recorded profile.
"""

import copy
import logging
from types import SimpleNamespace

import pytest
from azure.ai.projects.models import AgentSessionResource
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from shared_context import behavior_native_sessions
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import NativeRead, NativeRequest, NativeSessionFollow, NativeSessionRead
from shared_context.errors import Problem
from shared_context.native_binding import NativeBindingSwitch, binding_store
from shared_context.native_turns import TURNS_STORE, NativeTaskInput, NativeTurnRuntime, _json, binding_task_id
from test_native_automatic_environment import advance
from test_native_environment import invoke
from test_native_files_api import (
    PREFIX, file_api, file_loops, follow, instruction_models, jwk_client, local_env, native_api, signing_key,
    synthetic_standing_v1, token,
)
from test_native_follow_no_wake import source_calls
from test_native_session_prepare_api import project_client
from test_native_turn_wire import submit
from test_native_turns import Harness, OfflineSdk

OLD_PROFILE = "foundry-native-v0"


@pytest.fixture(autouse=True)
def binding_rows(monkeypatch):
    """The binding rows live in an in-memory Context Table (the fixture's Table double serves no other entity)."""
    from conftest import InMemoryRepository
    from shared_context import native_binding

    repository = InMemoryRepository()
    monkeypatch.setattr(native_binding, "TableRepository", lambda _client: repository)
    return repository


def platform(fixture, monkeypatch, *, status=None, profile="foundry-native-v1", idle_timeout=3600):
    """The offline platform: ``status`` (a value or a callable) overrides the source's metadata status
    ("missing" is 404, "unavailable" a 503); ``profile`` is the source version definition's ``FA_NATIVE_TURNS``."""
    files, source = fixture.files, fixture.source

    def get_session(name, sid):
        if sid == source.session_id and status is not None:
            value = status() if callable(status) else status
            if value == "missing":
                raise ResourceNotFoundError("OFFLINE the platform deleted the source")
            if value == "unavailable":
                error = HttpResponseError("OFFLINE metadata unavailable")
                error.status_code = 503
                raise error
            return AgentSessionResource({
                "agent_session_id": sid, "status": value,
                "version_indicator": {"type": "version_ref", "agent_version": source.agent_version}})
        return files.get_session(name, sid)

    def get_version(name, version):
        return SimpleNamespace(name=name, version=version, definition={
            "kind": "hosted", "session_configuration": {"idle_timeout_seconds": idle_timeout},
            "environment_variables": {
                "FA_NATIVE_TURNS": profile if version == source.agent_version else "foundry-native-v1"}})

    project_client(
        monkeypatch, get_session=get_session, get_version=get_version, stop_session=files.stop_session,
        list_sessions=lambda name, **_: iter(files.get_session(name, sid) for sid in files.targets),
        list_session_files=files.list_session_files,
        download_session_file=files.download_session_file, upload_session_file=files.upload_session_file)


def clock(monkeypatch, start=1000.0):
    now = [start]
    monkeypatch.setattr(behavior_native_sessions, "monotonic", lambda: now[0])
    return now


def seed(fixture, rid, status, *, session_id=None, cid=None, turn_number=None):
    """An unfinished receipt the named session accepted (default: the source) in conversation ``cid`` (default: one
    of its own); the raw stored item is returned for byte-for-byte checks. Nothing runs it: no task input exists."""
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.native_turns import _Rid

    command = submit(rid, cid, text="An unfinished message " + rid)
    receipt = NativeReceipt(
        agent_id=fixture.source.agent_id, hosted_session_id=session_id or fixture.source.session_id, request_id=rid,
        conversation_id=cid or "native-" + rid, command=command.command, capture=command.capture,
        submit_digest=command.sha256, capture_digest=command.capture.sha256, status=status, turn_number=turn_number)
    record = _Rid(receipt=receipt, call_id="OFFLINE-seed", run_token=None if status == "accepted" else "run-" + rid)

    async def write():
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.create_item("rid:" + rid, _json(record))
            return await raw_item(store, rid)

    return fixture.hosted.portal.call(write)


async def raw_item(store, rid):
    item = await store.get_item("rid:" + rid)
    return item.value, item.etag


def raw(fixture, rid):
    async def read():
        async with fixture.runtime._store(TURNS_STORE) as store:
            return await raw_item(store, rid)
    return fixture.hosted.portal.call(read)


def agent_record(fixture):
    async def read():
        async with fixture.runtime._store(TURNS_STORE) as store:
            return (await store.get_item("agent")).value
    return fixture.hosted.portal.call(read)


def store(fixture):
    return binding_store(fixture.case.composition)


def row(fixture):
    return store(fixture).read(fixture.source.agent_id)


def claim(fixture, *, observed=None):
    return store(fixture).claim(fixture.source.agent_id, hosted_session_id=fixture.source.session_id,
                                created_agent_version=fixture.source.agent_version, observed_version=observed)


def read_request(fixture, rid):
    return fixture.case.client.get(PREFIX + "/requests/" + rid, headers=fixture.case.headers)


def incompatible_idle(fixture, monkeypatch, status="idle"):
    platform(fixture, monkeypatch, status=status, profile=OLD_PROFILE)
    fixture.files.stopped.add(fixture.source.session_id)


def assert_abandoned(response, fixture):
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["state"] == "ready", response.text
    assert view["previous_hosted_session_id"] == fixture.source.session_id
    assert view["hosted_session_id"] == fixture.destination.session_id
    assert view["files_migrated"] is False and view["files_omission_reason"] == "source_not_migratable"
    assert source_calls(fixture) == ([], [], [])  # never invoked, read through Files or stopped
    return view


REQUEST_KEYS = ("request_id", "conversation_id", "command", "capture", "submit_digest", "capture_digest")


def test_a_unfinished_source_receipts_stay_byte_for_byte_and_read_through_the_new_session(file_api, monkeypatch):
    """(a) with M9: the switch never rewrites a started receipt; one the source accepted and never started is adopted
    right after the commit (same RID and request, now this session's, ``adopted_from`` the source) and runs here once."""
    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    started, accepted = seed(fixture, "stuck-started", "started"), seed(fixture, "stuck-accepted", "accepted")
    sdk, seen = tracked(fixture)
    assert_abandoned(advance(fixture), fixture)
    assert raw(fixture, "stuck-started") == started
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("stuck-accepted", "completed").turn_number == 1
    assert [rid for rid, *_ in seen] == ["stuck-accepted"]
    adopted = raw(fixture, "stuck-accepted")[0]
    assert adopted["adopted_from"] == fixture.source.session_id
    assert adopted["receipt"]["hosted_session_id"] == fixture.destination.session_id
    assert {key: adopted["receipt"][key] for key in REQUEST_KEYS} == {key: accepted[0]["receipt"][key] for key in REQUEST_KEYS}
    read = invoke(fixture.hosted, fixture.identity, NativeRead(request_id="stuck-started"))
    assert read.status_code == 200, read.text
    assert {key: read.json()[key] for key in ("request_id", "status", "hosted_session_id", "submit_digest")} == {
        key: started[0]["receipt"][key] for key in ("request_id", "status", "hosted_session_id", "submit_digest")}


def test_b_a_left_admission_never_holds_the_new_session(file_api, monkeypatch):
    fixture = file_api
    incompatible_idle(fixture, monkeypatch)

    async def reserve():
        async with fixture.runtime._store(TURNS_STORE) as store:
            item = await store.get_item("agent")
            value = dict(item.value)
            value["admitting"] = {"request_id": "admitting-rid", "submit_digest": fixture.saved.submit_digest}
            await store.set_item("agent", value, if_match=item.etag)
    fixture.hosted.portal.call(reserve)
    assert_abandoned(advance(fixture), fixture)
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    assert state["hosted_session_id"] == fixture.destination.session_id and "admission_request_id" not in state
    record = agent_record(fixture)
    assert "admitting" not in record
    assert record["chain_task_id"] == binding_task_id(fixture.source.agent_id, fixture.destination.session_id)
    fresh = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json={
        "request_id": "first-after-switch", "content": "A new message after the switch"})
    assert fresh.status_code == 202, fresh.text
    missing = read_request(fixture, "admitting-rid")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NATIVE_RID_MISSING", missing.text
    again = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json={
        "request_id": "admitting-rid", "content": "The same RID sent to the new session"})
    assert again.status_code == 202, again.text
    assert again.json()["hosted_session_id"] == fixture.destination.session_id


def test_c_a_busy_source_chain_task_is_logged_not_waited_for(file_api, monkeypatch, caplog):
    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    seen = []

    async def busy(*_args, **_kwargs):
        return False

    async def original(*, task_id=None):
        seen.append(task_id)
        return {"id": task_id, "status": "in_progress", "lease": {"owner": "OFFLINE"},
                "payload": {"steering": {"pending_inputs": [{"request_id": "a"}, {"request_id": "b"}],
                                         "active_input": {"request_id": "c"}}}}

    fixture.runtime.task_observer = SimpleNamespace(quiet=busy, original=original)
    caplog.set_level(logging.WARNING)
    assert_abandoned(advance(fixture), fixture)
    left = [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("NATIVE_ENVIRONMENT_SOURCE_WORK_LEFT")]
    assert left == [f"NATIVE_ENVIRONMENT_SOURCE_WORK_LEFT task_id={seen[0]} task_status=in_progress lease=True "
                    "pending_inputs=2 active_input=True admitting=None"]


def test_d_a_deleted_source_switches_like_deleted(file_api, monkeypatch):
    fixture = file_api
    now = clock(monkeypatch)
    platform(fixture, monkeypatch, status="missing")
    first = advance(fixture)
    assert first.status_code in {200, 409, 503} and first.json().get("state") != "ready", first.text
    assert row(fixture) is None or row(fixture).abandon is None  # one 404 of a compatible source decides nothing
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
    view = assert_abandoned(advance(fixture), fixture)
    switched = row(fixture)
    assert switched.last_switch == NativeBindingSwitch(
        reason="abandon_source", previous_hosted_session_id=fixture.source.session_id, files_migrated=False,
        basis="retired", source_status="missing")
    assert view["source_invocation_refused"] is True


def test_d_an_incompatible_deleted_source_needs_one_read(file_api, monkeypatch):
    fixture = file_api
    platform(fixture, monkeypatch, status="missing", profile=OLD_PROFILE)
    assert_abandoned(advance(fixture), fixture)
    assert row(fixture).last_switch.source_status == "missing"


def test_e_only_receipts_of_previous_sids_get_the_earlier_version_notice(file_api, monkeypatch):
    """(e), review round 2 P2-4/P2-5: a ``started`` receipt left on an idle source may still finish, so it reads as
    ordinary progress (no resend asked: boundary 7); once Hosted marks it after N unchanged minutes (the read itself
    advances the stall rule) it reads with the notice, keeping its own failure code. A SID neither current nor
    previous is undecidable."""
    from shared_context import native_left_receipts

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    seed(fixture, "stuck-started", "started")
    assert_abandoned(advance(fixture), fixture)
    # Written after the switch's one-time backfill (f), so its SID is neither current nor previous: undecidable.
    seed(fixture, "stranger", "started", session_id="fa-native-not-in-the-binding-row")
    seed(fixture, "new-inflight", "accepted", session_id=fixture.destination.session_id)
    kept = raw(fixture, "stuck-started")
    running = read_request(fixture, "stuck-started")
    assert running.status_code == 200 and running.json()["status"] == "started", running.text
    assert "environment_notice" not in running.json() and raw(fixture, "stuck-started") == kept
    now[0] += native_left_receipts.STALL_SECONDS + 1
    marked = read_request(fixture, "stuck-started").json()
    assert (marked["status"], marked["failure_code"]) == ("unknown", "NATIVE_SOURCE_STALLED"), marked
    assert marked["environment_notice"] == {"kind": "earlier_version_unfinished"}
    for rid in ("new-inflight", "stranger", "original-rid"):
        plain = read_request(fixture, rid)
        assert plain.status_code == 200 and "environment_notice" not in plain.json(), (rid, plain.text)


def test_f_the_commit_rewrites_the_binding_row_and_backfills_unfinished_sids(file_api, monkeypatch):
    fixture = file_api
    claim(fixture, observed="40")  # same-SID drift with a pending notice: both clear with the SID
    incompatible_idle(fixture, monkeypatch)
    seed(fixture, "older-stuck", "started", session_id="fa-native-an-older-sid")
    assert_abandoned(advance(fixture), fixture)
    switched = row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id
    assert switched.created_agent_version == fixture.destination.agent_version
    assert not switched.drift and not switched.notice_pending and switched.notice_to_version is None
    assert switched.abandon is None and switched.unfinished_checked == "verified"
    assert [(item.hosted_session_id, item.agent_version) for item in switched.previous_sessions] == [
        (fixture.source.session_id, fixture.source.agent_version), ("fa-native-an-older-sid", None)]
    assert switched.last_switch == NativeBindingSwitch(
        reason="abandon_source", previous_hosted_session_id=fixture.source.session_id, files_migrated=False,
        basis="incompatible", source_status="idle")
    older = read_request(fixture, "older-stuck")  # a backfilled SID is not known retired: it may still finish (P2-4)
    assert older.json()["status"] == "started" and "environment_notice" not in older.json(), older.text


def test_g_the_decision_is_kept_across_callers_and_never_flips(file_api, monkeypatch):
    """The first attempt (deploy-time, idle_only) decides and fails before dispatch; the retry is an explicit
    follow without idle_only on a source that now reads active and compatible."""
    fixture = file_api
    claim(fixture, observed="40")
    platform(fixture, monkeypatch, status="idle")
    fixture.files.stopped.add(fixture.source.session_id)
    seed(fixture, "stuck-started", "started")
    invoked = NativeTurnGateway._invoke
    failed = []

    def refuse_once(client, command):
        if command.operation == "session_abandon" and not failed:
            failed.append(command.request_id)
            raise Problem(503, "FOUNDRY_INVOCATION_UNKNOWN", "OFFLINE the switch was not sent")
        return invoked(client, command)

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(refuse_once))
    refused = advance(fixture)
    assert refused.status_code == 503 and failed, refused.text
    decision = row(fixture).abandon
    assert (decision.basis, decision.source_status) == ("drifted", "idle")
    fixture.files.stopped.discard(fixture.source.session_id)
    platform(fixture, monkeypatch, status="active")
    retry = follow(fixture, command={**fixture.command, "idle_only": False})
    assert_abandoned(retry, fixture)
    assert row(fixture).last_switch.basis == "drifted"


def test_h_an_active_incompatible_source_is_only_observed(file_api, monkeypatch, caplog):
    """Neal 2026-10-13: never a user-visible refusal; metadata observation and a log only, the existing follow goes
    on (the ADR revision makes this state impossible by deploy ordering)."""
    fixture = file_api
    claim(fixture)
    platform(fixture, monkeypatch, status="active", profile=OLD_PROFILE)
    caplog.set_level(logging.WARNING)
    result = advance(fixture)
    assert result.status_code == 200, result.text
    assert any(record.getMessage().startswith(
        f"NATIVE_ENVIRONMENT_SOURCE_INCOMPATIBLE_ACTIVE session_id={fixture.source.session_id}")
        for record in caplog.records)
    current = row(fixture)
    assert current.abandon is None
    assert current.last_switch is None or current.last_switch.reason == "version_follow"


def test_i_a_later_follow_does_not_wait_for_receipts_of_previous_sids(file_api, monkeypatch):
    from shared_context.native_environment import follow_session

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    assert_abandoned(advance(fixture), fixture)
    seed(fixture, "stuck-on-source", "started")
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    later = fixture.identity.model_copy(update={"session_id": "fa-native-a-later-version", "agent_version": "43"})

    async def quiet(*_args, **_kwargs):
        return True

    def later_follow(settled):
        probe = NativeTurnRuntime(later, OfflineSdk(), task_observer=SimpleNamespace(quiet=quiet))
        command = NativeSessionFollow(
            request_id="later-follow-" + str(len(settled)), expected_session_id=fixture.destination.session_id,
            expected_etag=invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()["etag"],
            stage="prepare", idle_only=True, settled_session_ids=settled)
        return fixture.hosted.portal.call(follow_session, probe, command)

    assert state["hosted_session_id"] == fixture.destination.session_id
    assert later_follow((fixture.source.session_id,)).reason == "source_stop_required"
    sessions = behavior_native_sessions.native_sessions(fixture.case.composition)
    destination = SimpleNamespace(target=SimpleNamespace(session_id="fa-native-a-later-version"))
    assert sessions._settled((None, row(fixture)), destination) == (fixture.source.session_id,)


def test_i_a_receipt_of_the_current_sid_is_still_waited_for(file_api, monkeypatch):
    from shared_context.native_environment import follow_session

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    assert_abandoned(advance(fixture), fixture)
    seed(fixture, "stuck-on-current", "started", session_id=fixture.destination.session_id)
    later = fixture.identity.model_copy(update={"session_id": "fa-native-a-later-version", "agent_version": "43"})

    async def quiet(*_args, **_kwargs):
        return True

    probe = NativeTurnRuntime(later, OfflineSdk(), task_observer=SimpleNamespace(quiet=quiet))
    command = NativeSessionFollow(
        request_id="later-follow", expected_session_id=fixture.destination.session_id,
        expected_etag=invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()["etag"],
        stage="prepare", idle_only=True, settled_session_ids=(fixture.source.session_id,))
    assert fixture.hosted.portal.call(follow_session, probe, command).reason == "accepted_or_started"


def test_j_a_crash_after_the_commit_is_repaired_by_the_rerun(file_api, monkeypatch):
    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    seed(fixture, "stuck-started", "started")
    fixture.lost.update({"operation": "session_abandon"})
    lost = advance(fixture)
    assert lost.status_code == 503, lost.text
    assert row(fixture).hosted_session_id == fixture.source.session_id  # committed in Hosted, not in the row
    pending = read_request(fixture, "stuck-started")
    assert pending.status_code == 200 and "environment_notice" not in pending.json(), pending.text
    assert_abandoned(advance(fixture), fixture)
    repaired = row(fixture)
    assert repaired.hosted_session_id == fixture.destination.session_id
    assert repaired.earlier(fixture.source.session_id) and repaired.last_switch.reason == "abandon_source"
    assert [call for call in fixture.files.calls if call[0] == "create"] == [("create", fixture.destination.session_id)]
    after = read_request(fixture, "stuck-started").json()  # idle source: ordinary progress until it settles (P2-4)
    assert after["status"] == "started" and "environment_notice" not in after


def test_j_a_rerun_on_a_newer_selector_keeps_both_previous_sids(file_api, monkeypatch):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    fixture = file_api
    agent, source = fixture.source.agent_id, fixture.source.session_id
    claim(fixture)
    assert store(fixture).decide_abandon(agent, hosted_session_id=source, basis="incompatible", source_status="idle",
                                         created_agent_version="41") is not None
    newer = NativeEnvironmentView(
        agent_id=agent, request_id="newer-follow", state="ready", previous_hosted_session_id="fa-native-first-switch",
        previous_agent_version="42", hosted_session_id="fa-native-newer", hosted_agent_version="43")
    written = store(fixture).record_switch(agent, newer)
    assert [item.hosted_session_id for item in written.previous_sessions] == [source, "fa-native-first-switch"]
    assert written.last_switch.reason == "version_follow" and written.abandon is None


def test_k_an_incompatible_sid_gets_zero_calls_at_every_choke_point(file_api, monkeypatch):
    """Enforcement is the integration point (off by default, Neal 2026-10-13); enforced, nothing reaches the SID."""
    import typing
    from shared_context.behavior_foundry_responses import FoundryResponsesClient
    from shared_context.behavior_session_files import SessionFiles

    fixture = file_api
    agent, sid = fixture.source.agent_id, fixture.source.session_id
    claim(fixture)
    store(fixture)._update(agent, lambda value: value.model_copy(update={"state_profile": OLD_PROFILE}))
    sessions = behavior_native_sessions.native_sessions(fixture.case.composition)
    sessions.guard.enforce = True
    source = fixture.case.foundry.for_target(fixture.source)
    destination = fixture.case.foundry.for_target(fixture.destination)
    mark = (len(fixture.control_versions), len(fixture.files.calls))
    union = typing.get_args(typing.get_args(NativeRequest)[0])
    operations = sorted(member.model_fields["operation"].default for member in union)
    assert len(operations) == len(union) and "submit" in operations and "session_abandon" in operations

    def refused(call):
        with pytest.raises(Problem) as error:
            call()
        assert error.value.status == 503 and error.value.body["error"]["code"] == "AGENT_VERSION_FOLLOWING"
        assert error.value.body["error"]["not_dispatched"] is True

    for operation in operations:
        refused(lambda: NativeTurnGateway._invoke(source, SimpleNamespace(operation=operation)))
    entry = SimpleNamespace(byte_length=1, sha256="0" * 64)
    refused(lambda: SessionFiles(destination).download(sid, "/home/x", entry))
    refused(lambda: SessionFiles(destination).upload(sid, "/home/x", b"x"))
    manifest = SimpleNamespace(source_session_id=sid, entries=())
    monkeypatch.setattr("shared_context.behavior_session_files.listing_directories", lambda _manifest: {"": ""})
    refused(lambda: SessionFiles(destination).list_page(manifest, SimpleNamespace(directory="", after=None)))
    refused(source.stop_session)
    responses = FoundryResponsesClient(source, team_id="team-one", conversation_mode="conversation")
    refused(lambda: responses._request("POST", "/responses", body={"agent_session_id": sid, "input": "x"}))
    platform(fixture, monkeypatch, status="active", profile=OLD_PROFILE)
    entry_point = advance(fixture)  # the automatic follow's own Files read and stop are refused the same way
    assert entry_point.status_code == 503 and entry_point.json()["error"]["code"] == "AGENT_VERSION_FOLLOWING", entry_point.text
    assert source_calls(fixture, since=mark) == ([], [], [])


def test_k_observing_by_default_logs_and_never_refuses(file_api, monkeypatch, caplog):
    fixture = file_api
    sessions = behavior_native_sessions.native_sessions(fixture.case.composition)
    assert sessions.guard.enforce is False
    sessions.guard.mark_incompatible(fixture.source.agent_id, fixture.source.session_id)
    caplog.set_level(logging.WARNING)
    source = fixture.case.foundry.for_target(fixture.source)
    assert source.stop_session() is not False
    assert ("stop", fixture.source.session_id) in fixture.files.calls
    assert any(record.getMessage() == "NATIVE_SESSION_CALL_INCOMPATIBLE operation=stop_session "
               f"session_id={fixture.source.session_id} enforced=False" for record in caplog.records)


def test_l_an_active_incompatible_source_past_its_idle_timeout_is_logged_once(file_api, monkeypatch, caplog):
    fixture = file_api
    now = clock(monkeypatch)
    claim(fixture)
    platform(fixture, monkeypatch, status="active", profile=OLD_PROFILE, idle_timeout=120)
    seed(fixture, "long-turn", "started")  # the existing follow keeps waiting for it
    caplog.set_level(logging.WARNING)

    def overdue():
        return [record.getMessage() for record in caplog.records
                if record.getMessage().startswith("NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE")]

    first = advance(fixture)
    assert first.status_code == 200 and first.json()["state"] == "pending", first.text
    assert overdue() == []
    now[0] += 121
    assert advance(fixture).json()["state"] == "pending"
    now[0] += 60
    assert advance(fixture).json()["state"] == "pending"
    assert overdue() == [f"NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE session_id={fixture.source.session_id} "
                         f"version={fixture.source.agent_version} waited_seconds=121"]
    assert not any(call[0] == "stop" for call in fixture.files.calls)
    assert row(fixture).abandon is None


@pytest.mark.parametrize("second", ["active", "unavailable"])
def test_m_a_compatible_source_is_not_abandoned_on_one_404_or_any_5xx(file_api, monkeypatch, second):
    fixture = file_api
    now = clock(monkeypatch)
    claim(fixture)
    reads = iter(["missing" if second == "active" else "unavailable", second])
    current = ["missing"]

    def status():
        return current[0]

    platform(fixture, monkeypatch, status=status)
    current[0] = next(reads)
    first = advance(fixture)
    assert first.json().get("state") != "ready", first.text
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
    current[0] = next(reads)
    follows = advance(fixture)
    if second == "active":
        # The existing follow goes on: it waits for the active source (no abandon, no stop of a source it can not
        # vouch for once the first read already moved it to the no-files path).
        assert follows.status_code == 200 and follows.json()["state"] == "pending", follows.text
        assert follows.json()["reason"] == "source_active"
    else:
        assert follows.json().get("state") != "ready", follows.text
    current = row(fixture)
    assert current.abandon is None and current.last_switch is None
    assert current.hosted_session_id == fixture.source.session_id


@pytest.mark.parametrize("metadata", ["idle", "active", "missing-then-idle"])
def test_n_a_drifted_idle_sid_does_not_hold_the_switch(file_api, monkeypatch, metadata):
    fixture = file_api
    now = clock(monkeypatch)
    claim(fixture, observed="40")
    current = ["missing" if metadata == "missing-then-idle" else metadata]
    platform(fixture, monkeypatch, status=lambda: current[0])
    if current[0] != "active":
        fixture.files.stopped.add(fixture.source.session_id)
    pinned, later = seed(fixture, "pinned-before-drift", "accepted"), seed(fixture, "accepted-after-drift", "started")
    sdk, seen = tracked(fixture)
    if metadata == "missing-then-idle":
        assert advance(fixture).json().get("state") != "ready"
        assert row(fixture).abandon is None  # one 404 is not "retired"
        now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
        current[0] = "idle"
    result = advance(fixture)
    if metadata == "active":
        assert result.status_code == 200 and result.json()["state"] == "pending", result.text
        assert result.json()["reason"] == "accepted_or_started" and row(fixture).abandon is None
        return
    assert result.status_code == 200 and result.json()["state"] == "ready", result.text
    assert source_calls(fixture) == ([], [], [])
    assert row(fixture).last_switch.basis == "drifted" and row(fixture).last_switch.source_status == "idle"
    # M9: the receipt pinned before the drift never started, so the new session adopts and runs it (no resend); the
    # started one stays byte for byte and reads as ordinary progress until it settles (P2-4).
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("pinned-before-drift", "completed")
    assert [rid for rid, *_ in seen] == ["pinned-before-drift"]
    assert raw(fixture, "pinned-before-drift")[0]["adopted_from"] == fixture.source.session_id
    assert raw(fixture, "accepted-after-drift") == later
    for rid in ("pinned-before-drift", "accepted-after-drift"):
        assert "environment_notice" not in read_request(fixture, rid).json()


@pytest.mark.parametrize("status", ["idle", "failed", "deleted", "expired", "missing"])
def test_o_the_switch_records_which_notice_applies(file_api, monkeypatch, status):
    fixture = file_api
    incompatible_idle(fixture, monkeypatch, status=status)
    assert_abandoned(advance(fixture), fixture)
    switched = row(fixture).last_switch
    assert (switched.reason, switched.files_migrated, switched.source_status) == ("abandon_source", False, status)


def test_p_a_never_drifted_sid_keeps_waiting(file_api, monkeypatch):
    fixture = file_api
    claim(fixture)
    platform(fixture, monkeypatch, status="idle")
    fixture.files.stopped.add(fixture.source.session_id)
    seed(fixture, "stuck-started", "started")
    result = advance(fixture)
    assert result.status_code == 200 and result.json()["state"] == "pending", result.text
    assert result.json()["reason"] == "accepted_or_started"
    assert not row(fixture).drift and row(fixture).abandon is None
    plain = read_request(fixture, "stuck-started")
    assert "environment_notice" not in plain.json()


def left_conversation(fixture, rid, status, *, cid=None, n=1, base=None, session_id=None):
    """A conversation whose last allocated receipt ``rid`` was left ``status`` by ``session_id`` (default: the
    source). ``cid`` None: a conversation of its own whose only turn is ``rid``; otherwise its meta is rewritten."""
    from shared_context.native_checkpoint import conversation_store_name
    from shared_context.native_turns import _Meta, _changed, _decode

    kept = seed(fixture, rid, status, session_id=session_id, cid=cid, turn_number=None if status == "accepted" else n)
    own = cid is None
    cid = cid or "native-" + rid

    async def meta():
        async with fixture.runtime._store(conversation_store_name(cid), create=True) as store:
            if own:
                await store.create_item("meta", _json(_Meta(
                    agent_id=fixture.source.agent_id, hosted_session_id=fixture.source.session_id,
                    conversation_id=cid, capture=fixture.saved.capture, sdk_session_id="sdk-left-" + rid, n=n, rid=rid,
                    base_ckpt=base)))
                return
            item = await store.get_item("meta")
            await store.set_item("meta", _json(_changed(_decode(item.value, _Meta), n=n, rid=rid, base_ckpt=base)),
                                 if_match=item.etag)
    fixture.hosted.portal.call(meta)
    return cid, kept


def conversation_meta(fixture, cid):
    from shared_context.native_turns import _Meta, _decode
    from shared_context.native_checkpoint import conversation_store_name

    async def read():
        async with fixture.runtime._store(conversation_store_name(cid)) as store:
            return _decode((await store.get_item("meta")).value, _Meta)
    return fixture.hosted.portal.call(read)


def left_queue(fixture, cid):
    from shared_context.native_checkpoint import conversation_store_name
    from shared_context.native_left_receipts import QUEUE_KEY

    async def read():
        async with fixture.runtime._store(conversation_store_name(cid)) as store:
            item = await store.get_item(QUEUE_KEY)
            return () if item is None else tuple(item.value["request_ids"])
    return fixture.hosted.portal.call(read)


def tracked(fixture):
    """Records (RID, SDK session name, resumed from a checkpoint, turn number) for each turn the new session runs."""
    sdk, seen = fixture.runtime.executor, []

    async def run(receipt, scope, previous, control):
        seen.append((receipt.request_id, scope.sdk_session_id, previous is not None, receipt.turn_number))
        return await sdk(receipt, scope, previous, control)

    fixture.runtime.executor = run
    return sdk, seen


def wall_clock(monkeypatch, start=5000.0):
    from shared_context import native_left_receipts

    now = [start]
    monkeypatch.setattr(native_left_receipts, "clock", lambda: now[0])
    monkeypatch.setattr(native_left_receipts, "POLL_SECONDS", 0.02)
    return now


def stall(fixture, rid):
    from shared_context.native_left_receipts import stall_key

    async def read():
        async with fixture.runtime._store(TURNS_STORE) as store:
            item = await store.get_item(stall_key(rid))
            return None if item is None else item.value
    return fixture.hosted.portal.call(read)


def eventually(check, *, seconds=8):
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("OFFLINE the expected state was not reached")


def status_of(fixture, rid):
    return fixture.hosted.portal.call(fixture.runtime.read, rid).status


def versioned(kept):
    from shared_context.native_turns import _Rid, _Versioned, _decode

    return _Versioned(_decode(kept[0], _Rid), kept[1])


def turn_checkpoint(fixture, cid, n):
    from shared_context.native_checkpoint import conversation_store_name
    from shared_context.native_turns import _Turn, _decode

    async def read():
        async with fixture.runtime._store(conversation_store_name(cid)) as store:
            return _decode((await store.get_item(f"turn:{n}")).value, _Turn).checkpoints[0]
    return fixture.hosted.portal.call(read)


def run_left(fixture, kept, meta):
    """The source finishes its left turn itself: its own SDK result saved by its own runtime under the meta it
    allocated, by CAS on the receipt ETag it holds."""
    from test_native_turns import sdk_result

    left = versioned(kept)
    return fixture.hosted.portal.call(
        fixture.original._save_turn, left, meta, sdk_result(fixture.original._scope(meta), "The left answer"))


def earlier_unfinished(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["failure_code"]) == ("unknown", "NATIVE_EARLIER_VERSION_UNFINISHED"), response.text
    assert body["environment_notice"] == {"kind": "earlier_version_unfinished"}


@pytest.mark.parametrize("left", ["unknown", "started-on-retired"])
def test_r_a_conversation_with_no_completed_turn_starts_without_a_checkpoint(file_api, monkeypatch, left):
    """ADR-0009 §7a (r): no turn of the conversation ever completed, so no checkpoint is owed; the new session starts it
    in a fresh, differently named SDK session (never a resume) and numbering goes on. The left receipt is untouched
    (a never-started one, on any earlier SID, is adopted instead: M9, ``test_m9_*``)."""
    fixture = file_api
    incompatible_idle(fixture, monkeypatch, status="idle" if left == "unknown" else "deleted")
    cid, kept = left_conversation(fixture, "first-turn-left", left.removesuffix("-on-retired"))
    assert_abandoned(advance(fixture), fixture)
    sdk, seen = tracked(fixture)
    accepted = invoke(fixture.hosted, fixture.identity, submit("next-in-left", cid, "Continue"))
    assert accepted.status_code == 202, accepted.text
    harness = Harness(fixture.runtime, sdk, fixture.hosted)
    assert harness.wait("next-in-left", "completed").turn_number == 2
    [(rid, fresh, resumed, number)] = seen
    assert (rid, resumed, number) == ("next-in-left", False, 2) and fresh != "sdk-left-first-turn-left"
    assert raw(fixture, "first-turn-left") == kept
    if left != "unknown":
        earlier_unfinished(read_request(fixture, "first-turn-left"))
    accepted = invoke(fixture.hosted, fixture.identity, submit("then-in-left", cid, "Again"))
    assert accepted.status_code == 202, accepted.text
    assert harness.wait("then-in-left", "completed").turn_number == 3
    assert seen[-1] == ("then-in-left", fresh, True, 3)  # the fresh session goes on from its own checkpoint


def test_r_a_conversation_with_a_completed_turn_still_owes_its_checkpoint(file_api, monkeypatch):
    """CTX-03 stands where a checkpoint is owed: a completed turn's checkpoint that can not be read is refused."""
    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    assert_abandoned(advance(fixture), fixture)
    cid = fixture.saved.conversation_id
    turn = conversation_meta(fixture, cid)
    assert turn.rid == "original-rid" and turn.n == 1

    async def lose_checkpoint():
        from shared_context.native_checkpoint import conversation_store_name

        async with fixture.runtime._store(conversation_store_name(cid)) as store:
            for key in [key.key for key in (await store.list_keys(limit=100)).keys]:
                if key.startswith("ckpt"):
                    await store.delete_item(key)
    fixture.hosted.portal.call(lose_checkpoint)
    accepted = invoke(fixture.hosted, fixture.identity, submit("after-lost-checkpoint", cid, "Continue"))
    assert accepted.status_code == 202, accepted.text
    ended = eventually(lambda: (lambda value: value if value.status != "accepted" and value.status != "started"
                                else None)(fixture.hosted.portal.call(fixture.runtime.read, "after-lost-checkpoint")))
    assert ended.status == "unknown" and ended.failure_code not in {None, "NATIVE_TURN_UNKNOWN"}, ended


def test_r_a_started_left_receipt_queues_the_next_message_until_it_stalls(file_api, monkeypatch):
    """"(r)(s) 的競爭" (1)(2)(3)(6): accepted at once (202, an ordinary queued message, no server_following); progress
    within N restarts the timer; N unchanged minutes mark the left receipt ``unknown`` first, then the message runs;
    the source's late save is fenced by the receipt's ETag (its ``turn:{n}`` is an orphan)."""
    from shared_context import native_left_receipts
    from shared_context.native_progress import _key as progress_key

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = left_conversation(fixture, "left-running", "started")
    allocated = conversation_meta(fixture, cid)
    assert_abandoned(advance(fixture), fixture)
    sdk, seen = tracked(fixture)
    accepted = invoke(fixture.hosted, fixture.identity, submit("queued-behind", cid, "Next"))
    assert accepted.status_code == 202 and accepted.json()["status"] == "accepted", accepted.text
    first = eventually(lambda: stall(fixture, "left-running"))
    assert (first["first_seen"], first["receipt_etag"], first["progress_etag"]) == (5000.0, kept[1], None)
    # A VM restart reads the persisted observation back: a new runtime of the same session does not restart it.
    restarted = NativeTurnRuntime(fixture.identity, OfflineSdk())
    now[0] += 60
    assert fixture.hosted.portal.call(native_left_receipts.settled, restarted, versioned(kept)) is False
    assert stall(fixture, "left-running")["first_seen"] == 5000.0
    now[0] += native_left_receipts.STALL_SECONDS - 61

    async def progress():
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.create_item(progress_key("left-running"), {"OFFLINE": "public progress moved"})
    fixture.hosted.portal.call(progress)
    moved = eventually(lambda: (lambda value: value if value["progress_etag"] else None)(stall(fixture, "left-running")))
    assert moved["first_seen"] == now[0]
    assert raw(fixture, "left-running") == kept and status_of(fixture, "queued-behind") == "accepted" and seen == []
    now[0] += native_left_receipts.STALL_SECONDS + 1
    harness = Harness(fixture.runtime, sdk, fixture.hosted)
    assert harness.wait("queued-behind", "completed").turn_number == 2
    marked = raw(fixture, "left-running")[0]["receipt"]
    assert (marked["status"], marked["failure_code"]) == ("unknown", "NATIVE_SOURCE_STALLED")
    assert len(seen) == 1 and seen[0][2:] == (False, 2)
    late = run_left(fixture, kept, allocated)  # the source's own save after the mark: the receipt's ETag fences it
    assert late.status == "unknown" and raw(fixture, "left-running")[0]["receipt"]["status"] == "unknown"
    assert conversation_meta(fixture, cid).rid == "queued-behind"


def test_r_a_failed_mark_keeps_waiting_and_never_reaches_the_owner(file_api, monkeypatch):
    """"(r)(s) 的競爭" (5): the mark's CAS is unconfirmed (503) and the receipt still reads ``started``: the message stays
    queued and the timer restarts; no 503 is returned to anyone."""
    from shared_context import native_left_receipts

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = left_conversation(fixture, "left-running", "started")
    assert_abandoned(advance(fixture), fixture)
    sdk, _ = tracked(fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("queued-behind", cid, "Next")).status_code == 202
    eventually(lambda: stall(fixture, "left-running"))
    marking, failed = fixture.runtime._unknown, []

    async def unconfirmed(original, code, **values):
        if original.value.receipt.request_id == "left-running" and not failed:
            failed.append(code)
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE the mark was not confirmed")
        return await marking(original, code, **values)

    monkeypatch.setattr(fixture.runtime, "_unknown", unconfirmed)
    now[0] += native_left_receipts.STALL_SECONDS + 1
    eventually(lambda: failed and stall(fixture, "left-running")["first_seen"] == now[0])
    assert raw(fixture, "left-running") == kept and status_of(fixture, "queued-behind") == "accepted"
    now[0] += native_left_receipts.STALL_SECONDS + 1
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("queued-behind", "completed").turn_number == 2


def test_r_a_left_turn_the_source_finishes_lets_the_queued_message_continue_from_its_checkpoint(file_api, monkeypatch):
    """"(r)(s) 的競爭" (1)(4): the source writes its left turn ``completed`` while the next message waits (so a mark CAS
    would fail on the changed ETag); the queued message then continues from that turn's checkpoint."""
    fixture = file_api
    wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid = fixture.saved.conversation_id
    base = conversation_meta(fixture, cid)
    _, kept = left_conversation(fixture, "left-running", "started", cid=cid, n=2,
                                base=turn_checkpoint(fixture, cid, base.n))
    allocated = conversation_meta(fixture, cid)
    assert_abandoned(advance(fixture), fixture)
    sdk, seen = tracked(fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("queued-behind", cid, "Next")).status_code == 202
    eventually(lambda: stall(fixture, "left-running"))
    assert status_of(fixture, "queued-behind") == "accepted" and seen == []
    assert run_left(fixture, kept, allocated).status == "completed"
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("queued-behind", "completed").turn_number == 3
    assert seen == [("queued-behind", base.sdk_session_id, True, 3)]


def test_fence_a_source_turn_numbered_after_the_switch_never_runs(file_api, monkeypatch):
    """"(r)(s) 的競爭" (7): a source turn that passed its start check before the switch reaches ``_allocate`` only after
    the new session's turn completed: this session no longer holds the Agent, so it is not numbered and never runs."""
    from shared_context.native_turns import NativeTurnControl

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    cid = fixture.saved.conversation_id
    late = seed(fixture, "late-source-turn", "started", cid=cid)
    assert_abandoned(advance(fixture), fixture)
    sdk, _ = tracked(fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("new-first", cid, "Next")).status_code == 202
    Harness(fixture.runtime, sdk, fixture.hosted).wait("new-first", "completed")
    calls = len(fixture.source_sdk.calls)
    ended = fixture.hosted.portal.call(
        fixture.original._execute_started, versioned(late), NativeTurnControl(),
        lambda _control: None)
    assert (ended.status, ended.failure_code) == ("unknown", "NATIVE_TURN_UNKNOWN")
    assert len(fixture.source_sdk.calls) == calls
    after = conversation_meta(fixture, cid)
    assert (after.n, after.rid) == (2, "new-first")


def test_fence_a_switch_between_the_source_check_and_its_numbering_queues_the_new_message(file_api, monkeypatch):
    """"(r)(s) 的競爭" (8): the switch commits after the source's ``_allocate`` SID check and before its meta CAS, so
    the source's turn is numbered: the new session's message queues behind it like any left ``started`` receipt."""
    fixture = file_api
    wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid = fixture.saved.conversation_id
    late = seed(fixture, "late-source-turn", "started", cid=cid)
    before = fixture.hosted.portal.call(fixture.original._agent_record)
    assert_abandoned(advance(fixture), fixture)

    async def checked_before_the_commit():
        return before
    monkeypatch.setattr(fixture.original, "_agent_record", checked_before_the_commit)
    _, allocated, _ = fixture.hosted.portal.call(fixture.original._allocate, versioned(late))
    assert (allocated.n, allocated.rid) == (2, "late-source-turn")
    sdk, seen = tracked(fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("after-late", cid, "Next")).status_code == 202
    eventually(lambda: stall(fixture, "late-source-turn"))
    assert status_of(fixture, "after-late") == "accepted" and seen == []
    assert run_left(fixture, raw(fixture, "late-source-turn"), allocated).status == "completed"
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("after-late", "completed").turn_number == 3
    assert seen[0][2:] == (True, 3)


def test_fence_a_late_numbering_never_replaces_an_unfinished_turn_of_another_session(file_api, monkeypatch):
    """"(r)(s) 的競爭" (9): the conversation's last allocated receipt is another session's unfinished turn; a late
    source turn that passed the SID check is still not numbered and the meta is not changed."""
    from shared_context.native_turns import _Rid, _Versioned

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    cid = fixture.saved.conversation_id
    late = seed(fixture, "late-source-turn", "started", cid=cid)
    before = fixture.hosted.portal.call(fixture.original._agent_record)
    assert_abandoned(advance(fixture), fixture)
    left_conversation(fixture, "new-running", "started", cid=cid, n=2, session_id=fixture.destination.session_id)

    async def checked_before_the_commit():
        return before
    monkeypatch.setattr(fixture.original, "_agent_record", checked_before_the_commit)
    with pytest.raises(Problem) as refused:
        fixture.hosted.portal.call(fixture.original._allocate, versioned(late))
    assert refused.value.body["error"]["code"] == "NATIVE_TURN_UNKNOWN"
    after = conversation_meta(fixture, cid)
    assert (after.n, after.rid) == (2, "new-running")


def response_holder(fixture, rid, status, *, session_id=None):
    """A Responses receipt holding the fixture conversation's slot, accepted by ``session_id`` (default: the source)."""
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.native_checkpoint import conversation_store_name
    from shared_context.native_turns import _Meta, _Rid, _changed, _decode
    from test_native_responses import response_submit

    cid = fixture.saved.conversation_id
    command = response_submit(rid, cid, "An unfinished Responses message")
    receipt = NativeReceipt(
        state_profile="foundry-responses-v1", agent_id=fixture.source.agent_id,
        hosted_session_id=session_id or fixture.source.session_id, request_id=rid, conversation_id=cid,
        command=command.command, capture=command.capture, submit_digest=command.sha256,
        capture_digest=command.capture.sha256, status=status,
        response_id=None if status == "accepted" else "caresp_left_" + rid)

    async def hold():
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.create_item("rid:" + rid, _json(_Rid(
                receipt=receipt, call_id="OFFLINE-seed", run_token=None if status == "accepted" else "run-" + rid)))
        async with fixture.runtime._store(conversation_store_name(cid)) as store:
            item = await store.get_item("meta")
            await store.set_item("meta", _json(_changed(_decode(item.value, _Meta), inflight_rid=rid)),
                                 if_match=item.etag)
    fixture.hosted.portal.call(hold)
    return cid, raw(fixture, rid)


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_s_a_slot_left_by_a_receipt_that_can_not_complete_is_released(file_api, monkeypatch):
    """ADR-0009 §7a (s): the slot moves to the new message by one meta CAS; the left receipt is untouched and reads
    as the earlier version's unfinished message (a never-started one, on any earlier SID, is adopted: M9)."""
    from test_native_responses import response_submit

    fixture = file_api
    incompatible_idle(fixture, monkeypatch, status="deleted")
    cid, kept = response_holder(fixture, "stuck-response", "started")
    assert_abandoned(advance(fixture), fixture)
    answered = invoke(fixture.hosted, fixture.identity, response_submit("next-response", cid, "Continue"))
    assert answered.status_code == 202, answered.text
    assert answered.json()["status"] == "accepted" and answered.json()["dispatch"]["state"] == "needs_response"
    assert conversation_meta(fixture, cid).inflight_rid == "next-response"
    assert raw(fixture, "stuck-response") == kept
    earlier_unfinished(read_request(fixture, "stuck-response"))


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_s_a_slot_left_started_queues_the_new_message_until_it_stalls(file_api, monkeypatch):
    """(s) with a ``started`` left receipt on an idle source: the human message is accepted and waits (``not_taken``,
    never ``busy_refused``); a status poll after N unchanged minutes marks the left receipt and takes the slot."""
    from shared_context import native_left_receipts
    from shared_context.behavior_native_responses_api import _advance_inflight
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from test_native_responses import response_submit

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = response_holder(fixture, "stuck-response", "started")
    assert_abandoned(advance(fixture), fixture)
    answered = invoke(fixture.hosted, fixture.identity, response_submit("next-response", cid, "Continue"))
    assert answered.status_code == 202, answered.text
    assert answered.json()["status"] == "accepted" and answered.json()["dispatch"]["state"] == "not_taken"
    assert stall(fixture, "stuck-response")["first_seen"] == 5000.0

    class NoPost:
        mode = "conversation"

        def start(self, *_args, **_kwargs):
            pytest.fail("OFFLINE a left holder was dispatched")

    # The API's own advance of the slot holder (sync submit path) leaves a left holder alone: no 409 resend loop.
    _advance_inflight(fixture.case.foundry.for_target(fixture.destination), NoPost(),
                      NativeReceipt.model_validate_json(answered.text), before_send=lambda: None)
    now[0] += native_left_receipts.STALL_SECONDS - 1
    polled = invoke(fixture.hosted, fixture.identity, NativeRead(request_id="next-response")).json()
    assert polled["dispatch"]["state"] == "not_taken" and raw(fixture, "stuck-response") == kept
    now[0] += 2
    polled = invoke(fixture.hosted, fixture.identity, NativeRead(request_id="next-response")).json()
    assert polled["status"] == "accepted" and polled["dispatch"]["state"] == "needs_response", polled
    marked = raw(fixture, "stuck-response")[0]["receipt"]
    assert (marked["status"], marked["failure_code"]) == ("unknown", "NATIVE_SOURCE_STALLED")
    assert conversation_meta(fixture, cid).inflight_rid == "next-response"


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_s_a_slot_held_by_the_current_session_keeps_one_at_a_time(file_api, monkeypatch):
    from test_native_responses import response_submit

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    assert_abandoned(advance(fixture), fixture)
    cid, _ = response_holder(fixture, "running-here", "started", session_id=fixture.destination.session_id)
    answered = invoke(fixture.hosted, fixture.identity, response_submit("next-response", cid, "Continue"))
    assert (answered.json()["status"], answered.json()["failure_code"]) == ("busy_refused", "NATIVE_TURN_BUSY")


def test_q_a_drift_back_still_counts_as_drifted(file_api, monkeypatch):
    fixture = file_api
    claim(fixture, observed="40")
    drifted_back = store(fixture).record_observed(
        fixture.source.agent_id, hosted_session_id=fixture.source.session_id, agent_version="41", source="vm")
    assert drifted_back.drift and drifted_back.observed.agent_version == drifted_back.created_agent_version
    platform(fixture, monkeypatch, status="idle")
    fixture.files.stopped.add(fixture.source.session_id)
    seed(fixture, "stuck-started", "started")
    result = advance(fixture)
    assert result.status_code == 200 and result.json()["state"] == "ready", result.text
    assert row(fixture).last_switch.basis == "drifted"


def test_abandon_through_the_owners_send_path(file_api, monkeypatch):
    """End to end: the owner's own send is what moves the Agent off an unreachable source (the submit-time follow);
    the message is then accepted by the new session, resent with the same RID at most once as the workbench does."""
    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    # The deploy created the selected version's session (it reads the Agent's preparation), so a send follows.
    fixture.files.targets[fixture.destination.session_id] = fixture.destination
    seed(fixture, "stuck-started", "started")
    body = {"request_id": "sent-after-the-source-went-away", "content": "A new message"}
    sent = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json=body)
    if sent.status_code >= 400 and sent.json()["error"].get("server_following") is True:
        sent = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json=body)
    assert sent.status_code == 202, sent.text
    assert sent.json()["hosted_session_id"] == fixture.destination.session_id
    assert source_calls(fixture) == ([], [], [])
    switched = row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id
    assert switched.last_switch.reason == "abandon_source"
    left = read_request(fixture, "stuck-started").json()  # may still finish on the idle source: no resend asked
    assert left["status"] == "started" and "environment_notice" not in left


def test_m_two_retired_reads_minutes_apart_are_not_a_confirmation(file_api, monkeypatch):
    fixture = file_api
    now = clock(monkeypatch)
    claim(fixture)
    platform(fixture, monkeypatch, status="missing")
    assert advance(fixture).json().get("state") != "ready"
    now[0] += behavior_native_sessions.RETIRED_PAIR_SECONDS + 1
    assert advance(fixture).json().get("state") != "ready"  # too long after the first: a new pair starts here
    assert row(fixture).abandon is None
    now[0] += behavior_native_sessions.RETIRED_CONFIRM_SECONDS + 1
    assert_abandoned(advance(fixture), fixture)


def test_i_an_older_destination_image_gets_the_follow_without_settled_sessions(file_api, monkeypatch):
    fixture = file_api
    sessions = behavior_native_sessions.native_sessions(fixture.case.composition)
    destination = fixture.case.foundry.for_target(fixture.destination)
    sent = []

    def older_image(client, command):
        sent.append(command)
        if command.settled_session_ids:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "OFFLINE an older image rejects the unknown field")
        return "OFFLINE-view", None

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(older_image))
    command = NativeSessionFollow(
        request_id="follow-older", expected_session_id=fixture.source.session_id, expected_etag="OFFLINE-etag",
        stage="prepare", settled_session_ids=("fa-native-an-earlier-sid",))
    assert sessions._follow_invoke(destination, command) == ("OFFLINE-view", None)
    assert [item.settled_session_ids for item in sent] == [("fa-native-an-earlier-sid",), ()]
    assert "settled_session_ids" not in sent[1].model_dump(mode="json")
    assert sessions._follow_invoke(destination, command.model_copy(update={"stage": "commit"}))[0] == "OFFLINE-view"
    assert len(sent) == 3 and sent[2].settled_session_ids == ()  # left out up front for that destination

    def newer_image(client, command):
        sent.append(command)
        raise Problem(422, "NATIVE_REQUEST_INVALID", "OFFLINE something else is invalid")

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(newer_image))
    other = fixture.case.foundry.for_target(fixture.source)
    with pytest.raises(Problem):
        sessions._follow_invoke(other, command.model_copy(update={"settled_session_ids": ()}))
    assert len(sent) == 4  # nothing to leave out: never sent twice


def test_k_enforcement_is_a_setting_off_by_default(file_api, monkeypatch):
    from shared_context.native_binding import SESSION_GUARD_ENV, session_guard_enforced

    fixture = file_api
    assert not session_guard_enforced({}) and not session_guard_enforced({SESSION_GUARD_ENV: "0"})
    assert not session_guard_enforced({SESSION_GUARD_ENV: "yes"})
    monkeypatch.setenv(SESSION_GUARD_ENV, "1")
    assert behavior_native_sessions.NativeSessions(fixture.case.composition).guard.enforce is True


# Review round 2 (2026-10-13).


def test_r_a_message_waiting_behind_a_left_receipt_never_holds_the_agents_other_conversations(file_api, monkeypatch):
    """P1-2: the waiting message leaves the Agent's chain (a wait task of its own holds it), so another conversation of
    the same Agent runs to completion meanwhile; the waiting one runs once the left receipt is marked."""
    from shared_context import native_left_receipts

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, _ = left_conversation(fixture, "left-running", "started")
    assert_abandoned(advance(fixture), fixture)
    sdk, seen = tracked(fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("waiting-here", cid, "Next")).status_code == 202
    eventually(lambda: stall(fixture, "left-running"))
    other = invoke(fixture.hosted, fixture.identity, submit("other-chat", fixture.saved.conversation_id, "Elsewhere"))
    assert other.status_code == 202, other.text
    harness = Harness(fixture.runtime, sdk, fixture.hosted)
    assert harness.wait("other-chat", "completed").turn_number == 2
    assert status_of(fixture, "waiting-here") == "accepted" and [rid for rid, *_ in seen] == ["other-chat"]
    now[0] += native_left_receipts.STALL_SECONDS + 1
    assert harness.wait("waiting-here", "completed").turn_number == 2


def test_r_messages_waiting_behind_a_left_receipt_run_oldest_first(file_api, monkeypatch):
    """P1-1 order (Invocations): two messages wait behind the same left receipt; once it is marked they run in the
    order they were accepted, each once."""
    from shared_context import native_left_receipts

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, _ = left_conversation(fixture, "left-running", "started")
    assert_abandoned(advance(fixture), fixture)
    sdk, seen = tracked(fixture)
    for rid in ("first-waiting", "second-waiting"):
        assert invoke(fixture.hosted, fixture.identity, submit(rid, cid, "Message " + rid)).status_code == 202
    eventually(lambda: stall(fixture, "left-running"))
    now[0] += native_left_receipts.STALL_SECONDS + 1
    harness = Harness(fixture.runtime, sdk, fixture.hosted)
    assert harness.wait("second-waiting", "completed").turn_number == 3
    assert harness.wait("first-waiting", "completed").turn_number == 2
    assert [rid for rid, *_ in seen] == ["first-waiting", "second-waiting"]


class RecordingResponses:
    """The API's Responses platform client: records which RIDs it is asked to send (nothing runs)."""
    mode = "conversation"

    def __init__(self):
        self.sent = []

    def start(self, headers, _content, **_values):
        self.sent.append(headers.request_id)


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_s_a_waiting_message_takes_the_slot_without_any_poll_and_goes_before_the_next(file_api, monkeypatch):
    """P1-1 (s) for a caller that never polls (a Teams-like relay): Hosted's own wait task advances the stall rule and
    gives the waiting message the slot; the owner's next message is accepted behind it (never refused, never ahead of
    it), and the API's advance of that send dispatches the first message, once."""
    from shared_context import native_left_receipts
    from shared_context.behavior_native_responses_api import _advance_inflight
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from test_native_responses import response_submit

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, _ = response_holder(fixture, "stuck-response", "started")
    assert_abandoned(advance(fixture), fixture)
    first = invoke(fixture.hosted, fixture.identity, response_submit("waiting-first", cid, "First"))
    assert first.status_code == 202 and first.json()["dispatch"]["state"] == "not_taken", first.text
    # From here on nothing reads either RID (no NativeRead, no status poll): only the store is looked at.
    eventually(lambda: stall(fixture, "stuck-response"))
    now[0] += native_left_receipts.STALL_SECONDS + 1
    eventually(lambda: conversation_meta(fixture, cid).inflight_rid == "waiting-first")
    marked = raw(fixture, "stuck-response")[0]["receipt"]
    assert (marked["status"], marked["failure_code"]) == ("unknown", "NATIVE_SOURCE_STALLED")
    second = invoke(fixture.hosted, fixture.identity, response_submit("then-second", cid, "Second"))
    assert second.status_code == 202, second.text
    assert (second.json()["status"], second.json()["dispatch"]["state"],
            second.json()["dispatch"]["inflight_request_id"]) == ("accepted", "not_taken", "waiting-first"), second.text
    platform_client = RecordingResponses()
    _advance_inflight(fixture.case.foundry.for_target(fixture.destination), platform_client,
                      NativeReceipt.model_validate_json(second.text), before_send=lambda: None, confirm=False)
    assert platform_client.sent == ["waiting-first"]
    assert conversation_meta(fixture, cid).inflight_rid == "waiting-first"


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_s_an_older_waiting_message_goes_first_even_before_its_wait_task_looks_again(file_api, monkeypatch):
    """P1-1 order (s): the left receipt can be marked by the next message's own admission before the older message's
    wait task looks again; the older one still takes the slot and the newer one waits behind it."""
    from shared_context import native_left_receipts
    from test_native_responses import response_submit

    fixture = file_api
    now = wall_clock(monkeypatch)
    monkeypatch.setattr(native_left_receipts, "POLL_SECONDS", 3600)
    incompatible_idle(fixture, monkeypatch)
    cid, _ = response_holder(fixture, "stuck-response", "started")
    assert_abandoned(advance(fixture), fixture)
    assert invoke(fixture.hosted, fixture.identity, response_submit("waiting-first", cid, "First")).status_code == 202
    now[0] += native_left_receipts.STALL_SECONDS + 1
    second = invoke(fixture.hosted, fixture.identity, response_submit("then-second", cid, "Second")).json()
    assert (second["status"], second["dispatch"]["state"], second["dispatch"]["inflight_request_id"]) == (
        "accepted", "not_taken", "waiting-first"), second
    assert conversation_meta(fixture, cid).inflight_rid == "waiting-first"
    assert raw(fixture, "stuck-response")[0]["receipt"]["failure_code"] == "NATIVE_SOURCE_STALLED"


@pytest.mark.parametrize("source", ["idle", "deleted"])
def test_m9_a_never_started_receipt_is_adopted_and_runs_before_newer_messages(file_api, monkeypatch, source):
    """P2-3 / M9: the source accepted a message in an existing conversation and never started it. The new session
    adopts it at the switch and runs it from the conversation's checkpoint before a message sent after the switch.
    Review round 3: the same on a retired source (the platform can not run it there); no resend is asked."""
    fixture = file_api
    incompatible_idle(fixture, monkeypatch, status=source)
    cid = fixture.saved.conversation_id
    kept = seed(fixture, "never-started", "accepted", cid=cid)
    sdk, seen = tracked(fixture)
    assert_abandoned(advance(fixture), fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("after-the-switch", cid, "Later")).status_code == 202
    harness = Harness(fixture.runtime, sdk, fixture.hosted)
    assert harness.wait("after-the-switch", "completed").turn_number == 3
    assert harness.wait("never-started", "completed").turn_number == 2
    assert [(rid, resumed) for rid, _, resumed, _ in seen] == [("never-started", True), ("after-the-switch", True)]
    adopted = raw(fixture, "never-started")[0]
    assert adopted["adopted_from"] == fixture.source.session_id
    assert {key: adopted["receipt"][key] for key in REQUEST_KEYS} == {key: kept[0]["receipt"][key] for key in REQUEST_KEYS}
    plain = read_request(fixture, "never-started")
    assert plain.json()["status"] == "completed" and "environment_notice" not in plain.json(), plain.text


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
@pytest.mark.parametrize("source", ["idle", "deleted"])
def test_m9_a_never_started_slot_holder_is_adopted_and_keeps_its_slot(file_api, monkeypatch, source):
    """(s) with M9: the slot holder never started, so the new session adopts it and it keeps the slot. Review round 3
    (decision 1): the next human message is accepted and queued behind it (never ``busy_refused``), and the API's
    advance of that send dispatches the adopted one, once."""
    from shared_context.behavior_native_responses_api import _advance_inflight
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from test_native_responses import response_submit

    fixture = file_api
    incompatible_idle(fixture, monkeypatch, status=source)
    cid, kept = response_holder(fixture, "never-started-response", "accepted")
    assert_abandoned(advance(fixture), fixture)
    adopted = raw(fixture, "never-started-response")[0]
    assert adopted["adopted_from"] == fixture.source.session_id
    assert adopted["receipt"]["hosted_session_id"] == fixture.destination.session_id
    read = invoke(fixture.hosted, fixture.identity, NativeRead(request_id="never-started-response")).json()
    assert (read["status"], read["dispatch"]["state"]) == ("accepted", "needs_response"), read
    answered = invoke(fixture.hosted, fixture.identity, response_submit("next-response", cid, "Continue"))
    assert answered.status_code == 202, answered.text
    assert (answered.json()["status"], answered.json()["dispatch"]["state"],
            answered.json()["dispatch"]["inflight_request_id"]) == (
        "accepted", "not_taken", "never-started-response"), answered.text
    assert "next-response" in left_queue(fixture, cid)
    platform_client = RecordingResponses()
    _advance_inflight(fixture.case.foundry.for_target(fixture.destination), platform_client,
                      NativeReceipt.model_validate_json(answered.text), before_send=lambda: None, confirm=False)
    assert platform_client.sent == ["never-started-response"]


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_m9_the_next_message_queues_behind_an_adopted_holder_even_without_its_queue_entry(file_api, monkeypatch):
    """Review round 3 (decision 1) on its own: the adopted holder's queue entry and wait task were not confirmed (the
    adoption itself was), so only the holder's state can tell the next human message to wait; it is accepted and
    queued, never ``busy_refused``."""
    from shared_context import native_left_receipts
    from test_native_responses import response_submit

    defer = native_left_receipts.defer

    async def adopted_unconfirmed(runtime, original, behind):
        if behind == "adopted":
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE the adopted receipt's queue write is unconfirmed")
        return await defer(runtime, original, behind)

    fixture = file_api
    monkeypatch.setattr(native_left_receipts, "defer", adopted_unconfirmed)
    incompatible_idle(fixture, monkeypatch)
    cid, _ = response_holder(fixture, "never-started-response", "accepted")
    assert_abandoned(advance(fixture), fixture)
    assert raw(fixture, "never-started-response")[0]["adopted_from"] == fixture.source.session_id
    assert left_queue(fixture, cid) == ()
    answered = invoke(fixture.hosted, fixture.identity, response_submit("next-response", cid, "Continue"))
    assert answered.status_code == 202, answered.text
    assert (answered.json()["status"], answered.json()["dispatch"]["state"],
            answered.json()["dispatch"]["inflight_request_id"]) == (
        "accepted", "not_taken", "never-started-response"), answered.text
    assert left_queue(fixture, cid) == ("next-response",)


def test_e_a_never_started_receipt_on_a_retired_source_whose_adoption_is_unconfirmed_is_never_called_lost(
        file_api, monkeypatch):
    """Review round 3 (decision 2): on a retired source only a ``started`` receipt can no longer complete; a
    never-started one is adopted, so while its adoption is unconfirmed it reads as it is (no "resend it" notice) and
    the next read adopts it."""
    from shared_context import native_left_receipts

    async def unconfirmed(_runtime, _original):
        return None

    fixture = file_api
    incompatible_idle(fixture, monkeypatch, status="deleted")
    assert_abandoned(advance(fixture), fixture)
    monkeypatch.setattr(native_left_receipts, "adopt", unconfirmed)
    seed(fixture, "not-adopted-yet", "accepted")
    read = read_request(fixture, "not-adopted-yet")
    assert read.status_code == 200 and read.json()["status"] == "accepted", read.text
    assert "environment_notice" not in read.json() and read.json().get("failure_code") is None, read.text


def test_m9_a_never_started_receipt_the_adoption_read_missed_is_adopted_when_read(file_api, monkeypatch):
    """M9 "API 讀到…請新 SID…接手": a never-started receipt of the source that the switch's bounded read did not
    adopt is adopted by the API's read of it and runs here; the read already reports it as this session's."""
    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    assert_abandoned(advance(fixture), fixture)
    sdk, seen = tracked(fixture)
    seed(fixture, "missed-by-the-scan", "accepted")
    first = read_request(fixture, "missed-by-the-scan").json()
    assert first["hosted_session_id"] == fixture.destination.session_id and "environment_notice" not in first, first
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("missed-by-the-scan", "completed")
    assert [rid for rid, *_ in seen] == ["missed-by-the-scan"]


@pytest.fixture(scope="module")
def release_28():
    """The native turn runtime of release 28 (``daa822dd``, the last image before phase 2b: what an abandoned v1
    source runs), loaded against this package; read before ``file_api`` forbids launching a process. None where that
    commit is not in the checkout (the release-28 case is then skipped)."""
    import importlib.util
    import subprocess
    import sys
    from pathlib import Path

    name = "shared_context._release_28_native_turns"
    if name not in sys.modules:
        try:
            source = subprocess.run(
                ["git", "show", "daa822dd:src/fa-shared-context-poc/shared_context/native_turns.py"],
                cwd=Path(__file__).resolve().parent, capture_output=True, check=True).stdout.decode("utf-8")
        except (OSError, subprocess.CalledProcessError):
            return None
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader=None))
        module.__package__ = "shared_context"
        sys.modules[name] = module
        exec(compile(source, "release-28/native_turns.py", "exec"), module.__dict__)
    return sys.modules[name]


@pytest.mark.parametrize("image", ["release-28", "this-image"])
def test_m9_a_source_turn_parked_before_the_switch_never_starts_or_writes_the_adopted_receipt(
        release_28, file_api, monkeypatch, image):
    """P2-3, against the source's own code: its turn passed the start check before the switch and waits for the
    execution slot (another turn holds it). The new session adopts the message and runs it once. The parked turn ends
    without starting or writing it: release 28 can not read the adopted item at all (``adopted_from`` with
    ``extra="forbid"``: ``NATIVE_STATE_INVALID`` at its next 0.1 s read), this image sees another session owns it."""
    import asyncio

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    kept = seed(fixture, "parked-on-source", "accepted", cid=fixture.saved.conversation_id)
    calls = []

    async def never_runs(*values):
        calls.append(values)
        raise RuntimeError("OFFLINE the parked source turn reached its SDK")

    if image == "release-28" and release_28 is None:
        pytest.skip("OFFLINE release 28 (daa822dd) is not in this checkout")
    runtime = release_28 if image == "release-28" else None
    source =(runtime.NativeTurnRuntime if runtime else NativeTurnRuntime)(fixture.original.identity, never_runs)
    fixture.hosted.portal.call(source.initialize)
    supplied = (runtime.NativeTaskInput if runtime else NativeTaskInput)(
        request_id="parked-on-source", accepted_etag=kept[1], call_id="OFFLINE-seed",
        execution_version=fixture.source.agent_version)
    binding, _ = fixture.hosted.portal.call(source._agent_record)
    fixture.hosted.portal.call(source.execution_lock.acquire)  # another source turn holds the slot
    async def exit_for_recovery():
        return "OFFLINE-exited"

    ctx = SimpleNamespace(input=supplied, task_id=binding.task_id, entry_mode="fresh", shutdown=asyncio.Event(),
                          cancel=asyncio.Event(), cancel_requested=False, timeout_exceeded=False,
                          exit_for_recovery=exit_for_recovery)
    parked = fixture.hosted.portal.start_task_soon(source._handle_turn, ctx)
    try:
        eventually(lambda: fixture.hosted.portal.call(lambda: bool(source.execution_lock._waiters)))
        sdk, seen = tracked(fixture)
        running = fixture.runtime.executor

        async def slow(receipt, scope, previous, control):
            await asyncio.sleep(0.5)  # the parked turn's 0.1 s reads see the adopted receipt ``started`` meanwhile
            return await running(receipt, scope, previous, control)

        fixture.runtime.executor = slow
        assert_abandoned(advance(fixture), fixture)
        assert Harness(fixture.runtime, sdk, fixture.hosted).wait("parked-on-source", "completed").turn_number == 2
        if runtime:
            with pytest.raises(Problem) as ended:
                parked.result(timeout=8)
            assert ended.value.body["error"]["code"] == "NATIVE_STATE_INVALID"
        else:
            assert parked.result(timeout=8).hosted_session_id == fixture.destination.session_id
    finally:
        if not parked.done():
            fixture.hosted.portal.call(ctx.shutdown.set)  # a failed case never leaves the turn behind the teardown
            fixture.hosted.portal.call(source.execution_lock.release)
            parked.exception(timeout=8)
        elif source.execution_lock.locked():
            fixture.hosted.portal.call(source.execution_lock.release)
    assert calls == [] and [rid for rid, *_ in seen] == ["parked-on-source"]
    saved = raw(fixture, "parked-on-source")[0]
    assert (saved["receipt"]["status"], saved["adopted_from"]) == ("completed", fixture.source.session_id)


def test_m9_an_image_before_the_adoption_field_can_not_read_an_adopted_receipt(file_api, monkeypatch):
    """The fence above without release 28's code: a ``rid:`` model with only its fields (``extra="forbid"``) refuses
    the adopted item, so every read of it there (the 0.1 s receipt read, the re-read after the slot, the read-back
    after a write the changed ETag refused) fails; its writes carry the accepted ETag and are refused by the store. The
    same item before adoption reads as usual."""
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.behavior_wire import Identifier, WireModel
    from shared_context.native_turns import _decode

    class Release28Rid(WireModel):
        receipt: NativeReceipt
        call_id: str | None = None
        run_token: Identifier | None = None

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    kept = seed(fixture, "adopted-here", "accepted")
    assert _decode(kept[0], Release28Rid).receipt.hosted_session_id == fixture.source.session_id
    assert_abandoned(advance(fixture), fixture)
    adopted = eventually(lambda: (lambda value: value if "adopted_from" in value else None)(raw(fixture, "adopted-here")[0]))
    with pytest.raises(Problem) as refused:
        _decode(adopted, Release28Rid)
    assert refused.value.body["error"]["code"] == "NATIVE_STATE_INVALID"


def test_r_a_mark_that_loses_to_the_source_finishing_continues_from_its_checkpoint(file_api, monkeypatch):
    """"(r)(s) 的競爭" (4) at the mark itself (P2-5): N unchanged minutes pass and the source saves its turn in the instant
    before the mark's CAS. The CAS loses on the changed ETag, the read-back is ``completed`` (never marked, no 503 to
    anyone), and the queued message continues from that turn's checkpoint. A coverage test: the wait task's next pass
    would also continue, so the read-back itself is pinned by ``test_s_a_mark_that_loses_…_in_the_same_pass``."""
    from shared_context import native_left_receipts
    from test_native_turns import sdk_result

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid = fixture.saved.conversation_id
    base = conversation_meta(fixture, cid)
    _, kept = left_conversation(fixture, "left-running", "started", cid=cid, n=2,
                                base=turn_checkpoint(fixture, cid, base.n))
    allocated = conversation_meta(fixture, cid)
    assert_abandoned(advance(fixture), fixture)
    sdk, seen = tracked(fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("queued-behind", cid, "Next")).status_code == 202
    eventually(lambda: stall(fixture, "left-running"))
    marking, finished = fixture.runtime._unknown, []

    async def source_saves_first(original, code, **values):
        if original.value.receipt.request_id == "left-running" and not finished:
            finished.append(await fixture.original._save_turn(
                versioned(kept), allocated, sdk_result(fixture.original._scope(allocated), "The left answer")))
        return await marking(original, code, **values)

    monkeypatch.setattr(fixture.runtime, "_unknown", source_saves_first)
    now[0] += native_left_receipts.STALL_SECONDS + 1
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("queued-behind", "completed").turn_number == 3
    assert [receipt.status for receipt in finished] == ["completed"]
    assert raw(fixture, "left-running")[0]["receipt"]["status"] == "completed"
    assert seen == [("queued-behind", base.sdk_session_id, True, 3)]


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_s_a_mark_that_loses_to_the_source_finishing_is_decided_by_its_read_back_in_the_same_pass(
        file_api, monkeypatch):
    """Review round 3 (decision 4): the mark's own read-back decides. The source writes its terminal ``unknown`` in the
    instant before the mark's CAS; the CAS loses on the changed ETag and the read-back sees the source's terminal
    receipt, so this very pass releases the slot: one status read of the waiting message takes it. The message is only
    queued here (no wait task runs), so no other pass can stand in for the read-back (without it this read answers
    ``not_taken``). The source's own failure code stays (never ``NATIVE_SOURCE_STALLED``)."""
    from shared_context import native_left_receipts
    from test_native_responses import response_submit

    async def queue_only(runtime, original, _behind):
        await native_left_receipts.enqueue(runtime, original)

    fixture = file_api
    now = wall_clock(monkeypatch)
    monkeypatch.setattr(native_left_receipts, "defer", queue_only)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = response_holder(fixture, "stuck-response", "started")
    assert_abandoned(advance(fixture), fixture)
    first = invoke(fixture.hosted, fixture.identity, response_submit("waiting-first", cid, "First"))
    assert first.status_code == 202 and first.json()["dispatch"]["state"] == "not_taken", first.text
    eventually(lambda: stall(fixture, "stuck-response"))
    marking, finished = fixture.runtime._unknown, []

    async def source_finishes_first(original, code, **values):
        if original.value.receipt.request_id == "stuck-response" and not finished:
            finished.append(await fixture.original._unknown(versioned(kept), "NATIVE_TURN_CANCELLED"))
        return await marking(original, code, **values)

    monkeypatch.setattr(fixture.runtime, "_unknown", source_finishes_first)
    now[0] += native_left_receipts.STALL_SECONDS + 1
    polled = invoke(fixture.hosted, fixture.identity, NativeRead(request_id="waiting-first")).json()
    assert [receipt.status for receipt in finished] == ["unknown"]
    assert (polled["status"], polled["dispatch"]["state"]) == ("accepted", "needs_response"), polled
    assert conversation_meta(fixture, cid).inflight_rid == "waiting-first"
    assert raw(fixture, "stuck-response")[0]["receipt"]["failure_code"] == "NATIVE_TURN_CANCELLED"


def api_driver(fixture, signing_key, *, behind_the_switch=0):
    """Review round 3: Hosted's own path to the API (``NativeAgentRelayClient`` with the Agent's identity, as in
    production ``native_sdk``) asking it to send a waiting Responses message; served by the real API app in-process.
    ``behind_the_switch``: that many first requests get the API's 409 ``NATIVE_SESSION_CHANGED`` (an API that has not
    caught up with the switch). Returns the RIDs Hosted asked for, one entry per request."""
    relay = relay_client(fixture, signing_key)
    asked = []

    async def dispatch(receipt):
        asked.append(receipt.request_id)
        if len(asked) <= behind_the_switch:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE the API still reads the source as current")
        return await relay.dispatch_waiting(receipt)

    fixture.runtime.on_slot_taken = dispatch
    return asked


def relay_client(fixture, signing_key):
    import httpx
    from shared_context.native_agent_tools import NativeAgentRelayClient

    agent = agent_token(fixture, signing_key)

    async def provider():
        return agent

    return NativeAgentRelayClient(
        "https://api.offline", provider, transport=httpx.ASGITransport(app=fixture.case.client.app))


def agent_token(fixture, signing_key):
    from test_m3_auth import token as signed

    target = next(target for target in fixture.case.c.h.config.targets if target.agent_id == fixture.source.agent_id)
    return signed(signing_key, {"oid": target.agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",))


def posts_of(fixture, content):
    return [body for body in fixture.response_posts if body["input"] == content]


@pytest.mark.parametrize("file_api", [{"lane": "foundry-responses-v1"}], indirect=True)  # the API's own capture
def test_s_a_teams_message_is_sent_once_after_the_relay_stops_polling_and_its_reply_is_read_later(
        file_api, monkeypatch, signing_key):
    """Review round 3 (decision 3), the Teams relay's own API path (``TeamsRelay.turn``): the message is accepted
    behind a left ``started`` receipt and the relay answers ``running``; it then stops polling (``WAIT_SECONDS`` 110,
    far below N) and nothing reads the RID or sends to the conversation. After N unchanged minutes Hosted's wait task
    marks the left receipt, takes the slot and asks the API to send the message; the API sends it once, the new session
    runs it, and the same Teams message read later gets the reply text (nothing is sent again)."""
    from conftest import InMemoryRepository
    from shared_context import native_left_receipts
    from shared_context.behavior_native_turn_wire import RELAY_PREFIX
    from shared_context.behavior_team_registry import resolve_agent_scope
    from shared_context.native_teams_relay import Chat, RelayTurn, TeamsRelay, _id

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, _ = response_holder(fixture, "stuck-response", "started")
    assert_abandoned(advance(fixture), fixture)
    asked = api_driver(fixture, signing_key)
    sdk, seen = tracked(fixture)
    composition, agent_id, relay_oid = fixture.case.composition, fixture.source.agent_id, "0e0e0e0e-0000-4000-8000-000000000001"
    records = InMemoryRepository()
    scope = resolve_agent_scope(composition.runtime.store, composition.config, agent_id)
    Chat(records, scope, agent_id, _id("tc-", relay_oid, "teams-chat")).remember(cid)  # this Teams chat's FA CID
    relay = TeamsRelay(composition, {relay_oid: agent_id}, records)
    message = RelayTurn(activity_id="act-1", chat_id="teams-chat", user_oid=scope.human_oid,
                        text="Sent from Teams before the relay gives up")
    rid = _id(RELAY_PREFIX, relay_oid, "teams-chat", "act-1")
    assert relay.turn(relay_oid, message).state == "running"
    assert status_of(fixture, rid) == "accepted"
    # From here on nobody reads the RID or sends anything to this conversation.
    eventually(lambda: stall(fixture, "stuck-response"))
    assert posts_of(fixture, message.text) == [] and asked == []
    now[0] += native_left_receipts.STALL_SECONDS + 1
    eventually(lambda: status_of(fixture, rid) == "completed")
    assert len(posts_of(fixture, message.text)) == 1 and set(asked) == {rid}
    assert [value for value, *_ in seen] == [rid]
    assert raw(fixture, "stuck-response")[0]["receipt"]["failure_code"] == "NATIVE_SOURCE_STALLED"
    later = relay.turn(relay_oid, message)
    # The final reply (read path) also tells the owner the Agent moved without its work files (ADR-0009 §8); this
    # direct call writes no HTTP response, so the slot stays pending for the relay route to clear after writing.
    assert later.state == "done" and later.text == (
        "Complete public answer\n\n已換到新版環境；這次沒有搬工作檔，舊環境與檔案都保留。"), later
    assert later.environment_notice.kind == "environment_switched_without_files"
    assert len(posts_of(fixture, message.text)) == 1


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_m9_an_adopted_slot_holder_is_sent_once_with_no_poll(file_api, monkeypatch, signing_key):
    """Review round 3 (decision 3) with M9: a never-started slot holder adopted at the switch gets a wait task of its
    own, which has the API send it; a 409 ``NATIVE_SESSION_CHANGED`` (an API still finishing the switch) is asked again
    after the ``early_delay`` backoff (shrunk here; review round 6). It runs once in the new session with no read or
    later message."""
    from shared_context import native_left_receipts

    fixture = file_api
    wall_clock(monkeypatch)
    monkeypatch.setattr(native_left_receipts, "RESEND_SECONDS", 0.05)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = response_holder(fixture, "never-started-response", "accepted")
    asked = api_driver(fixture, signing_key, behind_the_switch=2)
    assert_abandoned(advance(fixture), fixture)
    eventually(lambda: raw(fixture, "never-started-response")[0]["receipt"]["status"] == "completed")
    assert len(posts_of(fixture, kept[0]["receipt"]["command"]["content"])) == 1
    assert asked == ["never-started-response"] * 3
    assert raw(fixture, "never-started-response")[0]["adopted_from"] == fixture.source.session_id


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_the_api_sends_only_the_agents_own_waiting_responses_message(file_api, monkeypatch, signing_key):
    """The ``/waiting-dispatch`` route needs the Agent's own identity, sends a message waiting at ``needs_response`` in
    the Agent's current session once (asked again, it sends nothing more), and sends nothing for any other message."""
    from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
    from test_native_responses import response_submit

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    assert_abandoned(advance(fixture), fixture)
    cid, path = fixture.saved.conversation_id, NATIVE_AGENT_PREFIX + "/waiting-dispatch"
    waiting = invoke(fixture.hosted, fixture.identity, response_submit("waits-here", cid, "Send me once"))
    assert waiting.json()["dispatch"]["state"] == "needs_response", waiting.text
    body = {"conversation_id": cid, "request_id": "waits-here"}
    owner = fixture.case.client.post(path, headers=fixture.case.headers, json=body)
    assert owner.status_code == 403, owner.text
    agent = {"Authorization": "Bearer " + agent_token(fixture, signing_key)}
    other = fixture.case.client.post(path, headers=agent, json={**body, "conversation_id": "another-conversation"})
    assert other.status_code == 409 and other.json()["error"]["code"] == "NATIVE_STATE_SCOPE", other.text
    left = fixture.case.client.post(path, headers=agent, json={"conversation_id": cid, "request_id": "original-rid"})
    assert left.status_code == 409 and left.json()["error"]["code"] == "NATIVE_SESSION_CHANGED", left.text
    # Hosted's client keeps that code (``wait_turn`` asks again) and reports any other refusal as refused.
    relay = relay_client(fixture, signing_key)
    for rid, cid_, code in (("original-rid", cid, "NATIVE_SESSION_CHANGED"),
                            ("waits-here", "another-conversation", "NATIVE_RELAY_REFUSED")):
        receipt = fixture.hosted.portal.call(fixture.runtime.read, rid).model_copy(update={"conversation_id": cid_})
        with pytest.raises(Problem) as refused:
            fixture.hosted.portal.call(relay.dispatch_waiting, receipt)
        assert (refused.value.status, refused.value.body["error"]["code"]) == (409, code)
    assert posts_of(fixture, "Send me once") == []
    for _ in range(2):
        sent = fixture.case.client.post(path, headers=agent, json=body)
        assert sent.status_code == 200 and sent.json()["status"] in {"accepted", "started", "completed"}, sent.text
    assert Harness(fixture.runtime, fixture.runtime.executor, fixture.hosted).wait("waits-here", "completed")
    assert len(posts_of(fixture, "Send me once")) == 1


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_the_api_does_not_send_a_waiting_message_after_the_owner_policy_is_revoked(file_api, monkeypatch, signing_key):
    """Review round 4 (P2-1): like every owner send, ``/waiting-dispatch`` reads the owner's work policy again before
    the billable POST. Revoked: 403 ``WORK_POLICY_REVOKED``, nothing sent, and Hosted's client keeps that code."""
    from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
    from test_native_responses import response_submit

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    assert_abandoned(advance(fixture), fixture)
    cid, path = fixture.saved.conversation_id, NATIVE_AGENT_PREFIX + "/waiting-dispatch"
    waiting = invoke(fixture.hosted, fixture.identity, response_submit("waits-here", cid, "Never sent once revoked"))
    assert waiting.json()["dispatch"]["state"] == "needs_response", waiting.text
    fixture.case.c.control.revoke(fixture.case.c.human, "revoked-before-the-waiting-dispatch")
    agent = {"Authorization": "Bearer " + agent_token(fixture, signing_key)}
    refused = fixture.case.client.post(path, headers=agent, json={"conversation_id": cid, "request_id": "waits-here"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "WORK_POLICY_REVOKED", refused.text
    receipt = fixture.hosted.portal.call(fixture.runtime.read, "waits-here")
    with pytest.raises(Problem) as kept:
        fixture.hosted.portal.call(relay_client(fixture, signing_key).dispatch_waiting, receipt)
    assert (kept.value.status, kept.value.body["error"]["code"]) == (403, "WORK_POLICY_REVOKED")
    assert posts_of(fixture, "Never sent once revoked") == []
    assert status_of(fixture, "waits-here") == "accepted"


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_a_waiting_message_refused_for_a_revoked_owner_policy_waits_and_is_sent_once_restored(
        file_api, monkeypatch, signing_key):
    """Review round 5 (P2-2), end to end: the owner's policy is revoked while an adopted slot holder waits for its
    send. Hosted asks the API once; the API sends nothing (403 ``WORK_POLICY_REVOKED``) and the receipt is left as an
    owner send under that policy leaves it: ``accepted``, ``needs_response``, holding its slot, not ended and not asked
    again. Once the policy is restored, the API's next touch of the conversation (the owner's next message) sends it,
    once, and it runs; the owner's next message runs after it."""
    import time

    from shared_context.behavior_native_responses_api import _advance_inflight, platform_client
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from test_native_responses import response_submit

    fixture = file_api
    wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = response_holder(fixture, "never-started-response", "accepted")
    relay, asked = relay_client(fixture, signing_key), []
    control = fixture.case.c.control

    async def dispatch(receipt):
        if not asked:
            control.revoke(fixture.case.c.human, "revoked-while-the-message-waits")
        asked.append(receipt.request_id)
        return await relay.dispatch_waiting(receipt)

    fixture.runtime.on_slot_taken = dispatch
    assert_abandoned(advance(fixture), fixture)
    eventually(lambda: asked)
    time.sleep(0.3)  # the wait task's later looks (``POLL_SECONDS`` 0.02 here) would ask again or end it
    parked = raw(fixture, "never-started-response")[0]
    assert (parked["receipt"]["status"], parked["receipt"]["failure_code"]) == ("accepted", None)
    assert parked["adopted_from"] == fixture.source.session_id
    read = invoke(fixture.hosted, fixture.identity, NativeRead(request_id="never-started-response")).json()
    assert (read["status"], read["dispatch"]["state"]) == ("accepted", "needs_response"), read
    assert conversation_meta(fixture, cid).inflight_rid == "never-started-response"
    assert posts_of(fixture, kept[0]["receipt"]["command"]["content"]) == []
    assert asked == ["never-started-response"]
    # The owner's next message: accepted behind the holder; the API's advance of its send (``_advance_inflight``, the
    # owner's policy read again before the POST) is refused while the policy is revoked, nothing sent.
    answered = invoke(fixture.hosted, fixture.identity, response_submit("after-restore", cid, "The owner's next message"))
    assert (answered.json()["status"], answered.json()["dispatch"]["state"]) == ("accepted", "not_taken"), answered.text
    client = fixture.case.foundry.for_target(fixture.destination)
    platform = platform_client(fixture.case.composition, client)

    def owner_policy():
        if not control.installed().active:
            raise Problem(403, "WORK_POLICY_REVOKED", "OFFLINE the owner's policy is revoked")

    with pytest.raises(Problem) as refused:
        _advance_inflight(client, platform, NativeReceipt.model_validate_json(answered.text), before_send=owner_policy)
    assert refused.value.body["error"]["code"] == "WORK_POLICY_REVOKED"
    assert posts_of(fixture, kept[0]["receipt"]["command"]["content"]) == []
    assert raw(fixture, "never-started-response")[0]["receipt"]["status"] == "accepted"
    # The policy is restored (the revocation row is gone); the owner's next touch sends the waiting message, once.
    h = fixture.case.c.h
    h.table.backing.rows.pop(("Behavior", h.config.table.scope.partition_key, "behavior:v1:row:" + control.revocation_key))
    assert control.installed().active
    _advance_inflight(client, platform, NativeReceipt.model_validate_json(answered.text), before_send=owner_policy)
    eventually(lambda: raw(fixture, "never-started-response")[0]["receipt"]["status"] == "completed")
    assert len(posts_of(fixture, kept[0]["receipt"]["command"]["content"])) == 1
    eventually(lambda: raw(fixture, "after-restore")[0]["receipt"]["status"] == "completed")
    assert asked.count("never-started-response") == 1  # sent by the owner's touch, never asked again by Hosted
    assert len(posts_of(fixture, "The owner's next message")) == 1
    assert len(posts_of(fixture, kept[0]["receipt"]["command"]["content"])) == 1


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
@pytest.mark.parametrize("gate", ["after-the-slot", "before-the-start"])
def test_m9_a_source_responses_turn_parked_before_the_switch_never_touches_the_adopted_receipt(
        file_api, monkeypatch, signing_key, gate):
    """Review round 4 (P2-2), the Responses analog of the parked Invocations case, against this image's own handler:
    the source's Responses handler passed its checks before the switch and waits in its execution slot. The new
    session adopts the message, the API sends it there and it starts. The parked handler then goes on:

    - ``after-the-slot``: its re-read in the slot sees the receipt on the new SID and ends ``NATIVE_SESSION_CHANGED``
      (``native_responses.py`` guard after ``execution_slot``);
    - ``before-the-start``: it re-read before the switch, so its ``started`` CAS loses on the changed ETag and the
      read-back is the new session's ``started`` receipt, which it leaves as it is (guard on the lost CAS).

    Without either guard the parked handler re-enters the new session's running turn (``_reenter``) and writes it
    ``unknown``. The message runs exactly once, on the new SID; the source never reaches its SDK."""
    import asyncio
    from contextlib import asynccontextmanager

    from azure.ai.agentserver.core._request_context import FoundryAgentRequestContext, set_request_context
    from azure.ai.agentserver.responses import CreateResponse
    from azure.ai.agentserver.responses._id_generator import IdGenerator
    from shared_context.native_responses import NativeResponsesHandler
    from shared_context.native_responses_wire import (
        NATIVE_RESPONSE_CID_HEADER, NATIVE_RESPONSE_DIGEST_HEADER, NATIVE_RESPONSE_RID_HEADER,
    )

    fixture = file_api
    wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    rid = "parked-response"
    cid, kept = response_holder(fixture, rid, "accepted")
    accepted, calls = kept[0]["receipt"], []

    async def never_runs(*values):
        calls.append(values)
        raise RuntimeError("OFFLINE the parked source Responses turn reached its SDK")

    async def events():
        return asyncio.Event(), asyncio.Event()

    source = NativeTurnRuntime(fixture.original.identity, never_runs)
    fixture.hosted.portal.call(source.initialize)
    go, finish = fixture.hosted.portal.call(events)
    reached = []
    if gate == "after-the-slot":
        slot = source.execution_slot

        @asynccontextmanager
        async def gated(original, control, signals):
            async with slot(original, control, signals) as acquired:
                reached.append(acquired)
                await go.wait()
                yield acquired

        source.execution_slot = gated
    else:
        cas = source._cas

        async def gated(original, replacement):
            if replacement.receipt.status == "started" and not reached:
                reached.append(True)
                await go.wait()
            return await cas(original, replacement)

        source._cas = gated
    sdk, seen = tracked(fixture)
    running = fixture.runtime.executor

    async def held(receipt, scope, previous, control):
        await finish.wait()  # the new session's turn stays ``started`` while the parked handler goes on
        return await running(receipt, scope, previous, control)

    fixture.runtime.executor = held
    api_driver(fixture, signing_key)

    async def text():
        return accepted["command"]["content"]

    async def exit_for_recovery():
        return "OFFLINE-exited"

    context = SimpleNamespace(
        response_id=IdGenerator.new_response_id(), is_recovery=False, client_cancelled=False, get_input_text=text,
        shutdown=asyncio.Event(), exit_for_recovery=exit_for_recovery, client_headers={
            NATIVE_RESPONSE_RID_HEADER: rid, NATIVE_RESPONSE_CID_HEADER: cid,
            NATIVE_RESPONSE_DIGEST_HEADER: accepted["submit_digest"]})

    async def handle():
        set_request_context(FoundryAgentRequestContext(session_id=fixture.source.session_id))
        request = CreateResponse({"input": accepted["command"]["content"], "background": True, "store": True})
        return [event async for event in NativeResponsesHandler(source).handle(request, context, asyncio.Event())]

    fixture.hosted.portal.call(source.execution_lock.acquire)  # another source turn holds the slot
    parked = fixture.hosted.portal.start_task_soon(handle)
    try:
        eventually(lambda: fixture.hosted.portal.call(lambda: bool(source.execution_lock._waiters)))
        fixture.hosted.portal.call(source.execution_lock.release)
        eventually(lambda: reached)
        assert_abandoned(advance(fixture), fixture)
        eventually(lambda: (lambda value: (value["receipt"]["status"], value["receipt"]["hosted_session_id"]) == (
            "started", fixture.destination.session_id))(raw(fixture, rid)[0]))
        fixture.hosted.portal.call(go.set)
        ended = parked.result(timeout=8)[-1]
    finally:
        fixture.hosted.portal.call(go.set)
        fixture.hosted.portal.call(finish.set)
        if not parked.done():
            fixture.hosted.portal.call(context.shutdown.set)
            parked.exception(timeout=8)
    saved = eventually(lambda: (lambda value: value if value["receipt"]["status"] != "started" else None)(
        raw(fixture, rid)[0]))
    assert (saved["receipt"]["status"], saved["receipt"]["failure_code"]) == ("completed", None)
    assert (saved["receipt"]["hosted_session_id"], saved["adopted_from"]) == (
        fixture.destination.session_id, fixture.source.session_id)
    assert calls == [] and [value for value, *_ in seen] == [rid]
    assert len(posts_of(fixture, accepted["command"]["content"])) == 1
    assert ended["type"] == "response.failed"
    assert ended["response"]["error"]["code"] == (
        "NATIVE_SESSION_CHANGED" if gate == "after-the-slot" else "NATIVE_TURN_NOT_TAKEN")


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_an_api_without_the_waiting_dispatch_route_is_asked_again(file_api, monkeypatch, signing_key):
    """Review round 4 (P3): deploy order is API before Hosted, but a Hosted image that meets an older API (its
    ``/waiting-dispatch`` is 404, nothing sent) asks again after ``RESEND_SECONDS`` instead of giving up; once the
    API has the route, the adopted slot holder is sent once and runs."""
    import httpx
    from shared_context import native_left_receipts
    from shared_context.native_agent_tools import NativeAgentRelayClient

    fixture = file_api
    wall_clock(monkeypatch)
    monkeypatch.setattr(native_left_receipts, "RESEND_SECONDS", 0.05)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = response_holder(fixture, "never-started-response", "accepted")
    api, answers = httpx.ASGITransport(app=fixture.case.client.app), []

    class OlderApiFirst(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if not answers:
                answers.append(404)
                return httpx.Response(404, json={"detail": "Not Found"})  # FastAPI's answer for a missing route
            response = await api.handle_async_request(request)
            answers.append(response.status_code)
            return response

    agent = agent_token(fixture, signing_key)

    async def provider():
        return agent

    relay = NativeAgentRelayClient("https://api.offline", provider, transport=OlderApiFirst())
    fixture.runtime.on_slot_taken = relay.dispatch_waiting
    assert_abandoned(advance(fixture), fixture)
    eventually(lambda: raw(fixture, "never-started-response")[0]["receipt"]["status"] == "completed")
    assert answers[:2] == [404, 200] and len(posts_of(fixture, kept[0]["receipt"]["command"]["content"])) == 1


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
@pytest.mark.parametrize("failure", ["5xx", "transport", "404"])
def test_a_waiting_send_the_api_keeps_failing_ends_after_its_attempt_budget(file_api, monkeypatch, failure):
    """Review round 5 (P2-1): failed requests to send a waiting message (5xx, a transport error, an older API's 404)
    are bounded by ``SEND_ATTEMPTS``, not by the wait task's timeout (which now hands over to a successor; left long
    enough here not to fire, so the budget alone ends it): after that many failed requests the message ends
    ``unknown`` (``NATIVE_TURN_TIMEOUT``), never a silent ``needs_response``, and is not asked for again."""
    from datetime import timedelta

    from shared_context import native_left_receipts

    fixture = file_api
    assert native_left_receipts.WAIT_TIMEOUT_SECONDS == 3600 and native_left_receipts.SEND_ATTEMPTS == 60
    assert native_left_receipts.SEND_ATTEMPTS * native_left_receipts.RESEND_SECONDS < (
        native_left_receipts.WAIT_TIMEOUT_SECONDS)
    assert fixture.runtime.left_wait._opts.timeout == timedelta(seconds=native_left_receipts.WAIT_TIMEOUT_SECONDS)
    fixture.runtime.left_wait._opts.timeout = timedelta(seconds=5)
    monkeypatch.setattr(native_left_receipts, "SEND_ATTEMPTS", 4)
    monkeypatch.setattr(native_left_receipts, "RESEND_SECONDS", 0.02)
    wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = response_holder(fixture, "never-started-response", "accepted")
    asked = []

    async def failing(receipt):
        asked.append(receipt.request_id)
        if failure == "transport":
            raise OSError("OFFLINE the connection to the API failed")
        raise Problem(503 if failure == "5xx" else 404, "NATIVE_RELAY_OUTCOME_UNKNOWN", "OFFLINE the API keeps failing")

    fixture.runtime.on_slot_taken = failing
    assert_abandoned(advance(fixture), fixture)
    ended = eventually(lambda: (lambda value: value if value["receipt"]["status"] != "accepted" else None)(
        raw(fixture, "never-started-response")[0]))
    assert (ended["receipt"]["status"], ended["receipt"]["failure_code"]) == ("unknown", "NATIVE_TURN_TIMEOUT")
    assert asked == ["never-started-response"] * 4
    assert posts_of(fixture, kept[0]["receipt"]["command"]["content"]) == []
    assert "never-started-response" not in left_queue(fixture, cid)


def test_a_waiting_send_the_api_answers_early_backs_off_from_the_next_look_to_five_minutes():
    """Review round 6 (P3): a 409 ``NATIVE_SESSION_CHANGED`` in a row is asked again at the next look (5 s), then
    after ``RESEND_SECONDS`` (30 s), doubling up to 5 minutes, and stays there: never every 5 s without end, never a
    failure."""
    from shared_context import native_left_receipts

    assert (native_left_receipts.POLL_SECONDS, native_left_receipts.RESEND_SECONDS,
            native_left_receipts.EARLY_MAX_SECONDS) == (5, 30, 300)
    assert [native_left_receipts.early_delay(count) for count in range(1, 10)] == [
        5, 30, 60, 120, 240, 300, 300, 300, 300]


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
def test_a_waiting_send_the_api_answers_early_is_asked_again_with_backoff_and_runs(
        file_api, monkeypatch, signing_key):
    """Review round 6 (P3): the API keeps answering 409 ``NATIVE_SESSION_CHANGED`` (it has not caught up with the
    switch; nothing sent). The wait task asks again with the ``early_delay`` backoff (shrunk here), not at every look,
    never counts it as failed (``SEND_ATTEMPTS`` 2 here would end it), and the message runs once the API answers."""
    import time

    import httpx
    from shared_context import native_left_receipts
    from shared_context.native_agent_tools import NativeAgentRelayClient

    fixture = file_api
    wall_clock(monkeypatch)
    monkeypatch.setattr(native_left_receipts, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(native_left_receipts, "RESEND_SECONDS", 0.05)
    monkeypatch.setattr(native_left_receipts, "EARLY_MAX_SECONDS", 0.2)
    monkeypatch.setattr(native_left_receipts, "SEND_ATTEMPTS", 2)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = response_holder(fixture, "never-started-response", "accepted")
    agent = agent_token(fixture, signing_key)

    async def provider():
        return agent

    relay = NativeAgentRelayClient("https://api.offline", provider,
                                   transport=httpx.ASGITransport(app=fixture.case.client.app))
    asked = []

    async def early_then_sent(receipt):
        asked.append(time.monotonic())
        if len(asked) <= 7:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE the API has not caught up with the switch")
        return await relay.dispatch_waiting(receipt)

    fixture.runtime.on_slot_taken = early_then_sent
    assert_abandoned(advance(fixture), fixture)
    ended = eventually(lambda: (lambda value: None if value["receipt"]["status"] in {"accepted", "started"} else value)(
        raw(fixture, "never-started-response")[0]))
    assert (ended["receipt"]["status"], ended["receipt"]["failure_code"]) == ("completed", None)
    gaps = [later - earlier for earlier, later in zip(asked, asked[1:])][:7]
    assert all(gap >= expected * 0.9 for gap, expected in zip(gaps, [0.01, 0.05, 0.1, 0.2, 0.2, 0.2, 0.2])), gaps
    assert len(asked) == 8 and len(posts_of(fixture, kept[0]["receipt"]["command"]["content"])) == 1


def recorded_wait_tasks(fixture, monkeypatch, *, seconds):
    """Shrinks the wait task's SDK timeout to ``seconds`` (it fires meanwhile, as 1 h does in production) and records
    the ID of every wait task started from here on."""
    from datetime import timedelta

    wait = fixture.runtime.left_wait  # the SDK ``Task`` (``__slots__``): its handle is replaced, not patched
    wait._opts.timeout = timedelta(seconds=seconds)
    started = []

    async def recording(*, task_id, **values):
        started.append(task_id)
        return await wait.start(task_id=task_id, **values)

    monkeypatch.setattr(fixture.runtime, "left_wait", SimpleNamespace(start=recording, _opts=wait._opts))
    return started


def move_progress(fixture, rid, text):
    """The left turn's public progress snapshot changes (what counts as progress for the stall rule)."""
    from shared_context.native_progress import _key as progress_key

    async def write():
        async with fixture.runtime._store(TURNS_STORE) as store:
            found = await store.get_item(progress_key(rid))
            if found is None:
                await store.create_item(progress_key(rid), {"OFFLINE": text})
            else:
                await store.set_item(progress_key(rid), {"OFFLINE": text}, if_match=found.etag)
            return (await store.get_item(progress_key(rid))).etag
    return fixture.hosted.portal.call(write)


def test_r_a_message_waits_behind_a_healthy_left_turn_longer_than_one_wait_task(file_api, monkeypatch):
    """Review round 5 (P2-1): the left turn keeps progressing for more than an hour (simulated clock; each step under
    N). Each wait task's timeout (shrunk) hands over to a successor: the message stays ``accepted`` in its queue,
    never ``NATIVE_TURN_TIMEOUT``, and runs once the left turn is done (stalled here, so marked)."""
    import time

    from shared_context import native_left_receipts

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, kept = left_conversation(fixture, "left-running", "started")
    assert_abandoned(advance(fixture), fixture)
    started = recorded_wait_tasks(fixture, monkeypatch, seconds=0.3)
    sdk, seen = tracked(fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("queued-behind", cid, "Next")).status_code == 202
    eventually(lambda: stall(fixture, "left-running"))
    for step in range(8):  # 8 x 9 minutes: 72 minutes of a left turn that keeps moving
        now[0] += native_left_receipts.STALL_SECONDS - 60
        etag = move_progress(fixture, "left-running", "step " + str(step))
        eventually(lambda: (stall(fixture, "left-running") or {}).get("progress_etag") == etag)
        time.sleep(0.2)
    assert now[0] - 5000.0 > 3600
    assert len(set(started)) >= 3, started  # the first task and at least two successors
    current = raw(fixture, "queued-behind")[0]["receipt"]
    assert (current["status"], current["failure_code"]) == ("accepted", None)
    assert "queued-behind" in left_queue(fixture, cid)
    assert raw(fixture, "left-running") == kept and seen == []
    now[0] += native_left_receipts.STALL_SECONDS + 1
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("queued-behind", "completed").turn_number == 2
    assert raw(fixture, "left-running")[0]["receipt"]["failure_code"] == "NATIVE_SOURCE_STALLED"
    assert [rid for rid, *_ in seen] == ["queued-behind"]


def test_r_a_wait_task_recovered_past_its_timeout_hands_over_to_a_successor(file_api, monkeypatch):
    """Review round 5 (P2-1): after a VM restart the SDK recovers the wait task with its budget already passed and sets
    ``timeout_exceeded`` before its first look (``_compute_remaining_for_watchdog``). The message is not ended: the
    task starts one successor (a new task ID derived from its own) and returns; the same recovered task run again
    finds that successor (no second one)."""
    import asyncio

    from shared_context import native_left_receipts

    fixture = file_api
    wall_clock(monkeypatch)
    monkeypatch.setattr(native_left_receipts, "POLL_SECONDS", 3600)  # the first task looks once, then sleeps
    incompatible_idle(fixture, monkeypatch)
    cid, _ = left_conversation(fixture, "left-running", "started")
    assert_abandoned(advance(fixture), fixture)
    started = recorded_wait_tasks(fixture, monkeypatch, seconds=3600)
    assert invoke(fixture.hosted, fixture.identity, submit("queued-behind", cid, "Next")).status_code == 202
    eventually(lambda: stall(fixture, "left-running"))
    [first] = started
    accepted = raw(fixture, "queued-behind")

    async def exit_for_recovery():
        return "OFFLINE-exited"

    def recovered():
        async def run():
            cancel = asyncio.Event()
            cancel.set()  # the SDK sets it together with ``timeout_exceeded``
            ctx = SimpleNamespace(
                input=NativeTaskInput(request_id="queued-behind", accepted_etag=accepted[1], call_id=None,
                                      execution_version=fixture.identity.agent_version),
                task_id=first, shutdown=asyncio.Event(), cancel=cancel, cancel_requested=False,
                timeout_exceeded=True, exit_for_recovery=exit_for_recovery)
            return await native_left_receipts.wait_turn(fixture.runtime, ctx)
        return fixture.hosted.portal.call(run)

    assert recovered() == "queued-behind"
    assert len(started) == 2 and started[1] != first
    assert raw(fixture, "queued-behind") == accepted and "queued-behind" in left_queue(fixture, cid)
    assert recovered() == "queued-behind"  # run again (e.g. a second recovery): the same successor, retained
    assert started[2] == started[1] and raw(fixture, "queued-behind") == accepted


def test_r_a_left_turn_that_stalls_across_a_wait_task_timeout_is_marked_and_the_message_runs(file_api, monkeypatch):
    """Review round 5 (P2-1), the stalled side: the left turn stops changing; the wait task's timeout (shrunk) fires
    before and after N passes. The stall rule decides as before: the left receipt is marked ``unknown``
    (``NATIVE_SOURCE_STALLED``) and the queued message runs, once; the timeout itself never fails the message."""
    import time

    from shared_context import native_left_receipts

    fixture = file_api
    now = wall_clock(monkeypatch)
    incompatible_idle(fixture, monkeypatch)
    cid, _ = left_conversation(fixture, "left-running", "started")
    assert_abandoned(advance(fixture), fixture)
    started = recorded_wait_tasks(fixture, monkeypatch, seconds=0.2)
    sdk, seen = tracked(fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("queued-behind", cid, "Next")).status_code == 202
    eventually(lambda: stall(fixture, "left-running"))
    eventually(lambda: len(set(started)) >= 2)
    time.sleep(0.3)
    assert status_of(fixture, "queued-behind") == "accepted" and seen == []
    now[0] += native_left_receipts.STALL_SECONDS + 1
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("queued-behind", "completed").turn_number == 2
    marked = raw(fixture, "left-running")[0]["receipt"]
    assert (marked["status"], marked["failure_code"]) == ("unknown", "NATIVE_SOURCE_STALLED")
    assert [rid for rid, *_ in seen] == ["queued-behind"]


@pytest.mark.parametrize("failure", ["queue-503", "start-transport"])
def test_r_a_transient_error_handing_over_to_a_successor_is_retried(file_api, monkeypatch, failure):
    """Review round 6 (P3): the hand-over on ``timeout_exceeded`` meets a brief error once: a 503 writing the
    conversation's queue (``enqueue``) or a transport error starting the successor task. The task does not fail with it
    (that left the message ``accepted`` at its queue's head with no task looking at it): it looks again after
    ``POLL_SECONDS``, starts the successor, and the message runs once the left turn is marked stalled."""
    from shared_context import native_left_receipts

    fixture = file_api
    now = wall_clock(monkeypatch)
    monkeypatch.setattr(native_left_receipts, "POLL_SECONDS", 0.05)
    incompatible_idle(fixture, monkeypatch)
    cid, _ = left_conversation(fixture, "left-running", "started")
    assert_abandoned(advance(fixture), fixture)
    started = recorded_wait_tasks(fixture, monkeypatch, seconds=0.2)
    calls, failed = [], []
    if failure == "queue-503":
        enqueue = native_left_receipts.enqueue

        async def flaky_enqueue(runtime, original):
            calls.append(original.value.receipt.request_id)
            if len(calls) == 2:  # the first hand-over (the first call queued the message when it was sent)
                failed.append(failure)
                raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE the queue write is unconfirmed")
            return await enqueue(runtime, original)

        monkeypatch.setattr(native_left_receipts, "enqueue", flaky_enqueue)
    else:
        recording = fixture.runtime.left_wait

        async def flaky_start(*, task_id, **values):
            calls.append(task_id)
            if len(calls) == 2:  # the first hand-over's successor
                failed.append(failure)
                raise OSError("OFFLINE the connection to the task store failed")
            return await recording.start(task_id=task_id, **values)

        monkeypatch.setattr(fixture.runtime, "left_wait", SimpleNamespace(start=flaky_start, _opts=recording._opts))
    sdk, seen = tracked(fixture)
    assert invoke(fixture.hosted, fixture.identity, submit("queued-behind", cid, "Next")).status_code == 202
    eventually(lambda: stall(fixture, "left-running"))
    eventually(lambda: failed and len(set(started)) >= 2)  # the successor started after the failed hand-over
    assert failed == [failure] and status_of(fixture, "queued-behind") == "accepted" and seen == []
    assert "queued-behind" in left_queue(fixture, cid)
    now[0] += native_left_receipts.STALL_SECONDS + 1
    assert Harness(fixture.runtime, sdk, fixture.hosted).wait("queued-behind", "completed").turn_number == 2
    assert [rid for rid, *_ in seen] == ["queued-behind"]


@pytest.mark.parametrize("file_api", ["foundry-responses-v1"], indirect=True)
@pytest.mark.parametrize("caller", ["another-agent", "an-unknown-app"])
def test_only_the_agents_own_identity_can_have_its_waiting_message_sent(file_api, monkeypatch, signing_key, caller):
    """Review round 4 (P3): ``/waiting-dispatch`` with another Agent's token (or an app that is no Agent) is refused
    and nothing is sent; the message keeps waiting for its own Agent. A registered teammate reading through its own
    session: ``test_native_agent_api.test_another_agent_can_not_have_a_waiting_responses_message_sent``."""
    from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
    from test_m3_auth import token as signed
    from test_native_responses import response_submit

    fixture = file_api
    incompatible_idle(fixture, monkeypatch)
    assert_abandoned(advance(fixture), fixture)
    cid = fixture.saved.conversation_id
    waiting = invoke(fixture.hosted, fixture.identity, response_submit("waits-here", cid, "Only for its own Agent"))
    assert waiting.json()["dispatch"]["state"] == "needs_response", waiting.text
    others = [target for target in fixture.case.c.h.config.targets if target.agent_id != fixture.source.agent_id]
    if caller == "another-agent":
        assert others, "OFFLINE the fixture registers a second Agent"
    oid = others[0].agent_oid if caller == "another-agent" else "0e0e0e0e-0000-4000-8000-00000000abcd"
    stranger = signed(signing_key, {"oid": str(oid), "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",))
    refused = fixture.case.client.post(NATIVE_AGENT_PREFIX + "/waiting-dispatch", json={
        "conversation_id": cid, "request_id": "waits-here"}, headers={"Authorization": "Bearer " + stranger})
    # Not this Agent's scope (a second target with no Team registration here: AGENT_SCOPE_MISSING), or no Agent at all.
    assert refused.status_code == (409 if caller == "another-agent" else 403), refused.text
    assert posts_of(fixture, "Only for its own Agent") == []
    assert status_of(fixture, "waits-here") == "accepted"
