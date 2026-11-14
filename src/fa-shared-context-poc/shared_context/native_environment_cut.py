"""ADR-0009 M1 2d-i: the destination-side short cut (``NativeSessionCut``), run by the new (destination) session.

The API sends it in the background after a turn ends when the binding row's version is not the selected one. The cut
never invokes, reads from or stops the source; it reads the Agent's shared ``fa/v1/turns``:

Two steps (P4 review): ``mode="probe"`` runs the busy checks below and, when quiet, answers E0 (``quiet_etag``) and
commits nothing; the API lists the source's files after that answer, then sends ``mode="cut"`` with
``expected_etag=E0`` (required without files). A turn admitted after E0 moves the ETag, so the cut is busy and its
files are never left behind by a no-files commit.

- Busy, and the Agent record is not written (``state="current"`` with the reason): ``admitting`` is set, any receipt
  is open (``accepted``/``started``, the strict rule: no orphan rule, a source image may predate the start fence), the
  chain task is not quiet, or the ``agent`` ETag moved after E0. An open HITL card keeps its turn's receipt ``started``
  (ADR-0009 §5, gate 6), so every card kind is busy here too. The receipts read are only the open-RID set when it is
  complete for the source, otherwise every receipt, resumable across calls with E0 kept in this session's
  ``following:`` record. A busy call may still write that record and idempotent open-RID cleanups (a stale marker
  cleared, a terminal RID removed).
- Quiet, the source has no workspace files (the API's ``files_required`` is false): one CAS E0 -> ``ready`` (no
  ``pending``; as the §7a abandon), then the receipts other SIDs left never started are adopted (M9; only the
  open-RID set's when it was complete for the source) and the open-RID set is marked complete for this session.
- Quiet, with files: ``environment-before:<RID>`` is saved and one CAS E0 -> the H0-shaped ``pending`` the follow
  writes, with the same ``following:`` record (phase ``files``); the existing follow file stages move the files and
  ``session_follow`` commits (fence <= ~120 s, as today's follow).
- Quiet, with a 5b restore (``files_restore``): the probe ``prepare``s its staging (refused when the live root holds
  another tree); the cut confirms each file the API staged, publishes the tree, then the same CAS commits ``ready``
  with ``files_migrated`` and the manifest digest, so this record, not the API's row, says the files moved.

Every call logs one ``NATIVE_UPGRADE_CUT`` line (ADR-0009 gate 7): outcome, command mode, scan mode
(``set``/``full``/``none``), this call's scan time (the receipt reads only) and receipts read, the task check and
adoption times apart, and the attempt's call count and totals.

``NativeSessionWithdraw`` (below) undoes a ``pending``, this cut's fence or one an older follow left, from any H2d
session; H0 has no such command.
"""

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter

from .behavior_native_turn_wire import NativeEnvironmentView, material_digest
from .errors import Problem

log = logging.getLogger(__name__)


async def cut_session(runtime, command):
    # As the abandon: the commit and the adoption of what other SIDs never started run under the admission lock, so
    # every adopted message is queued before this session admits a new one.
    async with runtime._admission_lock:
        return await _cut(runtime, command)


async def _cut(runtime, command):
    from .native_environment import _key
    from .native_environment_abandon import _source_version

    me, source = runtime.identity.session_id, command.expected_session_id
    current, etag = await runtime._agent_record()
    existing = current.environment
    if current.hosted_session_id == me:
        return await _rerun(runtime, command, current)
    key = _key(runtime)
    if existing is not None and existing.state == "pending":
        return await _own_fence(runtime, command, current, key)
    if current.hosted_session_id != source:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original Agent environment comparison changed")
    restore = command.files_restore
    if restore is not None and (restore.agent_id, restore.target_session_id, restore.target_agent_version) != (
            runtime.identity.agent_id, me, runtime.identity.agent_version):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the restore names another Agent or destination")
    prepared = existing if existing is not None else current.preparation
    view = NativeEnvironmentView(
        agent_id=runtime.identity.agent_id, request_id=command.request_id,
        previous_hosted_session_id=source, previous_agent_version=_source_version(current),
        previous_files_profile=None if prepared is None else prepared.files_profile,
        hosted_session_id=me, hosted_agent_version=runtime.identity.agent_version, state="current")
    return await _attempt(runtime, _CutAttempt(command=command, current=current, etag=etag, key=key, view=view))


