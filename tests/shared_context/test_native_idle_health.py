"""ADR-0009 4b-2 (U3 health check) and 4b-3 (U9 finish) in the background ``cut`` (``FA_NATIVE_IDLE_UPGRADE``).

Real source and destination Hosted runtimes behind the real API (``file_api``, as ``test_native_cut``); the source has
no workspace files, so the cut commits directly. The destination runs #906's own ``session_checkpoint_check`` and
``session_prerestore``; a test makes one conversation's read-back refuse by replacing Hosted's ``_latest`` only.
"""

from datetime import timedelta
import logging

import pytest

from shared_context import behavior_native_sessions, native_checkpoint_check
from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_native_turn_api import _kept_conversations
from shared_context.errors import Problem
from shared_context.native_binding import NativeBindingStore
from shared_context.native_teams_relay import notice_text
import test_native_abandon_source as abandon
from test_native_abandon_source import binding_rows  # noqa: F401
import test_native_crash_loop as crash_loop
from test_native_crash_loop import veto
from test_native_cut import cut, operations, ready, replace_retried, sessions, state  # noqa: F401
from test_native_files_api import (  # noqa: F401
    file_api, file_loops, instruction_models, jwk_client, local_env, native_api, signing_key, synthetic_standing_v1,
)

RECENT = ("conv-1", "conv-2", "conv-3")  # oldest first, as owner submits keep them


def keep(fixture, conversations=RECENT):
    """``conversations`` kept by this API's owner submits, oldest first."""
    kept, lock = _kept_conversations(fixture.case.composition)
    with lock:
        for cid in conversations:
            kept[(fixture.source.agent_id, cid)] = None


def idle(fixture, monkeypatch, conversations=RECENT, *, files=False):
    """Bound to the 41 source (no files unless ``files``: the cut then goes through the files fence), selector on 42,
    the idle flag on, and ``conversations`` kept by this API's owner submits; returns the list of Hosted read-backs
    (CID, warm)."""
    ready(fixture, monkeypatch, files=files)
    sessions(fixture).idle_upgrade = True
    keep(fixture, conversations)
    monkeypatch.setattr(behavior_native_sessions, "HEALTH_RETRY_SECONDS", 0)  # the store's clock is fixed
    reads, latest = [], native_checkpoint_check._latest

    async def recorded(runtime, cid, *, warm):
        reads.append((cid, warm))
        return await latest(runtime, cid, warm=warm)

    monkeypatch.setattr(native_checkpoint_check, "_latest", recorded)
    return reads


def refusing(monkeypatch, reads, code, status=409, *, cids=("conv-2",), prerestore=False):
    """Hosted's read-back of ``cids`` refuses with ``code``: the check's, or only the prerestore's; the rest as
    before. Returns the read-back it replaced."""
    latest = native_checkpoint_check._latest

    async def refused(runtime, cid, *, warm):
        if cid in cids and warm is prerestore:
            reads.append((cid, warm))
            raise Problem(status, code, "OFFLINE the checkpoint did not read back")
        return await latest(runtime, cid, warm=warm)

    monkeypatch.setattr(native_checkpoint_check, "_latest", refused)
    return latest


def on_source(fixture, before):
    """Nothing moved: the row and Hosted still name the source, which was never stopped."""
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.source.session_id
    assert state(fixture)["hosted_session_id"] == fixture.source.session_id
    assert ("stop", fixture.source.session_id) not in fixture.files.calls
    assert ("session_cut", "probe") not in operations(fixture, before)
    return row


def test_a_health_pass_pre_restores_switches_then_stops_the_old_sid_and_prewarms(file_api, monkeypatch):
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    rows_at_stop = []
    fixture.files.on_stop = lambda: rows_at_stop.append(abandon.row(fixture).hosted_session_id)
    start = len(fixture.controls)
    assert cut(fixture) == ("ready", None, None)
    # U3 reads the five newest (here all three, newest first) from the store; U9 restores the newest two warm.
    assert reads == [("conv-3", False), ("conv-2", False), ("conv-1", False), ("conv-3", True), ("conv-2", True)]
    ops = [operation for operation, _mode in operations(fixture, start)]
    order = [ops.index("session_checkpoint_check"), ops.index("session_prerestore"), ops.index("session_cut"),
             ops.index("prewarm")]
    assert order == sorted(order) and ops.count("session_cut") == 2  # probe, then the commit
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.destination.session_id
    assert rows_at_stop == [fixture.destination.session_id]  # U9's stop came after U8's row write
    assert fixture.files.stopped == {fixture.source.session_id}


