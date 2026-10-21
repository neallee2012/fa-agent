"""Signed owner API seams and the API's in-process clock, with original metadata and an offline Foundry HTTP boundary."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from test_native_turn_api import native_api, submit


def clock_call(composition, agent_id, method, body=None):
    """Call due/trigger exactly as the API clock does and shape the result like the old HTTP reply."""
    from shared_context.errors import Problem
    from shared_context.native_routine_auth import NativeRoutineApiClock
    from shared_context.native_routine_wire import NativeRoutineDispatch, NativeRoutineTrigger
    from shared_context.native_routines import NativeRoutineService

    service = NativeRoutineService(composition, enabled=True)
    clock = NativeRoutineApiClock(composition.verifier.auth.tenant_id)
    try:
        value = (service.due(clock, agent_id) if method == "due" else
                 service.trigger(clock, agent_id, NativeRoutineTrigger.model_validate_json(json.dumps(body))))
    except Problem as error:
        status, payload = error.status, error.body
        return SimpleNamespace(status_code=status, json=lambda: payload, text=json.dumps(payload))
    raw = value.model_dump_json()
    status = 202 if isinstance(value, NativeRoutineDispatch) and value.status in {"accepted", "started"} else 200
    return SimpleNamespace(status_code=status, json=lambda: json.loads(raw), text=raw)


@pytest.fixture
def routine_api(request, monkeypatch, signing_key):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    if getattr(request, "param", None) == "clock":
        monkeypatch.setenv("FA_NATIVE_ROUTINE_CLOCK", "api")
    case = request.getfixturevalue("native_api")
    response = submit(case, "human-seed")
    assert response.status_code == 202, response.text
    case.remote.completed("human-seed")
    case.read_headers = case.headers
    case.headers = case.c.h.headers(signing_key, "Behavior.Read Behavior.Approve")
    case.agent_headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": case.foundry.target.agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",))}
    case.routine_prefix = "/native-routines/v1/agents/" + case.foundry.target.agent_id
    case.trigger = lambda body: clock_call(case.composition, case.foundry.target.agent_id, "trigger", body)
    case.due = lambda: clock_call(case.composition, case.foundry.target.agent_id, "due")
    return case


def daily_definition(request_id="definition-1", **changes):
    return {
        "request_id": request_id, "routine_id": "daily-summary", "expected_version_id": None,
        "conversation_id": "native-cid", "kind": "routine", "content": "Read the original work.",
        "schedule": {"kind": "daily", "time_zone": "Asia/Taipei", "local_time": "09:00"},
        **changes,
    }


def test_owner_can_save_and_read_a_disabled_definition_without_triggering_a_turn(routine_api):
    case = routine_api
    before = len(case.remote.calls)
    response = case.client.post(
        case.routine_prefix + "/definitions", json=daily_definition(), headers=case.headers)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["definition"]["request_id"] == "definition-1"
    assert saved["definition"]["owner_oid"] == case.c.human.oid
    assert saved["definition"]["agent_id"] == case.foundry.target.agent_id
    assert saved["definition"]["enabled"] is False
    assert saved["definition"]["schedule"]["time_zone"] == "Asia/Taipei"
    assert len(saved["definition_sha256"]) == 64
    read = case.client.get(case.routine_prefix + "/definitions/daily-summary", headers=case.headers)
    assert read.status_code == 200 and read.json() == saved
    assert set(case.remote.records) == {"human-seed"}
    assert all(command.operation in {"conversation", "session_read"} for _, command in case.remote.calls[before:])
    assert case.c.control.policy.routines == ()


def test_current_definition_is_listed_once_and_retrying_an_old_version_does_not_reactivate_it(routine_api):
    case = routine_api
    path = case.routine_prefix + "/definitions"
    first = case.client.post(path, json=daily_definition(enabled=True), headers=case.headers)
    assert first.status_code == 200, first.text
    second = case.client.post(path, json=daily_definition(
        "definition-2", expected_version_id="definition-1", enabled=False), headers=case.headers)
    assert second.status_code == 200, second.text
    repeated = case.client.post(path, json=daily_definition(enabled=True), headers=case.headers)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    current = case.client.get(path + "/daily-summary", headers=case.headers)
    assert current.json() == second.json()
    listed = case.client.get(path, headers=case.headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["definitions"] == [second.json()]
    assert listed.json()["next_after"] is None
    stale = case.client.post(path, json=daily_definition(
        "definition-3", expected_version_id="definition-1"), headers=case.headers)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "NATIVE_ROUTINE_CHANGED"
    conflict = case.client.post(path, json=daily_definition(content="different content"), headers=case.headers)
    assert conflict.status_code == 409
    denied = case.client.post(path, json=daily_definition("unapproved"), headers=case.read_headers)
    assert denied.status_code == 403


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_api_clock_admits_the_original_occurrence_without_becoming_the_owner(routine_api):
    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    saved = case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers)
    assert saved.status_code == 200, saved.text
    case.c.h.time = due + timedelta(seconds=2)
    body = {"routine_id": "daily-summary", "scheduled_for": due.isoformat()}
    result = case.trigger(body)
    assert result.status_code == 202, result.text
    reply = result.json()
    assert reply["status"] == "accepted"
    original = case.remote.records[reply["request_id"]]
    assert original.command.content == "Read the original work." and original.command.authorize_own_result is False
    assert original.routine.owner_oid == case.c.human.oid
    assert original.routine.occurrence.approved_version == "definition-1"
    assert original.routine.occurrence.definition_sha256 == saved.json()["definition_sha256"]
    assert original.conversation_id == "native-cid"
    assert "capture" not in reply and "content" not in reply
    case.remote.completed(original.request_id)
    before = len([1 for _, command in case.remote.calls if command.operation == "routine_receive"])
    repeated = case.trigger(body)
    assert repeated.status_code == 200 and repeated.json()["status"] == "completed"
    assert len([1 for _, command in case.remote.calls if command.operation == "routine_receive"]) == before
    assert case.c.control.policy.routines == ()


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_routine_waits_for_automatic_environment_and_retains_its_occurrence(routine_api, monkeypatch):
    from shared_context.behavior_native_sessions import NativeSessions
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView
    from shared_context.errors import Problem

    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    body = {"routine_id": "daily-summary", "scheduled_for": due.isoformat()}
    original_current = NativeSessions.current
    work_ready, environment_ready, advances = [False], [False], []

    def current(sessions, client, **kwargs):
        if not environment_ready[0]:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE new version needs following")
        return original_current(sessions, client, **kwargs)

    def destination(_sessions, client, _version, before_send):
        before_send()
        return client.for_target(case.remote.target)

    def progress(_sessions, client, command, *, before_send):
        before_send()
        return NativeEnvironmentView(
            agent_id=client.target.agent_id, request_id=command.request_id,
            previous_hosted_session_id="original-source", previous_agent_version="40",
            hosted_session_id=case.remote.target.session_id, hosted_agent_version=command.expected_agent_version,
            state="ready" if environment_ready[0] else "pending",
            reason=None if environment_ready[0] else "task_active",
            progress_token=("b" if work_ready[0] else "a") * 64,
            **({"files_migrated": False, "files_skip_request_id": "system-policy-decision",
                "files_omission_basis": "system_policy", "files_omission_reason": "source"}
               if environment_ready[0] else {}))

    def advance(sessions, client, command, *, before_send):
        assert command.idle_only
        advances.append(command)
        environment_ready[0] = work_ready[0]
        return progress(sessions, client, command, before_send=before_send)

    monkeypatch.setattr(NativeSessions, "current", current)
    monkeypatch.setattr(NativeSessions, "_destination", destination)
    monkeypatch.setattr(NativeSessions, "advance", advance)
    waiting = case.trigger(body)
    assert waiting.status_code == 200 and waiting.json()["status"] == "environment_pending", waiting.text
    rid = waiting.json()["request_id"]
    assert rid not in case.remote.records
    due_again = case.due()
    assert any(item["request_id"] == rid and item["registered"] for item in due_again.json()["occurrences"])
    assert len(advances) == 1
    work_ready[0] = True
    # The next minute's tick re-sends the same occurrence; the original environment advance repeats unchanged.
    received = case.trigger(body)
    assert received.status_code == 202 and received.json()["request_id"] == rid, received.text
    assert len(advances) == 2 and advances[0] == advances[1]
    assert case.remote.records[rid].routine.owner_oid == case.c.human.oid


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_lost_admission_response_remains_due_under_the_same_rid_after_the_next_day(routine_api):
    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    listed = case.due()
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["occurrences"]) == 1
    occurrence = listed.json()["occurrences"][0]
    body = {"routine_id": "daily-summary", "scheduled_for": due.isoformat()}
    case.remote.lose_submit = True
    lost = case.trigger(body)
    assert lost.status_code == 503 and lost.json()["error"]["code"] == "NATIVE_ROUTINE_UNCONFIRMED"
    assert lost.json()["error"]["request_id"] == occurrence["request_id"]
    original = case.remote.records[occurrence["request_id"]]
    case.remote.lose_submit = False
    case.c.h.time += timedelta(days=1)
    pending = case.due().json()["occurrences"]
    retained = next(item for item in pending if item["request_id"] == original.request_id)
    assert retained["registered"] is True
    again = case.trigger(body)
    assert again.status_code == 202, again.text
    assert case.remote.records[original.request_id].submit_digest == original.submit_digest
    case.remote.completed(original.request_id)
    assert case.trigger(body).status_code == 200
    remaining = case.due().json()["occurrences"]
    assert len(remaining) == 1 and remaining[0]["request_id"] != original.request_id


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_original_agent_rechecks_routine_withdrawal_without_invoking_or_changing_the_original(routine_api):
    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    saved = case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).json()
    case.c.h.time = due + timedelta(seconds=2)
    admitted = case.trigger({
        "routine_id": "daily-summary", "scheduled_for": due.isoformat()}).json()
    original = case.remote.records[admitted["request_id"]]
    check = {"request_id": original.request_id, "conversation_id": original.conversation_id,
             "definition_sha256": saved["definition_sha256"]}
    path = "/work-policy/v1/native-capabilities/routine/check"
    before = len(case.remote.calls)
    allowed = case.client.post(path, json=check, headers=case.agent_headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["definition_active"] is True and allowed.json()["authorization"] is False
    assert len(case.remote.calls) == before
    disabled = {**definition, "request_id": "definition-2", "expected_version_id": "definition-1", "enabled": False}
    assert case.client.post(case.routine_prefix + "/definitions", json=disabled, headers=case.headers).status_code == 200
    denied = case.client.post(path, json=check, headers=case.agent_headers)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "NATIVE_ROUTINE_INACTIVE"
    assert case.remote.records[original.request_id] == original


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_once_reminder_reads_the_original_unfinished_work_without_a_new_turn_or_cancel_revival(routine_api):
    from shared_context.behavior_native_turn_api import NativeTurnGateway
    from shared_context.behavior_native_turn_wire import NativeCancel

    case = routine_api
    assert submit(case, "unfinished", conversation_id="native-cid").status_code == 202
    due = case.c.h.time + timedelta(minutes=1)
    command = {
        "request_id": "reminder-definition", "routine_id": "answer-reminder", "conversation_id": "native-cid",
        "kind": "reminder", "related_request_id": "unfinished", "enabled": True,
        "schedule": {"kind": "once", "time_zone": "UTC", "scheduled_for": due.isoformat()},
    }
    saved = case.client.post(case.routine_prefix + "/definitions", json=command, headers=case.headers)
    assert saved.status_code == 200, saved.text
    path = case.routine_prefix + "/reminders"
    query = {"conversation_id": "native-cid"}
    assert case.client.post(path, json=query, headers=case.read_headers).json()["reminders"] == []
    case.c.h.time = due + timedelta(seconds=1)
    observed = case.client.post(path, json=query, headers=case.read_headers)
    assert observed.status_code == 200, observed.text
    items = observed.json()["reminders"]
    assert len(items) == 1 and items[0]["request_id"] == "unfinished"
    assert items[0]["conversation_id"] == "native-cid" and items[0]["status"] == "accepted"
    assert observed.json()["purpose"] == "attention-only-not-turn-dispatch"
    assert case.due().json()["occurrences"] == []
    assert set(case.remote.records) == {"human-seed", "unfinished"}
    NativeTurnGateway(case.composition, enabled=True).change(
        case.c.human, NativeCancel(conversation_id="native-cid", request_id="unfinished"))
    assert case.client.post(path, json=query, headers=case.read_headers).json()["reminders"] == []
    assert set(case.remote.records) == {"human-seed", "unfinished"}


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_official_continuation_keeps_routine_attribution_and_the_original_unknown_receipt(routine_api, monkeypatch):
    from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
    from shared_context.behavior_native_turn_wire import (
        NATIVE_REQUEST, NativeContinuationRead, NativeContinuationSubmit, NativeReceipt,
        check_continuation_link, continuation_request_id,
    )
    from shared_context.native_continuation_wire import NativeContinuation, NativeContinuationView

    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    admitted = case.trigger({
        "routine_id": "daily-summary", "scheduled_for": due.isoformat()}).json()
    source = NativeReceipt.model_validate_json(case.remote.records[admitted["request_id"]].model_copy(update={
        "status": "unknown", "failure_code": "NATIVE_TURN_UNKNOWN", "turn_number": 2}).model_dump_json())
    case.remote.records[source.request_id] = source
    ref = NativeContinuation(
        continuation_of=source.request_id, root_request_id=source.request_id, attempt=1,
        source_submit_digest=source.submit_digest, checkpoint_sha256="c" * 64)
    rid = continuation_request_id(source.agent_id, source.conversation_id, source.request_id, 1)
    view = NativeContinuationView(
        agent_id=source.agent_id, conversation_id=source.conversation_id, original_request_id=source.request_id,
        root_request_id=source.request_id, request_id=rid, continuation=ref, state="prepared",
        source_hosted_session_id=source.hosted_session_id)

    def native(request):
        command = NATIVE_REQUEST.validate_json(request.content)
        headers = {"content-type": "application/json", "x-agent-session-id": source.hosted_session_id,
                   "x-agent-invocation-id": "offline-routine-recovery"}
        if isinstance(command, NativeContinuationRead):
            return httpx.Response(200, content=view.model_dump_json().encode(), headers=headers)
        if isinstance(command, NativeContinuationSubmit):
            retained = NativeReceipt(
                agent_id=source.agent_id, hosted_session_id=source.hosted_session_id, request_id=rid,
                conversation_id=source.conversation_id, command=command.command, capture=command.capture,
                capture_digest=command.capture.sha256, submit_digest=command.sha256, status="accepted",
                state_profile=command.state_profile, source=command.source, routine=command.routine, continuation=command.continuation)
            case.remote.records[rid] = retained
            return httpx.Response(202, content=retained.model_dump_json().encode(), headers=headers)
        return case.remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(native))
    response = case.client.post(NATIVE_AGENT_PREFIX + "/continue", headers=case.agent_headers, json={
        "conversation_id": source.conversation_id, "request_id": source.request_id})
    assert response.status_code == 202, response.text
    resumed = NativeReceipt.model_validate_json(response.content)
    assert resumed.routine == source.routine and resumed.human_origin is False
    check_continuation_link(resumed, source)
    assert case.remote.records[source.request_id] == source


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_a_routine_turn_runs_on_its_conversations_model_not_the_project_default(routine_api, monkeypatch):
    """The seed ran on gpt-4.1-mini; the project default is now another model. Without the conversation's model the
    routine's capture took the default, a model-only change the Hosted refuses with NATIVE_CAPTURE_CHANGED."""
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from shared_context.native_turns import _conversation_capture

    case = routine_api

    def hosted(request):
        # Like the Hosted with model switching off (_capture_fits): after a completed turn the conversation capture,
        # model included, must match, or the routine is refused before any receipt.
        command = NATIVE_REQUEST.validate_json(request.content)
        if command.operation == "routine_receive" and any(
                record.conversation_id == command.command.conversation_id and record.status == "completed"
                and _conversation_capture(record.capture) != _conversation_capture(command.capture)
                for record in case.remote.records.values()):
            return httpx.Response(409, json={"error": {
                "code": "NATIVE_CAPTURE_CHANGED", "message": "private", "not_dispatched": True}})
        return case.remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(hosted))
    seeded = case.remote.records["human-seed"].capture.model.id
    assert seeded == "gpt-4.1-mini"
    case.c.control.models.default = "configured-second-model"
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    result = case.trigger({"routine_id": "daily-summary", "scheduled_for": due.isoformat()})
    assert result.status_code == 202, result.text
    original = case.remote.records[result.json()["request_id"]]
    assert original.command.model == seeded and original.capture.model.id == seeded


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_saved_definitions_and_occurrence_intents_survive_the_existing_startup_reader(routine_api):
    from test_conversation_transfer_startup import read_startup

    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    assert case.trigger({
        "routine_id": "daily-summary", "scheduled_for": due.isoformat()}).status_code == 202
    before = dict(case.remote.records)
    read_startup(case.c)
    assert case.remote.records == before


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_owner_can_observe_a_scheduled_original_before_it_has_completed_history(routine_api):
    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    case.c.h.time = due + timedelta(seconds=1)
    admitted = case.trigger({
        "routine_id": "daily-summary", "scheduled_for": due.isoformat()}).json()
    before = dict(case.remote.records)
    observed = case.client.post(case.routine_prefix + "/reminders", headers=case.read_headers,
                                json={"conversation_id": "native-cid"})
    assert observed.status_code == 200, observed.text
    assert observed.json()["routine_turns"] == [admitted]
    assert observed.json()["reminders"] == []
    assert case.remote.records == before


