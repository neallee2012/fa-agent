"""What ``NativeSessions.without_files`` sends to Hosted, in which order, and which refusal answers first.

A session whose Hosted, platform and follow are scripted: the trace is the permission re-checks (``before_send``), the
commands Hosted receives and the follow that resumes the original request, which is what a resend or a lost answer
depends on.
"""

from types import SimpleNamespace

import pytest

from shared_context import behavior_native_sessions
from shared_context.behavior_foundry import FoundryTarget
from shared_context.behavior_native_sessions import NativeSessions
from shared_context.behavior_native_turn_api import _OmissionBeforeDispatch
from shared_context.behavior_native_turn_wire import (
    NativeEnvironmentCommand, NativeEnvironmentReplacementPreview, NativeEnvironmentView, NativeEnvironmentWithoutFiles,
    NativeFilesCheckDetail, NativeSessionState, material_digest,
)
from shared_context.errors import Problem
from shared_context.native_files_wire import FILES_PROFILE

AGENT = "11111111-1111-1111-1111-111111111111"
OID = "22222222-2222-2222-2222-222222222222"
CONFIRMATION, ORIGINAL = "confirm-1", "follow-1"
SOURCE, DESTINATION, LATER = "session-6", "session-7", "session-8"
OMISSION_PROFILE = "fa-files-omission-v1"
BEFORE = ("before_send",)


def dispatched(stage, missing=None):
    return ("hosted", stage, missing)


class Client:
    def __init__(self, target):
        self.target = target

    def for_target(self, target):
        return Client(target)


class Scripted(NativeSessions):
    def __init__(self, answers, state):
        super().__init__(SimpleNamespace())
        self.answers, self.state, self.trace, self.sent, self.selected = answers, state, [], [], "7"

    def selected_version(self, _client):
        return self.selected

    def candidate(self, client, *, version=None, row=None, located=False):
        session = f"session-{version}"
        return client.for_target(client.target.model_copy(update={"session_id": session, "agent_version": version}))

    def _destination(self, client, expected_version, _before_send, *, mark=True, row=None):
        self.trace.append(("destination", expected_version))
        return self.candidate(client, version=expected_version)

    def _state(self, _client, *, environment_request_id=None):
        return self.state

    def _binding_row(self, _agent_id):
        return None

    def _follow_invoke(self, _destination, command):
        self.trace.append(dispatched(command.stage, command.missing))
        self.sent.append(command)
        answer = self.answers[command.stage]
        answer = answer.pop(0) if isinstance(answer, list) else answer
        if isinstance(answer, Exception):
            raise answer
        return answer, None

    def follow(self, _client, command, *, before_send):
        self.trace.append(("follow", command.request_id, command.expected_session_id, command.expected_agent_version))
        return "followed"


def view(**fields):
    return NativeEnvironmentView(**{
        "agent_id": AGENT, "request_id": ORIGINAL, "previous_hosted_session_id": SOURCE, "previous_agent_version": "6",
        "hosted_session_id": DESTINATION, "hosted_agent_version": "7", "state": "pending",
        "previous_files_profile": FILES_PROFILE, "files_profile": FILES_PROFILE, **fields})


def session_state(**fields):
    return NativeSessionState(**{
        "agent_id": AGENT, "hosted_session_id": SOURCE, "etag": "etag-1", "files_omission_profile": OMISSION_PROFILE,
        "files_profile": FILES_PROFILE, "environment": view(), **fields})


ACTIVE = view(state="ready", files_migrated=False, files_skip_request_id=CONFIRMATION)
PLAIN = view()
SAVED_SOURCE = view(files_confirmation_saved=CONFIRMATION, files_confirmation_reason="source")
SAVED_DESTINATION = view(files_confirmation_saved=CONFIRMATION, files_confirmation_reason="destination")
ABSENT = view(files_confirmation_absent=CONFIRMATION)


def metadata(version="6", status="active"):
    return SimpleNamespace(status=status, version_indicator=SimpleNamespace(agent_version=version))


@pytest.fixture
def platform(monkeypatch):
    sessions = {SOURCE: metadata(), DESTINATION: metadata("7"), LATER: metadata("8")}
    monkeypatch.setattr(behavior_native_sessions, "_session_metadata",
                        lambda client, **_options: sessions.get(client.target.session_id))
    return sessions


def client():
    return Client(FoundryTarget(executor_id="executor", project_endpoint="https://offline.services.ai.azure.com/api/projects/p",
                                agent_id=AGENT, agent_name="agent", agent_version="7", session_id=DESTINATION,
                                agent_oid=OID))