async def _rerun(runtime, command, current):
    """A rerun after the commit reads the same ready result; its adoption read finds only what one left, and the
    marker is written again (a crash between the commit's CAS and its mark left none)."""
    from .native_left_receipts import adopt_left
    from .native_open_rids import mark_complete

    me, source, existing = runtime.identity.session_id, command.expected_session_id, current.environment
    if existing is None or (existing.state, existing.hosted_session_id, existing.previous_hosted_session_id) != (
            "ready", me, source):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "this session already holds the Agent from another source")
    await adopt_left(runtime, source)
    if current.admission_started:
        # The scan's Responses finding is not kept; True only widens the next switch's task check.
        await mark_complete(runtime, me, existing.request_id, responses=True)
    return existing


async def _own_fence(runtime, command, current, key):
    """This cut's own file fence: the follow file stages and ``session_follow`` carry it on."""
    from .native_environment import _read

    existing = current.environment
    if (existing.hosted_session_id, existing.request_id, current.hosted_session_id) != (
            runtime.identity.session_id, command.request_id, command.expected_session_id):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "another environment transition is pending")
    record, _ = await _read(runtime, key=key)
    if record is None or record.view.request_id != command.request_id:
        raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original cut progress is missing")
    return record.view


@dataclass
class _CutAttempt:
    """One cut call past its rerun checks: the Agent item read at E0, the view it answers with, the following record's
    ETag, and what its ``NATIVE_UPGRADE_CUT`` line logs (this call's numbers, the attempt's totals before it)."""
    command: object
    current: object
    etag: str
    key: str
    view: NativeEnvironmentView
    record_etag: str | None = None
    responses: bool = False
    calls: int = 1
    total_ms: int = 0
    total_receipts: int = 0
    mode: str = "none"
    scan_ms: int = 0
    receipts: int = 0
    task_ms: int = 0
    adopt_ms: int = 0

    def done(self, outcome, result):
        command = self.command
        log.warning("NATIVE_UPGRADE_CUT outcome=%s reason=%s mode=%s scan=%s scan_ms=%d receipts=%d task_ms=%d "
                    "adopt_ms=%d calls=%d total_scan_ms=%d total_receipts=%d request_id_prefix=%s "
                    "source_session_id=%s session_id=%s files_required=%s", outcome, result.reason, command.mode,
                    self.mode, self.scan_ms, self.receipts, self.task_ms, self.adopt_ms, self.calls,
                    self.total_ms + self.scan_ms, self.total_receipts + self.receipts, command.request_id[:8],
                    command.expected_session_id, self.view.hosted_session_id, command.files_required)
        return result


async def _attempt(runtime, attempt):
    from .native_open_rids import check_hold
    from .native_turns import _changed

    command, view, existing = attempt.command, attempt.view, attempt.current.environment
    # The last point where the record names how the source became the Agent's.
    await check_hold(runtime, command.expected_session_id, None if existing is None else existing.request_id)
    reason = await _busy(runtime, attempt)
    if reason is not None:
        return attempt.done("scanning" if reason == "scan_pending" else "busy", _changed(view, reason=reason))
    if command.mode == "probe":
        if command.files_restore is not None:
            # Before the API's staging uploads.
            await asyncio.to_thread(runtime.workspace_files.prepare, command.files_restore)
        # Quiet at E0: the API lists the source's files after this answer and cuts with ``expected_etag=E0``.
        return attempt.done("quiet", _changed(view, quiet_etag=attempt.etag))
    fenced, replacement = await _fence(runtime, attempt)
    if not await _replaced(runtime, attempt.current, attempt.etag, replacement):
        # Read back as another value: a write after E0 (an admission) won the CAS. The Agent record is unchanged.
        return attempt.done("busy", _changed(view, reason="admission_pending"))
    if command.files_required:
        return attempt.done("files_pending", fenced)
    await _adopt(runtime, attempt)
    return attempt.done("ready", fenced)


