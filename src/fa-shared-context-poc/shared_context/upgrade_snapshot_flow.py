"""ADR-0009 phase 5b: U1 (snapshot the source's workspace into Blob) and U4 (restore it on the destination), run by
the background cut (``NativeSessions.cut``) after a quiet probe (E0), so the switch itself needs no files fence.

Only with ``FA_NATIVE_IDLE_UPGRADE`` and ``FA_UPGRADE_SNAPSHOT_CONTAINER_URL`` (both off by default); otherwise no
call is made here and the cut keeps its existing files path. API-only and SDK-independent.

- U1: the source's helper (``NativeSessionRead(workspace=True)``) answers its tree and the Agent ETag read before it
  (E_snap). It must still be the probe's E0, which the cut then CASes on, so no turn was admitted from the probe to the
  commit. Each Files download first reads the source's metadata: only an ``active`` source is called (ADR-0009 §10);
  one that went idle stops the snapshot (no manifest): nothing moves and nothing is switched this time.
- Incremental: a file whose path and hash match the Agent's previous published snapshot (same source SID) is copied
  from that snapshot's Blob, not downloaded again. The base is in process memory (shortcut: a restart makes the next
  snapshot full; correctness does not depend on it).
- U4: the published manifest is read back (an unknown format or shape is refused: no restore, no cut), then restored
  through the destination's own fence staging (``NativeWorkspaceFiles``): a probe with ``files_restore`` makes it
  ``prepare`` (refused when its live root holds another tree), the files go to the staging paths, and the cut confirms
  each one (hash, mode), publishes the tree with its empty directories and commits ``files_migrated`` with the digest
  in Hosted's record. The live root is untouched until that publish, so a failed restore costs no fresh destination.

Outcomes for ``cut``: None means "not carried" (flags off, an image without the helper, nothing to carry, a snapshot
or restore that failed): the existing files fence goes on, so the files still move. A tuple is the cut's own outcome
(busy, source idle, stopping): no switch now, the next turn end tries again. A ``NativeFilesManifest`` is the staged
restore the cut commits.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from time import monotonic
from uuid import uuid4

from .behavior_session_files import FILE_STEP_SECONDS
from .errors import Problem
from .native_files_wire import MAX_FILE_BYTES, WORKSPACE_ROOT, NativeFilesManifest, staging_relative_path

log = logging.getLogger(__name__)
_UNCONFIRMED = frozenset({"NATIVE_FILES_UNKNOWN", "NATIVE_FILES_ACCESS_DENIED", "NATIVE_FILES_TIMEOUT"})
# One Files step's own bound (``SessionFiles``: connect 5 s, then a 10 s read or a download's 10 s stream step), so a
# step that starts in time ends in time. shortcut: a step's Blob get and put keep only their client's own timeouts
# (connect 10 s, read 20 s each, no retry), so a step started with this reserve left may end about 45 s past the
# carry's deadline, into the commit's reserve; give them per-call timeouts if that matters.
FILE_RESERVE_SECONDS = FILE_STEP_SECONDS + 5
FALLBACK_READ_SECONDS = 10
# Kept for the cut's commit (Hosted confirms, publishes and CASes) out of the cut's one deadline: the carry ends earlier.
COMMIT_RESERVE_SECONDS = 15
RESTORE_FAILURE_LIMIT = 3  # consecutive failed restore switches for one target, then today's fence moves the files


class _Stopping(Exception):
    pass


@contextmanager
def snapshot_container(url, credential):
    """The dedicated snapshot container (5c); single put/get sizes cover one ``fa-workspace-files-v1`` file."""
    from azure.storage.blob import ContainerClient

    with ContainerClient.from_container_url(
            url, credential=credential, retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
            retry_to_secondary=False, logging_enable=False, connection_timeout=10, read_timeout=20,
            max_single_put_size=MAX_FILE_BYTES, max_single_get_size=MAX_FILE_BYTES) as container:
        yield container


def _invoke(sessions, client, command, timeout):
    """One Hosted call carrying a 5b field; an image before 5b refuses that field (422, only its wire's exact refusal:
    the Foundry client's ``untyped``), remembered per version so it is not asked again at every turn end. Only a
    ``session_read``'s refusal is: that body holds nothing else, so the field itself was refused. A ``session_cut``'s
    may be a 5b wire refusing this one manifest, and any other refusal is this attempt's failure only (``_fallback``'s
    read of the destination then tells an image before 5b apart)."""
    from .behavior_native_turn_api import NativeTurnGateway
    from .behavior_native_turn_wire import NativeSessionRead

    try:
        return NativeTurnGateway._invoke(client, command, timeout=timeout)[0]
    except Problem as error:
        if isinstance(command, NativeSessionRead) and error.status == 422 and error.body["error"].get("untyped") is True:
            sessions._snapshot_unsupported.add(client.target.agent_version)
        raise


def _tree(sessions, client, timeout):
    from .behavior_native_turn_wire import NativeSessionRead

    return _invoke(sessions, client, NativeSessionRead(workspace=True), timeout)


def _files(entries):
    return {entry.path: entry.sha256 for entry in entries if entry.kind == "file"}


def _left(stop, deadline, reserve=0, cap=30):
    """Seconds the next call may take: what is left of the cut's deadline, at most ``cap``. A Files step starts only
    with its own bound (``reserve``) left, so no single upload or download runs past the deadline."""
    if stop is not None and stop.is_set():
        raise _Stopping()
    left = deadline - monotonic()
    if left <= reserve:
        # Not "unknown": past its bound the snapshot has failed, so today's fence moves the files instead.
        raise Problem(503, "UPGRADE_SNAPSHOT_DEADLINE", "the workspace snapshot or restore ran past its bound")
    return min(cap, left)


def carry(sessions, client, source, destination, probe, quiet_etag, bound):
    """``probe``: the cut's quiet ``NativeSessionCut`` probe, sent again with the restore to prepare it. ``bound``: the
    cut's own ``_CutFence``, so the destination wait, this carry and the switch share one deadline."""
    url = getattr(sessions.composition, "upgrade_snapshot_container_url", None)
    if not sessions.idle_upgrade or url is None:
        return None
    if {source.target.agent_version, destination.target.agent_version} & sessions._snapshot_unsupported:
        return None
    failures = sessions._restore_failures.get(_target(destination), 0)
    if failures >= RESTORE_FAILURE_LIMIT:
        # Neal 2026-10-19: a failed snapshot falls back to the fence; a restore failing the same way at every attempt
        # (a rename Hosted is refused, say) would otherwise keep this Agent off the target for good.
        log.warning("NATIVE_UPGRADE_SNAPSHOT outcome=fallback reason=restore_failed failures=%d agent_id=%s", failures,
                    client.target.agent_id)
        return _before_fence(sessions, source, destination)
    started, counts = monotonic(), {"downloaded": 0, "copied": 0, "uploaded": 0}
    try:
        result = _carry(sessions, client, source, destination, probe, quiet_etag, bound.stop, url, counts,
                        bound.deadline - COMMIT_RESERVE_SECONDS)
        reason = result[1] if isinstance(result, tuple) else None
    except _Stopping:
        result, reason = ("stopping", "shutdown", None), "shutdown"
    except Problem as error:
        result, reason = _fallback(sessions, source, destination), error.body["error"]["code"]
        if counts.get("unresolved") or result is not None and result[1] == "restore_held":
            # Not progress (Blob down after a lost CAS, an upload whose read-back failed): counted, so the fence takes
            # over at the limit.
            switched(sessions, destination, failed=True)
    log.warning("NATIVE_UPGRADE_SNAPSHOT outcome=%s reason=%s ms=%d downloaded=%d copied=%d uploaded=%d agent_id=%s",
                "restored" if isinstance(result, NativeFilesManifest) else "fallback" if result is None else result[0],
                reason, round((monotonic() - started) * 1000), counts["downloaded"], counts["copied"],
                counts["uploaded"], client.target.agent_id)
    return result