def command(*, target=None, session=SOURCE):
    return NativeEnvironmentWithoutFiles(
        request_id=CONFIRMATION, confirmation="do-not-transfer-workspace-files", expected_agent_version=target,
        follow=NativeEnvironmentCommand(request_id=ORIGINAL, expected_agent_version="7", expected_session_id=session,
                                        idle_only=True))


def run(sessions, order=None, **options):
    return sessions.without_files(client(), order or command(), before_send=lambda: sessions.trace.append(BEFORE), **options)


def refusal(sessions, order=None, **options):
    with pytest.raises(Problem) as caught:
        run(sessions, order, **options)
    error = caught.value.body["error"]
    return type(caught.value), caught.value.status, error["code"], error["message"]


def no_helpers():
    """Destination reports no file helper, so the owner may confirm ``destination``."""
    return session_state(files_profile=None)


def test_a_confirmation_checks_read_only_then_confirms_then_resumes_the_original_follow(platform):
    sessions = Scripted({"check": PLAIN, "confirm": ACTIVE}, no_helpers())
    assert run(sessions) == "followed"
    assert sessions.trace == [
        ("destination", "7"), BEFORE, dispatched("check", "destination"), BEFORE, dispatched("confirm", "destination"),
        ("follow", ORIGINAL, SOURCE, "7")]


def test_the_confirmation_names_the_original_request_on_every_hosted_command(platform):
    sessions = Scripted({"check": PLAIN, "confirm": ACTIVE}, no_helpers())
    run(sessions)
    assert [(sent.request_id, sent.follow.request_id, sent.follow.expected_session_id, sent.follow.expected_etag,
             sent.follow.idle_only, sent.follow.files_required) for sent in sessions.sent] == [
        (CONFIRMATION, ORIGINAL, SOURCE, "etag-1", True, True)] * 2
    assert {sent.confirmation_digest for sent in sessions.sent} == {material_digest(command())}
    assert {sent.decision_basis for sent in sessions.sent} == {None}


def test_a_system_decision_carries_its_basis_instead_of_an_owner_digest(platform):
    sessions = Scripted({"check": PLAIN, "confirm": ACTIVE}, session_state())
    run(sessions, system_policy=True, refusal_code="NATIVE_FILES_TIMEOUT")
    assert [(sent.missing, sent.decision_basis, sent.confirmation_digest) for sent in sessions.sent] == [
        ("source_not_migratable", "system_policy", None)] * 2


@pytest.mark.parametrize(("answer", "code", "before"), [
    (Problem(409, "NATIVE_FILES_CONFLICT", "x"), "NATIVE_FILES_CONFLICT", 1),
    (Problem(409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "x"), "NATIVE_FILES_CONFIRMATION_REQUIRED", 1),
    (Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "x"), "NATIVE_ENVIRONMENT_CONFLICT", 1),
], ids=["conflict", "confirmation", "environment"])
def test_a_refused_check_reports_that_nothing_was_dispatched(platform, answer, code, before):
    sessions = Scripted({"check": answer}, no_helpers())
    with pytest.raises(_OmissionBeforeDispatch) as caught:
        run(sessions)
    assert caught.value.request_id == CONFIRMATION
    assert caught.value.status == 409 and caught.value.body["error"]["code"] == code
    assert caught.value.body["error"]["message"] == "the read-only check refused; no omission confirmation was dispatched"
    assert sessions.trace.count(BEFORE) == before and dispatched("confirm", "destination") not in sessions.trace


def test_a_refused_check_keeps_its_files_check_detail(platform):
    detail = {"category": "task_active"}
    sessions = Scripted({"check": Problem(409, "NATIVE_FILES_CONFLICT", "x", files_check=detail)}, no_helpers())
    with pytest.raises(_OmissionBeforeDispatch) as caught:
        run(sessions)
    assert caught.value.body["error"]["files_check"] == detail


@pytest.mark.parametrize("answer", [
    Problem(409, "NATIVE_SESSION_CHANGED", "changed"), Problem(503, "NATIVE_FILES_CONFLICT", "unknown"),
], ids=["other-code", "other-status"])
def test_any_other_check_failure_keeps_its_own_outcome(platform, answer):
    sessions = Scripted({"check": answer}, no_helpers())
    with pytest.raises(Problem) as caught:
        run(sessions)
    assert caught.value is answer