def test_native_routine_routes_are_absent_by_default(request, monkeypatch):
    monkeypatch.delenv("FA_NATIVE_ROUTINES_ENABLED", raising=False)
    monkeypatch.delenv("FA_NATIVE_ROUTINE_CLOCK", raising=False)
    case = request.getfixturevalue("native_api")
    prefix = "/native-routines/v1/agents/" + case.foundry.target.agent_id
    before = len(case.remote.calls)
    assert case.client.get(prefix + "/definitions", headers=case.headers).status_code == 404
    assert case.client.post(prefix + "/trigger", json={}, headers=case.headers).status_code == 404
    assert len(case.remote.calls) == before


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
@pytest.mark.parametrize("identity", ["owner", "agent", "former-producer"])
def test_no_identity_can_send_occurrences_over_http(routine_api, signing_key, identity):
    """The removed Job producer routes stay absent; only the in-process API clock sends due occurrences."""
    case = routine_api
    headers = {"owner": case.headers, "agent": case.agent_headers, "former-producer": {
        "Authorization": "Bearer " + token(signing_key, {
            "oid": "77777777-7777-4777-8777-777777777777", "idtyp": "app",
            "roles": ["Behavior.Routine.Trigger"]}, remove=("scp",))}}[identity]
    before = len(case.remote.calls)
    body = {"routine_id": "daily-summary", "scheduled_for": case.c.h.time.isoformat()}
    for method, path in (("post", "/trigger"), ("get", "/due"), ("post", "/environment-observe")):
        response = getattr(case.client, method)(case.routine_prefix + path, headers=headers,
                                                **({"json": body} if method == "post" else {}))
        assert response.status_code == 404, response.text
    assert len(case.remote.calls) == before


