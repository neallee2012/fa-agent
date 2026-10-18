"""ADR-0009 phase 1: the API binding row records where an Agent's session is and which version it actually ran.

Recording only: no turn result changes, and no session is called for the row. The owner response carries the pending
slot notice (drift, or a switch's §8 notice) and, from phase 2c, the owner's route clears it by CAS after the
response was written. Platform I/O and Hosted are offline doubles."""

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from azure.ai.projects.models import AgentSessionResource

from conftest import InMemoryRepository
from shared_context import behavior_native_sessions, native_binding
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_native_sessions import NativeSessions, native_session_id, native_sessions
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import (
    NOTICE_KINDS_HEADER, NOTICE_KINDS_VERSION, NativeEnvironmentNotice, NativeEnvironmentView, NativeReceipt,
    before_dispatch,
)
from shared_context.errors import Problem
from shared_context.native_binding import NativeBindingStore, binding_store
from test_native_turn_api import PREFIX, NativeTransport, native_api, submit
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from test_native_environment_api import environment_at
from test_native_responses_api import responses_api  # noqa: F401
from test_native_session_prepare_api import project_client

NOW = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)


class CountingRepository(InMemoryRepository):
    """Counts the Table writes; ``before_write`` lets another writer change the row first (a CAS race)."""

    def __init__(self):
        super().__init__()
        self.writes, self.before_write, self.fail_writes, self.fail_reads = [], None, 0, 0

    def get(self, team, row):
        if self.fail_reads:
            self.fail_reads -= 1
            raise Problem(503, "TABLE_UNAVAILABLE", "synthetic Table outage")
        return super().get(team, row)

    def _write(self, kind, team, row, payload, call):
        if self.before_write is not None:
            hook, self.before_write = self.before_write, None
            hook()
        if self.fail_writes:
            self.fail_writes -= 1
            raise Problem(503, "TABLE_UNAVAILABLE", "synthetic Table outage")
        result = call()
        self.writes.append((kind, payload))
        return result

    def create(self, team, row, payload):
        return self._write("create", team, row, payload, lambda: super(CountingRepository, self).create(team, row, payload))

    def replace(self, team, row, payload, etag):
        return self._write("replace", team, row, payload,
                           lambda: super(CountingRepository, self).replace(team, row, payload, etag))


@pytest.fixture
def bound(native_api, monkeypatch):
    case = native_api
    case.repository = CountingRepository()
    monkeypatch.setattr(native_binding, "TableRepository", lambda _client: case.repository)
    case.store = binding_store(case.composition)
    case.agent_id, case.sid = case.remote.target.agent_id, case.remote.target.session_id
    # A workbench bridge that decodes the phase 2c notice kinds; ``old_bridge`` removes the header again.
    case.headers = {**case.headers, NOTICE_KINDS_HEADER: NOTICE_KINDS_VERSION}
    return case


def old_bridge(case):
    """A bridge deployed before phase 2c (a deploy updates the API first): no notice-kinds header."""
    case.headers = {key: value for key, value in case.headers.items() if key != NOTICE_KINDS_HEADER}


def row(case):
    return case.store.read(case.agent_id)


def claim(case, observed=None, created="41"):
    return case.store.claim(case.agent_id, hosted_session_id=case.sid, created_agent_version=created,
                            observed_version=observed)


def refusing_submit(case, monkeypatch, body, status=409):
    """Hosted refuses the submit itself (every other operation answers as before)."""
    remote = case.remote

    def route(request):
        if json.loads(request.content)["operation"] == "submit":
            remote.calls.append((request, None))
            return httpx.Response(status, json={"error": body})
        return remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(route))


def no_session_calls(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("a binding row read or write must not call any session")

    for name in ("_session_metadata", "_read_platform_session", "_latest_version_reader", "_metadata_sessions",
                 "_active_session", "_require_native_version"):
        monkeypatch.setattr(behavior_native_sessions, name, forbidden)
    for name in ("invoke_native", "stop_session", "create_session"):
        monkeypatch.setattr(FoundryHttpClient, name, forbidden)
    monkeypatch.setattr("shared_context.behavior_session_files.SessionFiles.__init__", forbidden)


def test_row_reads_and_writes_make_no_session_call(bound, monkeypatch):
    case = bound
    sessions = native_sessions(case.composition)
    client = case.foundry.for_target(case.remote.target)
    no_session_calls(monkeypatch)
    assert case.store.read(case.agent_id) is None
    claimed = sessions.observed_binding(client, version="41")
    assert (claimed.hosted_session_id, claimed.created_agent_version, claimed.observed.agent_version) == (
        case.sid, "41", "41")
    assert claimed.observed.source == "vm" and not claimed.drift and not claimed.notice_pending
    assert sessions.observed_binding(client, version="40").drift
    view = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="follow", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id="fa-native-next", hosted_agent_version="42")
    sessions._switched(client, view)
    assert case.store.read(case.agent_id).hosted_session_id == "fa-native-next"
    assert [kind for kind, _ in case.repository.writes] == ["create", "replace", "replace"]


