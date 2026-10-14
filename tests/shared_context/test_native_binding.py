"""ADR-0009 phase 1: the API binding row records where an Agent's session is and which version it actually ran.

Recording only: no turn result changes, no session is called for the row, and the owner response carries a pending
drift notice that is not cleared until the phase 2 display exists. Platform I/O and Hosted are offline doubles."""

import json
from datetime import datetime, timezone

import httpx
import pytest
from azure.ai.projects.models import AgentSessionResource

from conftest import InMemoryRepository
from shared_context import behavior_native_sessions, native_binding
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_native_sessions import NativeSessions, native_session_id, native_sessions
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import NativeEnvironmentNotice, NativeEnvironmentView, NativeReceipt
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
    return case


def row(case):
    return case.store.read(case.agent_id)


def claim(case, observed=None, created="41"):
    return case.store.claim(case.agent_id, hosted_session_id=case.sid, created_agent_version=created,
                            observed_version=observed)


def refusing_submit(case, monkeypatch, body):
    """Hosted refuses the submit itself (every other operation answers as before)."""
    remote = case.remote

    def route(request):
        if json.loads(request.content)["operation"] == "submit":
            remote.calls.append((request, None))
            return httpx.Response(409, json={"error": body})
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
    claim(case, observed="40")  # recorded actual 40 (metadata at claim); the selector-derived reader expects 41
    before = case.repository.data.copy()
    refusing_submit(case, monkeypatch, {"code": "NATIVE_SESSION_CHANGED", "message": "private"})
    refused = submit(case)
    assert refused.status_code == 409
    assert case.repository.data == before and len(case.repository.writes) == 1
    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(case.remote))
    assert submit(case).status_code == 202  # the VM accepted expected=41: now it is a fact
    assert (row(case).observed.agent_version, row(case).observed.source) == ("41", "vm")


def test_drift_then_drift_back_keeps_the_flag(bound):
    case = bound
    claim(case, observed="40")
    accepted = submit(case)  # the session answers as 41, its creation version, again
    assert accepted.status_code == 202
    current = row(case)
    assert current.observed.agent_version == "41"
    assert current.drift is True and current.notice_pending is True
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
    # response to carry it. The next submit expects 41 and the VM accepts it, so 41 is recorded on top.
    claim(case, observed="40")
    accepted = submit(case)
    assert accepted.status_code == 202
    assert accepted.json()["environment_notice"] == {
        "kind": "platform_version_changed", "from_version": "41", "to_version": "40"}
    assert row(case).observed.agent_version == "41" and row(case).notice_to_version == "40"
    # Nothing clears the bit before the phase 2 display, so the next owner response carries it again.
    assert row(case).notice_pending is True
    case.remote.completed("native-rid")
    again = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert again.json()["environment_notice"]["to_version"] == "40" and row(case).notice_pending is True


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
    case.repository.fail_reads = 1  # the one read this submit makes
    plain = submit(case)
    assert plain.status_code == 202 and case.repository.fail_reads == 0, plain.text
    receipt = NativeReceipt.model_validate_json(plain.content)
    assert receipt.request_id == "native-rid" and receipt.environment_notice is None  # unread, so not carried
    assert row(case).notice_pending is True
    assert any("NATIVE_BINDING_UNRECORDED operation=observe code=TABLE_UNAVAILABLE" in record.getMessage()
               for record in caplog.records)


def test_responses_submit_route_carries_the_notice_to_the_owner(bound, responses_api):
    case = bound
    claim(case, observed="40")
    body = InstructionSubmit(request_id="responses-rid", content="Original user text").model_dump(mode="json")
    accepted = case.client.post("/work-policy/v1/responses-turns/submit", json=body, headers=case.headers)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["environment_notice"] == {
        "kind": "platform_version_changed", "from_version": "41", "to_version": "40"}


def test_store_partition_is_not_the_authority_inventory(bound):
    case = bound
    assert case.store.partition_key.startswith("native-binding:")
    assert case.store.partition_key != case.composition.config.table.scope.partition_key
    assert isinstance(case.store, NativeBindingStore)
