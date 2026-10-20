"""ADR-0009 phase 5b offline: the background cut's U1 snapshot (source workspace -> Blob) and U4 restore (Blob ->
destination, through the destination's fence staging), behind FA_NATIVE_IDLE_UPGRADE and
FA_UPGRADE_SNAPSHOT_CONTAINER_URL. Real API, Hosted runtimes and Blob SDK against the offline Session Files, State store
and Blob doubles; no Azure."""

from contextlib import ExitStack, contextmanager
import hashlib
import json
import logging
from pathlib import Path
import sys
from threading import Event
import time

import pytest
from azure.storage.blob import ContainerClient
from pydantic import ValidationError

from shared_context import behavior_native_sessions, native_turns, upgrade_snapshot_flow
from shared_context.behavior_native_sessions import NativeSessions, cut_request_id
from shared_context.behavior_native_turn_api import NativeTurnGateway, agent_client
from shared_context.behavior_native_turn_wire import NativeSessionCut
from shared_context.behavior_session_files import SessionFiles
from shared_context.errors import Problem
from shared_context.native_binding import NativeBindingStore
from shared_context.native_continuations import files_ready
from shared_context.native_cut import native_cut_driver
from shared_context.native_files_wire import MAX_FILE_BYTES, WORKSPACE_ROOT, NativeFileEntry, NativeFilesManifest
from shared_context.native_workspace_files import NativeWorkspaceFiles
from shared_context.upgrade_snapshot_store import MANIFEST_NAME, UpgradeSnapshotStore
from test_backup_store import Credential
from test_native_abandon_source import binding_rows  # noqa: F401
from test_native_cut import (  # noqa: F401
    PREFIX, _moved_with_files, abandon, control, cut, file_api, file_loops, finished, instruction_models, jwk_client,
    local_env, native_api, operations, ready, replace_retried, sessions, set_admitting, signing_key, state,
    synthetic_standing_v1, token)
from test_native_environment import invoke
from test_native_idle_health import keep, refusing
from test_upgrade_snapshot_store import CONTAINER, BlobFake

URL = "https://stsnaptok3n.blob.core.windows.net/upgrade-snapshots"


@pytest.fixture
def blob(monkeypatch):
    with ExitStack() as clients:
        fake = BlobFake()
        container = clients.enter_context(ContainerClient(
            "https://synthetic.blob.core.windows.net", CONTAINER, credential=Credential(), transport=fake,
            retry_total=0, max_single_put_size=MAX_FILE_BYTES, max_single_get_size=MAX_FILE_BYTES))
        opened = []

        @contextmanager
        def snapshot_container(url, credential):
            opened.append(url)
            yield container

        monkeypatch.setattr(upgrade_snapshot_flow, "snapshot_container", snapshot_container)
        fake.opened = opened
        yield fake


def phase5(fixture, monkeypatch, blob, *, on=True):
    """Both 5b switches on (``on``); ``files.on_restore(path, content)`` may change what a staging PUT writes."""
    files, upload = fixture.files, fixture.files.upload_session_file
    files.on_restore = None

    def staging(name, sid, content, *, path):
        if files.on_restore:
            content = files.on_restore(path, content)
        return upload(name, sid, content, path=path)

    files.upload_session_file = staging  # before ``ready``, which wires the project double to it
    (fixture.workspace.root / "binary.bin").chmod(0o555)  # a mode the restore must keep (read-only on Windows)
    ready(fixture, monkeypatch)
    if on:
        sessions(fixture).idle_upgrade = True
        fixture.case.composition.upgrade_snapshot_container_url = URL
    return fixture


def gets(fixture, since=0):
    return [call[2].removeprefix(WORKSPACE_ROOT + "/") for call in fixture.files.calls[since:]
            if call[0] == "get" and call[1] == fixture.source.session_id]


def restores(fixture, since=0):
    """PUTs on the destination: only ever to staging paths (``.fa-transfers/<operation>/upload/NNNN``)."""
    return [call[2] for call in fixture.files.calls[since:]
            if call[0] == "put" and call[1] == fixture.destination.session_id]


def manifests(blob):
    return sorted(name for name in blob.blobs if name.endswith("/" + MANIFEST_NAME))


def at_source(fixture, before):
    """Nothing switched: the Hosted record and the binding row are what they were."""
    assert state(fixture)["hosted_session_id"] == fixture.source.session_id
    assert abandon.row(fixture).hosted_session_id == before.hosted_session_id == fixture.source.session_id
    assert abandon.row(fixture).last_switch == before.last_switch


def trees(fixture):
    return fixture.workspace.tree(), fixture.target_workspace.tree()