def test_a_check_that_finds_the_omission_active_resumes_without_confirming_again(platform):
    sessions = Scripted({"check": ACTIVE}, no_helpers())
    assert run(sessions) == "followed"
    assert sessions.trace == [("destination", "7"), BEFORE, dispatched("check", "destination"),
                              ("follow", ORIGINAL, SOURCE, "7")]


def test_a_follow_refused_after_the_check_reports_that_no_confirmation_was_dispatched(platform):
    sessions = Scripted({"check": ACTIVE}, no_helpers())

    def conflict(_client, _command, *, before_send):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "moved")
    sessions.follow = conflict
    with pytest.raises(_OmissionBeforeDispatch):
        run(sessions)


def test_a_pending_scan_stays_pending_and_is_never_confirmed(platform):
    scanning = view(reason="scan_pending", files_check=NativeFilesCheckDetail(category="scan_pending"))
    sessions = Scripted({"check": scanning}, no_helpers())
    value = run(sessions)
    assert value.files_confirmation_pending == CONFIRMATION and value.reason == "scan_pending"
    assert value.files_confirmation_saved is None and value.files_confirmation_reason is None
    assert sessions.trace == [("destination", "7"), BEFORE, dispatched("check", "destination")]


@pytest.mark.parametrize("reason", ["accepted_or_started", "admission_pending", "task_active", "task_unconfirmed",
                                    "task_access_denied"])
def test_active_work_stops_a_system_decision_but_not_an_owners(platform, reason):
    waiting = view(reason=reason)
    system = Scripted({"check": waiting, "confirm": ACTIVE}, session_state())
    assert run(system, system_policy=True, refusal_code="NATIVE_FILES_TIMEOUT") == waiting
    assert dispatched("confirm", "source_not_migratable") not in system.trace
    owner = Scripted({"check": waiting, "confirm": ACTIVE}, no_helpers())
    assert run(owner) == "followed" and dispatched("confirm", "destination") in owner.trace


def test_the_scan_answer_wins_over_a_system_decisions_stop(platform):
    scanning = view(reason="scan_pending", files_check=NativeFilesCheckDetail(category="scan_pending"))
    value = run(Scripted({"check": scanning}, session_state()), system_policy=True, refusal_code="NATIVE_FILES_TIMEOUT")
    assert value.files_confirmation_pending == CONFIRMATION


def test_an_already_saved_confirmation_confirms_its_original_reason(platform):
    state = session_state(environment=view(previous_files_profile=None))
    sessions = Scripted({"check": SAVED_SOURCE, "confirm": ACTIVE}, state)
    assert run(sessions) == "followed"
    assert sessions.trace[3:5] == [BEFORE, dispatched("confirm", "source")]


def test_a_saved_reason_that_no_longer_holds_is_refused_before_dispatch(platform):
    sessions = Scripted({"check": SAVED_SOURCE}, no_helpers())
    with pytest.raises(_OmissionBeforeDispatch) as caught:
        run(sessions)
    assert caught.value.body["error"]["code"] == "NATIVE_FILES_CONFLICT"
    assert sessions.trace.count(BEFORE) == 1


def test_a_preview_only_checks(platform):
    sessions = Scripted({"check": PLAIN}, no_helpers())
    assert run(sessions, preview=True) == PLAIN
    assert sessions.trace == [BEFORE, dispatched("check", "destination")]


def test_continuing_reads_the_saved_confirmation_then_activates_its_reason(platform):
    sessions = Scripted({"read": SAVED_DESTINATION, "activate": ACTIVE}, no_helpers())
    assert run(sessions, resume=True) == "followed"
    assert sessions.trace == [BEFORE, dispatched("read"), dispatched("activate", "destination"),
                              ("follow", ORIGINAL, SOURCE, "7")]


def test_continuing_an_active_omission_only_resumes_the_follow(platform):
    sessions = Scripted({"read": ACTIVE}, no_helpers())
    assert run(sessions, resume=True) == "followed"
    assert sessions.trace == [BEFORE, dispatched("read"), ("follow", ORIGINAL, SOURCE, "7")]


def test_continuing_without_a_saved_confirmation_is_refused(platform):
    sessions = Scripted({"read": PLAIN}, no_helpers())
    assert refusal(sessions, resume=True) == (
        Problem, 409, "NATIVE_FILES_CONFLICT", "the saved omission reason is no longer confirmed")
    assert dispatched("activate", "destination") not in sessions.trace