def test_one_or_two_failures_retry_at_the_next_turn_end_and_a_pass_then_switches(file_api, monkeypatch, caplog):
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    latest = refusing(monkeypatch, reads, "NATIVE_CHECKPOINT_MISMATCH")
    caplog.set_level(logging.WARNING)
    for count in (1, 2):
        start = len(fixture.controls)
        assert cut(fixture) == ("health_failed", str(count), None)
        assert on_source(fixture, start).idle_upgrade.health_failures == count
        assert "session_prerestore" not in [operation for operation, _ in operations(fixture, start)]
    assert "NATIVE_UPGRADE_HEALTH_FAILED" in caplog.text and "failures=2 failed=conv-2" in caplog.text
    monkeypatch.setattr(native_checkpoint_check, "_latest", latest)  # the next check reads back
    assert cut(fixture) == ("ready", None, None)
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.destination.session_id
    assert row.idle_upgrade.health_failures == 0 and row.idle_upgrade.vetoed_versions == ()
    assert [call for call in fixture.files.calls if call[0] == "create"] == [("create", fixture.destination.session_id)]


def test_three_failures_veto_the_target_for_this_agent_only_and_it_keeps_serving(file_api, monkeypatch, caplog):
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    refusing(monkeypatch, reads, "NATIVE_CHECKPOINT_MISMATCH")
    caplog.set_level(logging.WARNING)
    assert [cut(fixture)[:2] for _ in range(3)] == [("health_failed", "1"), ("health_failed", "2"), ("vetoed", "health")]
    row = on_source(fixture, 0)
    assert row.idle_upgrade.vetoed_versions == ("42",) and row.idle_upgrade.health_failures == 3
    # Neal 2026-10-19: once, in the same write as the veto; no version numbers; the workbench's only (Teams: M4 #6)
    assert (row.switch_notice, row.notice().kind) == ("vetoed", "environment_upgrade_vetoed")
    assert row.notice(vetoed=False) is None  # a console before 4b-4 gets nothing and clears nothing
    assert notice_text(row.notice()) is None  # Teams never shows it
    assert veto(fixture) is None, "a health veto is this Agent's own; the project row stays crash-loop only"
    assert "NATIVE_UPGRADE_HEALTH_VETO" in caplog.text and "staying_on=41" in caplog.text
    start, checks = len(fixture.controls), len(reads)
    assert cut(fixture) == ("vetoed", "agent", None)  # U0 refuses it from now on, before any session call
    assert len(fixture.controls) == start and len(reads) == checks


def test_the_vetoed_notice_never_replaces_an_undelivered_switch_notice(file_api, monkeypatch):
    from shared_context import native_binding

    fixture = file_api
    idle(fixture, monkeypatch)
    store, row = abandon.store(fixture), abandon.row(fixture)
    record = store.repository.get(store.partition_key, store.row_key(row.agent_id))
    store.repository.replace(store.partition_key, store.row_key(row.agent_id), row.model_copy(update={
        "switch_notice": "without_files"}).model_dump(mode="json"), record.etag)
    vetoed = store.record_veto(row.agent_id, hosted_session_id=row.hosted_session_id, agent_version="42", notice=True)
    assert vetoed.idle_upgrade.vetoed_versions == ("42",) and vetoed.switch_notice == "without_files"
    # Not a switch: every later switch's notice replaces it in the slot.
    assert all(native_binding._SWITCH_RANK["vetoed"] < rank for kind, rank in native_binding._SWITCH_RANK.items()
               if kind != "vetoed")