def test_first_submit_claims_the_current_sid_once(bound):
    case = bound
    assert submit(case).status_code == 202
    claimed = row(case)
    assert (claimed.hosted_session_id, claimed.created_agent_version) == (case.sid, "41")
    assert (claimed.observed.agent_version, claimed.observed.source) == ("41", "vm")
    assert not claimed.drift and not claimed.notice_pending and claimed.previous_sessions == ()
    case.remote.completed("native-rid")
    assert submit(case, rid="native-rid-2", conversation_id="native-cid").status_code == 202
    assert [kind for kind, _ in case.repository.writes] == ["create"]


def test_claim_race_keeps_one_row_and_the_second_reads_it_back(bound):
    case = bound
    case.repository.before_write = lambda: claim(case, observed="39")
    second = claim(case)
    assert second == row(case)
    assert (second.observed.agent_version, second.drift, second.notice_pending) == ("39", True, True)
    assert [kind for kind, _ in case.repository.writes] == ["create"]


def test_cas_conflict_reads_the_row_again_and_recomputes(bound):
    case = bound
    claim(case)
    # Another writer records 40 between this write's read and its replace: the 412 rereads, and 39 is then written
    # on top of 40 without setting the notice bit a second time (the flag was already set by that writer).
    case.repository.before_write = lambda: case.store.record_observed(
        case.agent_id, hosted_session_id=case.sid, agent_version="40", source="vm")
    recorded = case.store.record_observed(case.agent_id, hosted_session_id=case.sid, agent_version="39", source="vm")
    assert recorded == row(case)
    assert (recorded.observed.agent_version, recorded.drift, recorded.notice_pending) == ("39", True, True)
    assert recorded.notice() == NativeEnvironmentNotice(from_version="41", to_version="40")  # the first drift
    assert [payload["observed"]["agent_version"] for _, payload in case.repository.writes] == ["41", "40", "39"]


def test_reported_drift_only_records_and_sets_flag_and_bit_in_one_write(bound, monkeypatch):
    case = bound
    claim(case)
    refusing_submit(case, monkeypatch, {"code": "NATIVE_SESSION_CHANGED", "message": "private"})
    plain = submit(case)
    writes = len(case.repository.writes)
    refusing_submit(case, monkeypatch, {"code": "NATIVE_SESSION_CHANGED", "message": "private", "actual_version": "40"})
    reported = submit(case)
    # The turn result is the same refusal with or without the reported version.
    assert (reported.status_code, reported.json()) == (plain.status_code, plain.json())
    assert len(case.repository.writes) == writes + 1
    kind, payload = case.repository.writes[-1]
    assert kind == "replace" and payload["drift"] is True and payload["notice_pending"] is True
    assert payload["observed"]["agent_version"] == "40" and payload["observed"]["source"] == "vm"


def test_reported_version_on_an_unclaimed_agent_claims_with_the_vm_as_source(bound, monkeypatch):
    case = bound
    refusing_submit(case, monkeypatch, {"code": "NATIVE_SESSION_CHANGED", "message": "private", "actual_version": "40"})
    assert submit(case).status_code == 409
    claimed = row(case)
    assert (claimed.observed.agent_version, claimed.observed.source) == ("40", "vm")
    assert claimed.drift and claimed.notice() == NativeEnvironmentNotice(from_version="41", to_version="40")
    assert [kind for kind, _ in case.repository.writes] == ["create"]


def test_old_image_writes_nothing_before_the_vm_accepts_the_observed_version(bound, monkeypatch):
    case = bound
    claim(case, observed="40")  # recorded actual 40 (metadata at claim); phase 3 expects it (V_rec)
    before = case.repository.data.copy()
    refusing_submit(case, monkeypatch, {"code": "NATIVE_SESSION_CHANGED", "message": "private"})
    refused = submit(case)
    assert refused.status_code == 409
    assert case.repository.data == before and len(case.repository.writes) == 1
    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(case.remote))
    # Phase 3 expects the recorded 40 and the VM accepts it: the row already says so, so no observation is written;
    # the only write is phase 2c's clear of the drift notice this response delivered.
    assert submit(case).status_code == 202
    assert (row(case).observed.agent_version, row(case).observed.source) == ("40", "claim")
    assert [kind for kind, _ in case.repository.writes] == ["create", "replace"]
    assert row(case).notice_pending is False and row(case).drift is True


def test_drift_then_drift_back_keeps_the_flag(bound):
    case = bound
    claim(case, observed="40")
    # Phase 3 expects the recorded 40; the session runs 41, its creation version, again and says so.
    case.remote.vm_version = "41"
    accepted = submit(case)
    assert accepted.status_code == 202
    current = row(case)
    assert current.observed.agent_version == "41"
    # The flag stays; the notice was delivered with this response and cleared after it was written.
    assert current.drift is True and current.notice_pending is False and current.notice_to_version == "40"
    # The notice names the version the drift went to, not the recorded one (which drifted back).
    assert accepted.json()["environment_notice"] == {
        "kind": "platform_version_changed", "from_version": "41", "to_version": "40"}


