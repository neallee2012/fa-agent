"""ADR-0009 M1 2d-i daily patrol (P8): ``get_session`` only, two reads >=5 s apart before an abandon decision.

The real API composition and binding row; only the platform (``AIProjectClient``) and the Hosted transport are offline
doubles. Any platform call other than ``get_session`` fails the test, and the Hosted transport counts invocations."""

import asyncio
import threading

from azure.ai.projects.models import AgentSessionResource
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from fastapi.testclient import TestClient
import pytest

from conftest import InMemoryRepository
from shared_context import native_binding
from shared_context.behavior_native_sessions import RETIRED_CONFIRM_SECONDS, RETIRED_PAIR_SECONDS
from shared_context.native_binding import binding_store
from shared_context.native_session_patrol import NativeSessionPatrol
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401  (fixture)
from test_instruction_flow import instruction_models  # noqa: F401  (fixture)
from test_m3_auth import jwk_client, signing_key  # noqa: F401  (fixtures)
from test_native_turn_api import native_api  # noqa: F401  (fixture)


class Platform:
    """``statuses[sid]``: one status or a list read in order; "missing" is 404, "unavailable" a 503."""

    def __init__(self):
        self.statuses, self.reads, self.other, self.now = {}, [], [], 0.0
        self.before_read, self.read_seconds = None, 0

    def get_session(self, name, sid):
        self.reads.append((sid, self.now))
        if self.before_read is not None:
            self.before_read()
        self.now += self.read_seconds
        value = self.statuses[sid]
        if isinstance(value, list):
            value = value.pop(0)
        if value == "missing":
            raise ResourceNotFoundError("OFFLINE the platform deleted the session")
        if value == "unavailable":
            error = HttpResponseError("OFFLINE metadata unavailable")
            error.status_code = 503
            raise error
        return AgentSessionResource({"agent_session_id": sid, "status": value,
                                     "version_indicator": {"type": "version_ref", "agent_version": "41"}})

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def patrol(native_api, monkeypatch):
    case = native_api
    repository = InMemoryRepository()
    monkeypatch.setattr(native_binding, "TableRepository", lambda _client: repository)
    platform = Platform()

    class Agents:
        get_session = staticmethod(platform.get_session)

        def __getattr__(self, name):  # invoke, stop_session, Session Files, list_sessions, get_version, ...
            platform.other.append(name)
            raise AssertionError("the patrol reads get_session only, not " + name)

    class Project:
        def __init__(self, **_):
            self.agents = Agents()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", Project)
    case.platform, case.store = platform, binding_store(case.composition)
    case.agent_id, case.sid = case.remote.target.agent_id, case.remote.target.session_id
    case.handed = []
    case.patrol = NativeSessionPatrol(case.composition, enabled=True, sleep=platform.sleep,
                                      clock=lambda: platform.now,
                                      switch=lambda agent_id: case.handed.append(agent_id) or True)
    return case


def bind(case, profile="foundry-native-v1"):
    return case.store.claim(case.agent_id, hosted_session_id=case.sid, created_agent_version="41",
                            state_profile=profile)


def roster(case, monkeypatch, n, status="deleted"):
    """``n`` registered Agents, each bound to its own SID; their source is A's client naming that SID."""
    ids = tuple(f"agent-{index:03d}" for index in range(n))
    for agent_id in ids:
        case.store.claim(agent_id, hosted_session_id="sid-" + agent_id, created_agent_version="41",
                         state_profile="foundry-native-v1")
        case.platform.statuses["sid-" + agent_id] = status
    monkeypatch.setattr("shared_context.behavior_team_registry.registered_agent_ids", lambda _store, _config: ids)
    source = case.patrol.source
    monkeypatch.setattr(case.patrol, "source", lambda _agent_id, row: source(case.agent_id, row))
    return ids


def pairs(case):
    reads = {}
    for sid, at in case.platform.reads:
        reads.setdefault(sid, []).append(at)
    return reads


def run(case):
    invocations = len(case.remote.calls)
    counts = case.patrol.run()
    assert len(case.remote.calls) == invocations, "the patrol never invokes a session"
    assert case.platform.other == [], "the patrol calls nothing but get_session"
    return counts


@pytest.mark.parametrize("status", ["idle", "active", "failed", "deleted", "expired", "missing", "unavailable"])
def test_patrol_is_metadata_only(patrol, status):
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = status
    run(case)
    assert {sid for sid, _ in case.platform.reads} == {case.sid}


