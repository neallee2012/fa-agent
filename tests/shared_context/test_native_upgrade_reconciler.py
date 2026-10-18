"""ADR-0009 §12 phase 6a: the upgrade reconciler hands (a) incompatible or decided rows and (b) active SIDs to the
background cut worker; it reads ``get_session`` only and writes nothing itself.

The patrol's fixture: the real API composition and binding row; only the platform and the Hosted transport are
offline doubles. Any platform call other than ``get_session`` fails the test."""

import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from shared_context.errors import Problem
from shared_context.native_upgrade_reconciler import RECONCILE_BACKOFF_SECONDS, RECONCILE_SECONDS, NativeUpgradeReconciler
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401  (fixture)
from test_instruction_flow import instruction_models  # noqa: F401  (fixture)
from test_m3_auth import jwk_client, signing_key  # noqa: F401  (fixtures)
from test_native_session_patrol import bind, own_loop, patrol, roster  # noqa: F401  (fixtures)
from test_native_turn_api import native_api  # noqa: F401  (fixture)


def reconciler(case, **options):
    options.setdefault("switch", lambda agent_id: case.handed.append(agent_id) or True)
    return NativeUpgradeReconciler(case.composition, enabled=True, sleep=case.platform.sleep,
                                   clock=lambda: case.platform.now, **options)


@pytest.fixture
def selector(monkeypatch):
    """The endpoint selector (two control-plane reads in production) names 42, so a v1 row on 41 has its cut due."""
    value = SimpleNamespace(version="42", reads=[])
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version",
                        lambda _runtime, target: value.reads.append(target.agent_name) or value.version)
    return value


def crowd(case, value, monkeypatch, n, status):
    """`n` registered Agents, each bound to its own SID with `status`; their source is A's client naming that SID."""
    ids = roster(case, monkeypatch, n, status)
    source = value.source
    monkeypatch.setattr(value, "source", lambda _agent_id, row: source(case.agent_id, row))
    return ids


def run(case, value):
    invocations = len(case.remote.calls)
    counts = value.run()
    assert len(case.remote.calls) == invocations, "the reconciler never invokes a session"
    assert case.platform.other == [], "the reconciler calls nothing but get_session"
    return counts


@pytest.mark.parametrize("status", ["idle", "failed", "deleted", "missing"])
def test_a_compatible_idle_or_retired_agent_is_left_alone(patrol, selector, status):
    case = patrol
    before = bind(case)
    case.platform.statuses[case.sid] = status
    counts = run(case, reconciler(case))
    assert counts["left"] == 1 and counts["handed"] == 0 and case.handed == []
    assert [sid for sid, _ in case.platform.reads] == [case.sid]
    assert case.store.read(case.agent_id) == before


@pytest.mark.parametrize("decided", [False, True])
def test_an_incompatible_or_decided_agent_is_handed_to_cut_without_a_platform_read(patrol, decided):
    case = patrol
    if decided:
        bind(case)
        case.store.decide_abandon(case.agent_id, hosted_session_id=case.sid, basis="retired",
                                  source_status="deleted", created_agent_version="41")
    else:
        bind(case, profile="incompatible")
    before = case.store.read(case.agent_id)
    counts = run(case, reconciler(case))
    assert counts["incompatible"] == 1 and counts["handed"] == 1 and case.handed == [case.agent_id]
    assert case.platform.reads == [] and case.store.read(case.agent_id) == before


def test_an_active_sid_is_handed_once_per_retry_window(patrol, selector):
    case = patrol
    before = bind(case)
    case.platform.statuses[case.sid] = "active"
    value = reconciler(case)
    assert run(case, value)["active"] == 1 and case.handed == [case.agent_id]
    case.platform.reads.clear()
    assert run(case, value)["recent"] == 1, "the last attempt time dedupes the next pass"
    assert case.platform.reads == [] and case.handed == [case.agent_id]
    case.platform.now += value.retry
    assert run(case, value)["handed"] == 1 and case.handed == [case.agent_id] * 2
    assert case.store.read(case.agent_id) == before


