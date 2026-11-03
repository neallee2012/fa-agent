"""``trigger`` and ``_reminders`` on the paths the API tests do not reach: every refusal, what each branch skips and the
order of the writes (reserve, environment, submit, accounting, settle). Offline Foundry HTTP, real metadata store."""

import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest

from shared_context import native_routines
from shared_context.behavior_native_sessions import NativeSessions, native_sessions
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import NativeConversationRead, NativeEnvironmentView
from shared_context.errors import Problem
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE
from shared_context.native_routine_wire import NativeReminderQuery, native_routine_request_id
from shared_context.native_routines import NativeRoutineService
from test_conversation_transfer_startup import synthetic_standing_v1  # fixture
from test_instruction_flow import instruction_models  # fixture
from test_m3_auth import jwk_client, signing_key  # fixture
from test_native_routine_api import daily_definition, reserve_continuing, routine_api  # fixture
from test_native_turn_api import native_api, submit  # fixture

pytestmark = pytest.mark.parametrize("routine_api", ["clock"], indirect=True)


def publish(case, definition):
    response = case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers)
    assert response.status_code == 200, response.text


def daily(case, **changes):
    """A published daily routine whose occurrence is due now; returns the time and the clock's body."""
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    publish(case, daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")}, **changes))
    case.c.h.time = due + timedelta(seconds=2)
    return due, {"routine_id": "daily-summary", "scheduled_for": due.isoformat()}


def code(response):
    return response.json()["error"]["code"]


def steps(monkeypatch):
    """The writes of one trigger, in order."""
    order = []
    reserve, submit_turn = NativeRoutineService._reserve, native_routines.submit_native_turn
    accounted, settle = NativeTurnGateway._accounted, NativeRoutineService._settle

    def spy_reserve(service, *args):
        order.append("reserve")
        return reserve(service, *args)

    def spy_submit(*args, **kwargs):
        order.append("submit")
        return submit_turn(*args, **kwargs)

    def spy_accounted(*args):
        order.append("accounted")
        return accounted(*args)

    def spy_settle(service, control, receipt):
        order.append("settle")
        return settle(service, control, receipt)

    monkeypatch.setattr(NativeRoutineService, "_reserve", spy_reserve)
    monkeypatch.setattr(native_routines, "submit_native_turn", spy_submit)
    monkeypatch.setattr(NativeTurnGateway, "_accounted", staticmethod(spy_accounted))
    monkeypatch.setattr(NativeRoutineService, "_settle", spy_settle)
    return order


def environment_view(client, command, case, state):
    return NativeEnvironmentView(
        agent_id=client.target.agent_id, request_id=command.request_id,
        previous_hosted_session_id="original-source", previous_agent_version="40",
        hosted_session_id=case.remote.target.session_id, hosted_agent_version=command.expected_agent_version,
        state=state, reason=None if state == "ready" else "task_active", progress_token="a" * 64,
        **({"files_migrated": False, "files_skip_request_id": "system-policy-decision",
            "files_omission_basis": "system_policy", "files_omission_reason": "source"} if state == "ready" else {}))


def changing_environment(case, monkeypatch, *, advance=None, state="ready", order=None):
    """The first session read says the Agent is following; the switch then ends with ``state`` (or ``advance`` raises)."""
    real, reads = NativeSessions.current, []

    def current(sessions, client, **kwargs):
        reads.append(True)
        if len(reads) == 1:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE new version needs following")
        return real(sessions, client, **kwargs)

    def destination(_sessions, client, _version, before_send):
        before_send()
        return client.for_target(case.remote.target)

    def advancing(_sessions, client, command, *, before_send):
        if order is not None:
            order.append("advance")
        before_send()
        if advance is not None:
            raise advance
        return environment_view(client, command, case, state)

    monkeypatch.setattr(NativeSessions, "current", current)
    monkeypatch.setattr(NativeSessions, "_destination", destination)
    monkeypatch.setattr(NativeSessions, "advance", advancing)
    return reads


def originals(monkeypatch, *answers):
    """``_original`` answers these in turn: the read before the environment and the one after it."""
    remaining = list(answers)

    def original(_client, _rid):
        return remaining.pop(0)

    monkeypatch.setattr(NativeRoutineService, "_original", staticmethod(original))
    return remaining


# trigger

def test_a_fresh_occurrence_reserves_then_submits_then_accounts_then_settles(routine_api, monkeypatch):
    case = routine_api
    _, body = daily(case)
    order = steps(monkeypatch)
    sent = case.trigger(body)
    assert sent.status_code == 202, sent.text
    assert order == ["reserve", "submit", "accounted", "settle"]


def test_a_session_read_refused_for_another_reason_reaches_the_clock_with_nothing_reserved(routine_api, monkeypatch):
    case = routine_api
    _, body = daily(case)
    order = steps(monkeypatch)

    def refused(*_args, **_kwargs):
        raise Problem(403, "WORK_POLICY_REVOKED", "OFFLINE revoked")

    monkeypatch.setattr(NativeSessions, "current", refused)
    denied = case.trigger(body)
    assert (denied.status_code, code(denied)) == (403, "WORK_POLICY_REVOKED")
    assert order == []


def test_a_receipt_with_no_intent_or_another_occurrence_is_another_source(routine_api, monkeypatch):
    case = routine_api
    _, body = daily(case)
    sent = case.trigger(body)
    assert sent.status_code == 202, sent.text
    order = steps(monkeypatch)
    with monkeypatch.context() as patched:
        patched.setattr(native_routines, "_same_occurrence", lambda *_: False)
        differs = case.trigger(body)
    assert (differs.status_code, code(differs)) == (409, "NATIVE_ROUTINE_CONFLICT")
    assert differs.json()["error"]["message"] == "the original native receipt has another source"
    with monkeypatch.context() as patched:
        patched.setattr(NativeRoutineService, "_intent", lambda *_: None)
        unknown = case.trigger(body)
    assert (unknown.status_code, code(unknown)) == (409, "NATIVE_ROUTINE_CONFLICT")
    assert order == []


def test_a_finished_receipt_is_accounted_settled_and_answered_even_after_the_routine_is_withdrawn(routine_api, monkeypatch):
    case = routine_api
    due, body = daily(case)
    rid = case.trigger(body).json()["request_id"]
    case.remote.completed(rid)
    publish(case, {**daily_definition(request_id="definition-2", enabled=False, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")}), "expected_version_id": "definition-1"})
    order = steps(monkeypatch)
    answered = case.trigger(body)
    assert answered.status_code == 200 and answered.json()["status"] == "completed", answered.text
    assert order == ["accounted", "settle"]


def test_a_reminder_never_starts_a_turn(routine_api, monkeypatch):
    case = routine_api
    due = case.c.h.time + timedelta(minutes=1)
    publish(case, {"request_id": "reminder-v1", "routine_id": "a-reminder", "kind": "reminder", "enabled": True,
                   "conversation_id": "native-cid", "related_request_id": "human-seed",
                   "schedule": {"kind": "once", "scheduled_for": due.isoformat()}})
    case.c.h.time = due + timedelta(seconds=2)
    order = steps(monkeypatch)
    denied = case.trigger({"routine_id": "a-reminder", "scheduled_for": due.isoformat()})
    assert (denied.status_code, code(denied)) == (403, "NATIVE_ROUTINE_ATTENTION_ONLY")
    assert order == []


def test_an_occurrence_older_than_its_approval_is_never_backfilled(routine_api, monkeypatch):
    case = routine_api
    past = case.c.h.time.replace(second=0, microsecond=0) - timedelta(minutes=1)
    publish(case, daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": past.strftime("%H:%M")}))
    order = steps(monkeypatch)
    denied = case.trigger({"routine_id": "daily-summary", "scheduled_for": past.isoformat()})
    assert (denied.status_code, code(denied)) == (409, "ROUTINE_OCCURRENCE_STALE")
    assert denied.json()["error"]["message"] == "an unregistered occurrence predating approval cannot be backfilled"
    assert order == []


def test_a_definition_withdrawn_before_the_home_read_refuses_with_no_intent(routine_api, monkeypatch):
    case = routine_api
    _, body = daily(case)
    order = steps(monkeypatch)
    monkeypatch.setattr(NativeRoutineService, "_active_definition", lambda *_a, **_k: (_ for _ in ()).throw(
        Problem(403, "NATIVE_ROUTINE_INACTIVE", "OFFLINE withdrawn")))
    invoke = NativeTurnGateway._invoke

    def no_home_read(client, command):
        assert not isinstance(command, NativeConversationRead), "the home conversation is read after the check"
        return invoke(client, command)

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(no_home_read))
    denied = case.trigger(body)
    assert (denied.status_code, code(denied)) == (403, "NATIVE_ROUTINE_INACTIVE")
    assert order == []


def responses_run(monkeypatch, opened):
    monkeypatch.setattr(native_routines, "turn_profile", lambda _capture: NATIVE_RESPONSES_PROFILE)
    monkeypatch.setattr(native_routines, "platform_client", lambda _composition, client: client)
    monkeypatch.setattr(native_routines, "open_conversation", lambda _client, *, before_send: opened)


def test_a_reserve_that_fails_after_opening_a_conversation_logs_it_unused(routine_api, monkeypatch, caplog):
    case = routine_api
    _, body = daily(case)
    responses_run(monkeypatch, "opened-conversation")

    def lost(*_args):
        raise Problem(503, "NATIVE_ROUTINE_UNCONFIRMED", "OFFLINE")

    monkeypatch.setattr(NativeRoutineService, "_reserve", lost)
    with caplog.at_level(logging.WARNING):
        failed = case.trigger(body)
    assert failed.status_code == 503
    assert "FA_NATIVE_ROUTINE_CONVERSATION_UNUSED" in caplog.text and "conversation=opened-conve" in caplog.text


@pytest.mark.parametrize("kept,unused", [("opened-conversation", False), ("other-conversation", True)])
def test_a_lost_reserve_race_logs_the_conversation_it_opened(routine_api, monkeypatch, caplog, kept, unused):
    case = routine_api
    _, body = daily(case)
    responses_run(monkeypatch, "opened-conversation")
    reserved = SimpleNamespace(submission=SimpleNamespace(
        command=None, capture=None, routine=SimpleNamespace(conversation_id=kept)))
    monkeypatch.setattr(NativeRoutineService, "_reserve", lambda *_: reserved)

    def stop(*_args, **_kwargs):
        raise Problem(409, "OFFLINE_STOP", "OFFLINE")

    monkeypatch.setattr(native_routines, "submit_native_turn", stop)
    monkeypatch.setattr(NativeRoutineService, "_original", staticmethod(lambda *_: None))
    with caplog.at_level(logging.WARNING):
        assert code(case.trigger(body)) == "OFFLINE_STOP"
    assert ("FA_NATIVE_ROUTINE_CONVERSATION_UNUSED" in caplog.text) is unused


def test_an_invocations_run_opens_no_platform_conversation(routine_api, monkeypatch):
    case = routine_api
    _, body = daily(case)
    monkeypatch.setattr(native_routines, "open_conversation", lambda *_a, **_k: pytest.fail("no platform conversation"))
    sent = case.trigger(body)
    assert sent.status_code == 202 and sent.json()["conversation_id"].startswith("native-"), sent.text


def test_an_environment_that_is_not_ready_keeps_the_intent_and_sends_nothing(routine_api, monkeypatch):
    case = routine_api
    due, body = daily(case)
    order = steps(monkeypatch)
    changing_environment(case, monkeypatch, state="pending", order=order)
    waiting = case.trigger(body)
    assert waiting.status_code == 200, waiting.text
    value = waiting.json()
    assert value["status"] == "environment_pending" and value["purpose"] == "original-routine-environment-progress"
    assert value["routine_id"] == "daily-summary" and value["request_id"] not in case.remote.records
    saved = NativeRoutineService(case.composition, enabled=True)._intent(
        case.c.control, case.foundry.target.agent_id, value["request_id"])
    assert value["conversation_id"] == saved.submission.routine.conversation_id
    assert order == ["reserve", "advance"]


def test_an_environment_that_fails_is_the_routines_own_answer(routine_api, monkeypatch):
    case = routine_api
    _, body = daily(case)
    order = steps(monkeypatch)
    changing_environment(case, monkeypatch, order=order, advance=Problem(502, "OFFLINE_ENVIRONMENT", "OFFLINE"))
    failed = case.trigger(body)
    assert (failed.status_code, code(failed)) == (502, "NATIVE_ROUTINE_UNCONFIRMED")
    assert failed.json()["error"]["environment_code"] == "OFFLINE_ENVIRONMENT"
    assert failed.json()["error"]["request_id"] and failed.json()["error"]["request_id"] not in case.remote.records
    assert order == ["reserve", "advance"]


def test_a_ready_environment_reads_the_session_again_then_submits(routine_api, monkeypatch):
    case = routine_api
    _, body = daily(case)
    order = steps(monkeypatch)
    reads = changing_environment(case, monkeypatch, order=order)
    sent = case.trigger(body)
    assert sent.status_code == 202, sent.text
    assert order == ["reserve", "advance", "submit", "accounted", "settle"] and len(reads) == 2


def test_a_continuing_run_checks_its_session_only_after_the_environment(routine_api, monkeypatch):
    case = routine_api
    due, body = daily(case)
    legacy = reserve_continuing(case, due)
    seed = case.remote.records["human-seed"]
    case.remote.records["human-seed"] = seed.model_copy(update={"hosted_session_id": "old-native-session"})
    order = steps(monkeypatch)
    changing_environment(case, monkeypatch, order=order)
    moved = case.trigger(body)
    assert (moved.status_code, code(moved)) == (409, "NATIVE_SESSION_CHANGED")
    assert order == ["advance"] and legacy.command.request_id not in case.remote.records


def test_a_continuing_run_is_submitted_after_its_environment(routine_api, monkeypatch):
    case = routine_api
    due, body = daily(case)
    legacy = reserve_continuing(case, due)
    order = steps(monkeypatch)
    changing_environment(case, monkeypatch, order=order)
    sent = case.trigger(body)
    assert sent.status_code == 202, sent.text
    assert case.remote.records[legacy.command.request_id].command == legacy.command
    assert order == ["advance", "submit", "accounted", "settle"]


def test_a_receipt_that_changed_during_the_environment_is_refused(routine_api, monkeypatch):
    case = routine_api
    _, body = daily(case)
    order = steps(monkeypatch)
    changing_environment(case, monkeypatch, order=order)
    originals(monkeypatch, None, SimpleNamespace(status="accepted"))
    monkeypatch.setattr(native_routines, "_same_occurrence", lambda *_: False)
    denied = case.trigger(body)
    assert (denied.status_code, code(denied)) == (409, "NATIVE_ROUTINE_CONFLICT")
    assert denied.json()["error"]["message"] == "the occurrence changed while its environment was being prepared"
    assert order == ["reserve", "advance"]


def admitted_then_environment(case, monkeypatch, *, complete):
    _, body = daily(case)
    sent = case.trigger(body)
    assert sent.status_code == 202, sent.text
    rid = sent.json()["request_id"]
    if complete:
        case.remote.completed(rid)
    return body, case.remote.records[rid]


def test_a_receipt_that_finished_during_the_environment_is_answered_without_a_submit(routine_api, monkeypatch):
    case = routine_api
    body, receipt = admitted_then_environment(case, monkeypatch, complete=True)
    order = steps(monkeypatch)
    changing_environment(case, monkeypatch, order=order)
    originals(monkeypatch, None, receipt)
    answered = case.trigger(body)
    assert answered.status_code == 200 and answered.json()["status"] == "completed", answered.text
    assert order == ["advance", "accounted", "settle"]


def test_a_receipt_accepted_during_the_environment_is_sent_again_with_its_own_capture(routine_api, monkeypatch):
    case = routine_api
    body, receipt = admitted_then_environment(case, monkeypatch, complete=False)
    seen = []
    submit_turn = native_routines.submit_native_turn

    def spy(composition, client, command, capture, **kwargs):
        seen.append((capture, kwargs["original"], kwargs["conversation"]))
        return submit_turn(composition, client, command, capture, **kwargs)

    monkeypatch.setattr(native_routines, "submit_native_turn", spy)
    changing_environment(case, monkeypatch)
    originals(monkeypatch, None, receipt)
    assert case.trigger(body).status_code == 202
    assert seen == [(receipt.capture, receipt, None)]


def test_a_guarded_switch_ends_in_the_request_and_never_runs_the_after_switch_step(routine_api, monkeypatch):
    case = routine_api
    _, body = daily(case)
    order = steps(monkeypatch)
    sessions = native_sessions(case.composition)
    monkeypatch.setattr(type(sessions.guard), "enforce", True, raising=False)
    monkeypatch.setattr(sessions.guard, "enforce", True, raising=False)
    changing_environment(case, monkeypatch, order=order)
    switched = []

    def in_request(_sessions, raw_client, command, authorize, rid):
        authorize()
        switched.append((command.idle_only, rid))
        return raw_client.for_target(case.remote.target)

    monkeypatch.setattr(NativeRoutineService, "_switched", staticmethod(in_request))
    sent = case.trigger(body)
    assert sent.status_code == 202, sent.text
    assert switched == [(True, sent.json()["request_id"])]
    assert order == ["reserve", "submit", "accounted", "settle"]


def test_a_guarded_switch_checks_a_continuing_runs_session_before_the_submit(routine_api, monkeypatch):
    case = routine_api
    due, body = daily(case)
    reserve_continuing(case, due)
    seed = case.remote.records["human-seed"]
    case.remote.records["human-seed"] = seed.model_copy(update={"hosted_session_id": "old-native-session"})
    sessions = native_sessions(case.composition)
    monkeypatch.setattr(type(sessions.guard), "enforce", True, raising=False)
    monkeypatch.setattr(sessions.guard, "enforce", True, raising=False)
    changing_environment(case, monkeypatch)
    monkeypatch.setattr(NativeRoutineService, "_switched", staticmethod(
        lambda _sessions, raw_client, *_: raw_client.for_target(case.remote.target)))
    moved = case.trigger(body)
    assert (moved.status_code, code(moved)) == (409, "NATIVE_SESSION_CHANGED")


# _reminders

def reminders(case, read_turn, *, conversation="native-cid", after=None, actionable_only=False):
    service = NativeRoutineService(case.composition, enabled=True)
    return service._reminders(case.c.control, case.foundry.target.agent_id,
                              NativeReminderQuery(conversation_id=conversation, after=after), read_turn,
                              actionable_only=actionable_only)


def reminder(request_id, routine_id, due, **changes):
    return {"request_id": request_id, "routine_id": routine_id, "kind": "reminder", "enabled": True,
            "conversation_id": "native-cid", "related_request_id": "unfinished",
            "schedule": {"kind": "once", "scheduled_for": due.isoformat()}, **changes}


@pytest.fixture
def unfinished(routine_api):
    assert submit(routine_api, "unfinished", conversation_id="native-cid").status_code == 202
    return routine_api.remote.records["unfinished"]


def run_case(case):
    """A run admitted in its own conversation; returns its receipt."""
    _, body = daily(case)
    sent = case.trigger(body)
    assert sent.status_code == 202, sent.text
    return case.remote.records[sent.json()["request_id"]]


def test_a_reference_with_no_intent_is_invalid(routine_api, monkeypatch):
    case = routine_api
    run_case(case)
    monkeypatch.setattr(NativeRoutineService, "_intent", lambda *_: None)
    with pytest.raises(Problem) as refused:
        reminders(case, lambda _rid: pytest.fail("no receipt read without an intent"))
    assert refused.value.body["error"]["code"] == "NATIVE_ROUTINE_INVALID"


@pytest.mark.parametrize("change", [{"agent_id": "another-agent"}, {"conversation_id": "another-conversation"}])
def test_a_run_receipt_that_is_not_its_own_is_a_conflict(routine_api, change):
    case = routine_api
    receipt = run_case(case)
    with pytest.raises(Problem) as refused:
        reminders(case, lambda _rid: receipt.model_copy(update=change))
    assert refused.value.body["error"]["code"] == "NATIVE_ROUTINE_CONFLICT"


def test_a_run_receipt_of_another_occurrence_is_a_conflict(routine_api, monkeypatch):
    case = routine_api
    receipt = run_case(case)
    monkeypatch.setattr(native_routines, "_same_occurrence", lambda *_: False)
    with pytest.raises(Problem) as refused:
        reminders(case, lambda _rid: receipt)
    assert refused.value.body["error"]["code"] == "NATIVE_ROUTINE_CONFLICT"


def test_a_conversation_of_neither_side_reads_no_receipt(routine_api):
    case = routine_api
    run_case(case)
    value = reminders(case, lambda _rid: pytest.fail("not this conversation's run"), conversation="third-conversation")
    assert value.routine_turns == () and value.reminders == ()


@pytest.mark.parametrize("own,actionable,attention,listed", [
    (True, False, True, True), (True, False, False, True), (True, True, True, True), (True, True, False, False),
    (False, False, True, True), (False, False, False, False), (False, True, True, False), (False, True, False, False)])
def test_a_run_is_listed_by_where_it_is_read_and_whether_it_needs_attention(routine_api, own, actionable, attention, listed):
    case = routine_api
    receipt = run_case(case)
    case.remote.completed(receipt.request_id) if not attention else None
    closed = case.remote.records[receipt.request_id]
    value = reminders(case, lambda _rid: closed, conversation=receipt.conversation_id if own else "native-cid",
                      actionable_only=actionable)
    assert [item.request_id for item in value.routine_turns] == ([receipt.request_id] if listed else [])


def due_reminders(case, *definitions):
    due = case.c.h.time + timedelta(minutes=1)
    for definition in definitions:
        publish(case, definition(due))
    case.c.h.time = due + timedelta(minutes=30)
    return due


def test_a_reminder_whose_work_is_missing_is_an_issue_and_others_still_show(routine_api, unfinished):
    case = routine_api
    due_reminders(case, lambda due: reminder("v1", "first", due), lambda due: reminder("v2", "second", due))

    def read_turn(_rid, reads=[]):
        reads.append(True)
        if len(reads) == 1:
            raise Problem(404, "NATIVE_RID_MISSING", "OFFLINE")
        return unfinished

    value = reminders(case, read_turn)
    assert [(item.routine_id, item.code) for item in value.issues] == [("first", "NATIVE_RID_MISSING")]
    assert [item.routine_id for item in value.reminders] == ["second"]
    assert reminders(case, lambda _rid: (_ for _ in ()).throw(Problem(404, "NATIVE_RID_MISSING", "OFFLINE")),
                     after="z" * 8).issues == ()


def test_any_other_read_failure_of_a_reminder_reaches_the_caller(routine_api, unfinished):
    case = routine_api
    due_reminders(case, lambda due: reminder("v1", "first", due))
    with pytest.raises(Problem) as refused:
        reminders(case, lambda _rid: (_ for _ in ()).throw(Problem(404, "NATIVE_OTHER", "OFFLINE")))
    assert refused.value.body["error"]["code"] == "NATIVE_OTHER"


@pytest.mark.parametrize("change", [{"agent_id": "another-agent"}, {"conversation_id": "another-conversation"}])
def test_a_reminder_target_of_another_conversation_is_refused(routine_api, unfinished, change):
    case = routine_api
    due_reminders(case, lambda due: reminder("v1", "first", due))
    with pytest.raises(Problem) as refused:
        reminders(case, lambda _rid: unfinished.model_copy(update=change))
    assert refused.value.body["error"]["code"] == "NATIVE_ROUTINE_SCOPE"


@pytest.mark.parametrize("change", [{"cancel_requested": True}, {"failure_code": "NATIVE_TURN_CANCELLED"},
                                    {"status": "completed"}, {"status": "busy_refused"}])
def test_finished_or_cancelled_work_is_not_a_reminder(routine_api, unfinished, change):
    case = routine_api
    due_reminders(case, lambda due: reminder("v1", "first", due))
    assert reminders(case, lambda _rid: SimpleNamespace(**{
        **{name: getattr(unfinished, name) for name in (
            "agent_id", "conversation_id", "request_id", "status", "failure_code", "cancel_requested")}, **change})).reminders == ()


def test_unknown_work_is_still_a_reminder_with_its_failure(routine_api, unfinished):
    case = routine_api
    due = due_reminders(case, lambda due: reminder("v1", "first", due))
    work = SimpleNamespace(agent_id=unfinished.agent_id, conversation_id=unfinished.conversation_id,
                           request_id="unfinished", status="unknown", failure_code="NATIVE_OTHER", cancel_requested=False)
    [value] = reminders(case, lambda _rid: work).reminders
    assert (value.routine_id, value.scheduled_for, value.request_id, value.status, value.failure_code, value.agent_id,
            value.conversation_id) == ("first", due, "unfinished", "unknown", "NATIVE_OTHER", unfinished.agent_id, "native-cid")
    assert value.occurrence_id == native_routine_request_id(
        case.c.control.policy.template.tenant_id, case.c.control.policy.template.team_id, unfinished.agent_id, "first", due)


@pytest.mark.parametrize("first,second,kept", [(0, 5, "second"), (5, 0, "first"), (5, 5, "first")])
def test_of_two_reminders_on_one_work_the_latest_due_stands_and_a_tie_keeps_the_first(
        routine_api, unfinished, first, second, kept):
    case = routine_api
    due_reminders(
        case, lambda due: reminder("v1", "first", due + timedelta(minutes=first)),
        lambda due: reminder("v2", "second", due + timedelta(minutes=second)))
    assert [item.routine_id for item in reminders(case, lambda _rid: unfinished).reminders] == [kept]


@pytest.mark.parametrize("change,conversation", [({"enabled": False}, "native-cid"), ({}, "another-conversation")])
def test_a_disabled_reminder_or_one_of_another_conversation_is_not_read(routine_api, unfinished, change, conversation):
    case = routine_api
    due_reminders(case, lambda due: reminder("v1", "first", due, **change))
    value = reminders(case, lambda _rid: pytest.fail("not this conversation's reminder"), conversation=conversation)
    assert value.reminders == ()


def test_a_reminder_not_due_yet_is_not_read(routine_api, unfinished):
    case = routine_api
    due = due_reminders(case, lambda due: reminder("v1", "first", due + timedelta(hours=2)))
    assert due
    assert reminders(case, lambda _rid: pytest.fail("not due")).reminders == ()


def test_the_page_stops_at_twenty_and_issues_show_only_on_the_first(routine_api, unfinished, monkeypatch):
    case = routine_api
    works = [unfinished.model_copy(update={"request_id": f"work-{index:02d}"}) for index in range(22)]
    due = case.c.h.time + timedelta(minutes=1)
    for index in range(22):
        publish(case, reminder(f"version-{index:02d}", f"reminder-{index:02d}", due, related_request_id="unfinished"))
    case.c.h.time = due + timedelta(minutes=1)
    remaining = list(works)
    first = reminders(case, lambda _rid: remaining.pop(0))
    assert [item.request_id for item in first.reminders] == [work.request_id for work in works[:20]]
    assert first.next_after == "work-19"
    remaining = list(works)
    second = reminders(case, lambda _rid: remaining.pop(0), after="work-19")
    assert [item.request_id for item in second.reminders] == ["work-20", "work-21"] and second.next_after is None