@pytest.mark.parametrize("status", ["deleted", "missing"])
def test_a_deleted_v1_sid_read_twice_5s_apart_records_the_abandon_decision_and_nothing_else(patrol, status):
    case = patrol
    before = bind(case)
    case.platform.statuses[case.sid] = status
    counts = run(case)
    (first, at_first), (second, at_second) = case.platform.reads
    assert first == second == case.sid and at_second - at_first >= 5
    after = case.store.read(case.agent_id)
    assert (after.abandon.hosted_session_id, after.abandon.basis, after.abandon.source_status) == (
        case.sid, "retired", status)
    # Only the decision is written: the SID, versions, profile and notices stay; no session is created or switched.
    assert after.model_copy(update={"abandon": None}) == before
    assert counts["abandoned"] == 1 and counts["handed"] == 1 and case.handed == [case.agent_id]
    # Decided rows are not read again; each run hands them to the background cut worker until it switches them.
    case.platform.reads.clear()
    again = run(case)
    assert (again["skipped"], again["handed"]) == (1, 1) and case.platform.reads == []
    assert case.handed == [case.agent_id] * 2


@pytest.mark.parametrize("statuses", ["idle", "active", "unavailable", ["missing", "active"], ["deleted", "idle"],
                                      ["missing", "unavailable"]])
def test_idle_active_unread_or_unconfirmed_sids_are_untouched(patrol, statuses):
    case = patrol
    before = bind(case)
    case.platform.statuses[case.sid] = statuses
    run(case)
    assert case.store.read(case.agent_id) == before


def test_v2_rows_are_not_read(patrol):
    case = patrol
    case.platform.statuses[case.sid] = "deleted"
    before = bind(case, profile="foundry-native-v2")
    assert run(case)["skipped"] == 1
    assert case.platform.reads == [] and case.store.read(case.agent_id) == before


@pytest.mark.parametrize("profile, read", [("foundry-native-v1", True), ("foundry-native-v2", False)])
def test_the_rows_it_reads_do_not_follow_the_send_profile(patrol, monkeypatch, profile, read):
    """ADR-0009 2d-ii flips the send constant; the patrol still reads only v1 rows (the fixed unlabeled profile)."""
    from shared_context import native_responses_wire, native_session_patrol

    for module in (native_responses_wire, native_session_patrol):
        monkeypatch.setattr(module, "NATIVE_STATE_PROFILE", "foundry-native-v2", raising=False)
    case = patrol
    case.platform.statuses[case.sid] = "idle"
    bind(case, profile=profile)
    counts = run(case)
    assert (counts["idle_or_active"], counts["skipped"]) == ((1, 0) if read else (0, 1))
    assert [sid for sid, _ in case.platform.reads] == ([case.sid] if read else [])


def test_one_bad_registration_skips_only_that_agent(patrol, monkeypatch, caplog):
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "deleted"
    # On the roster with a binding row, but its registration row is missing: resolving it refuses.
    unknown = "unregistered-agent"
    case.store.claim(unknown, hosted_session_id="unregistered-sid", created_agent_version="41",
                     state_profile="foundry-native-v1")
    monkeypatch.setattr("shared_context.behavior_team_registry.registered_agent_ids",
                        lambda _store, _config: (unknown,))
    counts = run(case)
    assert counts["unread"] == 1 and counts["abandoned"] == 1
    assert "NATIVE_SESSION_PATROL_UNREAD agent=unregist code=AGENT_SCOPE_DENIED" in caplog.text
    assert {sid for sid, _ in case.platform.reads} == {case.sid}


def test_a_row_moved_to_another_sid_between_the_reads_gets_no_decision(patrol, caplog):
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "deleted"
    other = case.sid[:-1] + ("0" if case.sid[-1] != "0" else "1")

    def switch():
        if len(case.platform.reads) == 2:  # the second read: the send path switched the row meanwhile
            case.store.repository.data.clear()
            case.store.claim(case.agent_id, hosted_session_id=other, created_agent_version="41",
                             state_profile="foundry-native-v1")

    case.platform.before_read = switch
    counts = run(case)
    after = case.store.read(case.agent_id)
    assert (after.hosted_session_id, after.abandon) == (other, None)
    assert counts["skipped"] == 1 and counts["abandoned"] == 0
    assert "NATIVE_SESSION_PATROL_RETIRED" not in caplog.text


def test_a_stopping_patrol_writes_no_decision(patrol):
    case = patrol
    before = bind(case)
    case.platform.statuses[case.sid] = "deleted"

    def stop():
        if len(case.platform.reads) == 2:
            case.patrol._stopping = True

    case.platform.before_read = stop
    assert run(case)["unconfirmed"] == 1
    assert len(case.platform.reads) == 2 and case.store.read(case.agent_id) == before