def test_once_the_flag_is_set_no_new_notice_bit(bound, monkeypatch):
    case = bound
    claim(case, observed="40")
    cleared, etag = case.store._read(case.agent_id)
    case.repository.replace(case.store.partition_key, case.store.row_key(case.agent_id),
                            cleared.model_copy(update={"notice_pending": False}).model_dump(mode="json"), etag)
    refusing_submit(case, monkeypatch, {"code": "NATIVE_SESSION_CHANGED", "message": "private", "actual_version": "39"})
    submit(case)
    current = row(case)
    assert current.observed.agent_version == "39" and current.drift is True and current.notice_pending is False


def test_claim_outside_a_request_sets_the_bit_and_owner_responses_carry_it(bound):
    case = bound
    # E.g. the deploy-time follow read the source metadata (created on 41, the target's version, now 40): no
    # response to carry it. The next submit expects the recorded 40; the VM runs 41 and says so, so 41 is recorded
    # on top (ADR-0009 phase 3: the binding row's V_rec is the expected version).
    claim(case, observed="40")
    case.remote.vm_version = "41"
    accepted = submit(case)
    assert accepted.status_code == 202
    assert accepted.json()["environment_notice"] == {
        "kind": "platform_version_changed", "from_version": "41", "to_version": "40"}
    assert row(case).observed.agent_version == "41" and row(case).notice_to_version == "40"
    # Phase 2c: written, then cleared by CAS; the next owner response carries nothing (once per Agent, M4).
    assert row(case).notice_pending is False and row(case).drift is True
    case.remote.completed("native-rid")
    again = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert again.status_code == 202 and "environment_notice" not in again.json()


def test_without_a_pending_notice_the_receipt_is_unchanged(bound):
    case = bound
    accepted = submit(case)
    assert accepted.status_code == 202 and "environment_notice" not in accepted.json()
    assert row(case) is not None and row(case).notice_pending is False  # claimed, nothing to carry


def test_non_owner_callers_never_carry_or_clear(bound):
    case = bound
    claim(case, observed="40")
    gateway = NativeTurnGateway(case.composition, enabled=True)
    # A group start (or any other caller of the shared submit) is not the owner's own response.
    receipt, _ = gateway.submit(case.c.human, InstructionSubmit(request_id="group-rid", content="Group question"))
    assert isinstance(receipt, NativeReceipt) and receipt.environment_notice is None
    assert row(case).notice_pending is True
    # The same row is carried by the owner's own response.
    owner, _ = gateway.submit(case.c.human, InstructionSubmit(request_id="owner-rid", content="Owner text"),
                              owner_notice=True)
    assert owner.environment_notice == NativeEnvironmentNotice(from_version="41", to_version="40")


def test_follow_commit_moves_the_row_to_the_new_sid(bound, monkeypatch):
    case = bound
    source, calls, sessions, view = environment_at(case, monkeypatch)
    claim(case, observed="40")
    command = {"request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42"}
    ready = case.client.post(PREFIX + "/environment", json=command, headers=case.headers)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    moved = row(case)
    assert moved.hosted_session_id == ready.json()["hosted_session_id"] != source.session_id
    assert moved.created_agent_version == "42" == ready.json()["hosted_agent_version"]
    assert moved.drift is False and moved.notice_pending is False
    assert [(item.hosted_session_id, item.agent_version) for item in moved.previous_sessions] == [
        (source.session_id, "41")]
    assert moved.last_switch.previous_hosted_session_id == source.session_id
    # This fixture's image has no workspace files (files_migrated absent): no file moved, so the honest §8 sentence
    # is the without-files one; the old SID's drift notice left with the SID. The other kinds are covered below.
    assert "files_migrated" not in ready.json() and moved.switch_notice == "without_files" and not moved.notice_pending


def test_rerun_after_a_crash_between_commit_and_row_write_writes_the_same_switch(bound, monkeypatch):
    case = bound
    source, calls, sessions, view = environment_at(case, monkeypatch)
    claim(case)
    command = {"request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42"}
    case.repository.fail_writes = 1  # the commit lands; the row write after it is lost
    first = case.client.post(PREFIX + "/environment", json=command, headers=case.headers)
    assert first.status_code == 200 and first.json()["state"] == "ready", first.text
    assert row(case).hosted_session_id == source.session_id
    case.composition._native_sessions = NativeSessions(case.composition)
    rerun = case.client.post(PREFIX + "/environment", json=command, headers=case.headers)
    assert rerun.status_code == 200 and rerun.json() == first.json()
    assert sum(action == "create" for action, _ in calls) == 1
    repaired = row(case)
    assert repaired.hosted_session_id == first.json()["hosted_session_id"] and repaired.created_agent_version == "42"
    assert [item.hosted_session_id for item in repaired.previous_sessions] == [source.session_id]
    writes = len(case.repository.writes)
    again = case.client.post(PREFIX + "/environment", json=command, headers=case.headers)
    assert again.json() == first.json() and len(case.repository.writes) == writes  # idempotent