def lose_the_cas_after_the_publish(fixture, monkeypatch):
    """A turn admitted on the source while the destination publishes: the commit's CAS is lost after the files are
    live on the destination."""
    publish = NativeWorkspaceFiles.publish

    def published_then_admitted(self, manifest):
        publish(self, manifest)
        monkeypatch.setattr(NativeWorkspaceFiles, "publish", publish)
        set_admitting(fixture, "late-rid")
        set_admitting(fixture, None)

    monkeypatch.setattr(NativeWorkspaceFiles, "publish", published_then_admitted)
    assert cut(fixture) == ("busy", "admission_pending", None)
    assert fixture.target_workspace.root.is_dir()  # live on a destination the Agent did not move to


def test_a_turn_end_snapshot_is_written_then_restored_and_switched_without_a_fence(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    rows_at_stop = []
    fixture.files.on_stop = lambda: rows_at_stop.append(abandon.row(fixture).hosted_session_id)
    driver = native_cut_driver(fixture.case.composition)
    assert driver.turn_ended(control(fixture), fixture.source.agent_id, "turn-1")
    finished(driver, fixture.source.agent_id)
    # U1: every file, then the manifest last (the publish marker), under the Team's prefix of the snapshot container.
    puts = [name.removeprefix(f"/{CONTAINER}/") for name in blob.puts()]
    assert len(manifests(blob)) == 1 and puts[-1] == manifests(blob)[0] and len(puts) == 3
    assert blob.opened == [URL]
    assert sorted(gets(fixture)) == ["binary.bin", "nested/draft.txt"]
    # No files fence: no H0 ``pending``, no follow file stage; the source is stopped only by U9, after U8's row write.
    assert not any(name in {"session_files", "session_follow"} for name, _ in operations(fixture))
    assert fixture.files.stopped == {fixture.source.session_id}
    assert rows_at_stop == [fixture.destination.session_id]
    # U4 through the fence's staging: the same tree, its modes and its empty directory included.
    assert all("/.fa-transfers/" in path and "/upload/" in path for path in restores(fixture))
    assert len(restores(fixture)) == 2
    source_tree, target_tree = trees(fixture)
    assert target_tree == source_tree and (fixture.target_workspace.root / "empty").is_dir()
    # Hosted's own record says the files moved (U8: the commit is the only fact); the row is its copy.
    environment = state(fixture)["environment"]
    assert environment["files_migrated"] is True and environment["files_manifest_sha256"]
    assert fixture.hosted.portal.call(files_ready, fixture.runtime) is True
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id and switched.upgrade is None
    assert switched.last_switch.files_migrated is True and switched.switch_notice == "updated"
    assert state(fixture)["hosted_session_id"] == fixture.destination.session_id


def test_the_cut_checks_health_then_carries_then_commits_then_finishes(file_api, monkeypatch, blob):
    """U3 (and U9's pre-restore) before the probe; then U1/U4's carry (the source's tree, the restore's probe), the
    commit, and U9's stop of the old SID and prewarm of the new one."""
    fixture = phase5(file_api, monkeypatch, blob)
    keep(fixture)
    start, stopped_at = len(fixture.controls), []
    fixture.files.on_stop = lambda: stopped_at.append(len(fixture.controls) - start)
    assert cut(fixture)[:2] == ("ready", "snapshot")
    sent = [(command.get("operation"), command.get("mode"), bool(command.get("workspace") or command.get("files_restore")))
            for command, _ in fixture.controls[start:]]
    order = [sent.index(step) for step in (
        ("session_checkpoint_check", None, False), ("session_prerestore", None, False), ("session_cut", "probe", False),
        ("session_read", None, True), ("session_cut", "probe", True), ("session_cut", None, True))]  # "cut": default
    assert order == sorted(order)
    assert order[-1] < stopped_at[0] <= sent.index(("prewarm", None, False))
    assert fixture.files.stopped == {fixture.source.session_id}


def test_a_failed_health_check_never_snapshots(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    keep(fixture)
    refusing(monkeypatch, [], "NATIVE_CHECKPOINT_MISMATCH")
    before, start = abandon.row(fixture), len(fixture.controls)
    assert cut(fixture) == ("health_failed", "1", None)
    assert blob.opened == [] and gets(fixture) == [] and restores(fixture) == []
    assert not any(command.get("workspace") or command.get("files_restore") for command, _ in fixture.controls[start:])
    at_source(fixture, before)


def test_a_rollback_cut_only_logs_a_failed_health_check_and_still_carries_the_files(file_api, monkeypatch, blob,
                                                                                    caplog):
    fixture = phase5(file_api, monkeypatch, blob)
    keep(fixture)
    refusing(monkeypatch, [], "NATIVE_CHECKPOINT_MISMATCH")
    caplog.set_level(logging.WARNING)
    client = agent_client(fixture.case.composition, control(fixture))
    assert sessions(fixture).cut(client, target="42")[:2] == ("ready", "snapshot")  # 4d's rollback to 42
    assert "NATIVE_UPGRADE_HEALTH_FAILED" in caplog.text and "rollback=True failed=conv-2" in caplog.text
    assert len(manifests(blob)) == 1 and len(restores(fixture)) == 2
    _moved_with_files(fixture)


def test_a_row_rewritten_from_hosted_after_a_lost_row_write_still_says_the_files_moved(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    record_switch, failed = NativeBindingStore.record_switch, []

    def once(self, *args, **kwargs):
        if not failed:
            failed.append(True)
            raise RuntimeError("OFFLINE the row write after the commit was lost")
        return record_switch(self, *args, **kwargs)

    monkeypatch.setattr(NativeBindingStore, "record_switch", once)
    assert cut(fixture)[:2] == ("ready", "snapshot")
    assert abandon.row(fixture).hosted_session_id == fixture.source.session_id  # Hosted moved, the row did not
    assert fixture.files.stopped == set()  # U9 waits for the row (U8)
    start = len(fixture.controls)
    assert cut(fixture)[0] == "repaired"  # the next trigger records the commit the row missed (§7a (j))
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id
    assert switched.last_switch.files_migrated is True and switched.switch_notice == "updated"
    # U9 once the repair recorded it: the old SID stopped, the new one prewarmed.
    assert fixture.files.stopped == {fixture.source.session_id} and ("prewarm", None) in operations(fixture, start)


def test_an_incremental_snapshot_carries_only_the_changes(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    fixture.files.on_restore = lambda path, content: (set_admitting(fixture, "late-rid"), set_admitting(fixture, None),
                                                      setattr(fixture.files, "on_restore", None))[-1] or content
    assert cut(fixture) == ("busy", "admission_pending", None)  # a turn admitted during the restore: no switch
    at_source(fixture, abandon.row(fixture))
    assert not fixture.target_workspace.root.exists()  # only staging was written
    (fixture.workspace.root / "nested" / "added.txt").write_bytes(b"written by the late turn\n")
    calls, attempt = len(fixture.files.calls), abandon.row(fixture).attempt("42")
    assert cut(fixture)[:2] == ("ready", "snapshot")
    # Only the changed file left the source; the rest came from the first snapshot. The same destination took it.
    assert gets(fixture, calls) == ["nested/added.txt"] and len(restores(fixture, calls)) == 3
    assert len(manifests(blob)) == 2 and abandon.row(fixture).attempt("42") == attempt
    assert (fixture.target_workspace.root / "nested" / "added.txt").read_bytes() == b"written by the late turn\n"
    _moved_with_files(fixture)


def test_a_wrong_staged_copy_never_switches_and_the_next_trigger_uses_the_same_destination(file_api, monkeypatch,
                                                                                           blob):
    fixture = phase5(file_api, monkeypatch, blob)
    before, attempt = abandon.row(fixture), abandon.row(fixture).attempt("42")
    fixture.files.on_restore = lambda path, content: b"not the original" if content.startswith(b"uncommitted") else content
    assert cut(fixture) == ("busy", "NATIVE_FILES_CONFLICT", None)  # Hosted's confirm checks every staged hash
    at_source(fixture, before)
    assert not fixture.target_workspace.root.exists()
    fixture.files.on_restore = None
    assert cut(fixture)[:2] == ("ready", "snapshot")
    assert abandon.row(fixture).attempt("42") == attempt  # no fresh destination
    _moved_with_files(fixture)


def test_a_restore_that_keeps_failing_falls_back_to_the_fence_after_three_attempts(file_api, monkeypatch, blob,
                                                                                   caplog):
    fixture = phase5(file_api, monkeypatch, blob)
    before, publish = abandon.row(fixture), NativeWorkspaceFiles.publish

    def refused(self, manifest):  # e.g. a directory mode without owner write when Hosted does not run as root
        if manifest.request_id.startswith("restore-"):
            raise Problem(503, "NATIVE_FILES_UNKNOWN", "OFFLINE the restore's rename was refused")
        return publish(self, manifest)  # the fence's own publish still works

    monkeypatch.setattr(NativeWorkspaceFiles, "publish", refused)
    for _ in range(3):
        outcome, reason, _fence = cut(fixture)
        assert outcome == "busy" and reason is not None
        at_source(fixture, before)
    caplog.set_level(logging.WARNING)
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None  # today's fence moved the files
    assert "NATIVE_UPGRADE_SNAPSHOT outcome=fallback reason=restore_failed failures=3" in caplog.text
    assert sessions(fixture)._restore_failures == {}  # the fence's switch clears the count for that target
    _moved_with_files(fixture)


def test_a_lost_restore_ack_is_read_back_and_the_switch_completes_without_a_second_put(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    fixture.files.upload_error = True  # every staging PUT lands, every ACK is lost (NATIVE_FILES_UNKNOWN)
    assert cut(fixture)[:2] == ("ready", "snapshot")  # read back, matched: switched in this one cut
    assert len(restores(fixture)) == 2 == len(set(restores(fixture)))  # one PUT per file, none sent again
    assert sessions(fixture)._restore_unconfirmed == set() and sessions(fixture)._restore_failures == {}
    _moved_with_files(fixture)


def test_a_lost_restore_ack_whose_read_back_fails_hands_the_files_to_the_fence(file_api, monkeypatch, blob, caplog):
    fixture = phase5(file_api, monkeypatch, blob)
    fixture.files.upload_error = True  # every PUT lands, every ACK is lost: the fence's too
    staged, staging, download = set(), upgrade_snapshot_flow.staging_relative_path, SessionFiles.download
    counted, switched = [], upgrade_snapshot_flow.switched

    def recorded(manifest, index):
        staged.add(staging(manifest, index))
        return staging(manifest, index)

    def unread(self, sid, path, entry):  # only the restore's staging paths; the fence's own read back fine
        if path in staged:
            raise Problem(503, "NATIVE_FILES_UNKNOWN", "OFFLINE the restore's read-back did not answer")
        return download(self, sid, path, entry)

    def recording(sessions_, destination, failed):
        counted.append(failed)
        return switched(sessions_, destination, failed)

    monkeypatch.setattr(upgrade_snapshot_flow, "staging_relative_path", recorded)
    monkeypatch.setattr(upgrade_snapshot_flow, "switched", recording)
    monkeypatch.setattr(SessionFiles, "download", unread)
    caplog.set_level(logging.WARNING)
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None  # the fence moved the files in this same cut
    assert "NATIVE_UPGRADE_SNAPSHOT outcome=fallback reason=NATIVE_FILES_UNKNOWN" in caplog.text
    assert [path for path in restores(fixture) if path in staged] == sorted(staged) and len(staged) == 1  # never again
    assert counted == [True, False]  # the unresolved restore counted, then the fence's switch cleared it
    _moved_with_files(fixture)


def test_an_unknown_snapshot_shape_is_refused_and_the_last_valid_state_kept(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    before = abandon.row(fixture)
    save = UpgradeSnapshotStore.save

    def newer(self, *args, **kwargs):
        result = save(self, *args, **kwargs)
        name = manifests(blob)[-1]
        body = json.dumps({**json.loads(blob.blobs[name][0]), "format": "fa-upgrade-snapshot-v9"}).encode()
        meta = {**blob.blobs[name][1], "x-ms-meta-sha256": hashlib.sha256(body).hexdigest(),
                "x-ms-meta-bytes": str(len(body))}
        blob.blobs[name] = (body, meta)  # a format this API does not know, under consistent metadata
        return result

    monkeypatch.setattr(UpgradeSnapshotStore, "save", newer)
    assert cut(fixture) == ("busy", "UPGRADE_SNAPSHOT_INTEGRITY_ERROR", None)
    assert restores(fixture) == [] and not fixture.target_workspace.root.exists()
    at_source(fixture, before)
    assert fixture.source.agent_id not in sessions(fixture)._snapshot_bases  # never a base for the next snapshot


def test_a_snapshot_failure_falls_back_to_the_existing_files_path(file_api, monkeypatch, blob, caplog):
    fixture = phase5(file_api, monkeypatch, blob)
    blob.fail_put = 1  # the Blob write of the first file fails: no manifest
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None  # today's fence moved the files instead
    assert manifests(blob) == [] and "NATIVE_UPGRADE_SNAPSHOT outcome=fallback" in caplog.text
    _moved_with_files(fixture)


def test_the_cut_s_one_deadline_covers_the_destination_wait_and_a_late_snapshot_falls_back_to_the_fence(
        file_api, monkeypatch, blob, caplog):
    fixture = phase5(file_api, monkeypatch, blob)
    offset, destination = [0], NativeSessions._destination

    def slow_destination(self, *args, **kwargs):
        found = destination(self, *args, **kwargs)
        offset[0] = behavior_native_sessions.CUT_DESTINATION_SECONDS - 10  # waiting for it used most of the bound
        return found

    def clock():
        return time.monotonic() + offset[0]

    monkeypatch.setattr(behavior_native_sessions, "monotonic", clock)
    monkeypatch.setattr(upgrade_snapshot_flow, "monotonic", clock)
    monkeypatch.setattr(NativeSessions, "_destination", slow_destination)
    caplog.set_level(logging.WARNING)
    # 10 s left: the probe fits; the carry, which leaves the commit its own reserve, does not.
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None  # today's fence moved the files instead
    assert "NATIVE_UPGRADE_SNAPSHOT outcome=fallback reason=UPGRADE_SNAPSHOT_DEADLINE" in caplog.text
    assert blob.puts() == []  # refused before its first file step: nothing was written to the snapshot
    _moved_with_files(fixture)


def restore_commits(monkeypatch, lost):
    """Every restore commit (the no-fence cut with ``files_restore``) is recorded as its timeout; ``lost(timeout)`` may
    raise in its place (before Hosted gets it) or after it (Hosted committed, the answer was lost)."""
    invoke_, sent = NativeTurnGateway._invoke, []

    def recording(client, command, timeout=None):
        if not (isinstance(command, NativeSessionCut) and command.mode == "cut" and command.files_restore is not None):
            return invoke_(client, command, timeout=timeout)
        sent.append(timeout)
        return lost(lambda: invoke_(client, command, timeout=timeout))

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(recording))
    return sent


def test_the_carry_stops_early_enough_for_the_commit_to_keep_its_reserve(file_api, monkeypatch, blob, caplog):
    fixture = phase5(file_api, monkeypatch, blob)
    offset, destination = [0], NativeSessions._destination

    def slow_destination(self, *args, **kwargs):
        found = destination(self, *args, **kwargs)
        offset[0] = behavior_native_sessions.CUT_DESTINATION_SECONDS - upgrade_snapshot_flow.FILE_RESERVE_SECONDS - 1
        return found

    def clock():
        return time.monotonic() + offset[0]

    monkeypatch.setattr(behavior_native_sessions, "monotonic", clock)
    monkeypatch.setattr(upgrade_snapshot_flow, "monotonic", clock)
    monkeypatch.setattr(NativeSessions, "_destination", slow_destination)
    # 16 s left: a file step (15 s) would still start, and a slow last upload would leave the commit a few seconds.
    uploads = []
    fixture.files.on_restore = lambda path, content: (uploads.append(path), offset.__setitem__(
        0, offset[0] + 10 * (len(uploads) == 2)), content)[-1]
    sent = restore_commits(monkeypatch, lambda commit: commit())
    caplog.set_level(logging.WARNING)
    outcome, reason, fence = cut(fixture)
    assert sent == []  # the carry left the commit its reserve: no restore commit with less than that was sent
    assert (outcome, reason) == ("ready", None) and fence is not None  # today's fence moved the files instead
    assert "NATIVE_UPGRADE_SNAPSHOT outcome=fallback reason=UPGRADE_SNAPSHOT_DEADLINE" in caplog.text
    _moved_with_files(fixture)


def test_an_unknown_restore_commit_is_read_back_and_never_counted_as_a_failure(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    before, answers = abandon.row(fixture), []

    def lost(commit):
        answers.append(len(answers))
        if answers[-1]:
            commit()  # Hosted committed; only its answer was lost
        raise Problem(503, "FOUNDRY_INVOCATION_UNKNOWN", "OFFLINE native transport outcome is unknown")

    restore_commits(monkeypatch, lost)
    assert cut(fixture) == ("busy", "FOUNDRY_INVOCATION_UNKNOWN", None)  # read back: never reached Hosted
    at_source(fixture, before)
    assert sessions(fixture)._restore_failures == {}
    assert fixture.files.stopped == set()
    start = len(fixture.controls)
    assert cut(fixture) == ("repaired", "FOUNDRY_INVOCATION_UNKNOWN", None)  # read back: Hosted's commit, recorded
    switched = abandon.row(fixture)
    assert switched.hosted_session_id == fixture.destination.session_id
    assert switched.last_switch.files_migrated is True and switched.switch_notice == "updated"
    assert sessions(fixture)._restore_failures == {}
    # U9 after the repaired commit too: the old SID stopped, the new one prewarmed.
    assert fixture.files.stopped == {fixture.source.session_id} and ("prewarm", None) in operations(fixture, start)


def test_a_turn_admitted_before_the_snapshot_is_never_snapshotted(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    before, tree = abandon.row(fixture), upgrade_snapshot_flow._tree

    def admitted_first(owner, client, timeout):
        if client.target.session_id == fixture.source.session_id:
            set_admitting(fixture, "late-rid")  # admitted after the probe's E0, before the tree read (E_snap)
            set_admitting(fixture, None)
        return tree(owner, client, timeout)

    monkeypatch.setattr(upgrade_snapshot_flow, "_tree", admitted_first)
    assert cut(fixture) == ("busy", "snapshot_stale", None)
    assert blob.puts() == [] and gets(fixture) == [] and restores(fixture) == []
    at_source(fixture, before)


def test_a_source_idle_mid_snapshot_is_called_no_more_and_nothing_moves(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    before, status, tree = abandon.row(fixture), ["active"], upgrade_snapshot_flow._tree

    def going_idle(owner, client, timeout):
        value = tree(owner, client, timeout)
        if client.target.session_id == fixture.source.session_id:
            status[0] = "idle"  # the platform reclaimed its compute right after the helper answered
        return value

    abandon.platform(fixture, monkeypatch, status=lambda: status[0])
    monkeypatch.setattr(upgrade_snapshot_flow, "_tree", going_idle)
    calls = len(fixture.files.calls)
    assert cut(fixture) == ("source_idle", "snapshot", None)
    assert [call for call in fixture.files.calls[calls:] if fixture.source.session_id in call] == []
    assert manifests(blob) == [] and restores(fixture) == []
    at_source(fixture, before)


def test_a_restore_left_live_by_a_lost_cas_is_reused_or_replaced_never_overwritten(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    lose_the_cas_after_the_publish(fixture, monkeypatch)
    attempt = abandon.row(fixture).attempt("42")
    assert cut(fixture)[:2] == ("ready", "snapshot")  # the same tree: the destination already holds it
    assert abandon.row(fixture).attempt("42") == attempt
    _moved_with_files(fixture)


def test_a_changed_tree_after_a_lost_cas_gets_a_fresh_destination(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    lose_the_cas_after_the_publish(fixture, monkeypatch)
    (fixture.workspace.root / "binary.bin").chmod(0o755)
    (fixture.workspace.root / "binary.bin").unlink()  # removed at the source after that publish
    attempt = abandon.row(fixture).attempt("42")
    assert cut(fixture) == ("busy", "restore_conflict", None)  # Hosted's prepare refuses to overwrite it
    assert abandon.row(fixture).attempt("42") == attempt + 1
    assert state(fixture)["hosted_session_id"] == fixture.source.session_id


def source_down(fixture, monkeypatch):
    """The source's tree read fails (a transient 503) until the returned undo is called."""
    tree = upgrade_snapshot_flow._tree

    def failing(owner, client, timeout):
        if client.target.session_id == fixture.source.session_id:
            raise Problem(503, "NATIVE_FILES_UNAVAILABLE", "OFFLINE the source tree read failed")
        return tree(owner, client, timeout)

    monkeypatch.setattr(upgrade_snapshot_flow, "_tree", failing)
    return lambda: monkeypatch.setattr(upgrade_snapshot_flow, "_tree", tree)


def test_a_failed_source_read_after_a_lost_cas_keeps_a_destination_holding_the_same_tree(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    lose_the_cas_after_the_publish(fixture, monkeypatch)
    undo = source_down(fixture, monkeypatch)
    attempt, since = abandon.row(fixture).attempt("42"), len(fixture.controls)
    # It holds the last published snapshot of this source: no new VM, and not the fence its live files would refuse.
    assert cut(fixture) == ("busy", "restore_held", None)
    assert abandon.row(fixture).attempt("42") == attempt
    assert not any(name == "session_files" for name, _ in operations(fixture, since))
    undo()
    assert cut(fixture)[:2] == ("ready", "snapshot")  # the next turn end, on the same destination
    assert abandon.row(fixture).attempt("42") == attempt
    _moved_with_files(fixture)


def test_a_failed_source_read_after_a_lost_cas_takes_a_fresh_destination_not_the_fence(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    lose_the_cas_after_the_publish(fixture, monkeypatch)
    source_down(fixture, monkeypatch)
    sessions(fixture)._snapshot_bases.clear()  # an API restart: what the destination holds can not be matched
    attempt, since = abandon.row(fixture).attempt("42"), len(fixture.controls)
    # Today's fence would meet the live files: its prepare refuses, it is withdrawn, and the Agent never moves.
    assert cut(fixture) == ("busy", "restore_conflict", None)
    assert abandon.row(fixture).attempt("42") == attempt + 1
    assert not any(name == "session_files" for name, _ in operations(fixture, since))


def blob_down(monkeypatch):
    def down(self, *args, **kwargs):
        raise Problem(503, "BLOB_UNAVAILABLE", "OFFLINE the snapshot container does not answer")

    monkeypatch.setattr(UpgradeSnapshotStore, "save", down)


def test_blob_down_after_a_lost_cas_hands_the_held_destination_to_the_fence(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    lose_the_cas_after_the_publish(fixture, monkeypatch)
    blob_down(monkeypatch)
    attempt = abandon.row(fixture).attempt("42")
    for _ in range(upgrade_snapshot_flow.RESTORE_FAILURE_LIMIT):
        assert cut(fixture) == ("busy", "restore_held", None)  # kept: it holds the last snapshot, but nothing moves
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None  # today's fence, on that same destination
    assert abandon.row(fixture).attempt("42") == attempt and sessions(fixture)._restore_failures == {}
    _moved_with_files(fixture)


def test_at_the_restore_limit_a_destination_holding_an_older_tree_takes_a_fresh_sid_not_the_fence(
        file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    lose_the_cas_after_the_publish(fixture, monkeypatch)
    (fixture.workspace.root / "nested" / "added.txt").write_bytes(b"written after that publish\n")
    blob_down(monkeypatch)
    attempt = abandon.row(fixture).attempt("42")
    for _ in range(upgrade_snapshot_flow.RESTORE_FAILURE_LIMIT):
        assert cut(fixture) == ("busy", "restore_held", None)
    # The fence's prepare would refuse that live root at every attempt (withdrawn, NATIVE_FILES_CONFLICT): M5.
    assert cut(fixture) == ("busy", "restore_conflict", None)
    assert abandon.row(fixture).attempt("42") == attempt + 1
    assert state(fixture)["hosted_session_id"] == fixture.source.session_id


def destination_wire_refuses(fixture, monkeypatch, *fields):
    """The destination's Hosted wire refuses a body naming any of ``fields`` (``extra=forbid`` or a manifest it does
    not take): its own refusal, unchanged. The source's runtime keeps its wire. Returns the refused operations."""
    typed, refused = native_turns.NATIVE_REQUEST, []

    class Refusing:
        def validate_json(self, raw):
            runtime = sys._getframe(1).f_locals["runtime"]  # the Hosted handler validating this body
            if runtime.identity.session_id == fixture.destination.session_id and any(
                    f'"{field}"'.encode() in raw for field in fields):
                refused.append(json.loads(bytes(raw))["operation"])
                raise ValueError("OFFLINE refused by this wire")
            return typed.validate_json(raw)

    monkeypatch.setattr(native_turns, "NATIVE_REQUEST", Refusing())
    return refused


def test_an_image_before_5b_refuses_the_restore_and_the_fence_moves_the_files(file_api, monkeypatch, blob, caplog):
    fixture = phase5(file_api, monkeypatch, blob)
    refused = destination_wire_refuses(fixture, monkeypatch, "workspace", "files_restore")
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None
    # The restore probe's refusal alone is not remembered; the destination's tree read (nothing but ``workspace``) is.
    assert refused == ["session_cut", "session_read"] and sessions(fixture)._snapshot_unsupported == {"42"}
    _moved_with_files(fixture)
    # Remembered: the next turn end on that version makes no 5b call at all.
    def asked(*_args):
        raise AssertionError("an image that refused 5b was asked again")

    monkeypatch.setattr(upgrade_snapshot_flow, "_carry", asked)
    client = agent_client(fixture.case.composition, control(fixture))
    assert upgrade_snapshot_flow.carry(sessions(fixture), client, client, client.for_target(
        fixture.destination), None, None, None) is None


def test_a_5b_wire_refusing_one_restore_manifest_fails_this_snapshot_only(file_api, monkeypatch, blob, caplog):
    fixture = phase5(file_api, monkeypatch, blob)
    refused = destination_wire_refuses(fixture, monkeypatch, "files_restore")  # the same 422 as an unknown field
    caplog.set_level(logging.WARNING)
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None  # this snapshot failed: today's fence
    assert refused == ["session_cut"] and "outcome=fallback reason=NATIVE_REQUEST_INVALID" in caplog.text
    assert sessions(fixture)._snapshot_unsupported == set()  # not remembered: 5b is asked again next time
    _moved_with_files(fixture)


def test_another_422_from_a_5b_image_fails_this_snapshot_only(file_api, monkeypatch, blob, caplog):
    fixture = phase5(file_api, monkeypatch, blob)
    prepare = NativeWorkspaceFiles.prepare

    def refused(self, manifest):  # Hosted refuses this one restore request, not the field
        if manifest.request_id.startswith("restore-"):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "OFFLINE this restore request is refused")
        return prepare(self, manifest)

    monkeypatch.setattr(NativeWorkspaceFiles, "prepare", refused)
    caplog.set_level(logging.WARNING)
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None  # this snapshot failed: today's fence
    assert "NATIVE_UPGRADE_SNAPSHOT outcome=fallback reason=NATIVE_REQUEST_INVALID" in caplog.text
    assert sessions(fixture)._snapshot_unsupported == set()  # not remembered: 5b is asked again next time
    _moved_with_files(fixture)


def test_shutdown_during_the_restore_stops_it_and_switches_nothing(file_api, monkeypatch, blob):
    fixture = phase5(file_api, monkeypatch, blob)
    before, stop = abandon.row(fixture), Event()
    fixture.files.on_restore = lambda path, content: (stop.set(), content)[-1]
    outcome = sessions(fixture).cut(agent_client(fixture.case.composition, control(fixture)), stop=stop)
    assert outcome == ("stopping", "shutdown", None)
    assert len(restores(fixture)) == 1  # the next file was never sent
    at_source(fixture, before)


def restore_probe(fixture, **scope):
    """A restore probe as the API would send it, as JSON, aimed at the destination's Hosted runtime directly (no API
    check on the way): one file under one directory, the manifest's scope changed by ``scope``."""
    target, source = fixture.destination, fixture.source
    manifest = NativeFilesManifest(**{
        "agent_id": source.agent_id, "request_id": "restore-offline", "source_session_id": source.session_id,
        "source_agent_version": source.agent_version, "target_session_id": target.session_id,
        "target_agent_version": target.agent_version, **scope}, root_mode=0o755, entries=(
        NativeFileEntry(path="nested", kind="directory", mode=0o755),
        NativeFileEntry(path="nested/a.txt", kind="file", mode=0o644, byte_length=2, sha256=hashlib.sha256(b"a\n").hexdigest())))
    return manifest, NativeSessionCut(request_id=cut_request_id(source.agent_id, source.session_id, target.session_id),
                                      expected_session_id=source.session_id, mode="probe",
                                      files_restore=manifest).model_dump(mode="json")


@pytest.mark.parametrize("scope", [{"agent_id": "00000002-2222-2222-2222-222222222222"},
                                   {"target_session_id": "fa-native-another-destination"},
                                   {"target_agent_version": "43"}], ids=["agent", "session", "version"])
def test_hosted_refuses_a_restore_named_for_another_agent_or_destination(file_api, monkeypatch, blob, scope):
    fixture = phase5(file_api, monkeypatch, blob)
    manifest, body = restore_probe(fixture, **scope)
    answer = invoke(fixture.hosted, fixture.identity, body)
    assert answer.status_code == 409 and answer.json()["error"] == {
        "code": "NATIVE_ENVIRONMENT_CONFLICT", "message": "the restore names another Agent or destination"}
    assert not fixture.target_workspace._transfer(manifest).exists()  # nothing was prepared
    assert state(fixture)["hosted_session_id"] == fixture.source.session_id
    # The same probe naming this Agent and this session is prepared.
    manifest, body = restore_probe(fixture)
    answer = invoke(fixture.hosted, fixture.identity, body)
    assert answer.status_code == 200 and answer.json()["quiet_etag"]
    assert fixture.target_workspace._transfer(manifest).is_dir()


TRAVERSAL = "workspace paths cannot be absolute, aliases or traversals"


@pytest.mark.parametrize("unsafe, check", [
    ({"path": "../escape"}, TRAVERSAL),  # ``relative_path``: before ``check_tree``'s parent check would see ".."
    ({"path": "nested/../../escape"}, TRAVERSAL),
    ({"path": "other/a.txt"}, "every parent directory must be in the manifest"),  # ``check_tree``
    ({"kind": "symlink"}, "literal_error"),  # ``NativeFileEntry.kind``'s Literal: ``file_material`` never runs
    ({"kind": "directory"}, "only a regular file has a content digest"),  # ``file_material``
    ({"byte_length": MAX_FILE_BYTES + 1}, "less_than_equal"), ({"mode": 0o1777}, "less_than_equal"),
    ({"mode": -1}, "greater_than_equal"),
], ids=["traversal", "inner-traversal", "orphan", "symlink", "digest-on-directory", "oversize", "setuid-sticky",
        "negative-mode"])
def test_hosted_refuses_an_unsafe_restore_manifest(file_api, monkeypatch, blob, unsafe, check):
    """Hosted's own wire refuses first (before any staging); its 422 does not say which check did, so the same body is
    validated against that wire here: exactly ``check`` refuses it, and the test fails once that check is removed."""
    fixture = phase5(file_api, monkeypatch, blob)
    manifest, body = restore_probe(fixture)
    body["files_restore"]["entries"][1].update(unsafe)  # past the API's own model, as a forged request would be
    answer = invoke(fixture.hosted, fixture.identity, body)
    assert answer.status_code == 422 and answer.json()["error"] == {
        "code": "NATIVE_REQUEST_INVALID", "message": "only the typed native request is accepted"}  # Hosted's own wire
    assert not fixture.target_workspace._transfer(manifest).exists() and not fixture.target_workspace.root.exists()
    with pytest.raises(ValidationError) as refused:
        native_turns.NATIVE_REQUEST.validate_json(json.dumps(body))
    errors = refused.value.errors()
    assert len(errors) == 1 and check in {errors[0]["type"], errors[0]["msg"].removeprefix("Value error, ")}


def test_hosted_refuses_a_staged_symlink_and_switches_nothing(file_api, monkeypatch, blob):
    """A link where the API staged a file (the manifest itself can not name one): the cut's confirm refuses it."""
    fixture = phase5(file_api, monkeypatch, blob)
    before, is_symlink = abandon.row(fixture), Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda node: "/upload/" in node.as_posix() or is_symlink(node))
    outcome, reason, _fence = cut(fixture)
    assert (outcome, reason) == ("busy", "NATIVE_FILES_UNSAFE")
    assert not fixture.target_workspace.root.exists()
    at_source(fixture, before)


@pytest.mark.parametrize("switch", ["idle_flag", "container_url"])
def test_with_either_switch_off_phase_5_makes_no_call(file_api, monkeypatch, blob, switch):
    fixture = phase5(file_api, monkeypatch, blob, on=False)
    if switch == "container_url":
        sessions(fixture).idle_upgrade = True
    else:
        fixture.case.composition.upgrade_snapshot_container_url = URL
    outcome, reason, fence = cut(fixture)
    assert (outcome, reason) == ("ready", None) and fence is not None  # today's files fence, unchanged
    assert blob.opened == [] and blob.requests == []
    assert not any(command.get("workspace") or command.get("files_restore") for command, _ in fixture.controls)
    _moved_with_files(fixture)