def test_a_day_of_passes_reads_each_agent_once_and_get_session_only_when_its_cut_is_due(patrol, selector, monkeypatch):
    """96 passes 15 minutes apart. Nothing due (V_rec = selector): one selector read per Agent per day
    and no ``get_session``; due but idle: one of each; a binding row that changes is read at the next pass."""
    case = patrol
    value = reconciler(case)
    ids = crowd(case, value, monkeypatch, 3, "idle")

    def day():
        selected, reads, results = len(selector.reads), len(case.platform.reads), []
        for _ in range(RECONCILE_BACKOFF_SECONDS // RECONCILE_SECONDS):
            results.append(run(case, value))
            case.platform.now += RECONCILE_SECONDS
        return len(selector.reads) - selected, len(case.platform.reads) - reads, results

    selector.version = "41"
    selected, reads, results = day()
    assert (selected, reads) == (3, 0) and results[0]["current"] == 3
    assert all(counts["recent"] == 3 for counts in results[1:])
    selector.version = "42"
    selected, reads, results = day()
    assert (selected, reads) == (3, 3) and results[0]["left"] == 3 and case.handed == []
    case.store.decide_abandon(ids[0], hosted_session_id="sid-" + ids[0], basis="retired", source_status="deleted",
                              created_agent_version="41")
    case.platform.now -= RECONCILE_SECONDS  # the day's last pass again, still inside the others' backoff
    counts = run(case, value)
    assert (counts["incompatible"], counts["recent"], counts["handed"]) == (1, 2, 1) and case.handed == [ids[0]]


def test_a_selector_that_is_not_ready_is_read_once_a_day_until_the_row_moves(patrol, monkeypatch):
    """The selected version is "not ready" (a Problem, not ``SelectorUnreadable``): one selector read per day, not per pass."""
    case = patrol
    bind(case)
    reads = []

    def not_ready(_runtime, target):
        reads.append(target.agent_name)
        raise Problem(503, "AGENT_VERSION_FOLLOWING", "OFFLINE the selected version is not ready")

    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", not_ready)
    value = reconciler(case)
    passes = RECONCILE_BACKOFF_SECONDS // RECONCILE_SECONDS
    for day in (1, 2):
        results = []
        for _ in range(passes):
            results.append(run(case, value))
            case.platform.now += RECONCILE_SECONDS
        assert len(reads) == day and results[0]["unread"] == 1
        assert all(counts["recent"] == 1 and counts["unread"] == 0 for counts in results[1:])
    assert case.platform.reads == [] and case.handed == []
    case.store.decide_abandon(case.agent_id, hosted_session_id=case.sid, basis="retired", source_status="deleted",
                              created_agent_version="41")  # a changed row is read again at the next pass
    assert run(case, value)["incompatible"] == 1 and case.handed == [case.agent_id]


@pytest.mark.parametrize(("outcomes", "read"), [
    (["fail"] * 6, 3),  # three unreadable selectors in a row stop the pass
    (["fail", "fail", "41", "fail", "41", "41"], 4),  # a readable selector (nothing due) does not reset the count
    (["fail", "fail", "42", "fail", "fail", "41"], 6)])  # a get_session answer does (idle, no stop)
def test_only_a_get_session_answer_resets_the_unavailable_count(patrol, monkeypatch, outcomes, read):
    from shared_context.behavior_agent_versions import SelectorUnreadable

    case = patrol
    value = reconciler(case)
    crowd(case, value, monkeypatch, 6, "idle")
    answers = iter(outcomes)

    def selector(_runtime, _target):
        answer = next(answers)
        if answer == "fail":
            raise SelectorUnreadable(503, "AGENT_VERSION_FOLLOWING", "OFFLINE the selector is unreadable")
        return answer

    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", selector)
    assert run(case, value)["deferred"] == 6 - read


def test_the_attempt_memory_forgets_agents_no_longer_served_and_outcomes_past_their_window(patrol, selector,
                                                                                            monkeypatch):
    case = patrol
    value = reconciler(case)
    ids = crowd(case, value, monkeypatch, 3, "idle")
    run(case, value)
    assert set(value._attempted) == set(ids)
    monkeypatch.setattr("shared_context.behavior_team_registry.registered_agent_ids", lambda _store, _config: ids[:2])
    run(case, value)
    assert set(value._attempted) == set(ids[:2]), "an Agent that is not served any more is dropped"
    case.platform.now += value.backoff
    case.store.repository.data.clear()  # no rows any more: nothing re-adds them
    run(case, value)
    assert value._attempted == {}, "outcomes past their window are dropped"


def test_two_passes_in_one_process_queue_one_cut_attempt_and_write_nothing(patrol, monkeypatch):
    """Two reconcilers on one composition at once reach its one cut worker (``CutDriver``), whose per-Agent
    ``_running`` set runs one attempt and refuses the other; neither pass writes the binding row or calls a session.

    This is not a cross-replica test: two API replicas have a worker each, and what keeps them apart is inside ``cut``.
    test_native_cut.py covers it: ``test_two_replicas_cut_the_same_rid_and_only_one_drives`` (the same deterministic
    destination SID and ``cut_request_id``, then the ``record_upgrade`` CAS: the second replica gets ``fence_live``)
    and ``test_a_turn_admitted_after_the_probe_makes_the_cut_busy_and_its_files_still_move`` (the probe's E0 as
    ``expected_etag``: a turn admitted after it makes the cut ``busy``)."""
    from shared_context import native_cut

    case = patrol
    before = bind(case, profile="incompatible")
    entered, release, attempts = threading.Event(), threading.Event(), []

    def attempt(self, control, agent_id):
        attempts.append(agent_id)
        entered.set()
        assert release.wait(5)
        with self._lock:
            self._running.discard(agent_id)

    monkeypatch.setattr(native_cut.CutDriver, "_attempt", attempt)
    first, second = (NativeUpgradeReconciler(case.composition, enabled=True, sleep=case.platform.sleep)
                     for _ in range(2))
    results = []
    threads = [threading.Thread(target=lambda value=value: results.append(run(case, value)))
               for value in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert entered.wait(5)
    assert sorted(counts["handed"] for counts in results) == [0, 1]
    release.set()
    native_cut.close_cut_drivers(case.composition)
    assert attempts == [case.agent_id], "one cut attempt for two replicas"
    assert case.store.read(case.agent_id) == before and case.platform.reads == []


@pytest.mark.parametrize(("reconcile", "enforce", "enabled"), [
    ("1", "1", True), (None, "1", False), ("1", None, False), ("yes", "1", False)])
def test_the_flags_off_mean_no_task(own_loop, native_api, monkeypatch, reconcile, enforce, enabled):
    from shared_context.behavior_server import application

    for name, value in (("FA_NATIVE_UPGRADE_RECONCILER", reconcile), ("FA_NATIVE_SESSION_GUARD_ENFORCE", enforce)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    composition = native_api.composition
    monkeypatch.setattr(composition, "installation", lambda: None)
    monkeypatch.setattr(composition.runtime, "validate_startup", lambda: None)
    app = application(composition)
    value = app.state.native_upgrade_reconciler
    assert isinstance(value, NativeUpgradeReconciler) and value.enabled is enabled
    with TestClient(app, backend_options={"loop_factory": lambda: own_loop}):
        assert (value._task is not None) is enabled and (value._executor is not None) is enabled
    assert value._task is None and value._executor is None


def test_an_unread_sid_is_counted_and_read_again_at_the_next_pass(patrol, selector):
    case = patrol
    before = bind(case)
    case.platform.statuses[case.sid] = "unavailable"  # a 503 after azure-core's retries
    value = reconciler(case)
    assert [run(case, value)["unread"] for _ in range(2)] == [1, 1]
    assert [sid for sid, _ in case.platform.reads] == [case.sid] * 2 and case.handed == []
    assert case.agent_id not in value._attempted and case.store.read(case.agent_id) == before


def test_a_failed_hand_off_is_logged_and_the_next_agent_is_still_handed(patrol, selector, monkeypatch, caplog):
    case = patrol

    def switch(agent_id):
        if agent_id == ids[0]:
            raise RuntimeError("the cut worker is gone")
        return case.handed.append(agent_id) or True

    value = reconciler(case, switch=switch)
    ids = crowd(case, value, monkeypatch, 2, "active")
    counts = run(case, value)
    assert (counts["active"], counts["handed"]) == (2, 1) and case.handed == [ids[1]]
    assert "NATIVE_UPGRADE_RECONCILER_UNHANDED agent=agent-00 code=RuntimeError" in caplog.text
    assert set(value._attempted) == set(ids), "a failed hand-off waits for the retry window like a handed one"


@pytest.mark.parametrize("reason", ["unavailable", "budget"])
def test_a_stopped_pass_defers_the_rest_and_the_next_pass_starts_there(patrol, selector, monkeypatch, caplog, reason):
    case = patrol
    value = reconciler(case, budget=10)
    ids = crowd(case, value, monkeypatch, 5, "unavailable" if reason == "unavailable" else "idle")
    if reason == "budget":
        case.platform.read_seconds = 11  # the first get_session already passes the budget
    reached = 3 if reason == "unavailable" else 1  # three unavailable reads in a row, or one slow one
    counts = run(case, value)
    # The configured Agent A (unbound here) comes first in the roster, then the five registered ones.
    assert counts["deferred"] == 5 - reached
    assert f"NATIVE_UPGRADE_RECONCILER_STOPPED reason={reason} deferred={5 - reached}" in caplog.text
    first = len(case.platform.reads)
    case.platform.read_seconds = 0
    run(case, value)
    assert case.platform.reads[first][0] == "sid-" + ids[reached], "the next pass starts at the first deferred Agent"


def test_the_loop_waits_a_jittered_interval_before_every_pass_and_survives_a_failed_one(monkeypatch, caplog):
    import asyncio

    from shared_context import native_session_patrol

    value = NativeUpgradeReconciler(None, enabled=True, interval=600, jitter=0)
    waits, passes = [], []

    async def sleep(seconds):
        waits.append(seconds)
        value._stopping = len(waits) == 2

    def run_pass():
        passes.append(len(waits))
        if len(passes) == 1:
            raise RuntimeError("one pass failed")

    monkeypatch.setattr(native_session_patrol, "asyncio",
                        SimpleNamespace(sleep=sleep, get_running_loop=asyncio.get_running_loop))
    monkeypatch.setattr(value, "run", run_pass)
    asyncio.run(value._loop())
    assert waits == [600, 600] and passes == [1, 2], "never at the start itself: the interval comes first"
    assert "NATIVE_UPGRADE_RECONCILER_FAILED code=RuntimeError" in caplog.text