def test_rerun_on_a_newer_selector_keeps_both_previous_sids(bound):
    case = bound
    claim(case)
    first = native_session_id(case.remote.target, "unused", version="42")
    # The 41→42 commit's row write was lost; the rerun is a new follow 42→43 whose previous SID is 42's.
    view = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="follow-43", state="ready", previous_hosted_session_id=first,
        previous_agent_version="42", hosted_session_id="fa-native-43", hosted_agent_version="43")
    moved = case.store.record_switch(case.agent_id, view)
    assert (moved.hosted_session_id, moved.created_agent_version) == ("fa-native-43", "43")
    assert [(item.hosted_session_id, item.agent_version) for item in moved.previous_sessions] == [
        (case.sid, "41"), (first, "42")]


class DeclaredProfiles:
    """The ``HostedVersions.profile`` part of the version resolver: what each version's definition declares."""

    def __init__(self, **declared):
        self.declared, self.reads = declared, []

    def profile(self, target, version):
        self.reads.append(version)
        value = self.declared[version]
        if isinstance(value, Exception):
            raise value
        return value


def declared(case, monkeypatch, **profiles):
    resolver = DeclaredProfiles(**profiles)
    monkeypatch.setattr(case.composition.runtime.agent_message_composition, "hosted_versions", resolver, raising=False)
    return resolver


def test_claim_and_switch_rows_record_the_profile_each_version_declares(bound, monkeypatch):
    """ADR-0009 2d-i: 2d-ii supports only v2, so a row never records the API's own v1 for a v2 session."""
    case = bound
    declared(case, monkeypatch, **{"41": "foundry-native-v2", "42": "foundry-native-v2"})
    sessions = native_sessions(case.composition)
    client = case.foundry.for_target(case.remote.target)
    assert sessions.observed_binding(client, version="41").state_profile == "foundry-native-v2"
    view = NativeEnvironmentView(
        agent_id=case.agent_id, request_id="follow", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id="fa-native-next", hosted_agent_version="42")
    sessions._switched(client, view)
    moved = row(case)
    assert (moved.hosted_session_id, moved.state_profile) == ("fa-native-next", "foundry-native-v2")
    assert moved.model_dump(mode="json")["state_profile"] == "foundry-native-v2" and moved.supported()


def test_v1_definitions_write_the_same_row_bytes_as_before_2d_i(bound, monkeypatch):
    """Claim, drift and switch with v1-declaring versions: every Table write equals the digest taken on 8bf86af0."""
    case = bound
    declared(case, monkeypatch, **{"41": "foundry-native-v1", "42": "foundry-native-v1"})
    sessions = native_sessions(case.composition)
    client = case.foundry.for_target(case.remote.target)
    sessions.observed_binding(client, version="41")
    sessions.observed_binding(client, version="40")
    sessions._switched(client, NativeEnvironmentView(
        agent_id=case.agent_id, request_id="follow", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id="fa-native-next", hosted_agent_version="42"))
    written = b"\n".join(json.dumps(payload, sort_keys=True).encode() for _, payload in case.repository.writes)
    assert len(case.repository.writes) == 3
    assert hashlib.sha256(written).hexdigest() == "430a603e1d76bfd0604f931dfbf0420e1ef2f486337cd3d66f74b8e6877eeb0e"


def test_an_unreadable_definition_writes_no_claim_rather_than_a_wrong_profile(bound, monkeypatch, caplog):
    case = bound
    declared(case, monkeypatch, **{"41": Problem(503, "AGENT_VERSION_FOLLOWING", "unreadable")})
    client = case.foundry.for_target(case.remote.target)
    assert native_sessions(case.composition).observed_binding(client, version="41") is None
    assert row(case) is None and case.repository.writes == []
    assert "NATIVE_BINDING_UNRECORDED operation=observe code=AGENT_VERSION_FOLLOWING" in caplog.text


def test_an_existing_row_records_a_drift_without_reading_any_definition(bound, monkeypatch):
    """The bound drift path already read V_obs's definition (``_version_supported``): a claim reads the profile only
    when it inserts, so an existing row costs no ``get_version``, and a failing read can not drop the drift."""
    case = bound
    claim(case)
    unreadable = Problem(503, "AGENT_VERSION_FOLLOWING", "unreadable")
    resolver = declared(case, monkeypatch, **{"40": unreadable, "41": unreadable})
    moved = case.foundry.for_target(case.remote.target.model_copy(update={"agent_version": "40"}))
    recorded = native_sessions(case.composition).observed_binding(moved, version="40")
    assert recorded is not None and recorded == row(case)
    assert (recorded.observed.agent_version, recorded.drift, recorded.notice_pending) == ("40", True, True)
    assert resolver.reads == []