def attempted(fixture, seconds_ago):
    """The row's last failed check moved ``seconds_ago`` by the store's (fixed) clock."""
    store, row = abandon.store(fixture), abandon.row(fixture)
    record = store.repository.get(store.partition_key, store.row_key(row.agent_id))
    store.repository.replace(store.partition_key, store.row_key(row.agent_id), row.model_copy(update={
        "idle_upgrade": row.idle_upgrade.model_copy(update={"last_attempt_at": (
            store.clock() - timedelta(seconds=seconds_ago)).isoformat()})}).model_dump(mode="json"), record.etag)


def test_a_failed_check_is_retried_only_after_the_interval_and_its_jitter(file_api, monkeypatch):
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    refusing(monkeypatch, reads, "NATIVE_CHECKPOINT_MISMATCH")
    monkeypatch.setattr(behavior_native_sessions, "HEALTH_RETRY_SECONDS", 60)
    monkeypatch.setattr(behavior_native_sessions.random, "random", lambda: 0.5)  # the jitter: 30 s more
    assert cut(fixture) == ("health_failed", "1", None)
    assert abandon.row(fixture).idle_upgrade.last_attempt_at is not None
    for seconds_ago in (0, 89):
        attempted(fixture, seconds_ago)
        start, checks = len(fixture.controls), len(reads)
        assert cut(fixture) == ("health_wait", None, None)
        assert len(fixture.controls) == start and len(reads) == checks  # no destination call, no check
    attempted(fixture, 91)
    assert cut(fixture) == ("health_failed", "2", None)


def test_an_unanswered_check_records_its_time_and_waits_the_interval_too(file_api, monkeypatch):
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    refusing(monkeypatch, reads, "NATIVE_STATE_STORE_UNKNOWN", 503)
    monkeypatch.setattr(behavior_native_sessions, "HEALTH_RETRY_SECONDS", 60)
    assert cut(fixture) == ("health_unread", "NATIVE_STATE_STORE_UNKNOWN", None)
    kept = abandon.row(fixture).idle_upgrade
    assert kept.last_attempt_at is not None and kept.health_failures == 0
    start, checks = len(fixture.controls), len(reads)
    assert cut(fixture) == ("health_wait", None, None)
    assert len(fixture.controls) == start and len(reads) == checks  # no destination call, no check


def test_two_replicas_checking_from_one_read_count_one_failure(file_api, monkeypatch):
    """Each replica's CutDriver dedupes only its own process: the count is keyed by the attempt it read
    (``last_attempt_at``), so a second write of the same attempt, or one for another target, counts nothing."""
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    refusing(monkeypatch, reads, "NATIVE_CHECKPOINT_MISMATCH")
    assert cut(fixture) == ("health_failed", "1", None)
    store, row = abandon.store(fixture), abandon.row(fixture)
    for target, read in (("42", None), ("43", row.idle_upgrade.last_attempt_at)):
        again = store.record_health_failure(row.agent_id, hosted_session_id=row.hosted_session_id,
                                            target_version=target, last_attempt_at=read)
        assert again == row and again.idle_upgrade.health_failures == 1
    assert store.record_health_failure(
        row.agent_id, hosted_session_id=row.hosted_session_id, target_version="42",
        last_attempt_at=row.idle_upgrade.last_attempt_at).idle_upgrade.health_failures == 2  # the next attempt


@pytest.mark.parametrize("code,status,counted", [
    ("NATIVE_CHECKPOINT_MISMATCH", 409, True),  # a missing or different segment: unreadable
    ("SDK_STATE_UNSUPPORTED_FORMAT", 409, True),  # an unknown codec: unsupported
    ("NATIVE_STATE_STORE_UNKNOWN", 503, False),  # the store did not answer: no result at all
    ("NATIVE_STATE_SCOPE", 403, False),  # the API named another Agent's conversation: not this image's answer
])
def test_an_unreadable_or_unsupported_answer_counts_and_an_unanswered_check_does_not(
        file_api, monkeypatch, code, status, counted):
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    refusing(monkeypatch, reads, code, status)
    outcome = cut(fixture)
    row = on_source(fixture, 0)
    if counted:
        assert outcome == ("health_failed", "1", None) and row.idle_upgrade.health_failures == 1
    else:
        assert outcome == ("health_unread", code, None) and row.idle_upgrade.health_failures == 0