async def _busy(runtime, attempt):
    """Why the cut is busy (``scan_pending``: its scan goes on in the next call), or None when quiet at E0."""
    command, current = attempt.command, attempt.current
    if (command.files_required and (runtime.workspace_files is None or not current.following_supported)
            or command.files_restore is not None and runtime.workspace_files is None):
        return "files_unsupported"
    if current.admitting is not None or command.expected_etag not in {None, attempt.etag}:
        return "admission_pending"
    return await _scan(runtime, attempt)


async def _scan(runtime, attempt):
    from .native_environment import _CutScan, _Following, _ScanProgress, _drain_pages, _progress, _read, _tasks_pending
    from .native_turns import _changed

    command, current, source = attempt.command, attempt.current, attempt.command.expected_session_id
    record, attempt.record_etag = await _read(runtime, key=attempt.key)
    scan = None if record is None else record.cut
    if scan is not None and (scan.etag, record.view.request_id, record.view.previous_hosted_session_id) != (
            attempt.etag, command.request_id, source):
        # Another attempt's progress, or the ETag moved since its E0: this call is a new attempt with a new E0.
        scan = None
    if scan is None:
        progress, responses = _ScanProgress(cursor_source="none"), False
    else:
        progress, responses = _ScanProgress.from_record(record), bool(record.responses)
        attempt.calls, attempt.total_ms, attempt.total_receipts = scan.calls + 1, scan.scan_ms, scan.receipts
    started = perf_counter()
    reason, after, attempt.responses = await _drain_pages(
        runtime, progress.after, source, current.task_id, responses=responses, idle_only=True, progress=progress,
        restart_changed_page=True, settled=frozenset(command.settled_session_ids))
    attempt.scan_ms = round((perf_counter() - started) * 1000)
    attempt.mode, attempt.receipts = progress.mode or "none", progress.receipts
    if reason is None:
        started = perf_counter()
        reason = await _tasks_pending(runtime, current, source, responses=attempt.responses)
        attempt.task_ms = round((perf_counter() - started) * 1000)
    if reason == "scan_pending":
        await _progress(runtime, _Following(
            view=_changed(attempt.view, reason="scan_pending"), after=after, cursor_profile="page-last-id-v1",
            page_offset=progress.page_offset, page_digest=progress.page_digest, responses=attempt.responses,
            idle_only=True, files_required=command.files_required,
            cut=_CutScan(etag=attempt.etag, calls=attempt.calls, scan_ms=attempt.total_ms + attempt.scan_ms,
                         receipts=attempt.total_receipts + attempt.receipts)), attempt.record_etag, key=attempt.key)
    return reason