def test_clock_principal_is_refused_when_the_clock_flag_is_off(routine_api):
    case = routine_api
    before = len(case.remote.calls)
    for response in (case.due(), case.trigger({"routine_id": "daily-summary",
                                               "scheduled_for": case.c.h.time.isoformat()})):
        assert response.status_code == 403 and response.json()["error"]["code"] == "NATIVE_ROUTINE_PRODUCER_DENIED"
    assert len(case.remote.calls) == before


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
@pytest.mark.parametrize("date,local_time,code", [
    ("2027-03-14", "02:30", "ROUTINE_LOCAL_TIME_MISSING"),
    ("2027-11-07", "01:30", "ROUTINE_LOCAL_TIME_AMBIGUOUS"),
])
def test_dst_refusal_is_one_routine_issue_and_does_not_block_other_due_work(routine_api, date, local_time, code):
    case = routine_api
    midnight = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    case.c.h.time = midnight
    bad = daily_definition("bad-version", routine_id="dst-routine", enabled=True,
                           schedule={"kind": "daily", "time_zone": "America/New_York", "local_time": local_time})
    good = daily_definition("good-version", routine_id="healthy-routine", enabled=True,
                            schedule={"kind": "daily", "time_zone": "UTC", "local_time": "09:00"})
    for definition in (bad, good):
        saved = case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers)
        assert saved.status_code == 200, saved.text
    case.c.h.time = midnight + timedelta(hours=14)
    observed = case.due()
    assert observed.status_code == 200, observed.text
    page = observed.json()
    assert [item["routine_id"] for item in page["occurrences"]] == ["healthy-routine"]
    assert [(item["routine_id"], item["code"]) for item in page["issues"]] == [("dst-routine", code)]
    value = page["occurrences"][0]
    accepted = case.trigger({
        "routine_id": value["routine_id"], "scheduled_for": value["scheduled_for"]})
    assert accepted.status_code == 202, accepted.text


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_dst_reminder_issue_does_not_hide_another_original_work_reminder(routine_api):
    case = routine_api
    case.c.h.time = datetime(2027, 3, 14, tzinfo=timezone.utc)
    assert submit(case, "unfinished", conversation_id="native-cid").status_code == 202
    for name, schedule in (
        ("dst-reminder", {"kind": "daily", "time_zone": "America/New_York", "local_time": "02:30"}),
        ("healthy-reminder", {"kind": "once", "scheduled_for": "2027-03-14T09:00:00Z"}),
    ):
        saved = case.client.post(case.routine_prefix + "/definitions", headers=case.headers, json={
            "request_id": name + "-v1", "routine_id": name, "kind": "reminder", "related_request_id": "unfinished",
            "conversation_id": "native-cid", "enabled": True, "schedule": schedule})
        assert saved.status_code == 200, saved.text
    case.c.h.time += timedelta(hours=14)
    observed = case.client.post(case.routine_prefix + "/reminders", headers=case.read_headers,
                                json={"conversation_id": "native-cid"})
    assert observed.status_code == 200, observed.text
    assert [item["routine_id"] for item in observed.json()["reminders"]] == ["healthy-reminder"]
    assert observed.json()["issues"][0]["code"] == "ROUTINE_LOCAL_TIME_MISSING"


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_unadmitted_intent_stays_explicit_without_poisoning_other_reminders_after_disable(routine_api, monkeypatch):
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeRoutineSubmit

    case = routine_api
    assert submit(case, "unfinished", conversation_id="native-cid").status_code == 202
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    routine = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=routine, headers=case.headers).status_code == 200
    reminder = {"request_id": "reminder-v1", "routine_id": "healthy-reminder", "kind": "reminder", "enabled": True,
                "conversation_id": "native-cid", "related_request_id": "unfinished",
                "schedule": {"kind": "once", "scheduled_for": due.isoformat()}}
    assert case.client.post(case.routine_prefix + "/definitions", json=reminder, headers=case.headers).status_code == 200
    sent = []

    def refuse_before_admission(request):
        command = NATIVE_REQUEST.validate_json(request.content)
        if isinstance(command, NativeRoutineSubmit):
            sent.append(command)
            return httpx.Response(409, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        return case.remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(refuse_before_admission))
    case.c.h.time = due + timedelta(seconds=2)
    refused = case.trigger({
        "routine_id": "daily-summary", "scheduled_for": due.isoformat()})
    assert refused.status_code == 409 and len(sent) == 1
    rid = sent[0].command.request_id
    disabled = {**routine, "request_id": "definition-2", "expected_version_id": "definition-1", "enabled": False}
    assert case.client.post(case.routine_prefix + "/definitions", json=disabled, headers=case.headers).status_code == 200
    for day in (0, 1, 7):
        case.c.h.time = due + timedelta(days=day, seconds=2)
        observed = case.client.post(case.routine_prefix + "/reminders", headers=case.read_headers,
                                    json={"conversation_id": "native-cid"})
        assert observed.status_code == 200, observed.text
        value = observed.json()
        assert [item["request_id"] for item in value["reminders"]] == ["unfinished"]
        assert [(item["request_id"], item["code"]) for item in value["issues"]] == [(rid, "NATIVE_RID_MISSING")]
        assert value.get("routine_turns", []) == []
    assert rid not in case.remote.records and len(sent) == 1


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_cancelled_routine_is_owner_history_not_an_agent_unfinished_work_reminder(routine_api):
    from shared_context.behavior_native_turn_api import NativeTurnGateway
    from shared_context.behavior_native_turn_wire import NativeCancel, NativeReceipt

    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    admitted = case.trigger({
        "routine_id": "daily-summary", "scheduled_for": due.isoformat()}).json()
    rid = admitted["request_id"]
    NativeTurnGateway(case.composition, enabled=True).change(
        case.c.human, NativeCancel(request_id=rid, conversation_id="native-cid"))
    original = NativeReceipt.model_validate_json(case.remote.records[rid].model_copy(update={
        "status": "unknown", "failure_code": "NATIVE_TURN_CANCELLED"}).model_dump_json())
    case.remote.records[rid] = original
    disabled = {**definition, "request_id": "definition-2", "expected_version_id": "definition-1", "enabled": False}
    assert case.client.post(case.routine_prefix + "/definitions", json=disabled, headers=case.headers).status_code == 200
    case.c.h.time += timedelta(days=3)
    owner = case.client.post(case.routine_prefix + "/reminders", headers=case.read_headers,
                             json={"conversation_id": "native-cid"})
    assert owner.status_code == 200, owner.text
    assert owner.json()["routine_turns"][0]["cancel_requested"] is True
    agent = case.client.post("/work-policy/v1/native-capabilities/routine/reminders", headers=case.agent_headers,
                             json={"conversation_id": "native-cid"})
    assert agent.status_code == 200, agent.text
    assert agent.json().get("routine_turns", []) == [] and agent.json()["reminders"] == []
    assert case.remote.records[rid] == original


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_closed_observations_do_not_fill_the_first_agent_reminder_page(routine_api):
    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    ids = []
    for day in range(22):
        scheduled = due + timedelta(days=day)
        case.c.h.time = scheduled + timedelta(seconds=1)
        result = case.trigger({
            "routine_id": "daily-summary", "scheduled_for": scheduled.isoformat()})
        assert result.status_code == 202, result.text
        ids.append(result.json()["request_id"])
    active = max(ids)
    for rid in ids:
        if rid != active:
            case.remote.completed(rid)
    observed = case.client.post("/work-policy/v1/native-capabilities/routine/reminders", headers=case.agent_headers,
                                json={"conversation_id": "native-cid"})
    assert observed.status_code == 200, observed.text
    assert [item["request_id"] for item in observed.json()["routine_turns"]] == [active]
    assert observed.json()["next_after"] is None