def test_a_conversation_that_repeats_the_scope_refusal_is_skipped_so_the_agent_is_not_stuck(file_api, monkeypatch, caplog):
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    refusing(monkeypatch, reads, "NATIVE_STATE_SCOPE", 403)
    assert cut(fixture) == ("health_unread", "NATIVE_STATE_SCOPE", None)  # once: no answer
    caplog.set_level(logging.WARNING)
    del reads[:]
    assert cut(fixture) == ("ready", None, None)  # it repeats: skipped, the other two read back ok
    assert "NATIVE_UPGRADE_HEALTH_SKIPPED code=NATIVE_STATE_SCOPE conversation_id=conv-2" in caplog.text
    assert [read for read in reads if not read[1]] == [("conv-3", False), ("conv-2", False), ("conv-1", False)]
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id


def test_an_image_that_refuses_every_conversation_its_scope_is_vetoed_and_never_switches(file_api, monkeypatch, caplog):
    """A scope regression in the new image refuses every conversation: skipping each one in turn would leave none to
    check and switch unchecked. Every one refused is one failure per attempt, so the third vetoes the target."""
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    refusing(monkeypatch, reads, "NATIVE_STATE_SCOPE", 403, cids=RECENT)
    caplog.set_level(logging.WARNING)
    outcomes = [cut(fixture)[:2] for _ in range(6)]
    vetoed = [("vetoed", "health")] + [("vetoed", "agent")] * 3
    assert outcomes == [("health_failed", "1"), ("health_failed", "2"), *vetoed]
    assert on_source(fixture, 0).idle_upgrade.vetoed_versions == ("42",)
    assert "NATIVE_UPGRADE_HEALTH_SCOPE_REFUSED" in caplog.text


def test_a_single_known_conversation_refused_by_scope_is_never_the_images_veto(file_api, monkeypatch):
    """#912 review P3: right after an API restart the kept list holds one CID. Its refusal proves no image-wide scope
    regression, so it stays ``health_unread`` (no failure, no veto) however often it repeats."""
    fixture = file_api
    reads = idle(fixture, monkeypatch, conversations=("conv-1",))
    refusing(monkeypatch, reads, "NATIVE_STATE_SCOPE", 403, cids=("conv-1",))
    outcomes = [cut(fixture)[:2] for _ in range(5)]
    assert outcomes == [("health_unread", "NATIVE_STATE_SCOPE")] * 5
    row = on_source(fixture, 0)
    assert row.idle_upgrade.health_failures == 0 and row.idle_upgrade.vetoed_versions == ()


def test_a_scope_skip_holds_for_its_own_target_only(file_api, monkeypatch):
    """conv-2 refused twice for 42 is skipped for 42; for 43 it is asked again, and its first refusal answers
    nothing (``health_unread``), never a pass."""
    from types import SimpleNamespace

    from shared_context.behavior_native_turn_api import NativeTurnGateway
    from shared_context.behavior_native_turn_wire import NativePrewarmView

    fixture = file_api
    idle(fixture, monkeypatch)
    row, value = abandon.row(fixture), sessions(fixture)

    def invoked(_client, command, timeout=None):
        (cid,) = command.conversation_ids
        if cid == "conv-2":
            raise Problem(403, "NATIVE_STATE_SCOPE", "OFFLINE another Agent's conversation")
        return NativePrewarmView(agent_id=row.agent_id, hosted_session_id="dest", toolboxes=0,
                                 checkpoints={cid: "ok"}), None

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(invoked))
    destination = SimpleNamespace(target=SimpleNamespace(session_id="dest"))
    unread = ("health_unread", "NATIVE_STATE_SCOPE", None)
    assert [value._health(destination, row, target, lambda: None) for target in ("42", "42", "43")] == [
        unread, None, unread]