async def _fence(runtime, attempt):
    """The view the cut commits and the Agent item that names it: with files the ``pending`` fence, else ``ready``."""
    from .native_environment import _Following, _progress, save_before
    from .native_turns import TURNS_STORE, _changed, binding_task_id

    command, current, view, restore = attempt.command, attempt.current, attempt.view, attempt.command.files_restore
    if command.files_required:
        # The H0-shaped fence of today's follow, after its drain: H0's file stages decode this record and require
        # this ``pending``. The record goes first, so a lost CAS leaves only a record no ``pending`` names.
        fenced = _changed(view, state="pending", reason="files_pending")
        await _progress(runtime, _Following(
            view=fenced, phase="files", cursor_profile="page-last-id-v1", responses=attempt.responses, idle_only=True,
            files_required=True), attempt.record_etag, key=attempt.key)
        await save_before(runtime, current, command.request_id)
        return fenced, _changed(current, environment=fenced)
    if restore is not None:
        # Published before the CAS: a lost CAS leaves true files on a destination the Agent never moved to.
        await asyncio.to_thread(_restore, runtime.workspace_files, restore)
    elif runtime.workspace_files is not None:
        runtime.workspace_files.initialize()
    fenced = _changed(view, state="ready", reason=None,
                      files_profile="fa-workspace-files-v1" if runtime.workspace_files is not None else None,
                      **({} if restore is None else {"files_migrated": True, "files_manifest_sha256": restore.sha256}))
    async with runtime._store(TURNS_STORE) as store:
        await runtime._immutable(store, "environment:" + material_digest(fenced), fenced)
    return fenced, _changed(current, hosted_session_id=view.hosted_session_id, environment=fenced,
                            following_supported=True,
                            chain_task_id=binding_task_id(runtime.identity.agent_id, view.hosted_session_id))


async def _adopt(runtime, attempt):
    from .native_left_receipts import adopt_left
    from .native_open_rids import mark_complete, read as read_open

    source = attempt.command.expected_session_id
    started = perf_counter()
    # A set complete for the source lists every open receipt (none was admitted since the scan: the CAS above).
    open_set = await read_open(runtime) if attempt.mode == "set" else None
    await adopt_left(runtime, source, rids=open_set.rids if open_set is not None and open_set.complete_for == source
                     else None)
    attempt.adopt_ms = round((perf_counter() - started) * 1000)
    if attempt.current.admission_started:
        # The scan above saw every open receipt; from this commit only this session admits, and adds each RID.
        await mark_complete(runtime, attempt.view.hosted_session_id, attempt.command.request_id,
                            responses=attempt.responses)


async def _replaced(runtime, current, etag, replacement):
    """CAS ``replacement`` over ``current`` at ``etag``; False when the read-back found another value (a write after
    ``etag`` won), so the Agent record is unchanged."""
    from .native_environment import _save_agent

    try:
        await _save_agent(runtime, current, etag, replacement)
    except Problem as error:
        if error.body["error"]["code"] != "NATIVE_ENVIRONMENT_UNKNOWN":
            raise
        return False
    return True


def _restore(files, manifest):
    """ADR-0009 5b U4 through the fence's own staging: modes, empty directories and hashes as the manifest says."""
    files.prepare(manifest)
    for index in range(len(manifest.entries)):
        files.confirm(manifest, index)
    files.publish(manifest)


async def withdraw_session(runtime, command):
    """ADR-0009 lifecycle "pending 切換用法" ①: one CAS (If-Match ``expected_etag``) puts back what the named
    ``pending`` replaced: ``environment-before:<RID>`` when it was saved (this cut, or a follow of this image), else, for
    a ``pending`` an older image's follow left, the ready view rebuilt from ``pending.previous_*`` (that write replaced
    the ready view; nothing else is left of it). A committed switch is answered as committed (the API records it);
    whichever of the withdraw and the commit lands its CAS first wins, the other reads it back. A ``pending`` whose
    files progress records FA's stop of the source is never withdrawn: the state is answered with it still in place,
    and the caller drives it to its commit. Returns the Agent's state as one read sees it."""
    from .native_environment import _EnvironmentBefore, before_key
    from .native_turns import TURNS_STORE, _decode

    current, etag = await runtime._agent_record()
    if _committed(command, current):
        return _answer(runtime, current, etag)
    view = current.environment
    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item(before_key(command.pending_request_id))
    before = None if item is None else _decode(item.value, _EnvironmentBefore)
    if not _names_pending(command, view):
        return _withdrawn(runtime, command, current, etag, before)
    if await _stop_recorded(runtime, view):
        # ADR-0009 "例外才接手": once FA's stop of the source is recorded the fence is only driven to its commit,
        # whichever API replica asks. shortcut: the stop is recorded after the platform accepts it, so a stop accepted
        # but not yet recorded is covered only by the API row's marker; record it before the stop to close that window.
        log.warning("NATIVE_UPGRADE_WITHDRAW_REFUSED request_id_prefix=%s pending_session_id=%s",
                    command.pending_request_id[:8], command.pending_session_id)
        return _answer(runtime, current, etag)
    if etag != command.expected_etag:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the pending transition changed; read it again")
    if not await _replaced(runtime, current, etag, _restored(command, current, view, before)):
        current, etag = await runtime._agent_record()
        if _committed(command, current):
            return _answer(runtime, current, etag)
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the pending transition changed; read it again") from None
    log.warning("NATIVE_UPGRADE_WITHDRAWN request_id_prefix=%s pending_session_id=%s restored=%s",
                command.pending_request_id[:8], command.pending_session_id, "saved" if before is not None else "rebuilt")
    current, etag = await runtime._agent_record()
    return _answer(runtime, current, etag)