def test_an_abandon_decision_that_claims_the_row_records_the_sources_declared_profile(bound, monkeypatch):
    case = bound
    declared(case, monkeypatch, **{"41": "foundry-native-v0"})
    monkeypatch.setattr(behavior_native_sessions, "_session_metadata", lambda _client: SimpleNamespace(status="idle"))
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", lambda _client, **_: "foundry-native-v0")
    sessions = native_sessions(case.composition)
    source = case.foundry.for_target(case.remote.target)
    decided = sessions._abandon_decision(case.store, None, source, waiting=False)
    assert (decided.basis, decided.source_status) == ("incompatible", "idle")
    assert row(case).state_profile == "foundry-native-v0" and not row(case).supported()


@pytest.mark.parametrize("source_profile", [None, Problem(503, "AGENT_VERSION_FOLLOWING", "unreadable")])
def test_an_incompatible_source_without_a_known_profile_is_never_recorded_as_supported(bound, monkeypatch,
                                                                                       source_profile):
    """An enforcing guard in another process reads only the row: a v1 fallback would let it call the SID again."""
    case = bound
    declared(case, monkeypatch, **{"41": source_profile})
    monkeypatch.setattr(behavior_native_sessions, "_session_metadata", lambda _client: SimpleNamespace(status="idle"))
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", lambda _client, **_: None)
    source = case.foundry.for_target(case.remote.target)
    decided = native_sessions(case.composition)._abandon_decision(case.store, None, source, waiting=False)
    assert decided.basis == "incompatible"
    assert row(case).state_profile == native_binding.INCOMPATIBLE_STATE_PROFILE and not row(case).supported()


def test_follow_source_status_read_claims_with_the_version_it_shows(bound, monkeypatch):
    from shared_context.behavior_session_files import FollowFiles

    case = bound
    source, calls, sessions, view = environment_at(case, monkeypatch)
    # Only the status read before the stop is under test; the file transfer's own version guard is not.
    monkeypatch.setattr(FollowFiles, "require_source_version", lambda self, **_: None)

    def get_session(name, sid):
        calls.append(("status", sid))
        drifted = sid == source.session_id
        return AgentSessionResource({
            "agent_session_id": sid, "status": "creating" if drifted else "active",
            "created_at": int(NOW.timestamp()),
            "version_indicator": {"type": "version_ref", "agent_version": "40" if drifted else sessions[sid].agent_version}})

    project_client(monkeypatch, get_session=get_session,
                   list_sessions=lambda name, **_: iter(get_session(name, sid) for sid in sessions))
    pending = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42"})
    assert pending.status_code == 200 and pending.json()["reason"] == "source_active", pending.text
    claimed = row(case)
    assert (claimed.hosted_session_id, claimed.created_agent_version, claimed.observed.agent_version) == (
        source.session_id, "41", "40")
    assert claimed.drift and claimed.notice_pending and claimed.created_at == NOW.isoformat()


def test_a_failed_row_write_never_changes_the_turn(bound, caplog):
    case = bound
    case.repository.fail_writes = 5
    accepted = submit(case)
    assert accepted.status_code == 202 and row(case) is None
    assert NativeReceipt.model_validate_json(accepted.content).request_id == "native-rid"
    assert any("NATIVE_BINDING_UNRECORDED operation=observe code=TABLE_UNAVAILABLE" in record.getMessage()
               for record in caplog.records)


def test_a_failed_row_read_never_changes_the_turn(bound, caplog):
    case = bound
    claim(case, observed="40")
    case.repository.fail_reads = 1  # the locating read before the send (ADR-0009 phase 3)
    plain = submit(case)
    assert plain.status_code == 202 and case.repository.fail_reads == 0, plain.text
    receipt = NativeReceipt.model_validate_json(plain.content)
    # The legacy locator stood in (never a refusal); the row read after the answer still carries the notice, which
    # the route clears once the response is written (phase 2c).
    assert receipt.request_id == "native-rid" and receipt.environment_notice is not None
    assert row(case).notice_pending is False
    assert any("NATIVE_BINDING_UNRECORDED operation=locate code=TABLE_UNAVAILABLE" in record.getMessage()
               for record in caplog.records)
    case.repository.fail_reads, case.repository.fail_writes = 1, 1
    case.remote.completed("native-rid")
    again = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert again.status_code == 202, again.text


def test_responses_submit_route_carries_the_notice_to_the_owner(bound, responses_api):
    case = bound
    claim(case, observed="40")
    body = InstructionSubmit(request_id="responses-rid", content="Original user text").model_dump(mode="json")
    accepted = case.client.post("/work-policy/v1/responses-turns/submit", json=body, headers=case.headers)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["environment_notice"] == {
        "kind": "platform_version_changed", "from_version": "41", "to_version": "40"}