def test_u3_ends_within_its_deadline_checking_the_newest_conversations_first(file_api, monkeypatch, caplog):
    """Hosted reads each named conversation one by one; five slow ones in one call would outlast the API's 30 s read
    timeout and never answer. One call per conversation, each bounded by what is left of U3's deadline."""
    import asyncio

    fixture = file_api
    reads = idle(fixture, monkeypatch)
    latest = native_checkpoint_check._latest

    async def slow(runtime, cid, *, warm):
        await asyncio.sleep(0 if warm else 1.5)
        return await latest(runtime, cid, warm=warm)

    monkeypatch.setattr(native_checkpoint_check, "_latest", slow)
    monkeypatch.setattr(behavior_native_sessions, "HEALTH_CHECK_SECONDS", 6)
    timeouts, invoke = [], FoundryHttpClient.invoke_native

    def spied(self, command, **options):
        if command.operation == "session_checkpoint_check":
            timeouts.append(options.get("timeout"))
        return invoke(self, command, **options)

    monkeypatch.setattr(FoundryHttpClient, "invoke_native", spied)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("ready", None, None)
    assert [read for read in reads if not read[1]] == [("conv-3", False)]  # 1.5 s spent: under 5 s left, none more
    assert len(timeouts) == 1 and 5 <= timeouts[0] <= 6
    assert "NATIVE_UPGRADE_HEALTH_PARTIAL checked=1 of=3" in caplog.text


def test_an_image_that_does_not_run_the_check_switches_unchecked(file_api, monkeypatch, caplog):
    """A destination image older than #906 refuses the G0b-reserved commands (its exact refusal below): unchecked, as
    every cut before U3."""
    fixture = file_api
    idle(fixture, monkeypatch)

    async def reserved(runtime, command):  # the pre-#906 image's own refusal (native_turns, G0b)
        raise Problem(422, "NATIVE_REQUEST_INVALID", f"{command.operation} is reserved; this image does not run it")

    monkeypatch.setattr(native_checkpoint_check, "read_back", reserved)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("ready", None, None)
    assert "NATIVE_UPGRADE_HEALTH_UNCHECKED code=NATIVE_REQUEST_INVALID" in caplog.text
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.destination.session_id and row.idle_upgrade.health_failures == 0


def test_any_other_422_answered_nothing_and_nothing_moves(file_api, monkeypatch):
    """A 422 that is not the pre-#906 refusal (here the image refused the request itself) says nothing about the
    checkpoints: no failure is counted and the Agent does not switch unchecked."""
    fixture = file_api
    idle(fixture, monkeypatch)

    async def refused(runtime, command):
        raise Problem(422, "NATIVE_REQUEST_INVALID", "OFFLINE the request is not one fixed native command")

    monkeypatch.setattr(native_checkpoint_check, "read_back", refused)
    assert cut(fixture) == ("health_unread", "NATIVE_REQUEST_INVALID", None)
    assert on_source(fixture, 0).idle_upgrade.health_failures == 0


@pytest.mark.parametrize("file_api", [crash_loop.ROLLBACK], indirect=True)
def test_a_rollback_goes_ahead_when_its_target_reads_a_checkpoint_back_unreadable(file_api, monkeypatch, caplog):
    """Coordinator 2026-10-19: 42 crash-loops and left a bad checkpoint, so 41's read-back says ``unreadable``. A
    rollback cut only logs U3: counting it would veto 41 at the third turn end and leave the Agent on 42 for good."""
    fixture = file_api
    crash_loop.switched(fixture, monkeypatch)
    keep(fixture)
    reads = []
    refusing(monkeypatch, reads, "NATIVE_CHECKPOINT_MISMATCH", cids=RECENT)
    for n in (1, 2, 3):
        crash_loop.crash(fixture, f"rid-{n}", f"cid-{n}")
    caplog.set_level(logging.WARNING)
    row = crash_loop.rolled_back(fixture, cut(fixture))
    assert ("conv-3", False) in reads  # 41's destination did run the check
    assert row.idle_upgrade.vetoed_versions == ("42",) and row.idle_upgrade.health_failures == 0
    assert "NATIVE_UPGRADE_HEALTH_FAILED" in caplog.text and "rollback=True" in caplog.text