def _target(destination):
    return destination.target.agent_id, destination.target.agent_version


def switched(sessions, destination, failed):
    """The cut's no-fence switch with a restore: ``failed`` counts one more consecutive failure for this target (shortcut:
    in process memory, so an API restart allows ``RESTORE_FAILURE_LIMIT`` more before the fence); a commit clears it."""
    key = _target(destination)
    if failed:
        sessions._restore_failures[key] = sessions._restore_failures.get(key, 0) + 1
    else:
        sessions._restore_failures.pop(key, None)


def _fallback(sessions, source, destination):
    """Before today's fence: a destination whose live root already holds files (a restore published, then its commit
    lost the CAS) would refuse the fence's ``prepare`` at every attempt, so it gets a fresh SID instead (M5). When it
    holds exactly the Agent's last published snapshot of this source, it is kept (no new VM): the next turn end
    snapshots again, and Hosted's ``prepare`` takes it as is when nothing changed, or refuses it (then a fresh SID)."""
    if destination.target.agent_version in sessions._snapshot_unsupported:
        return None  # an image before 5b never took a restore
    try:
        # Its own short bound: the cut's may be spent (a deadline is one way here), and today's fence follows.
        held = _tree(sessions, destination, FALLBACK_READ_SECONDS)
    except Problem:
        return None
    if not held.workspace_entries:
        return None
    kept = sessions._snapshot_bases.get(destination.target.agent_id)
    if kept is not None and kept[0] == source.target.session_id and kept[2] == (held.workspace_root_mode,
                                                                                 held.workspace_entries):
        return "busy", "restore_held", None
    sessions._raise_attempt(destination, getattr(destination, "_fresh", None))
    return "busy", "restore_conflict", None