@pytest.mark.parametrize("change", [
    {"drift": False, "notice_to_version": None},  # a pending notice without the drift flag
    {"notice_to_version": None},  # the flag without the version it was set by
    {"drift": False, "notice_pending": False},  # a notice version without the flag
    {"notice_to_version": "41"},  # the "drift" version equals the creation version
    {"previous_sessions": [{"hosted_session_id": "SID", "agent_version": "39"}]},  # the current SID as a previous one
    {"previous_sessions": [{"hosted_session_id": "old", "agent_version": "39"},
                           {"hosted_session_id": "old", "agent_version": "38"}]},  # a previous SID twice
])
def test_a_stored_row_breaking_the_drift_or_previous_sid_rules_reads_back_invalid(bound, change):
    case = bound
    payload = claim(case, observed="40").model_dump(mode="json")
    assert (payload["drift"], payload["notice_pending"], payload["notice_to_version"]) == (True, True, "40")
    changed = {**payload, **change}
    for item in changed["previous_sessions"]:
        if item["hosted_session_id"] == "SID":
            item["hosted_session_id"] = case.sid
    case.repository.data.clear()
    case.repository.create(case.store.partition_key, case.store.row_key(case.agent_id), changed)
    with pytest.raises(Problem) as invalid:
        case.store.read(case.agent_id)
    assert (invalid.value.status, invalid.value.body["error"]["code"]) == (409, "NATIVE_BINDING_INVALID")


@pytest.mark.parametrize("status,code,reason", [
    (409, "NATIVE_SESSION_CHANGED", "version"), (403, "NATIVE_TURNS_DISABLED", "profile")])
def test_a_newer_images_refusal_before_dispatch_records_the_vm_version_and_is_never_a_refusal(
        bound, monkeypatch, status, code, reason):
    """ADR-0009 phase 3 consumes what phase 2a added (``not_dispatched``, ``reason``, ``actual_version``): the VM's
    version goes into the binding row (``vm``), and the owner never sees a version refusal. The version refusal is
    resent once with the same RID at that version; the profile refusal is a same-RID resend by the client
    (``server_following``) after the retained follow (no in-request switch before 2d-i)."""
    case = bound
    claim(case)
    monkeypatch.setattr(NativeSessions, "advance", lambda self, client, command, **_: NativeEnvironmentView(
        agent_id=case.agent_id, request_id=command.request_id, state="pending", reason="source_active",
        previous_hosted_session_id=case.sid, previous_agent_version="41", hosted_session_id="fa-native-next",
        hosted_agent_version="41"))
    refusal = before_dispatch(Problem(status, code, "private"), reason, "40")
    refusing_submit(case, monkeypatch, refusal.body["error"], status=status)
    reported = submit(case)
    recorded = row(case)
    assert (recorded.observed.agent_version, recorded.observed.source) == ("40", "vm")
    assert recorded.drift and recorded.notice() == NativeEnvironmentNotice(from_version="41", to_version="40")
    error = reported.json()["error"]
    # This double refuses every submit, so the one resend at 40 is refused too and the client takes over.
    assert (reported.status_code, error["code"]) == (
        (409, "NATIVE_SESSION_CHANGED") if status == 409 else (503, "AGENT_VERSION_FOLLOWING")), reported.text
    assert error["not_dispatched"] is True and error["server_following"] is True
    assert "actual_version" not in error and "reason" not in error
    submits = [request.headers["x-client-fa-expected-version"] for request, command in case.remote.calls
               if command is None]
    assert submits == (["41", "40"] if status == 409 else ["41"])


def test_store_partition_is_not_the_authority_inventory(bound):
    case = bound
    assert case.store.partition_key.startswith("native-binding:")
    assert case.store.partition_key != case.composition.config.table.scope.partition_key
    assert isinstance(case.store, NativeBindingStore)


def switch_view(case, *, files_migrated, to="fa-native-next", version="42", previous=None):
    return NativeEnvironmentView(
        agent_id=case.agent_id, request_id="follow-switch", state="ready",
        previous_hosted_session_id=previous or case.sid, previous_agent_version="41", hosted_session_id=to,
        hosted_agent_version=version, **({"files_migrated": True, "files_manifest_sha256": "a" * 64}
                                         if files_migrated is True else
                                         {"files_migrated": False, "files_skip_request_id": "skip"}
                                         if files_migrated is False else {}))


@pytest.mark.parametrize(("files_migrated", "abandon", "kind"), [
    (True, None, "environment_updated"),
    (False, None, "environment_switched_without_files"),
    (False, "idle", "environment_switched_without_files"),
    (False, "deleted", "environment_source_retired"),
    (False, "missing", "environment_source_retired"),
])
def test_a_switch_sets_its_section_8_notice_in_the_slot(bound, files_migrated, abandon, kind):
    case = bound
    claim(case, observed="40")  # a drift notice is pending on the old SID; the switch clears it with the SID
    if abandon is not None:
        case.store.decide_abandon(case.agent_id, hosted_session_id=case.sid, basis="incompatible",
                                  source_status=abandon, created_agent_version="41")
    moved = case.store.record_switch(case.agent_id, switch_view(case, files_migrated=files_migrated))
    assert moved.notice() == NativeEnvironmentNotice(kind=kind)
    assert moved.drift is False and moved.notice_pending is False


