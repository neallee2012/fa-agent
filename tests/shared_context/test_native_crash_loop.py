"""ADR-0009 phase 4d (API): the crash-loop judgement after a switch, the vetoes and the rollback through ``cut``.

The Agent has just switched from 41 to 42: the binding row names the 42 SID (``file_api``'s source, a real Hosted
runtime) with the 41 generation-0 SID as its previous one and the switch time, and the selector names 42. Crashes are
recorded by the source runtime's own ``crashes:<SID>`` code (#888). The rollback destination is the 41 SID derived
from the current one (``file_api``'s fresh destination, the second real runtime); the original 41 SID has no runtime
at all, so any call to it shows up in the counts.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from shared_context import behavior_native_sessions
from shared_context.behavior_native_sessions import native_session_id
from shared_context.behavior_native_turn_api import agent_client
from shared_context.native_binding import NativeBindingIdleUpgrade, NativeBindingSession
from shared_context.native_version_veto import veto_store
import test_native_abandon_source as abandon
from test_native_abandon_source import binding_rows  # noqa: F401
from test_native_cut import cut, replace_retried, sessions  # noqa: F401
from test_native_files_api import (  # noqa: F401
    file_api, file_loops, instruction_models, jwk_client, local_env, native_api, signing_key, synthetic_standing_v1,
)

ROLLBACK = {"lane": "foundry-native-v1", "source_version": "42", "target_version": "41", "fresh": True}
pytestmark = pytest.mark.parametrize("file_api", [ROLLBACK], indirect=True)


def switched(fixture, monkeypatch, *, minutes_ago=1, idle=True, backfilled=False, notice=None, vetoed=()):
    """The row after the 41 -> 42 switch ``minutes_ago``; returns the original 41 SID and a list of metadata reads.
    ``backfilled``: the first switch's ``record_unfinished`` also listed an unfinished receipt's SID, version unknown,
    after the old one. ``notice``: the slot's switch notice of the 41 -> 42 switch, not yet delivered. ``vetoed``: this
    Agent's own vetoes, kept by that switch."""
    abandon.platform(fixture, monkeypatch)
    for name in ("behavior_agent_versions", "behavior_native_responses_api"):
        monkeypatch.setattr(f"shared_context.{name}.current_hosted_version", lambda *_a, **_k: "42")
    abandon.claim(fixture)
    composition = fixture.case.composition
    old = native_session_id(fixture.source, composition.config.identity.managed_identity_client_id, version="41")
    store, row = abandon.store(fixture), abandon.row(fixture)
    record = store.repository.get(store.partition_key, store.row_key(row.agent_id))
    store.repository.replace(store.partition_key, store.row_key(row.agent_id), row.model_copy(update={
        "previous_sessions": (NativeBindingSession(hosted_session_id=old, agent_version="41"),) + ((
            NativeBindingSession(hosted_session_id="unfinished-receipt-sid", agent_version=None),) if backfilled else ()),
        "idle_upgrade": NativeBindingIdleUpgrade(
            switched_at=(store.clock() - timedelta(minutes=minutes_ago)).isoformat(), vetoed_versions=vetoed),
        "switch_notice": notice}).model_dump(mode="json"),
        record.etag)
    sessions(fixture).idle_upgrade = idle
    metadata, read = [], behavior_native_sessions._session_metadata

    def counted(client, **options):
        metadata.append(client.target.session_id)
        return read(client, **options)

    monkeypatch.setattr(behavior_native_sessions, "_session_metadata", counted)
    return old, metadata


def aged(fixture, minutes_ago=None, *, at=None):
    """The row's switch time moved to ``minutes_ago`` by the store's (fixed) clock, or to ``at``; all else (its
    vetoes) kept."""
    store, row = abandon.store(fixture), abandon.row(fixture)
    at = at or store.clock() - timedelta(minutes=minutes_ago)
    record = store.repository.get(store.partition_key, store.row_key(row.agent_id))
    store.repository.replace(store.partition_key, store.row_key(row.agent_id), row.model_copy(update={
        "idle_upgrade": row.idle_upgrade.model_copy(update={"switched_at": at.isoformat()})}).model_dump(mode="json"),
        record.etag)


