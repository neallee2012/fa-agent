"""API-resident routine clock: one fixed RID per occurrence through the original due/trigger seams. No cloud."""

import time
from datetime import timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401  (fixture)
from test_instruction_flow import instruction_models  # noqa: F401  (fixture)
from test_m3_auth import jwk_client, signing_key  # noqa: F401  (fixtures)
from test_native_turn_api import native_api  # noqa: F401  (fixture)
from test_native_routine_api import daily_definition, routine_api  # noqa: F401  (fixture)


@pytest.fixture
def clock_api(request, monkeypatch):
    monkeypatch.setenv("FA_NATIVE_ROUTINE_CLOCK", "api")
    case = request.getfixturevalue("routine_api")
    case.clock = case.client.app.state.native_routine_clock
    case.agent_id = case.foundry.target.agent_id
    # The harness serves one native Agent; roster enumeration has its own case below.
    case.clock.agent_ids = lambda: (case.agent_id,)
    return case


def publish(case, local_time, request_id="definition-1", **changes):
    definition = daily_definition(request_id, **{"enabled": True, "schedule": {
        "kind": "daily", "time_zone": "UTC", "local_time": local_time}, **changes})
    saved = case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers)
    assert saved.status_code == 200, saved.text
    return saved.json()


def routine_records(case):
    return {rid: record for rid, record in case.remote.records.items() if record.routine is not None}


def due_minute(case, minutes=1):
    return case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=minutes)


def expected_rid(case, scheduled):
    from shared_context.native_routine_wire import native_routine_request_id
    binding = case.c.control.policy.template
    return native_routine_request_id(binding.tenant_id, binding.team_id, case.agent_id, "daily-summary", scheduled)


def test_clock_is_off_by_default_and_its_principal_is_refused(routine_api):
    from shared_context.errors import Problem
    from shared_context.native_routine_auth import NativeRoutineApiClock
    from shared_context.native_routines import NativeRoutineService

    case = routine_api
    assert getattr(case.client.app.state, "native_routine_clock", None) is None
    service = NativeRoutineService(case.composition, enabled=True)
    with pytest.raises(Problem) as refused:
        service.due(NativeRoutineApiClock(case.composition.verifier.auth.tenant_id), case.foundry.target.agent_id)
    assert refused.value.status == 403 and refused.value.body["error"]["code"] == "NATIVE_ROUTINE_PRODUCER_DENIED"


@pytest.mark.parametrize("environ,code", [
    ({"FA_NATIVE_ROUTINE_CLOCK": "job"}, "NATIVE_ROUTINES_DISABLED"),
    ({"FA_NATIVE_ROUTINE_CLOCK": "api"}, "NATIVE_ROUTINES_DISABLED"),
    ({"FA_NATIVE_ROUTINE_CLOCK": "api", "FA_NATIVE_ROUTINES_ENABLED": "1"}, None),
    ({"FA_NATIVE_ROUTINE_CLOCK": "0", "FA_NATIVE_ROUTINES_ENABLED": "1"}, False),
    ({}, False),
])
def test_clock_flag_is_explicit(environ, code):
    from shared_context.errors import Problem
    from shared_context.native_routine_auth import routine_clock_enabled

    if code is None or code is False:
        assert routine_clock_enabled(environ) is (code is None)
        return
    with pytest.raises(Problem) as refused:
        routine_clock_enabled(environ)
    assert refused.value.body["error"]["code"] == code


def test_clock_flag_without_native_turns_builds_no_clock_and_says_so(clock_api, caplog):
    from shared_context.native_routine_api import install_native_routine_routes

    app = FastAPI()
    install_native_routine_routes(app, clock_api.composition, enabled=False)
    assert getattr(app.state, "native_routine_clock", None) is None
    assert "FA_NATIVE_ROUTINE_CLOCK_DISABLED reason=native_turns_off" in caplog.text


def test_tick_without_due_work_reads_no_hosted_session(clock_api):
    case = clock_api
    publish(case, due_minute(case, 5).strftime("%H:%M"))
    before = len(case.remote.calls)
    counts = case.clock.tick()
    assert counts["triggered"] == 0 and counts["agent_failures"] == 0
    assert len(case.remote.calls) == before