def test_reconcile_returns_a_ready_answer_as_it_is(platform):
    ready = view(state="ready")
    sessions = Scripted({"read": ready}, session_state())
    assert run(sessions, reconcile=True) == ready
    assert sessions.trace == [BEFORE, dispatched("read")]


@pytest.mark.parametrize("original", [ABSENT, SAVED_DESTINATION], ids=["absent", "saved"])
def test_reconcile_of_an_unconfirmed_omission_confirms_it_again_under_the_same_id(platform, original):
    sessions = Scripted({"read": original, "check": PLAIN, "confirm": ACTIVE}, no_helpers())
    assert run(sessions, reconcile=True) == "followed"
    assert sessions.trace == [
        BEFORE, dispatched("read"), BEFORE, ("destination", "7"), BEFORE, dispatched("check", "destination"), BEFORE,
        dispatched("confirm", "destination"), ("follow", ORIGINAL, SOURCE, "7")]
    assert {sent.request_id for sent in sessions.sent} == {CONFIRMATION}


def test_reconcile_resumes_an_active_omission(platform):
    active = view(files_migrated=False, files_skip_request_id=CONFIRMATION)
    sessions = Scripted({"read": active}, session_state())
    assert run(sessions, reconcile=True) == "followed"
    assert sessions.trace[-1] == ("follow", ORIGINAL, SOURCE, "7")


def test_reconcile_keeps_an_unresolved_answer_without_resending(platform):
    other = view(reason="source_active")
    sessions = Scripted({"read": other}, session_state())
    assert run(sessions, reconcile=True) == other
    assert sessions.trace == [BEFORE, dispatched("read")]


def test_observing_never_resumes_or_resends(platform):
    sessions = Scripted({"read": ABSENT}, session_state())
    assert run(sessions, reconcile=True, observe_only=True) == ABSENT
    assert sessions.trace == [BEFORE, dispatched("read")]


def test_a_confirmed_answer_still_pending_is_returned_without_a_follow(platform):
    pending = view(reason="accepted_or_started", files_confirmation_pending=CONFIRMATION)
    sessions = Scripted({"check": PLAIN, "confirm": pending}, no_helpers())
    assert run(sessions) == pending
    assert sessions.trace[-1] == dispatched("confirm", "destination")


def test_an_operation_already_ready_is_only_read(platform):
    ready = view(state="ready", files_migrated=False, files_skip_request_id=CONFIRMATION)
    sessions = Scripted({"read": ready}, session_state(hosted_session_id=DESTINATION, environment=None, following=ready))
    assert run(sessions) == ready
    assert sessions.trace == [("destination", "7"), BEFORE, dispatched("read")]


@pytest.mark.parametrize(("arrange", "expected"), [
    ("source_changed", "source_changed"),
    ("source_changed+destination", "source_changed"),
    ("destination+source", "destination"),
    ("source+policy", "source"),
    ("policy", "source_not_migratable"),
], ids=lambda value: value)
def test_the_first_true_reason_is_the_one_confirmed(platform, arrange, expected):
    state = session_state(files_profile=None) if "destination" in arrange else session_state()
    if "source_changed" in arrange:
        platform[SOURCE] = metadata("5")
    if "+source" in arrange or arrange.startswith("source+"):
        platform[SOURCE] = metadata(status="stopped")
    policy = {"system_policy": True, "refusal_code": "NATIVE_FILES_TIMEOUT"} if "policy" in arrange else {}
    sessions = Scripted({"check": PLAIN, "confirm": ACTIVE}, state)
    run(sessions, **policy)
    assert sessions.sent[0].missing == expected


def test_a_refused_source_invocation_means_the_source_cannot_help(platform):
    sessions = Scripted({"check": PLAIN, "confirm": ACTIVE}, session_state(source_invocation_refused=True))
    run(sessions)
    assert sessions.sent[0].missing == "source"


def test_a_missing_source_session_cannot_help(platform):
    del platform[SOURCE]
    sessions = Scripted({"check": PLAIN, "confirm": ACTIVE}, session_state())
    run(sessions)
    assert sessions.sent[0].missing == "source"


def test_with_both_helpers_available_nothing_is_dispatched(platform):
    sessions = Scripted({}, session_state())
    assert refusal(sessions) == (_OmissionBeforeDispatch, 409, "NATIVE_FILES_CONFLICT",
                                 "both helpers are available; no omission confirmation was dispatched")
    assert sessions.trace == [("destination", "7")]