def test_a_switch_without_the_files_feature_says_no_files_were_moved(bound):
    case = bound
    claim(case)
    moved = case.store.record_switch(case.agent_id, switch_view(case, files_migrated=None))
    assert moved.notice() == NativeEnvironmentNotice(kind="environment_switched_without_files")


def test_an_undelivered_no_files_notice_is_never_hidden_by_a_later_update(bound):
    case = bound
    claim(case)
    case.store.record_switch(case.agent_id, switch_view(case, files_migrated=False))
    later = switch_view(case, files_migrated=True, to="fa-native-43", version="43", previous="fa-native-next")
    moved = case.store.record_switch(case.agent_id, later)
    assert moved.notice() == NativeEnvironmentNotice(kind="environment_switched_without_files")


def test_drift_after_a_switch_never_replaces_its_undelivered_notice(bound):
    case = bound
    claim(case)
    case.store.record_switch(case.agent_id, switch_view(case, files_migrated=False))
    drifted = case.store.record_observed(case.agent_id, hosted_session_id="fa-native-next", agent_version="40",
                                         source="vm")
    assert drifted.drift and drifted.notice_pending  # recorded as before
    assert drifted.notice() == NativeEnvironmentNotice(kind="environment_switched_without_files")
    cleared = case.store.clear_notice(case.agent_id, drifted.notice())
    assert cleared.notice() is None and cleared.drift  # the more severe notice covered the drift one


def test_owner_submit_carries_the_switch_notice_once_and_clears_it_after_writing(bound):
    case = bound
    # Phase 3 locates by this row: a switch (no claim before it) made the fixture's SID the current one on the
    # selected version 41.
    case.store.record_switch(case.agent_id, switch_view(case, files_migrated=False, to=case.sid, version="41",
                                                        previous="older"))
    accepted = submit(case)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["environment_notice"] == {"kind": "environment_switched_without_files"}
    assert row(case).switch_notice is None
    case.remote.completed("native-rid")
    again = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert again.status_code == 202 and "environment_notice" not in again.json()


def asgi_submit(case, events, send_fails=False):
    """POST /submit straight through the ASGI app; ``events`` records each message sent, in time order."""
    body = json.dumps(InstructionSubmit(request_id="native-rid", content="Original user text").model_dump(
        mode="json")).encode()
    headers = [(key.lower().encode(), value.encode()) for key, value in {
        **case.headers, "content-type": "application/json", "content-length": str(len(body))}.items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": PREFIX + "/submit", "raw_path": (PREFIX + "/submit").encode(), "query_string": b"",
             "root_path": "", "headers": headers, "client": ("testclient", 50000), "server": ("testserver", 80)}
    chunks = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message):
        if send_fails and message["type"] == "http.response.body":
            raise OSError("synthetic: the client went away before the body was written")
        events.append(message["type"])

    async def run():
        try:
            await case.client.app(scope, receive, send)
        except OSError:
            events.append("send-failed")

    with case.client._portal_factory() as portal:  # the fixture's own loop: no new event loop or socket
        portal.call(run)


def test_the_notice_is_cleared_only_after_the_response_body_was_written(bound, monkeypatch):
    case = bound
    claim(case, observed="40")
    original = NativeBindingStore.clear_notice
    events = []

    def clearing(store, agent_id, delivered):
        events.append("clear")
        return original(store, agent_id, delivered)

    monkeypatch.setattr(NativeBindingStore, "clear_notice", clearing)
    asgi_submit(case, events)  # one list: the clear is recorded where it happened relative to the sends
    assert events == ["http.response.start", "http.response.body", "clear"]
    assert row(case).notice_pending is False


def test_a_response_never_written_leaves_the_notice_pending(bound, monkeypatch):
    case = bound
    claim(case, observed="40")
    monkeypatch.setattr(NativeBindingStore, "clear_notice",
                        lambda *_a, **_k: pytest.fail("cleared before the response body was written"))
    events = []
    asgi_submit(case, events, send_fails=True)
    assert events == ["http.response.start", "send-failed"]
    assert row(case).notice_pending is True  # the next owner response shows it again (at least once)


@pytest.mark.parametrize(("meanwhile", "cleared"), [
    ({}, True),  # another writer touched the row (e.g. an observation), same notice: reread and clear again
    ({"switch_notice": "without_files"}, False),  # a newer, more severe notice was never shown: left alone
])
def test_a_clear_conflict_rereads_and_clears_only_the_delivered_notice(bound, meanwhile, cleared):
    case = bound
    delivered = claim(case, observed="40").notice()

    def other_writer():
        current, etag = case.store._read(case.agent_id)
        changed = current.model_copy(update={"created_at": NOW.isoformat(), **meanwhile})
        case.repository.replace(case.store.partition_key, case.store.row_key(case.agent_id),
                                changed.model_dump(mode="json"), etag)

    case.repository.before_write = other_writer
    result = case.store.clear_notice(case.agent_id, delivered)
    assert (result.notice() is None) is cleared
    assert row(case).created_at == NOW.isoformat()  # the other write was kept, never overwritten
    if not cleared:
        assert row(case).notice() == NativeEnvironmentNotice(kind="environment_switched_without_files")


