"""ADR-0009 §12 phase 6a: the upgrade reconciler hands (a) incompatible or decided rows and (b) active SIDs to the
background cut worker; it reads ``get_session`` only and writes nothing itself.

The patrol's fixture: the real API composition and binding row; only the platform and the Hosted transport are
offline doubles. Any platform call other than ``get_session`` fails the test."""

import threading
from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from shared_context.behavior_native_turn_wire import NativeEnvironmentNotice, NativeEnvironmentView
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


# --- phase 6a (c): the 6-hour "有新版本" status-line note -------------------------------------------------------------

def defer(case, agent_id=None, sid=None, *, hours, target="42"):
    """U0 recorded ``target`` as due ``hours`` ago (the store's own clock, moved back for the write)."""
    store, clock = case.store, case.store.clock
    store.clock = lambda: clock() - timedelta(hours=hours)
    try:
        return store.record_due(agent_id or case.agent_id, hosted_session_id=sid or case.sid, target_version=target)
    finally:
        store.clock = clock


def table_calls(case, monkeypatch):
    """Counts the Table reads and writes the binding row sees from now on."""
    repository, calls = case.store.repository, {"reads": 0, "writes": 0}
    get, replace = repository.get, repository.replace
    monkeypatch.setattr(repository, "get", lambda *a, **k: calls.update(reads=calls["reads"] + 1) or get(*a, **k))
    monkeypatch.setattr(repository, "replace",
                        lambda *a, **k: calls.update(writes=calls["writes"] + 1) or replace(*a, **k))
    return calls


def test_a_deferral_over_six_hours_sets_the_note_once_and_a_rerun_does_not(patrol, selector, monkeypatch):
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "idle"  # due but idle: left alone, as in (b)
    defer(case, hours=7)
    value = reconciler(case, notes=True)
    calls = table_calls(case, monkeypatch)
    first = run(case, value)
    row = case.store.read(case.agent_id)
    assert first["noted"] == 1 and row.idle_upgrade.waiting == "pending"
    assert row.waiting_version() == "42"
    assert row.notice() is None, "Teams (and an old bridge) never carry it"
    assert (case.handed, [sid for sid, _ in case.platform.reads]) == ([], [case.sid]), "no wake, no cut"
    case.platform.now += value.backoff  # the backoff ends: the row is read and judged again
    calls.update(reads=0, writes=0)
    assert run(case, value)["noted"] == 0 and calls["writes"] == 0
    # The owner's workbench shows it once; a later pass does not make it again for the same target.
    case.store.clear_waiting(case.agent_id, "42")
    assert case.store.read(case.agent_id).idle_upgrade.waiting == "shown"
    case.platform.now += value.backoff
    calls.update(reads=0, writes=0)
    assert run(case, value)["noted"] == 0 and calls["writes"] == 0
    assert case.store.read(case.agent_id).waiting_version() is None


def test_a_deferral_under_six_hours_does_nothing(patrol, selector, monkeypatch):
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "idle"  # due but idle: left alone, as in (b)
    defer(case, hours=5)
    before = case.store.read(case.agent_id)
    calls = table_calls(case, monkeypatch)
    assert run(case, reconciler(case, notes=True))["noted"] == 0
    assert calls["writes"] == 0 and case.store.read(case.agent_id) == before


def test_the_note_is_made_when_the_six_hours_pass_inside_the_backoff_of_an_unchanged_row(patrol, selector, monkeypatch):
    """The Agent was read (and left alone) while under 6 hours; its row did not change, so the backoff alone would skip
    it for a day: the 6-hour check comes before the backoff."""
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "idle"  # due but idle: left alone, as in (b)
    defer(case, hours=5)
    value = reconciler(case, notes=True)
    assert run(case, value)["noted"] == 0 and run(case, value)["recent"] == 1
    later = case.store.clock() + timedelta(hours=2)
    monkeypatch.setattr(case.composition.runtime, "_now", lambda: later, raising=False)
    assert run(case, value)["noted"] == 1

@pytest.mark.parametrize(("reconcile", "guard", "idle", "noted"), [
    ("1", "1", "1", True),
    ("1", "1", None, False),  # the reconciler runs, but (c) is the idle upgrade's
    ("1", "1", "0", False),
    (None, "1", "1", False),  # the reconciler itself is off: no task
    ("1", None, None, False)])  # the guard is off: no task