def test_a_preview_with_both_helpers_is_refused_as_keep_the_migration(platform):
    sessions = Scripted({}, session_state())
    assert refusal(sessions, preview=True) == (
        Problem, 409, "NATIVE_FILES_CONFLICT", "both helpers are available; keep the original migration")


def test_continuing_with_both_helpers_is_refused_as_keep_the_migration(platform):
    sessions = Scripted({}, session_state())
    assert refusal(sessions, resume=True) == (
        Problem, 409, "NATIVE_FILES_CONFLICT", "both helpers are available; keep the original migration")


@pytest.mark.parametrize("mode", ["preview", "resume"])
def test_a_displayed_version_that_moved_is_refused_first(platform, mode):
    sessions = Scripted({}, session_state(files_omission_profile=None))
    sessions.selected = "8"
    assert refusal(sessions, **{mode: True}) == (
        Problem, 409, "NATIVE_ENVIRONMENT_CONFLICT", "the displayed destination version changed")


@pytest.mark.parametrize("mode", ["reconcile", "preview", "resume"])
def test_an_unreadable_destination_is_unknown_and_creates_nothing(platform, mode):
    del platform[DESTINATION]
    sessions = Scripted({}, session_state(files_omission_profile=None))
    assert refusal(sessions, **{mode: True}) == (
        Problem, 503, "NATIVE_ENVIRONMENT_UNKNOWN", "read the original omission destination; nothing was created")
    assert sessions.trace == []


def test_a_destination_on_another_version_is_unknown(platform):
    platform[DESTINATION] = metadata("9")
    assert refusal(Scripted({}, session_state()), resume=True)[2] == "NATIVE_ENVIRONMENT_UNKNOWN"


def test_an_old_destination_image_cannot_confirm(platform):
    assert refusal(Scripted({}, session_state(files_omission_profile=None)), command(session=DESTINATION))[1:3] == (
        409, "NATIVE_FILES_CONFIRMATION_UNSUPPORTED")


def retarget_id(source=SOURCE):
    return material_digest({"kind": "files-omission-retarget", "confirmation": CONFIRMATION, "original": ORIGINAL,
                            "source": source, "target": LATER})


def fresh_id():
    return material_digest({"kind": "files-omission-follow", "confirmation": CONFIRMATION, "original": ORIGINAL,
                            "source": DESTINATION, "target": LATER})


def later(state=None):
    sessions = Scripted({"check": PLAIN, "confirm": ACTIVE}, state or session_state(files_profile=None))
    sessions.selected = "8"
    return sessions


def test_a_changed_target_retargets_under_an_id_derived_from_the_confirmation(platform):
    sessions = later()
    assert run(sessions, command(target="8")) == "followed"
    assert sessions.trace[-1] == ("follow", retarget_id(), SOURCE, "8")
    assert {(sent.follow.request_id, sent.pending_request_id, sent.pending_session_id) for sent in sessions.sent} == {
        (retarget_id(), ORIGINAL, DESTINATION)}


def test_resending_a_changed_target_uses_the_same_id(platform):
    first, second = later(), later()
    run(first, command(target="8"))
    run(second, command(target="8"))
    assert first.sent[0].follow.request_id == second.sent[0].follow.request_id == retarget_id()


def test_a_ready_original_is_left_behind_by_a_fresh_follow(platform):
    ready = view(state="ready", files_migrated=True, files_manifest_sha256="a" * 64)
    sessions = later(session_state(hosted_session_id=DESTINATION, environment=ready, files_profile=None))
    assert run(sessions, command(target="8", session=SOURCE)) == "followed"
    assert sessions.trace[-1] == ("follow", fresh_id(), DESTINATION, "8")
    assert {(sent.pending_request_id, sent.pending_session_id) for sent in sessions.sent} == {(None, None)}


def test_a_ready_original_from_another_source_is_refused(platform):
    ready = view(state="ready", files_migrated=True, files_manifest_sha256="a" * 64)
    sessions = later(session_state(hosted_session_id=DESTINATION, environment=ready, files_profile=None))
    assert refusal(sessions, command(target="8", session="session-other")) == (
        Problem, 409, "NATIVE_ENVIRONMENT_CONFLICT", "the referenced ready operation has another source")


def test_a_recorded_fresh_follow_keeps_its_source(platform):
    fresh = view(request_id=fresh_id(), previous_hosted_session_id=DESTINATION, previous_agent_version="7",
                 hosted_session_id=LATER, hosted_agent_version="8")
    sessions = later(session_state(hosted_session_id=DESTINATION, environment=fresh, files_profile=None))
    assert run(sessions, command(target="8", session=None)) == "followed"
    assert sessions.trace[-1] == ("follow", fresh_id(), DESTINATION, "8")