def test_a_long_pass_confirms_every_sid_inside_the_pair_window(patrol, monkeypatch):
    case = patrol
    roster(case, monkeypatch, 100)
    case.platform.read_seconds = 1  # the 100 first reads alone take longer than RETIRED_PAIR_SECONDS
    assert run(case)["abandoned"] == 100
    reads = pairs(case)
    assert len(reads) == 100
    assert all(len(at) == 2 and RETIRED_CONFIRM_SECONDS <= at[1] - at[0] <= RETIRED_PAIR_SECONDS
               for at in reads.values())


def test_a_first_read_older_than_the_pair_window_is_read_again_next_run(patrol, monkeypatch):
    case = patrol
    first, second = roster(case, monkeypatch, 2)

    def slow():  # the next Agent's read outlasts the window (retries, timeouts)
        if len(case.platform.reads) == 2:
            case.platform.now += RETIRED_PAIR_SECONDS + 1

    case.platform.before_read = slow
    counts = run(case)
    assert (counts["unconfirmed"], counts["abandoned"]) == (1, 1)
    assert len(pairs(case)["sid-" + first]) == 1, "a stale first read is not paired with a second one"
    assert case.store.read(first).abandon is None and case.store.read(second).abandon is not None

    case.platform.before_read = None
    assert run(case)["abandoned"] == 1 and case.store.read(first).abandon is not None


def test_a_second_read_returning_after_the_pair_window_decides_nothing(patrol):
    case = patrol
    before = bind(case)
    case.platform.statuses[case.sid] = "deleted"

    def slow():
        if len(case.platform.reads) == 2:
            case.platform.now += RETIRED_PAIR_SECONDS

    case.platform.before_read = slow
    assert run(case)["unconfirmed"] == 1
    assert case.store.read(case.agent_id) == before


def test_reads_are_paced_and_a_run_over_budget_defers_the_rest_to_the_next_run(patrol, monkeypatch, caplog):
    case = patrol
    ids = roster(case, monkeypatch, 10, status="idle")
    # The configured Agents (unbound: no read) come first; each read takes 1 s after a 1 s pace.
    case.platform.read_seconds, case.patrol.budget = 1, 7
    counts = run(case)
    times = [at for _, at in case.platform.reads]
    assert all(later - earlier == 1 + 1 for earlier, later in zip(times, times[1:])), "PATROL_PACE_SECONDS apart"
    assert counts["idle_or_active"] == 4 and counts["deferred"] == 6
    assert "NATIVE_SESSION_PATROL_STOPPED reason=budget deferred=6" in caplog.text

    case.platform.reads.clear()
    case.patrol.budget = 3600
    counts = run(case)
    assert case.platform.reads[0][0] == "sid-" + ids[4], "the next run starts where the last one stopped"
    assert counts["idle_or_active"] == 10 and counts["deferred"] == 0


def test_repeated_unavailable_reads_stop_the_run(patrol, monkeypatch, caplog):
    case = patrol
    roster(case, monkeypatch, 10, status="unavailable")
    counts = run(case)
    assert len(case.platform.reads) == 3 and counts["unread"] == 3 and counts["deferred"] == 7
    assert "NATIVE_SESSION_PATROL_STOPPED reason=unavailable deferred=7" in caplog.text