def crash(fixture, rid, cid):
    """One crash of the source SID, appended by Hosted's own code (a RID already there counts once)."""
    fixture.hosted.portal.call(fixture.original._record_crash, SimpleNamespace(request_id=rid, conversation_id=cid))


def reentry(fixture, rid, cid, *, deferred):
    """A recovered re-entry of a started run; ``deferred``: its lifetime ended by FA's graceful deferral first."""
    token = "run-" + rid
    if deferred:
        fixture.hosted.portal.call(fixture.original._mark_deferred, rid, token)
    original = SimpleNamespace(value=SimpleNamespace(run_token=token, receipt=SimpleNamespace(
        status="started", request_id=rid, conversation_id=cid)))
    fixture.hosted.portal.call(fixture.original._count_unmarked_reentry, original)


def veto(fixture, version="42"):
    composition = fixture.case.composition
    name = agent_client(composition, composition.runtime.work_policy).target.agent_name
    return veto_store(composition).read(name, version)


def untouched(fixture, before):
    """Nothing decided: no veto, the row as it was, still on 42."""
    assert veto(fixture) is None
    assert abandon.row(fixture) == before and before.hosted_session_id == fixture.source.session_id


def rolled_back(fixture, outcome):
    assert outcome[:2] == ("crash_loop", "ready")
    row = abandon.row(fixture)
    assert (row.hosted_session_id, row.created_agent_version) == (fixture.destination.session_id, "41")
    assert row.earlier(fixture.source.session_id)  # §7a (e): its unfinished receipts read as the earlier version's
    assert veto(fixture).reason == "crash_loop" and row.idle_upgrade.vetoed_versions == ("42",)
    return row


def test_three_distinct_crashes_veto_the_version_roll_back_and_notice_it_once(file_api, monkeypatch):
    fixture = file_api
    switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    row = rolled_back(fixture, cut(fixture))
    assert row.idle_upgrade.switched_at is None and row.idle_upgrade.target_version is None  # no window on 41
    notice = row.notice()
    assert (row.switch_notice, notice.kind) == ("rolled_back", "environment_rolled_back")
    abandon.store(fixture).clear_notice(row.agent_id, notice)  # an owner response delivered it
    # The next turn end: 42 is vetoed for this Agent, so nothing moves and no second 已退回 is written.
    assert cut(fixture) == ("vetoed", "agent", None)
    after = abandon.row(fixture)
    assert after.notice() is None and after.hosted_session_id == fixture.destination.session_id


def test_two_crashes_decide_nothing_until_a_third_conversation_crashes(file_api, monkeypatch):
    fixture = file_api
    switched(fixture, monkeypatch)
    crash(fixture, "rid-1", "cid-1")
    crash(fixture, "rid-2", "cid-2")
    before = abandon.row(fixture)
    assert cut(fixture) == ("current", None, None)
    untouched(fixture, before)
    crash(fixture, "rid-3", "cid-3")
    rolled_back(fixture, cut(fixture))


def test_a_rid_retried_counts_once_and_one_conversation_is_not_three(file_api, monkeypatch):
    fixture = file_api
    switched(fixture, monkeypatch)
    for _ in range(3):
        crash(fixture, "rid-1", "cid-1")  # the same RID retried
    crash(fixture, "rid-2", "cid-2")
    crash(fixture, "rid-3", "cid-2")  # three RIDs, but only two conversations
    before = abandon.row(fixture)
    assert cut(fixture) == ("current", None, None)
    untouched(fixture, before)
    crash(fixture, "rid-4", "cid-4")
    rolled_back(fixture, cut(fixture))


def test_a_deferred_exit_does_not_count(file_api, monkeypatch):
    fixture = file_api
    switched(fixture, monkeypatch)
    crash(fixture, "rid-1", "cid-1")
    crash(fixture, "rid-2", "cid-2")
    reentry(fixture, "rid-3", "cid-3", deferred=True)  # a platform shutdown: FA deferred the run first
    before = abandon.row(fixture)
    assert cut(fixture) == ("current", None, None)
    untouched(fixture, before)
    reentry(fixture, "rid-4", "cid-4", deferred=False)  # no marker: the lifetime crashed
    rolled_back(fixture, cut(fixture))