def _answer(runtime, record, etag):
    from .behavior_native_turn_wire import NativeSessionState

    return NativeSessionState(
        agent_id=record.agent_id, hosted_session_id=record.hosted_session_id, etag=etag,
        preparation=record.preparation, environment=record.environment,
        admission_request_id=None if record.admitting is None else record.admitting.request_id,
        actual_version=runtime.identity.agent_version)


def _committed(command, record):
    view = record.environment
    return (view is not None and view.state == "ready" and record.hosted_session_id == command.pending_session_id
            and (view.request_id, view.hosted_session_id) == (command.pending_request_id, command.pending_session_id))


def _names_pending(command, view):
    return view is not None and view.state == "pending" and (view.request_id, view.hosted_session_id) == (
        command.pending_request_id, command.pending_session_id)


def _withdrawn(runtime, command, current, etag, before):
    """Already withdrawn, by this call's earlier attempt or another withdraw: the record is what it wrote."""
    view = current.environment
    if (before is not None and (current.hosted_session_id, view) == (before.hosted_session_id, before.environment)
            or view is not None and view.state == "ready" and view.request_id == command.request_id):
        return _answer(runtime, current, etag)
    raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the named pending transition is not the Agent's")


async def _stop_recorded(runtime, view):
    """Whether this ``pending``'s own files progress records FA's stop of its source."""
    from .native_environment import _original_progress

    try:
        record = (await _original_progress(runtime, view))[0]
    except Problem as error:
        if error.body["error"]["code"] not in {"NATIVE_ENVIRONMENT_UNKNOWN", "NATIVE_ENVIRONMENT_CONFLICT"}:
            raise
        # No progress of its own (a legacy pending), or a retained retarget that names another pending: nothing says
        # FA stopped this pending's source, so an unmarked fence can still be withdrawn.
        return False
    return record is not None and record.files is not None and record.files.stop_started


def _restored(command, current, view, before):
    from .native_turns import _changed

    try:
        if before is not None:
            if before.hosted_session_id != current.hosted_session_id:
                raise ValueError("the saved environment belongs to another session")
            return _changed(current, environment=before.environment)
        # The rebuilt view names this withdraw as its request and the withdrawn target as its previous session:
        # the source's open-RID marker (from the request that first made it current) is then checked again by a
        # full scan at the next switch, never trusted from a guess.
        if view.previous_hosted_session_id != current.hosted_session_id:
            raise ValueError("the pending transition names another source session")
        return _changed(current, environment=NativeEnvironmentView(
            agent_id=current.agent_id, request_id=command.request_id,
            previous_hosted_session_id=view.hosted_session_id, previous_agent_version=view.hosted_agent_version,
            hosted_session_id=view.previous_hosted_session_id, hosted_agent_version=view.previous_agent_version,
            files_profile=view.previous_files_profile, state="ready"))
    except ValueError:
        raise Problem(409, "NATIVE_ENVIRONMENT_UNAVAILABLE",
                      "the environment before this pending transition can not be restored") from None