def test_a_failed_clear_keeps_the_notice_for_the_next_owner_response(bound, caplog):
    case = bound
    claim(case, observed="40")
    original = case.repository.replace
    failed = []

    def replace(team, row_key, payload, etag):
        if not failed and payload.get("notice_pending") is False and payload.get("drift") is True:
            failed.append(True)
            raise Problem(503, "TABLE_UNAVAILABLE", "synthetic Table outage")
        return original(team, row_key, payload, etag)

    case.repository.replace = replace
    first = submit(case)
    assert first.status_code == 202 and first.json()["environment_notice"]["to_version"] == "40"
    assert failed and row(case).notice_pending is True
    assert any("NATIVE_BINDING_UNRECORDED operation=clear code=TABLE_UNAVAILABLE" in record.getMessage()
               for record in caplog.records)
    case.remote.completed("native-rid")
    again = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert again.json()["environment_notice"]["to_version"] == "40"  # shown again: at least once
    assert row(case).notice_pending is False


def test_non_owner_reads_and_reconcile_never_clear(bound):
    case = bound
    claim(case, observed="40")
    gateway = NativeTurnGateway(case.composition, enabled=True)
    gateway.submit(case.c.human, InstructionSubmit(request_id="group-rid", content="Group question"))
    # Even an owner_notice=True call outside the owner's HTTP route never clears: nothing was written yet.
    gateway.submit(case.c.human, InstructionSubmit(request_id="owner-rid", content="Owner text"), owner_notice=True)
    read = case.client.get(PREFIX + "/requests/owner-rid", headers=case.headers)
    assert read.status_code == 200 and "environment_notice" not in read.json()
    assert row(case).notice_pending is True


def test_an_old_bridge_gets_todays_drift_notice_only_and_nothing_is_cleared(bound):
    """A bridge without the notice-kinds header strictly decodes only the kinds it knew: no switch kind reaches it,
    so it never answers 502, and nothing is cleared it could not show; the drift notice is carried as before 2c."""
    case = bound
    claim(case, observed="40")  # the drift notice is pending
    current, etag = case.store._read(case.agent_id)
    case.repository.replace(case.store.partition_key, case.store.row_key(case.agent_id),
                            current.model_copy(update={"switch_notice": "without_files"}).model_dump(mode="json"),
                            etag)
    old_bridge(case)
    first = submit(case)
    assert first.status_code == 202, first.text
    assert first.json()["environment_notice"] == {
        "kind": "platform_version_changed", "from_version": "41", "to_version": "40"}
    assert row(case).switch_notice == "without_files" and row(case).notice_pending is True
    case.remote.completed("native-rid")
    case.headers = {**case.headers, NOTICE_KINDS_HEADER: NOTICE_KINDS_VERSION}  # the console is updated
    again = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert again.json()["environment_notice"] == {"kind": "environment_switched_without_files"}
    assert row(case).switch_notice is None and row(case).notice_pending is False


def test_an_old_bridge_with_only_a_switch_notice_pending_gets_no_notice(bound):
    case = bound
    # The switch that created this row moved the Agent to the target's session and version: no drift notice.
    case.store.record_switch(case.agent_id, switch_view(case, files_migrated=False, to=case.sid, previous="older",
                                                        version="41"))
    old_bridge(case)
    accepted = submit(case)
    assert accepted.status_code == 202 and "environment_notice" not in accepted.json()
    assert row(case).switch_notice == "without_files"


def test_clearing_a_lost_fence_never_clears_the_newer_one_that_replaced_it():
    """#858 review P3: ``record_upgrade(upgrade=None, lost=...)`` clears only that lost fence."""
    from shared_context.native_binding import NativeBindingUpgrade

    store = NativeBindingStore(InMemoryRepository(), tenant_id="tenant", team_id="team", clock=lambda: NOW)
    store.claim("agent", hosted_session_id="fa-native-source", created_agent_version="41")

    def fence(sid, rid):
        return NativeBindingUpgrade(hosted_session_id=sid, agent_version="42", request_id=rid,
                                    started_at=NOW.isoformat())

    lost, newer = fence("fa-native-lost", "rid-lost"), fence("fa-native-newer", "rid-newer")
    store.record_upgrade("agent", hosted_session_id="fa-native-source", upgrade=newer)
    store.record_upgrade("agent", hosted_session_id="fa-native-source", upgrade=None, lost=lost)
    assert store.read("agent").upgrade == newer
    store.record_upgrade("agent", hosted_session_id="fa-native-source", upgrade=None, lost=newer)
    assert store.read("agent").upgrade is None