def test_the_retained_operation_decides_the_source(platform):
    assert refusal(Scripted({}, session_state()), command(session="session-other")) == (
        Problem, 409, "NATIVE_ENVIRONMENT_CONFLICT", "the displayed source differs from the original file operation")
    sessions = Scripted({"check": PLAIN, "confirm": ACTIVE}, session_state(
        hosted_session_id="session-3", environment=None, following=view(), files_profile=None))
    run(sessions, command(session=None))
    assert sessions.sent[0].follow.expected_session_id == SOURCE


def test_a_source_equal_to_the_destination_has_no_transition_to_confirm(platform):
    sessions = Scripted({}, session_state(environment=None, hosted_session_id=DESTINATION))
    assert refusal(sessions, command(session=None)) == (
        Problem, 409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "there is no original environment transition to confirm")


def test_a_preview_of_a_moved_original_destination_offers_the_replacement_without_a_session(platform):
    del platform[LATER]
    platform[DESTINATION] = metadata("9")
    sessions = later(session_state(files_omission_profile=None))
    value = run(sessions, command(target="8"), preview=True)
    assert isinstance(value, NativeEnvironmentReplacementPreview)
    assert (value.reason, value.original_request_id, value.hosted_session_id, value.observed_agent_version) == (
        "destination_version_changed", ORIGINAL, LATER, "9")
    assert sessions.trace == []


def test_a_preview_of_an_unstarted_replacement_needs_a_reader(platform):
    del platform[LATER]
    assert run(later(session_state(files_omission_profile=None)), command(target="8"), preview=True).reason == "reader_required"


def test_a_moved_original_destination_is_a_reason_unless_a_fresh_follow_replaced_it(platform):
    platform[DESTINATION] = metadata("9")
    sessions = later(session_state())
    run(sessions, command(target="8"))
    assert sessions.sent[0].missing == "original_destination"


def test_a_moved_source_is_named_before_a_moved_original_destination(platform):
    platform[SOURCE] = metadata("5")
    platform[DESTINATION] = metadata("9")
    sessions = later(session_state())
    run(sessions, command(target="8"))
    assert sessions.sent[0].missing == "source_changed"


def test_a_fresh_follow_no_longer_counts_the_moved_original_destination(platform):
    fresh = view(request_id=fresh_id(), previous_hosted_session_id=DESTINATION, previous_agent_version="7",
                 hosted_session_id=LATER, hosted_agent_version="8")
    platform[DESTINATION] = metadata("9")
    saved = view(files_confirmation_saved=CONFIRMATION, files_confirmation_reason="original_destination")
    sessions = later(session_state(hosted_session_id=DESTINATION, environment=fresh))
    sessions.answers["check"] = saved
    with pytest.raises(_OmissionBeforeDispatch) as caught:
        run(sessions, command(target="8", session=None))
    assert caught.value.body["error"]["code"] == "NATIVE_FILES_CONFLICT"


def test_reconcile_of_a_system_decision_stays_a_system_decision(platform):
    sessions = Scripted({"read": ABSENT, "check": PLAIN, "confirm": ACTIVE}, session_state())
    run(sessions, reconcile=True, system_policy=True, refusal_code="NATIVE_FILES_TIMEOUT")
    assert [(sent.stage, sent.missing, sent.decision_basis) for sent in sessions.sent] == [
        ("read", None, "system_policy"), ("check", "source_not_migratable", "system_policy"),
        ("confirm", "source_not_migratable", "system_policy")]


def test_when_two_refusals_apply_the_earlier_one_answers(platform):
    moved = Scripted({}, session_state(files_omission_profile=None))
    moved.selected = "8"
    del platform[DESTINATION]
    assert refusal(moved, resume=True)[3] == "the displayed destination version changed", "version before the destination"
    platform[DESTINATION] = metadata("7")
    old_image = session_state(files_omission_profile=None, environment=None, hosted_session_id=DESTINATION)
    assert refusal(Scripted({}, old_image), command(session=None))[2] == "NATIVE_FILES_CONFIRMATION_UNSUPPORTED", (
        "image before the transition")
    assert refusal(Scripted({}, session_state(hosted_session_id=DESTINATION, environment=None)),
                   command(session=DESTINATION))[2] == "NATIVE_FILES_CONFIRMATION_REQUIRED", "no transition before helpers"