@pytest.mark.parametrize("recorded", [True, False])
def test_the_old_sid_is_stopped_only_once_the_switch_is_on_the_binding_row(file_api, monkeypatch, recorded):
    fixture = file_api
    idle(fixture, monkeypatch)
    if not recorded:
        def unavailable(self, *_args, **_kw):
            raise Problem(503, "UPSTREAM_ERROR", "OFFLINE table timeout", service="table")

        monkeypatch.setattr(NativeBindingStore, "record_switch", unavailable)
    start = len(fixture.controls)
    assert cut(fixture) == ("ready", None, None)
    assert state(fixture)["hosted_session_id"] == fixture.destination.session_id  # Hosted's commit is the fact
    stopped = ("stop", fixture.source.session_id) in fixture.files.calls
    prewarmed = ("prewarm", None) in operations(fixture, start)
    assert (stopped, prewarmed) == (recorded, recorded)


def test_prerestore_stop_and_prewarm_failures_never_block_the_switch(file_api, monkeypatch, caplog):
    fixture = file_api
    reads = idle(fixture, monkeypatch)
    refusing(monkeypatch, reads, "NATIVE_STATE_STORE_UNKNOWN", 503, cids=RECENT[-1:], prerestore=True)

    def stop_failed(self, **_options):
        raise Problem(503, "FOUNDRY_SESSION_UNAVAILABLE", "OFFLINE stop failed")

    async def prewarm_failed(command):
        raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE prewarm failed")

    monkeypatch.setattr(FoundryHttpClient, "stop_session", stop_failed)
    fixture.runtime.prewarm = prewarm_failed
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("ready", None, None)
    assert "NATIVE_UPGRADE_PRERESTORE_FAILED code=NATIVE_STATE_STORE_UNKNOWN" in caplog.text
    assert "NATIVE_UPGRADE_FINISH_FAILED step=stop code=FOUNDRY_SESSION_UNAVAILABLE" in caplog.text
    assert "NATIVE_UPGRADE_FINISH_FAILED step=prewarm code=NATIVE_STATE_STORE_UNKNOWN" in caplog.text
    row = abandon.row(fixture)
    assert row.hosted_session_id == fixture.destination.session_id
    assert state(fixture)["hosted_session_id"] == fixture.destination.session_id
    # No work lost: the source's finished turn reads back through the new SID.
    original = abandon.read_request(fixture, "original-rid")
    assert original.status_code == 200 and original.json()["status"] == "completed", original.text


def test_no_recent_conversation_sends_no_check_and_switches(file_api, monkeypatch):
    fixture = file_api
    reads = idle(fixture, monkeypatch, conversations=())
    start = len(fixture.controls)
    assert cut(fixture) == ("ready", None, None)
    ops = [operation for operation, _ in operations(fixture, start)]
    assert "session_checkpoint_check" not in ops and "session_prerestore" not in ops and reads == []
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id


def test_an_answer_that_leaves_out_its_conversation_answered_nothing(file_api, monkeypatch):
    from shared_context.behavior_native_turn_wire import NativePrewarmView

    fixture = file_api
    idle(fixture, monkeypatch)

    async def silent(runtime, command):  # 200, but no result for the conversation it was asked about
        return NativePrewarmView(agent_id=runtime.identity.agent_id, hosted_session_id=runtime.identity.session_id,
                                 toolboxes=0, checkpoints={})

    monkeypatch.setattr(native_checkpoint_check, "read_back", silent)
    assert cut(fixture) == ("health_unread", "NATIVE_STATE_INVALID", None)
    assert on_source(fixture, 0).idle_upgrade.health_failures == 0


def test_a_switch_through_the_files_fence_prewarms_and_never_stops_the_source_again(file_api, monkeypatch):
    """The files fence stops the source itself before its commit, so U9 only prewarms the new SID."""
    fixture = file_api
    idle(fixture, monkeypatch, files=True)
    start = len(fixture.controls)
    assert cut(fixture)[0] == "ready"
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id
    assert ("prewarm", None) in operations(fixture, start)
    assert fixture.files.calls.count(("stop", fixture.source.session_id)) == 1  # the fence's own stop only