def test_after_the_thirty_minute_window_nothing_is_read_or_judged(file_api, monkeypatch):
    fixture = file_api
    _, metadata = switched(fixture, monkeypatch, minutes_ago=31)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    before, calls = abandon.row(fixture), len(fixture.control_versions)
    assert cut(fixture) == ("current", None, None)
    assert metadata == [] and len(fixture.control_versions) == calls
    untouched(fixture, before)
    switched(fixture, monkeypatch, minutes_ago=29)
    rolled_back(fixture, cut(fixture))


def test_without_the_idle_flag_nothing_is_read(file_api, monkeypatch):
    fixture = file_api
    _, metadata = switched(fixture, monkeypatch, idle=False)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    before, calls = abandon.row(fixture), len(fixture.control_versions)
    assert cut(fixture) == ("current", None, None)
    assert metadata == [] and len(fixture.control_versions) == calls
    untouched(fixture, before)
    sessions(fixture).idle_upgrade = True
    assert cut(fixture)[:2] == ("crash_loop", "ready")
    assert metadata and fixture.control_versions[calls][0] == fixture.source.session_id  # the reads it skipped


def test_the_rollback_never_calls_the_original_old_sid(file_api, monkeypatch):
    fixture = file_api
    old, metadata = switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    calls, stops = len(fixture.control_versions), len(fixture.files.calls)
    rolled_back(fixture, cut(fixture))
    assert fixture.destination.session_id != old
    invoked = {sid for sid, _version, _operation in fixture.control_versions[calls:]}
    assert invoked == {fixture.source.session_id, fixture.destination.session_id}
    assert old not in metadata and all(old not in call for call in fixture.files.calls[stops:])
    assert ("create", fixture.destination.session_id) in fixture.files.calls[stops:]


def test_a_backfilled_sid_of_unknown_version_is_never_the_rollback_target(file_api, monkeypatch):
    # The first 41 -> 42 switch's record_unfinished lists an unfinished receipt's SID (version None) after the 41 SID.
    fixture = file_api
    switched(fixture, monkeypatch, backfilled=True)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    rolled_back(fixture, cut(fixture))


def test_a_decided_rollback_that_was_busy_is_finished_after_the_window(file_api, monkeypatch):
    from shared_context.errors import Problem

    fixture = file_api
    switched(fixture, monkeypatch, minutes_ago=25)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    profile, failed = behavior_native_sessions._native_profile, []

    def unread_once(client):
        if not failed:
            failed.append(client.target.agent_version)
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE the definition read failed once")
        return profile(client)

    monkeypatch.setattr(behavior_native_sessions, "_native_profile", unread_once)
    assert cut(fixture)[:2] == ("crash_loop", "target_unread:NATIVE_STATE_STORE_UNKNOWN")
    assert failed == ["41"] and abandon.row(fixture).hosted_session_id == fixture.source.session_id
    assert abandon.row(fixture).idle_upgrade.vetoed_versions == ("42",)  # decided, not yet done
    aged(fixture, 31)  # the window closed before the next turn end
    rolled_back(fixture, cut(fixture))


def test_the_rollbacks_own_switch_opens_no_window_so_41_is_never_judged(file_api, monkeypatch):
    fixture = file_api
    _, metadata = switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    rolled_back(fixture, cut(fixture))
    reads = len(metadata)
    # The next turn end on 41: no crash-loop read of the 41 SID (a judgement could veto 41 project-wide and leave the
    # Agent with no version to run), only U0's refusal of the vetoed 42.
    assert cut(fixture) == ("vetoed", "agent", None)
    assert len(metadata) == reads and veto(fixture, "41") is None