def _before_fence(sessions, source, destination):
    """M5 on the fence path too: the fence's ``prepare`` takes a live root holding exactly the source's tree and
    refuses any other at every attempt, so a destination holding another tree gets a fresh SID instead."""
    try:
        held = _tree(sessions, destination, FALLBACK_READ_SECONDS)
        if not held.workspace_entries:
            return None
        live = _tree(sessions, source, FALLBACK_READ_SECONDS)
    except Problem:
        return None  # unread: the fence's own prepare decides
    if (live.workspace_root_mode, live.workspace_entries) == (held.workspace_root_mode, held.workspace_entries):
        return None
    sessions._raise_attempt(destination, getattr(destination, "_fresh", None))
    return "busy", "restore_conflict", None


def _carry(sessions, client, source, destination, probe, quiet_etag, stop, url, counts, deadline):
    from .behavior_native_sessions import _session_metadata
    from .behavior_session_files import SessionFiles
    from .upgrade_snapshot_store import UpgradeSnapshotStore

    composition = sessions.composition
    agent_id, source_sid = client.target.agent_id, source.target.session_id
    state = _tree(sessions, source, _left(stop, deadline))  # U1: an image before 5b refuses the flag (422): the fence
    if state.etag != quiet_etag:
        return "busy", "snapshot_stale", None  # a turn admitted since the probe: the next turn end, incrementally
    if not _files(state.workspace_entries):
        return None  # nothing to carry: the existing path (a direct commit, or empty directories under the fence)
    team = composition.runtime.work_policy.policy.template.team_id
    bases = sessions._snapshot_bases
    with snapshot_container(url, client.credential) as container:
        store = UpgradeSnapshotStore(container, str(composition.config.identity.tenant_id))
        base = None
        kept = bases.get(agent_id)
        if kept is not None and kept[0] == source_sid:
            try:
                base = store.manifest(team, agent_id, kept[1])
            except Problem as error:  # an unknown shape is never a base: this snapshot is full
                log.warning("NATIVE_UPGRADE_SNAPSHOT_BASE_REFUSED code=%s agent_id=%s", error.body["error"]["code"],
                            agent_id)
        reuse = {} if base is None else {entry.path: (index, entry.sha256) for index, entry in enumerate(base.entries)
                                         if entry.kind == "file"}
        idle = []

        def read(entry):
            _left(stop, deadline, FILE_RESERVE_SECONDS)
            kept_file = reuse.get(entry.path)
            if kept_file is not None and kept_file[1] == entry.sha256:
                counts["copied"] += 1
                return store.read_file(base, kept_file[0])
            metadata = _session_metadata(source)  # ADR-0009 §6: before every Files call; never wake it (§10)
            status = "missing" if metadata is None else metadata.status
            if status != "active":
                idle.append(status)
                return None
            counts["downloaded"] += 1
            return SessionFiles(source).download(source_sid, WORKSPACE_ROOT + "/" + entry.path, entry)

        upgrade_id = uuid4().hex
        saved = store.save(team, agent_id, upgrade_id, state.workspace_root_mode, state.workspace_entries, read)
        if not saved.published:
            if idle:  # §6: stop at once, call the source no more; it moves at a turn end that finds it active
                return ("source_idle", "snapshot", None) if idle[-1] == "idle" else ("source_unavailable", idle[-1], None)
            raise Problem(409, saved.reason, "the workspace snapshot was not published")
        bases[agent_id] = (source_sid, upgrade_id, (state.workspace_root_mode, state.workspace_entries))
        return _restore(sessions, store, team, agent_id, upgrade_id, source, destination, probe, quiet_etag, counts,
                        stop, deadline)