def test_due_occurrence_is_one_rid_and_one_turn_across_ticks_and_a_restart(clock_api):
    from shared_context.native_routine_clock import NativeRoutineClock

    case = clock_api
    due = due_minute(case)
    saved = publish(case, due.strftime("%H:%M"))
    assert case.clock.tick()["triggered"] == 0
    case.c.h.time = due + timedelta(seconds=2)
    first = case.clock.tick()
    assert first["triggered"] == 1, first
    records = routine_records(case)
    rid = expected_rid(case, due)
    assert list(records) == [rid]
    original = records[rid]
    assert original.routine.owner_oid == case.c.human.oid
    assert original.routine.occurrence.definition_sha256 == saved["definition_sha256"]
    assert original.command.content == "Read the original work."
    # Still accepted: later ticks and a fresh process re-check the same RID and N1 reads it, never a new turn.
    case.c.h.time += timedelta(minutes=1)
    case.clock.tick()
    restarted = NativeRoutineClock(case.clock.service)
    restarted.agent_ids = case.clock.agent_ids
    restarted.tick()
    assert routine_records(case) == {rid: original}
    case.remote.completed(rid)
    assert restarted.tick()["triggered"] == 1
    before = len(case.remote.calls)
    case.c.h.time += timedelta(hours=1)
    assert restarted.tick()["triggered"] == 0
    assert len(case.remote.calls) == before and list(routine_records(case)) == [rid]


def test_two_overlapping_clocks_admit_one_turn(clock_api, monkeypatch):
    """Old and new revisions both pass the read before either reserves: CAS intent + N1 RID keep one turn."""
    from shared_context.native_routine_clock import NativeRoutineClock
    from shared_context.native_routines import NativeRoutineService

    case = clock_api
    due = due_minute(case)
    publish(case, due.strftime("%H:%M"))
    case.c.h.time = due + timedelta(seconds=2)
    other = NativeRoutineClock(NativeRoutineService(case.composition, enabled=True))
    other.agent_ids = case.clock.agent_ids
    original_reserve = NativeRoutineService._reserve
    reserves, nested = [], []

    def interleaved(self, control, agent_id, definition, submission):
        reserves.append(submission.command.request_id)
        if len(reserves) == 1:
            nested.append(other.tick())
        return original_reserve(self, control, agent_id, definition, submission)

    monkeypatch.setattr(NativeRoutineService, "_reserve", interleaved)
    outer = case.clock.tick()
    rid = expected_rid(case, due)
    assert reserves == [rid, rid]
    assert outer["triggered"] == 1 and nested[0]["triggered"] == 1
    assert list(routine_records(case)) == [rid]
    received = [command for _, command in case.remote.calls if command.operation == "routine_receive"]
    assert len(received) == 2 and len({command.sha256 for command in received}) == 1


def test_downtime_catch_up_sends_only_the_latest_occurrence_after_approval(clock_api):
    case = clock_api
    due = due_minute(case)
    publish(case, due.strftime("%H:%M"))
    case.c.h.time = due + timedelta(days=3, hours=2)
    assert case.clock.tick()["triggered"] == 1
    assert list(routine_records(case)) == [expected_rid(case, due + timedelta(days=3))]


def test_an_occurrence_before_approval_is_not_backfilled(clock_api):
    case = clock_api
    passed = case.c.h.time.replace(second=0, microsecond=0) - timedelta(minutes=5)
    publish(case, passed.strftime("%H:%M"))
    assert case.clock.tick()["triggered"] == 0 and routine_records(case) == {}
    case.c.h.time = passed + timedelta(days=1, seconds=2)
    assert case.clock.tick()["triggered"] == 1
    assert list(routine_records(case)) == [expected_rid(case, passed + timedelta(days=1))]


def test_disabled_routine_does_not_run_its_next_occurrence(clock_api):
    case = clock_api
    due = due_minute(case)
    publish(case, due.strftime("%H:%M"))
    case.c.h.time = due + timedelta(seconds=2)
    assert case.clock.tick()["triggered"] == 1
    case.remote.completed(expected_rid(case, due))
    case.clock.tick()
    publish(case, due.strftime("%H:%M"), "definition-2", expected_version_id="definition-1", enabled=False)
    case.c.h.time = due + timedelta(days=1, seconds=2)
    before = len(case.remote.calls)
    assert case.clock.tick()["triggered"] == 0
    assert len(case.remote.calls) == before and list(routine_records(case)) == [expected_rid(case, due)]