def test_a_rollback_fence_finished_by_a_later_converge_still_says_rolled_back(file_api, monkeypatch):
    from test_native_cut import Crash

    fixture = file_api
    switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    stopping = behavior_native_sessions._CutFence.stopping

    def crash_after_marker(self):
        stopping(self)
        raise Crash()

    monkeypatch.setattr(behavior_native_sessions._CutFence, "stopping", crash_after_marker)
    with pytest.raises(Crash):  # the API died inside the rollback's fence, after FA's stop was marked
        cut(fixture)
    fence = abandon.row(fixture).upgrade
    assert fence.rollback and fence.stop_started and fence.agent_version == "41"
    monkeypatch.setattr(behavior_native_sessions._CutFence, "stopping", stopping)
    monkeypatch.setattr(behavior_native_sessions, "CUT_FENCE_SECONDS", -1)  # a marked fence is never withdrawn
    assert cut(fixture) == ("ready", None, None)  # the next turn end's _converge commits it, no rollback context
    row = rolled_back(fixture, ("crash_loop", "ready"))
    assert row.switch_notice == "rolled_back" and row.idle_upgrade.switched_at is None


def test_crashes_from_before_the_switch_do_not_count(file_api, monkeypatch):
    fixture = file_api
    switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    # The switch came after all three (Hosted keeps a SID's crashes for a day): Hosted stamps them with the wall
    # clock, the binding store runs on the test's fixed one, so the window stays open.
    aged(fixture, at=datetime.now(timezone.utc))
    before = abandon.row(fixture)
    assert cut(fixture) == ("current", None, None)
    untouched(fixture, before)


def test_an_idle_current_sid_is_read_once_and_never_woken_for_its_crashes(file_api, monkeypatch):
    fixture = file_api
    _, metadata = switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    fixture.files.stopped.add(fixture.source.session_id)  # the platform reclaimed it after the crashes
    before, calls = abandon.row(fixture), len(fixture.control_versions)
    assert cut(fixture) == ("current", None, None)
    assert metadata == [fixture.source.session_id] and len(fixture.control_versions) == calls  # §10: no state read
    untouched(fixture, before)


@pytest.mark.parametrize("case", ["rollback_moved_none", "earlier_switch_moved_none"])
def test_rolled_back_keeps_the_no_files_sentence_visible(file_api, monkeypatch, case):
    from shared_context.native_teams_relay import NOTICE_TEXT, notice_text
    from test_native_cut import _remove

    fixture = file_api
    switched(fixture, monkeypatch, notice="without_files" if case == "earlier_switch_moved_none" else None)
    if case == "rollback_moved_none":
        for child in fixture.workspace.root.iterdir():
            _remove(child)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    row = rolled_back(fixture, cut(fixture))
    assert (row.last_switch.files_migrated is True) is (case == "earlier_switch_moved_none")  # None: nothing to move
    notice = row.notice()
    assert row.switch_notice == "rolled_back_without_files"
    assert notice.model_dump(mode="json") == {"kind": "environment_rolled_back", "without_files": True}
    assert notice_text(notice) == (
        "助理已退回先前的版本；剛才中斷的訊息請再送一次。" + NOTICE_TEXT["environment_switched_without_files"])
    assert notice_text(row.model_copy(update={"switch_notice": "rolled_back"}).notice()) == (
        "助理已退回先前的版本；剛才中斷的訊息請再送一次。")
    assert abandon.store(fixture).clear_notice(row.agent_id, notice).notice() is None  # delivered once, then cleared


def project_veto(fixture, version):
    """A project-level veto row of ``version`` (another Agent of the same Hosted agent judged it)."""
    composition = fixture.case.composition
    name = agent_client(composition, composition.runtime.work_policy).target.agent_name
    return veto_store(composition).veto(name, version)


def test_a_project_veto_on_the_earlier_version_does_not_block_the_rollback(file_api, monkeypatch):
    # Coordinator 2026-10-19: 42's crash-loop on this Agent outweighs another Agent's veto of 41.
    fixture = file_api
    switched(fixture, monkeypatch)
    project_veto(fixture, "41")
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    rolled_back(fixture, cut(fixture))