@pytest.fixture
def own_loop():
    """Made before ``native_api``, whose startup fixture forbids the socket pair a new Windows event loop opens."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def test_the_first_pass_runs_within_an_hour_of_a_start_then_daily(own_loop, monkeypatch):
    """dev restarts several times a day: a first wait of a day would never end, so the first pass comes 5-60 minutes
    after the start (random, so two replicas of a deploy do not read together), then the daily interval."""
    from shared_context import native_session_patrol

    patrol = NativeSessionPatrol(None, enabled=True)
    waits, runs = [], []

    async def sleep(seconds):
        waits.append(seconds)
        patrol._stopping = len(waits) == 3

    monkeypatch.setattr(native_session_patrol.asyncio, "sleep", sleep)
    monkeypatch.setattr(patrol, "run", lambda: runs.append(len(waits)))
    own_loop.run_until_complete(patrol._loop())
    assert 300 <= waits[0] <= 3600 and runs == [1, 2, 3]
    assert all(patrol.interval <= wait <= patrol.interval + patrol.jitter for wait in waits[1:])


@pytest.mark.parametrize("enforced", ["1", None])
def test_the_work_policy_application_registers_the_patrol_and_its_lifespan_runs_it(
        own_loop, native_api, monkeypatch, enforced):
    from shared_context.behavior_server import application

    if enforced is None:
        monkeypatch.delenv("FA_NATIVE_SESSION_GUARD_ENFORCE", raising=False)
    else:
        monkeypatch.setenv("FA_NATIVE_SESSION_GUARD_ENFORCE", enforced)
    composition = native_api.composition
    assert composition.agent_id is None and composition.runtime.work_policy is not None
    # The fixture's synthetic Team has no approved installation history; startup validation is not under test here.
    monkeypatch.setattr(composition, "installation", lambda: None)
    monkeypatch.setattr(composition.runtime, "validate_startup", lambda: None)
    app = application(composition)
    patrol = app.state.native_session_patrol
    assert isinstance(patrol, NativeSessionPatrol) and patrol.composition is composition
    assert patrol.enabled is (enforced == "1")
    with TestClient(app, backend_options={"loop_factory": lambda: own_loop}):
        assert (patrol._task is not None) is patrol.enabled
        assert (patrol._executor is not None) is patrol.enabled
    assert patrol._task is None and patrol._executor is None


def test_an_overlapping_run_is_prevented(patrol):
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "idle"
    reading, release = threading.Event(), threading.Event()

    def hold():
        reading.set()
        assert release.wait(5)

    case.platform.before_read = hold
    results = []
    first = threading.Thread(target=lambda: results.append(case.patrol.run()))
    first.start()
    assert reading.wait(5)
    assert case.patrol.run() is None
    release.set()
    first.join(5)
    assert results[0]["idle_or_active"] == 1 and len(case.platform.reads) == 1


@pytest.mark.parametrize("enforced", [True, False])
def test_the_one_shot_cli_runs_one_pass_with_the_same_rules_and_refuses_with_the_flag_off(
        patrol, monkeypatch, capsys, enforced):
    from contextlib import contextmanager
    import json

    from shared_context import behavior_agent_versions, native_session_patrol

    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "deleted"
    monkeypatch.setattr(case.composition, "installation", lambda: None)

    @contextmanager
    def server(_configuration):
        yield case.composition

    monkeypatch.setattr("shared_context.behavior_server.build_server", server)
    monkeypatch.setattr("shared_context.behavior_server_config.load_configuration", lambda path: path)
    monkeypatch.setattr(native_session_patrol, "NativeSessionPatrol", lambda composition, **kwargs: (
        NativeSessionPatrol(composition, sleep=case.platform.sleep, clock=lambda: case.platform.now, **kwargs)))
    monkeypatch.setenv("FA_NATIVE_SESSION_GUARD_ENFORCE", "1" if enforced else "0")
    with pytest.raises(SystemExit):  # one pass only: --once is required
        behavior_agent_versions.main(["patrol"])
    invocations = len(case.remote.calls)
    assert behavior_agent_versions.main(["patrol", "--once"]) == (0 if enforced else 1)
    output = capsys.readouterr()
    assert len(case.remote.calls) == invocations and case.platform.other == []
    if not enforced:
        assert json.loads(output.err.splitlines()[-1]) == {"state": "failed", "reason": "NATIVE_SESSION_GUARD_DISABLED"}
        assert case.platform.reads == [] and case.store.read(case.agent_id).abandon is None
        return
    summary = json.loads(output.out)
    assert summary["state"] == "ok" and summary["patrol"]["abandoned"] == 1 and summary["patrol"]["handed"] == 0
    assert len(case.platform.reads) == 2 and case.store.read(case.agent_id).abandon is not None


def test_a_decided_agent_is_handed_to_the_background_cut_worker_which_switches_it(patrol, monkeypatch):
    """P6 (the P8 gap): the patrol hands its decision to the cut worker (ADR M1 step 4(b) "背景工作…切走") and calls
    nothing itself; ``NativeSessions.cut`` carries a written decision out (test_native_cut)."""
    from shared_context import native_cut

    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "deleted"
    triggered = []
    monkeypatch.setattr(native_cut.CutDriver, "turn_ended",
                        lambda self, control, agent_id, rid: triggered.append((agent_id, rid)) or True)
    case.patrol.switch = case.patrol.hand_off
    assert run(case)["handed"] == 1
    assert [agent for agent, _ in triggered] == [case.agent_id]
    assert run(case)["handed"] == 1 and len({rid for _, rid in triggered}) == 2  # a new trigger each run