def test_one_failing_agent_or_occurrence_does_not_block_the_others(clock_api, monkeypatch, caplog):
    from shared_context.errors import Problem
    from shared_context.native_routines import NativeRoutineService

    case = clock_api
    due = due_minute(case)
    publish(case, due.strftime("%H:%M"))
    publish(case, due.strftime("%H:%M"), "definition-b", routine_id="broken-routine")
    case.c.h.time = due + timedelta(seconds=2)
    case.clock.agent_ids = lambda: ("unknown-agent", case.agent_id)
    original_trigger = NativeRoutineService.trigger

    def trigger(self, producer, agent_id, command):
        if command.routine_id == "broken-routine":
            raise Problem(503, "NATIVE_ROUTINE_UNCONFIRMED", "OFFLINE one occurrence is unconfirmed")
        return original_trigger(self, producer, agent_id, command)

    monkeypatch.setattr(NativeRoutineService, "trigger", trigger)
    counts = case.clock.tick()
    assert counts["agent_failures"] == 1 and counts["refused"] == 1 and counts["triggered"] == 1
    assert list(routine_records(case)) == [expected_rid(case, due)]
    logged = caplog.text
    assert "FA_NATIVE_ROUTINE_CLOCK_AGENT agent=unknown- code=" in logged
    assert "code=NATIVE_ROUTINE_UNCONFIRMED" in logged
    # A steady failure is logged once, not every minute.
    caplog.clear()
    case.clock.tick()
    assert "unknown-" not in caplog.text and "NATIVE_ROUTINE_UNCONFIRMED" not in caplog.text


def test_budget_defers_work_to_the_next_tick(clock_api):
    case = clock_api
    due = due_minute(case)
    publish(case, due.strftime("%H:%M"))
    case.c.h.time = due + timedelta(seconds=2)
    now = [0.0]
    case.clock.monotonic = lambda: now[0]
    original_due = case.clock.service.due

    def slow_due(*args, **kwargs):
        now[0] += case.clock.budget + 1
        return original_due(*args, **kwargs)

    case.clock.service.due = slow_due
    counts = case.clock.tick()
    assert counts["triggered"] == 0 and counts["deferred"] == 1 and routine_records(case) == {}
    case.clock.service.due = original_due
    assert case.clock.tick()["triggered"] == 1


def test_roster_lists_configured_then_registered_agents_once(clock_api, monkeypatch):
    from shared_context.native_routine_clock import NativeRoutineClock

    case = clock_api
    clock = NativeRoutineClock(case.clock.service)
    configured = tuple(target.agent_id for target in case.composition.config.targets)
    monkeypatch.setattr("shared_context.native_routine_clock.registered_agent_ids",
                        lambda _store, _config: (configured[0], "registered-agent"))
    assert clock.agent_ids() == (*dict.fromkeys(configured), "registered-agent")


def test_lifespan_starts_one_tick_at_once_and_stops_the_clock():
    from shared_context.behavior_server import _lifespan
    from shared_context.native_routine_clock import NativeRoutineClock

    ticks, closed = [], []
    clock = object.__new__(NativeRoutineClock)
    clock.interval, clock.offset, clock.budget, clock.wall = 60, 2, 45, lambda: 0.0
    clock._executor = clock._task = None
    clock._stopping = False
    clock.tick = lambda: ticks.append(1) or {"triggered": 0, "refused": 0, "deferred": 0}
    composition = SimpleNamespace(_peer_router=None, assistant=None, _peers={}, close=lambda: closed.append(1))
    app = FastAPI()
    app.state.native_routine_clock = clock
    _lifespan(app, composition, lambda: None)
    with TestClient(app):
        for _ in range(200):
            if ticks:
                break
            time.sleep(0.01)
        assert ticks == [1] and clock._task is not None
    assert clock._task is None and clock._executor is None and clock._stopping is True and closed == [1]