def test_an_earlier_version_this_agent_vetoed_leaves_it_on_the_current_one_logged_once(file_api, monkeypatch, caplog):
    fixture = file_api
    _, metadata = switched(fixture, monkeypatch, vetoed=("41",))
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    with caplog.at_level("WARNING"):
        assert cut(fixture) == ("current", None, None)  # no rollback: the selector's 42 is the current version
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.source.session_id and row.created_agent_version == "42"
    assert row.idle_upgrade.vetoed_versions == ("41", "42") and row.idle_upgrade.rollback_blocked
    assert veto(fixture).reason == "crash_loop"  # 42 is still vetoed project-wide
    assert sum("NATIVE_CRASH_LOOP_NO_ROLLBACK" in item.message for item in caplog.records) == 1
    caplog.clear()
    reads, calls = len(metadata), len(fixture.control_versions)
    with caplog.at_level("WARNING"):
        assert cut(fixture) == ("current", None, None)  # the next turn end: recorded, so nothing is retried
    assert len(metadata) == reads and len(fixture.control_versions) == calls
    assert not [item for item in caplog.records if "NATIVE_CRASH_LOOP" in item.message]
    assert abandon.row(fixture) == row


def test_an_own_veto_that_is_never_written_in_the_window_is_retried_after_it(file_api, monkeypatch):
    from shared_context.errors import Problem
    from shared_context.native_binding import NativeBindingStore

    fixture = file_api
    switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    record = NativeBindingStore.record_veto

    def failing(self, *args, **kwargs):
        raise Problem(503, "NATIVE_BINDING_UNAVAILABLE", "OFFLINE the veto write failed")

    monkeypatch.setattr(NativeBindingStore, "record_veto", failing)
    assert cut(fixture) == ("crash_loop", "veto_unrecorded", None)
    assert veto(fixture).reason == "crash_loop"  # written first: a restart now still keeps other Agents off 42
    aged(fixture, 31)  # the window closed while the write kept failing
    assert cut(fixture) == ("crash_loop", "veto_unrecorded", None)  # still retried, not silently left on 42
    assert abandon.row(fixture).hosted_session_id == fixture.source.session_id
    monkeypatch.setattr(NativeBindingStore, "record_veto", record)
    rolled_back(fixture, cut(fixture))


def test_a_project_veto_that_is_never_written_never_blocks_the_agents_own_veto_or_rollback(file_api, monkeypatch):
    from shared_context.errors import Problem
    from shared_context.native_version_veto import NativeVersionVetoStore

    fixture = file_api
    switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")

    def failing(self, *args, **kwargs):
        raise Problem(503, "NATIVE_VERSION_VETO_UNAVAILABLE", "OFFLINE the project veto write failed")

    monkeypatch.setattr(NativeVersionVetoStore, "veto", failing)
    assert cut(fixture)[:2] == ("crash_loop", "ready")
    row = abandon.row(fixture)
    assert (row.hosted_session_id, row.created_agent_version) == (fixture.destination.session_id, "41")
    assert row.idle_upgrade.vetoed_versions == ("42",) and veto(fixture) is None


def test_no_trusted_clock_judges_nothing_and_reads_nothing(file_api, monkeypatch):
    fixture = file_api
    _, metadata = switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    before, calls = abandon.row(fixture), len(fixture.control_versions)
    monkeypatch.setattr(fixture.case.composition.runtime, "_now", lambda: None)
    assert cut(fixture) == ("current", None, None)
    assert metadata == [] and len(fixture.control_versions) == calls
    untouched(fixture, before)


class _Destination(Exception):
    pass