def test_after_the_switch_any_finish_failure_only_logs_and_the_cut_keeps_its_outcome(file_api, monkeypatch, caplog):
    """A failure that is not a ``Problem`` (here the toolbox list) would have made the background attempt log
    ``outcome=error`` for a switch Hosted had committed."""
    from shared_context import behavior_native_turn_api

    fixture = file_api
    idle(fixture, monkeypatch)

    def broken(runtime):
        raise RuntimeError("OFFLINE toolbox list")

    monkeypatch.setattr(behavior_native_turn_api, "_prewarm_toolboxes", broken)
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("ready", None, None)
    assert "NATIVE_UPGRADE_FINISH_FAILED step=prewarm code=RuntimeError" in caplog.text
    assert "OFFLINE toolbox list" in caplog.text and "Traceback" in caplog.text  # logged with its traceback
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id


def test_a_finish_that_can_not_read_the_row_back_says_so(file_api, monkeypatch, caplog):
    fixture = file_api
    idle(fixture, monkeypatch)
    switched, record, read = [], NativeBindingStore.record_switch, behavior_native_sessions.NativeSessions._binding_row
    monkeypatch.setattr(NativeBindingStore, "record_switch",
                        lambda self, *a, **k: switched.append(True) or record(self, *a, **k))
    monkeypatch.setattr(behavior_native_sessions.NativeSessions, "_binding_row",
                        lambda self, agent_id: None if switched else read(self, agent_id))
    caplog.set_level(logging.WARNING)
    assert cut(fixture) == ("ready", None, None)
    assert "NATIVE_UPGRADE_FINISH_SKIPPED reason=binding_unread" in caplog.text
    assert ("stop", fixture.source.session_id) not in fixture.files.calls


def test_u9_after_a_long_files_fence_still_prewarms_within_its_own_bound(file_api, monkeypatch):
    """The files fence may use most of the cut's 120 s; U9's own bound starts when U9 does."""
    fixture = file_api
    idle(fixture, monkeypatch, files=True)
    fenced, late = behavior_native_sessions.NativeSessions._fenced, []
    real = behavior_native_sessions.monotonic

    def long_fence(self, *args, **kwargs):
        value = fenced(self, *args, **kwargs)
        late.append(behavior_native_sessions.CUT_DESTINATION_SECONDS)  # the cut's bound is over from here on
        return value

    monkeypatch.setattr(behavior_native_sessions.NativeSessions, "_fenced", long_fence)
    monkeypatch.setattr(behavior_native_sessions, "monotonic", lambda: real() + sum(late))
    start = len(fixture.controls)
    assert cut(fixture)[0] == "ready"
    assert late and ("prewarm", None) in operations(fixture, start)


def test_u9_calls_carry_timeouts_and_stop_at_the_cuts_bound(file_api, monkeypatch, caplog):
    """The pre-restore, the stop and the prewarm each get what is left of the cut's bound as their timeout; once the
    bound ends (here the API shuts down during the stop) the prewarm is not sent and the switch stands."""
    from threading import Event

    from shared_context.behavior_native_turn_api import agent_client
    from test_native_cut import control

    fixture = file_api
    idle(fixture, monkeypatch)
    stopping, timeouts, invoke, stop = Event(), {}, FoundryHttpClient.invoke_native, FoundryHttpClient.stop_session

    def spied(self, command, **options):
        timeouts.setdefault(command.operation, options.get("timeout"))
        return invoke(self, command, **options)

    def stopped(self, **options):
        timeouts["stop"] = options.get("timeout")
        accepted = stop(self, **options)
        stopping.set()  # shutdown begins while the old SID stops
        return accepted

    monkeypatch.setattr(FoundryHttpClient, "invoke_native", spied)
    monkeypatch.setattr(FoundryHttpClient, "stop_session", stopped)
    caplog.set_level(logging.WARNING)
    composition = fixture.case.composition
    assert sessions(fixture).cut(agent_client(composition, control(fixture)), stop=stopping) == ("ready", None, None)
    assert 0 < timeouts["session_prerestore"] <= 30 and 0 < timeouts["stop"] <= 10
    assert "prewarm" not in timeouts
    assert "NATIVE_UPGRADE_FINISH_FAILED step=prewarm code=fence_overrun" in caplog.text
    assert fixture.files.stopped == {fixture.source.session_id}
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id