def test_with_any_flag_off_nothing_is_noted(patrol, selector, monkeypatch, reconcile, guard, idle, noted):
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "idle"  # due but idle: left alone, as in (b)
    defer(case, hours=7)
    before = case.store.read(case.agent_id)
    for name, value in (("FA_NATIVE_UPGRADE_RECONCILER", reconcile), ("FA_NATIVE_SESSION_GUARD_ENFORCE", guard),
                        ("FA_NATIVE_IDLE_UPGRADE", idle)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    value = NativeUpgradeReconciler(case.composition, sleep=case.platform.sleep, clock=lambda: case.platform.now,
                                    switch=lambda agent_id: case.handed.append(agent_id) or True)
    assert value.enabled is (reconcile == "1" and guard == "1") and value.notes is (guard == "1" and idle == "1")
    if value.enabled:
        assert run(case, value)["noted"] == int(noted)
    assert (case.store.read(case.agent_id) == before) is (not noted)


def test_a_new_target_or_the_switch_ends_the_note(patrol, selector):
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "idle"  # due but idle: left alone, as in (b)
    defer(case, hours=7)
    run(case, reconciler(case, notes=True))
    assert case.store.read(case.agent_id).waiting_version() == "42"
    moved = case.store.record_due(case.agent_id, hosted_session_id=case.sid, target_version="43")
    assert moved.idle_upgrade.waiting is None and moved.waiting_version() is None, "the target changed"
    defer(case, hours=7, target="43")  # no-op: the target holds its start
    run_again = reconciler(case, notes=True)
    assert run(case, run_again)["noted"] == 0, "the new target started its own 6 hours just now"
    case.store.record_switch(case.agent_id, NativeEnvironmentView(
        agent_id=case.agent_id, request_id="follow-switch", state="ready", previous_hosted_session_id=case.sid,
        previous_agent_version="41", hosted_session_id="fa-native-next", hosted_agent_version="43",
        files_migrated=False, files_skip_request_id="skip"))
    row = case.store.read(case.agent_id)
    assert row.idle_upgrade is None and row.waiting_version() is None and row.notice() == NativeEnvironmentNotice(
        kind="environment_switched_without_files")


@pytest.mark.parametrize("project", [False, True])
def test_a_vetoed_target_gets_no_waiting_note(patrol, selector, project):
    """4b-4: after U3's veto "沒有更新" said the Agent stays; a later "有新版本 v42" for that target would contradict
    it. This Agent's own veto or the project row: U0 never moves the Agent there."""
    from shared_context.native_version_veto import veto_store

    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "idle"  # due but idle: left alone, as in (b)
    defer(case, hours=7)
    value = reconciler(case, notes=True)
    if project:
        name = value.source(case.agent_id, case.store.read(case.agent_id)).target.agent_name
        veto_store(case.composition).veto(name, "42")
    else:
        case.store.record_veto(case.agent_id, hosted_session_id=case.sid, agent_version="42", notice=True)
    before = case.store.read(case.agent_id)
    assert run(case, value)["noted"] == 0
    row = case.store.read(case.agent_id)
    assert row == before and row.idle_upgrade.waiting is None and row.waiting_version() is None


def test_a_note_made_before_this_agents_veto_is_not_shown_after_it(patrol, selector):
    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "idle"  # due but idle: left alone, as in (b)
    defer(case, hours=7)
    run(case, reconciler(case, notes=True))
    assert case.store.read(case.agent_id).waiting_version() == "42"
    vetoed = case.store.record_veto(case.agent_id, hosted_session_id=case.sid, agent_version="42", notice=True)
    assert vetoed.idle_upgrade.waiting == "pending" and vetoed.waiting_version() is None


def test_a_note_made_before_the_project_veto_is_cleared_by_the_next_pass(patrol, selector):
    """#912 review P3: the project row is not on the binding row, so ``waiting_version()`` kept answering 42."""
    from shared_context.native_version_veto import veto_store

    case = patrol
    bind(case)
    case.platform.statuses[case.sid] = "idle"  # due but idle: left alone, as in (b)
    defer(case, hours=7)
    value = reconciler(case, notes=True)
    run(case, value)
    assert case.store.read(case.agent_id).waiting_version() == "42"
    name = value.source(case.agent_id, case.store.read(case.agent_id)).target.agent_name
    veto_store(case.composition).veto(name, "42")
    case.platform.now += value.backoff
    assert run(case, value)["noted"] == 0
    row = case.store.read(case.agent_id)
    assert row.idle_upgrade.waiting == "shown" and row.waiting_version() is None


def test_a_pass_reads_one_row_per_agent_and_a_noted_agent_costs_two_more_reads_and_one_write(patrol, selector, monkeypatch):
    case = patrol
    value = reconciler(case, notes=True)
    ids = crowd(case, value, monkeypatch, 3, "idle")
    for agent_id in ids[:2]:
        defer(case, agent_id, "sid-" + agent_id, hours=7)
    calls = table_calls(case, monkeypatch)
    counts = run(case, value)
    # Every served Agent has its row read once (the unbound configured ones too); each of the two noted ones has its
    # project veto row read, is re-read by its CAS and written once. Nothing else is read.
    agents = counts["agents"]
    assert counts["noted"] == 2 and agents == counts["unbound"] + 3
    assert (calls["reads"], calls["writes"]) == (agents + 4, 2)
    assert len(case.platform.reads) == 3 and case.handed == [], "the same get_session reads as without (c)"
    case.platform.now += RECONCILE_SECONDS
    calls.update(reads=0, writes=0)
    counts = run(case, value)
    # Each of the two pending notes reads the project veto row once more (a veto since then clears it), writes nothing.
    assert (counts["noted"], counts["recent"]) == (0, 3) and (calls["reads"], calls["writes"]) == (agents + 2, 0)