@pytest.mark.parametrize("basis", ["retired", "incompatible"])
def test_an_abandoned_sid_after_the_rollback_never_moves_back_to_the_vetoed_version(file_api, monkeypatch, basis):
    # 42 -> 41 -> 42 must not happen: the 41 SID is abandoned with the selector still on 42, which this Agent vetoed.
    fixture = file_api
    switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    row = rolled_back(fixture, cut(fixture))
    assert abandon.store(fixture).decide_abandon(
        row.agent_id, hosted_session_id=row.hosted_session_id, basis=basis, source_status="failed",
        created_agent_version="41") is not None
    went = []

    def destination(client, version, *args, **kwargs):
        went.append(version)
        raise _Destination()

    monkeypatch.setattr(sessions(fixture), "_destination", destination)
    if basis == "retired":
        with pytest.raises(_Destination):
            cut(fixture)
        assert went == ["41"]  # a fresh SID of the current version (M5), never 42
    else:
        # A fresh 41 SID is just as incompatible, so 42 is the only version left to keep serving on (never a 503 loop).
        with pytest.raises(_Destination):
            cut(fixture)
        assert went == ["42"]


def counted_veto_writes(monkeypatch, fail_first=False):
    """The project veto Inserts (``NativeVersionVetoStore.veto``) made from now on; ``fail_first``: the first fails."""
    from shared_context.errors import Problem
    from shared_context.native_version_veto import NativeVersionVetoStore

    write, writes = NativeVersionVetoStore.veto, []

    def counting(self, *args, **kwargs):
        writes.append(args)
        if fail_first and len(writes) == 1:
            raise Problem(503, "NATIVE_VERSION_VETO_UNAVAILABLE", "OFFLINE the project veto write failed once")
        return write(self, *args, **kwargs)

    monkeypatch.setattr(NativeVersionVetoStore, "veto", counting)
    return writes


def test_back_on_the_vetoed_version_an_incompatible_rollback_target_is_blocked_once(file_api, monkeypatch, caplog):
    # The state after the carry-out above: on 42 again, 42 in this Agent's vetoes, 41 now incompatible.
    from shared_context.native_binding import INCOMPATIBLE_STATE_PROFILE

    fixture = file_api
    switched(fixture, monkeypatch, vetoed=("42",))
    profile, targets = behavior_native_sessions._native_profile, []

    def profiled(client):
        if client.target.agent_version == "41":
            targets.append("41")  # only a rollback cut reads 41's definition
            return INCOMPATIBLE_STATE_PROFILE
        return profile(client)

    monkeypatch.setattr(behavior_native_sessions, "_native_profile", profiled)
    writes, stops = counted_veto_writes(monkeypatch), len(fixture.files.calls)
    with caplog.at_level("WARNING"):
        assert cut(fixture) == ("crash_loop", f"target_incompatible:{INCOMPATIBLE_STATE_PROFILE}", None)
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.source.session_id and row.idle_upgrade.rollback_blocked
    assert (len(targets), len(writes)) == (1, 1)
    assert sum("NATIVE_CRASH_LOOP_NO_ROLLBACK" in item.message and "target_incompatible" in item.message
               for item in caplog.records) == 1
    # The next turn end: recorded, so no rollback cut and no veto write; the Agent keeps serving on 42.
    assert cut(fixture) == ("current", None, None)
    assert (len(targets), len(writes)) == (1, 1) and abandon.row(fixture) == row
    assert not [call for call in fixture.files.calls[stops:] if call[0] == "create"]


def test_a_failed_project_veto_write_is_retried_at_the_next_turn_end_then_never_again(file_api, monkeypatch):
    from shared_context.errors import Problem

    fixture = file_api
    switched(fixture, monkeypatch)
    for n in (1, 2, 3):
        crash(fixture, f"rid-{n}", f"cid-{n}")
    writes, profile = counted_veto_writes(monkeypatch, fail_first=True), behavior_native_sessions._native_profile

    def unread(client):
        if client.target.agent_version == "41":
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE the definition read failed")
        return profile(client)

    monkeypatch.setattr(behavior_native_sessions, "_native_profile", unread)
    for expected in (1, 2, 2):  # the decided rollback stays busy: the failed write is retried once, then confirmed
        assert cut(fixture)[:2] == ("crash_loop", "target_unread:NATIVE_STATE_STORE_UNKNOWN")
        assert len(writes) == expected
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", profile)
    rolled_back(fixture, cut(fixture))
    assert len(writes) == 2