def _restore(sessions, store, team, agent_id, upgrade_id, source, destination, probe, quiet_etag, counts, stop,
             deadline):
    """U4 through the destination's fence staging. Only its staging is written before the cut's publish, so a failure
    here leaves the live root as it was and today's fence can still move the files."""
    from .behavior_native_turn_wire import material_digest
    from .behavior_session_files import SessionFiles

    try:
        manifest = store.manifest(team, agent_id, upgrade_id)
    except Problem as error:  # ADR-0009 rule 8: an unknown shape is refused and the last valid state kept
        sessions._snapshot_bases.pop(agent_id, None)
        return "busy", error.body["error"]["code"], None
    if manifest is None:
        sessions._snapshot_bases.pop(agent_id, None)
        return "busy", "snapshot_unpublished", None
    target = destination.target
    tree = {"root_mode": manifest.root_mode, "entries": [entry.model_dump(mode="json") for entry in manifest.entries]}
    try:
        # One staging operation per (source, destination, tree): a retry of the same tree finds its staging built.
        restore = NativeFilesManifest(
            agent_id=agent_id, request_id="restore-" + material_digest(tree)[:56],
            source_session_id=source.target.session_id, source_agent_version=source.target.agent_version,
            target_session_id=target.session_id, target_agent_version=target.agent_version,
            root_mode=manifest.root_mode, entries=manifest.entries)
    except ValueError:
        raise Problem(413, "NATIVE_FILES_LIMIT", "the restore manifest exceeds its bound") from None
    try:
        prepared = _invoke(sessions, destination, probe.model_copy(update={"files_restore": restore}),
                           _left(stop, deadline))
    except Problem as error:
        if error.body["error"]["code"] != "NATIVE_FILES_CONFLICT":
            raise
        # Its live root holds another tree (an earlier restore published, then lost its CAS): a fresh SID (M5).
        sessions._raise_attempt(destination, getattr(destination, "_fresh", None))
        return "busy", "restore_conflict", None
    if prepared.reason == "files_unsupported":
        return None
    if prepared.quiet_etag != quiet_etag:
        return "busy", prepared.reason or "admission_pending", None
    files, unconfirmed = SessionFiles(destination), sessions._restore_unconfirmed
    for index, entry in enumerate(restore.entries):
        if entry.kind != "file":
            continue
        _left(stop, deadline, FILE_RESERVE_SECONDS)
        path = staging_relative_path(restore, index)
        key = (target.session_id, path)
        if key not in unconfirmed:
            try:
                files.upload(target.session_id, path, store.read_file(manifest, index))
                counts["uploaded"] += 1
                continue
            except Problem as error:
                if error.body["error"]["code"] not in _UNCONFIRMED:
                    raise
            # shortcut: in process memory, so after an API restart this path may be PUT once more (only staging; the
            # cut's confirm checks every hash); keep it in Blob if that matters.
            unconfirmed.add(key)
            _left(stop, deadline, FILE_RESERVE_SECONDS)
        # Rule 7, as the fence does: a lost ACK is read back, never PUT again. A read-back that fails leaves through
        # ``carry``'s fallback to today's fence within this cut, and counts as a failed restore.
        try:
            files.download(target.session_id, path, entry)
        except Problem:
            counts["unresolved"] = 1
            raise
        unconfirmed.discard(key)
        counts["uploaded"] += 1
    return restore
