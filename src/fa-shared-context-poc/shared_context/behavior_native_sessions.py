"""Explicit API-MI bootstrap; normal turn traffic never creates or changes a session."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import random
import re
from threading import Event, Lock, RLock
from time import monotonic, sleep

from azure.core.exceptions import AzureError, ResourceNotFoundError

from .behavior_agent_versions import SelectorUnreadable
from .behavior_foundry import PLATFORM_NO_RETRY, PLATFORM_READ_RETRY, FoundryTarget
from .behavior_timing import timing_id
from .behavior_native_turn_wire import (
    NATIVE_STATE_PROFILE, NativeEnvironmentMetadata, NativeEnvironmentReplacementPreview, NativeEnvironmentView, NativeSessionMetadata,
    NativeSessionAbandon, NativeSessionFollow, NativeSessionRead, NativeSessionPrepare, NativeSessionRetarget,
    NativeSessionFilesOmission, NativeUnfinishedRead,
    NativeEnvironmentCommand, NativeEnvironmentRetargetCommand, material_digest,
)
from . import upgrade_snapshot_flow
from .errors import Problem
from .native_binding import (
    INCOMPATIBLE_STATE_PROFILE, SUPPORTED_STATE_PROFILES, NativeBindingUpgrade, SessionGuard, _storable,
    binding_store, idle_upgrade_enabled, session_guard_enforced,
)


class _RetiredSession(Problem):
    """A known terminal platform session is not a candidate for new execution."""


class _MetadataUnconfirmed(Problem):
    """The platform did not say the session is gone: its metadata read failed (not 401/403) or found it creating or
    updating. A process that used the SID within ``READER_IDLE_SECONDS`` keeps calling it, as main did."""


# Foundry deletes an unused session after 30 days (Learn: manage-hosted-sessions); a process that used a binding within
# this long is warm: a metadata read that fails or runs out of time keeps the SID, as main's day-long reuse did.
READER_IDLE_SECONDS = 24 * 3600
# A session can also be deleted or fail at any time, and invoking a deleted SID makes the platform re-create it empty
# (dev 2026-10-15). So anything that invokes the row's SID (sends and reads alike) reads its platform metadata in its
# own request, alongside its selector read (no added serial step); a read started at most this long ago is reused, so
# a burst of sends or 1 s status polls share one read.
METADATA_REUSE_SECONDS = 10
# A warm process's metadata read holds a request at most this long after the request's own selector read started, with
# no retry (azure-core per-call transport timeouts end the read itself); past it the SID is kept as on main.
METADATA_BUDGET_SECONDS = 2
# Another process (agent-versions sync, a follow, a deploy) can move the endpoint selector while a reader is kept.
# Invoking a session of an unselected version makes the platform bring it back (possibly as another version), so a
# kept reader is used only after a selector read that started at most this long ago still names its version.
# Only reads and polls use this window; anything that delivers work (submit, owner writes, routine, Agent relay,
# prewarm) uses a selector read started within its own request.
SELECTOR_CHECK_SECONDS = 10
# A read or status poll keeps using a reader whose version the selector confirmed at most this long ago while the
# selector itself cannot be read (SelectorUnreadable); work delivery never does.
SELECTOR_STALE_READ_SECONDS = 120
# ADR-0009 A2d compatibility floor: a version created before 9aaa0824 (2026-10-02 03:08:16 +08) runs an image without
# Hosted's pending refusal and per-RID admission CAS, so it is incompatible even when it declares foundry-native-v1.
COMPATIBILITY_FLOOR = datetime(2026, 10, 1, 19, 8, 16, tzinfo=timezone.utc)
# Refusals meaning the cached binding moved: the platform is read once more (reads are idempotent).
_MOVED = frozenset({"NATIVE_SESSION_CHANGED", "AGENT_VERSION_FOLLOWING", "FOUNDRY_NATIVE_BINDING_MISMATCH"})
# ADR-0009 §7a (m): a compatible source must read retired twice, at least this far apart, before it is abandoned.
# Without FA_NATIVE_SESSION_GUARD_ENFORCE: across follow attempts, never by sleeping in a request, kept in memory (a
# restart starts the pair again). With it (2d-i P6): the first read is durable on the binding row, and a send waits
# for the second read inside its own in-request wait (ADR M1 "來源 404 或已失效").
RETIRED_CONFIRM_SECONDS = 5
# ...and at most this far apart: an older first read starts a new pair (two reads minutes apart are not a confirmation).
RETIRED_PAIR_SECONDS = 60
_RETIRED = frozenset({"failed", "deleted", "expired", "missing"})
# Follow results that wait only on work left on the source (ADR-0009 §7a applicability (iii)).
_WORK_WAITS = frozenset({"accepted_or_started", "admission_pending", "task_active", "task_unconfirmed", "task_access_denied"})
_WORK_WAIT_CHECKS = frozenset({"active_waiting", "admission_pending", "execution_active", "task_active",
                               "task_unconfirmed", "task_access_denied"})
# The platform's documented idle timeout range ends at one hour; an unreadable setting assumes it.
_IDLE_TIMEOUT_FALLBACK_SECONDS = 3600
# ADR-0009 phase 3: a row-located call reads the selector alongside the row SID's platform metadata (both were already
# on main's path, one after the other); only a call that needs both uses a worker. Sized above anyio's default 40
# worker threads (FastAPI's sync endpoints; nothing here sets another limiter) plus the background callers, so a
# request never queues behind another request's selector read.
_LOCATE = ThreadPoolExecutor(max_workers=64, thread_name_prefix="fa-native-locate")
# ADR-0009 M1 2d-i step 2: the background cut's files fence (``pending`` to commit) lasts about this long at most, as
# today's follow; past it the background job withdraws it and the next turn end tries again. Once FA's stop of the
# source may be accepted the fence is never withdrawn (ADR-0009 "例外才接手"): it is driven forward to its commit.
CUT_FENCE_SECONDS = 120
# The cut's wait for a new destination to be ready (and its sends before any fence), under the same ``_CutFence``.
CUT_DESTINATION_SECONDS = 120
# The fence sends FA's stop of the source only with this much of its bound left, so its marker is written before
# another replica may judge the fence abandoned (older than ``CUT_FENCE_SECONDS``, no marked stop).
_STOP_RESERVE_SECONDS = 15
# After FA's stop a fence is driven past its bound (with a warning) for at most this much longer; then the pending is
# left in place, never withdrawn, for the next trigger.
CUT_DRIVE_SECONDS = 120
# ADR-0009 "共用 store 一直不安靜": an ``admitting`` seen this long without becoming a receipt is abandoned by the §7a
# path. With the flag its start is durable (the first cut or blocked send that saw it); without, in this process.
STUCK_ADMISSION_SECONDS = 300
# Dev-only fault hook (gate 2): ``after_pending`` leaves the files fence without progress until its bound withdraws it.
# ``deploy/stage/ci_deploy.py`` refuses it for stage.
CUT_FAULT_ENV = "FA_NATIVE_CUT_FAULT"
_FENCE_STEPS = frozenset({"files_pending", "source_stop_required", "source_active"})
# ADR-0009 fence lifecycle: drive errors after FA's stop that no retry can clear. The files differ from their saved
# manifest or exceed a bound (CONFLICT, LIMIT, UNSAFE), the source listing changed (SOURCE_CHANGED), the source was
# never stopped by FA (SOURCE_UNAVAILABLE), the transfer has no verified manifest (UNAVAILABLE, UNSUPPORTED). With the
# staging verified, SOURCE_UNAVAILABLE and UNAVAILABLE say the source is awake again (``_WOKEN``) only when its
# metadata then reads it active: never unmovable, the drive waits. Otherwise they are the destination's own: a stuck
# ``admitting`` (409) stays unmovable, an OSError (503) is transient. Every other error is transient (5xx, 429, timeouts, a slow download
# (NATIVE_FILES_TIMEOUT),
# NATIVE_FILES_UNKNOWN, Session Files 401/403 (ACCESS_DENIED: a role assignment can be fixed), a metadata read, an ETag
# race), bounded by ``CUT_TRANSIENT_SECONDS``. Confirmed by a second read ``RETIRED_CONFIRM_SECONDS`` later.
_UNMOVABLE = frozenset({
    "NATIVE_FILES_CONFLICT", "NATIVE_FILES_LIMIT", "NATIVE_FILES_UNSAFE", "NATIVE_FILES_SOURCE_CHANGED",
    "NATIVE_FILES_SOURCE_UNAVAILABLE", "NATIVE_FILES_UNAVAILABLE", "NATIVE_FILES_UNSUPPORTED",
})
_WOKEN = frozenset({"NATIVE_FILES_SOURCE_UNAVAILABLE", "NATIVE_FILES_UNAVAILABLE"})
# A fence whose drive has failed only with transient errors this long (from a durable start with the flag, else in
# this process) is treated as unmovable.
CUT_TRANSIENT_SECONDS = 1800
# P9 gate, limit ④ (flag on): a fence whose destination session definitely fails this long (durable, from the first
# such read; a 5xx outage does not count) is left: withdrawn by a fresh session when FA's stop is not marked, else by
# the §7a abandon path to a fresh SID. Sends meanwhile wait (``server_following``), so it stays well inside the
# workbench's 600 s same-RID resend window, as the stuck-admission bound does.
FENCE_BROKEN_SECONDS = 300
# The operator's ``--clear`` drives a marked fence this many passes, this far apart, before a no-files commit.
CLEAR_DRIVE_PASSES = 5
CLEAR_PASS_SECONDS = 2
_clear_sleep = sleep
# ADR-0009 §5 / 4d crash-loop (``FA_NATIVE_IDLE_UPGRADE``): this many crashes, of as many distinct CIDs and RIDs, within
# this long after a switch veto the new version and roll the Agent back.
CRASH_LOOP_SECONDS = 1800
CRASH_LOOP_COUNT = 3
# ADR-0009 U3: the destination reads back this many recent conversations' checkpoints (the wire's bound); this many
# failed checks of one target veto it for this Agent.
HEALTH_CHECK_CONVERSATIONS = 5
HEALTH_VETO_COUNT = 3
# One conversation per call, newest first, all of U3 within this many seconds (each call gets what is left as its own
# timeout, so U3 stays under the client's 30 s); no call starts with less than the minimum left: the older ones are
# then not checked this time. A CID refused with NATIVE_STATE_SCOPE this many times for one target is skipped.
HEALTH_CHECK_SECONDS = 20
HEALTH_CALL_MIN_SECONDS = 5
HEALTH_SCOPE_REPEATS = 2
# After a failed check the next one waits this long plus a random part of it up to as long again (§5's jitter).
HEALTH_RETRY_SECONDS = 60
# ADR-0009 U9: this many recent conversations are restored into the destination before the switch (the wire's bound).
PRERESTORE_CONVERSATIONS = 2
# U9's stop (at most 10 s) and prewarm (at most 30 s) after the switch, measured from U9's own start.
FINISH_SECONDS = 40
# The version a 4d rollback goes to, set only inside that rollback's own ``cut`` (each background attempt runs in its
# own context copy, so no send or other Agent sees it): every selector read of that cut names it instead.
_ROLLBACK = ContextVar("fa_native_rollback", default=None)


def _unvetoed(row, version):
    """The version a §7a carry-out of ``row``'s decision goes to: ``version``, unless this Agent vetoed it (4d: it
    rolled back from it, so 42 -> 41 -> 42 never happens); then a fresh SID of the current version (M5). When the
    decision is that the current version is incompatible a fresh SID of it is no better, so ``version`` it is: the Agent
    keeps serving, and the next rollback from it ends in ``target_incompatible``, recorded once (``rollback_blocked``)."""
    vetoed = () if row.idle_upgrade is None else row.idle_upgrade.vetoed_versions
    if version == row.created_agent_version or version not in vetoed:
        return version
    if row.abandon is not None and row.abandon.basis == "incompatible":
        return version
    logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_SWITCH_VETOED agent_id=%s version=%s instead=%s",
                                        row.agent_id, version, row.created_agent_version)
    return row.created_agent_version


def _wall(store):
    """The binding store's trusted UTC clock, which every durable bound (``NativeBindingClock``) is measured by."""
    return store.clock()


class _FenceOverrun(Exception):
    """The files fence ran past ``CUT_FENCE_SECONDS`` or the API is shutting down."""


class _CutFence:
    """The background cut's ``before_send``: raises ``_FenceOverrun`` past the bound or on shutdown until FA's stop of
    the source is marked (``stopping``); from there on it never raises, so a stop the platform may have accepted is
    always driven to its commit. ``mark``: writes the fence's durable marker, True once it is confirmed."""

    def __init__(self, seconds, stop, mark=None):
        self.started, self.stop, self.mark, self.stopped = monotonic(), stop, mark, False
        self.deadline = self.started + seconds

    def __call__(self, reserve=0):
        if not self.stopped and (self.deadline - monotonic() < reserve or self.stop.is_set()):
            raise _FenceOverrun()

    def stopping(self):
        """Right before every ``stop_session`` until the marker is written: the reserve, then the marker itself."""
        if self.stopped:
            return
        self(_STOP_RESERVE_SECONDS)
        if self.mark is None or not self.mark():
            raise _FenceOverrun()  # no confirmed marker: no stop is sent, and the fence is withdrawn
        self.stopped = True


def _left(bound, cap=30):
    """Seconds one background call may take: what is left of the cut's ``bound``, at most ``cap``; ``bound()`` raises
    ``_FenceOverrun`` past it or on shutdown."""
    bound()
    return min(cap, bound.deadline - monotonic())


def cut_request_id(agent_id, source_session_id, session_id):
    """One RID per (source, destination), so a busy cut's resumable scan and its own fence are found again."""
    return material_digest({"kind": "fa-native-cut-v1", "agent": agent_id, "source": source_session_id,
                            "target": session_id})


def _without_settled(command):
    """``command`` without ``settled_session_ids`` (the same object when it carries none)."""
    if isinstance(command, NativeSessionFollow) and command.settled_session_ids:
        return command.model_copy(update={"settled_session_ids": ()})
    if isinstance(command, NativeSessionFilesOmission) and command.follow.settled_session_ids:
        return command.model_copy(update={"follow": command.follow.model_copy(update={"settled_session_ids": ()})})
    return command


def abandon_request_id(agent_id, source_session_id, session_id):
    return material_digest({"kind": "fa-abandon-source-v1", "agent": agent_id, "source": source_session_id,
                            "target": session_id})


def _scope(target):
    return target.executor_id, target.project_endpoint, target.agent_id, target.agent_name, target.agent_oid


def native_session_id(target, client_id, *, version=None):
    if not isinstance(client_id, str) or not client_id:
        raise Problem(503, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE", "the original API managed identity is required")
    material = ["fa-native-api-session-v1", target.project_endpoint, target.agent_id, client_id]
    if version is not None:
        if not isinstance(version, str) or re.fullmatch(r"[1-9][0-9]{0,15}", version) is None:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "one concrete version is required")
        material = ["fa-native-version-session-v1", *material[1:], version]
    return "fa-native-" + hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()[:48]


def fresh_session_id(target, client_id, *, current_session_id, version, attempt=0):
    """ADR-0009 M5 (phase 3): a new SID derived from the Agent's current SID, the target version and an attempt
    number, so the same version can get a fresh session. The same ``fa-native-`` prefix and length as generation 0
    (``native_session_id``), which stays valid: the binding row, not the SID's shape, locates the Agent's session."""
    if not isinstance(client_id, str) or not client_id:
        raise Problem(503, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE", "the original API managed identity is required")
    if not isinstance(version, str) or re.fullmatch(r"[1-9][0-9]{0,15}", version) is None:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "one concrete version is required")
    if not isinstance(current_session_id, str) or not current_session_id or type(attempt) is not int or attempt < 0:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the current SID and an attempt number are required")
    material = ["fa-native-session-v2", target.project_endpoint, target.agent_id, client_id, current_session_id,
                version, attempt]
    return "fa-native-" + hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()[:48]


def _target(original, **changes):
    return FoundryTarget.model_validate_json(original.model_copy(update=changes).model_dump_json())


def _session_metadata(client, **options):
    """``options``: azure-core client settings replacing the default retry and timeouts (``_located``'s budget)."""
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import VersionRefIndicator

    try:
        with AIProjectClient(endpoint=client.target.project_endpoint, credential=client.credential,
                             **(options or {**PLATFORM_READ_RETRY, "connection_timeout": 5, "read_timeout": 10})
                             ) as project:
            session = project.agents.get_session(client.target.agent_name, client.target.session_id)
    except ResourceNotFoundError:
        return None
    except AzureError as error:
        status = getattr(error, "status_code", None)
        logging.getLogger(__name__).warning("NATIVE_BOOTSTRAP_PLATFORM_READ status=%s",
                                           status if type(status) is int else None)
        if status in {401, 403}:
            raise Problem(status, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE",
                          "the original platform session could not be read") from None
        raise _MetadataUnconfirmed(503, "NATIVE_BOOTSTRAP_UNKNOWN",
                                   "the original platform session could not be read") from None
    indicator = session.version_indicator
    if (session.agent_session_id != client.target.session_id or not isinstance(indicator, VersionRefIndicator)
            or not isinstance(indicator.agent_version, str)
            or re.fullmatch(r"[1-9][0-9]{0,15}", indicator.agent_version) is None):
        raise Problem(409, "NATIVE_SESSION_CHANGED", "the platform session identity or concrete version differs")
    return session


def _read_platform_session(client, **options):
    session = _session_metadata(client, **options)
    if session is None:
        return None
    if session.status in {"failed", "deleted", "expired"}:
        raise _RetiredSession(503, "NATIVE_BOOTSTRAP_UNKNOWN",
                              "the original session is retired; no replacement was created")
    if session.status in {"creating", "updating"}:
        raise _MetadataUnconfirmed(503, "NATIVE_BOOTSTRAP_UNKNOWN",
                                   "the original session is not ready; no replacement was created")
    if session.status not in {"active", "idle"}:
        raise Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "the original session is not ready; no replacement was created")
    return _target(client.target, agent_version=session.version_indicator.agent_version)


def _active_session(client):
    """Platform metadata (it wakes nothing) says ``client``'s session is active on the version it was recorded with."""
    session = _session_metadata(client)
    return (session is not None and session.status == "active"
            and session.version_indicator.agent_version == client.target.agent_version)


def _source_session_status(client, phase, observe=None):
    """``observe(session)``: the metadata this read already has (ADR-0009 §2 claim); it reads nothing more."""
    session = _session_metadata(client)
    if session is None or not isinstance(session.status, str) or session.status not in {
            "active", "idle", "creating", "updating", "deleting", "failed", "deleted", "expired"}:
        raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original source session status is unconfirmed")
    observed_version = session.version_indicator.agent_version
    if observed_version != client.target.agent_version:
        logging.getLogger(__name__).warning(
            "NATIVE_ENVIRONMENT_SOURCE_VERSION_CHANGED phase=%s session_id=%s recorded_version=%s observed_version=%s",
            phase, client.target.session_id, client.target.agent_version, observed_version)
    if observe is not None:
        observe(session)
    return session.status


def _native_profile(client, *, floor=False):
    """The native state profile ``client``'s version definition enables (None when it enables none, or with
    ``floor`` when the version was created before ``COMPATIBILITY_FLOOR``). One ``get_version`` read: no session is
    called (ADR-0009 §3)."""
    from azure.ai.projects import AIProjectClient
    try:
        with AIProjectClient(endpoint=client.target.project_endpoint, credential=client.credential,
                             **PLATFORM_READ_RETRY,
                             connection_timeout=5, read_timeout=10) as project:
            version = project.agents.get_version(client.target.agent_name, client.target.agent_version)
    except AzureError as error:
        status = getattr(error, "status_code", None)
        raise Problem(status if status in {401, 403} else 503, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE",
                      "the selected Hosted version could not be read") from None
    definition = version.definition
    environment = definition.get("environment_variables") if definition is not None else None
    if (version.name != client.target.agent_name or version.version != client.target.agent_version
            or definition is None or definition.get("kind") != "hosted" or not isinstance(environment, dict)):
        return None
    created = getattr(version, "created_at", None)
    if floor and isinstance(created, datetime) and created < COMPATIBILITY_FLOOR:
        return None
    profile = environment.get("FA_NATIVE_TURNS")
    return profile if isinstance(profile, str) else None


def _require_native_version(client):
    if _native_profile(client) not in SUPPORTED_STATE_PROFILES:
        raise Problem(409, "NATIVE_TURNS_DISABLED", "the selected build version must explicitly enable native turns")


def _latest_version_reader(client, client_id, selected):
    """This API identity's active or idle version session: ``selected``'s when listed, else any other version's.

    Generation 0 only (version-derived SIDs): it locates an Agent that has no binding row yet. With a row, the row
    locates the session (ADR-0009 phase 3), so fresh (M5) SIDs never need to be listed.

    Versions are not ordered (neither the listing nor the version numbers say which is current), so another
    version's session only says that one exists. Listing invokes no session; a caller invokes the result only
    when it is ``selected``'s (``_require_selected_version``). The bound is one page of 100 listed sessions of any
    status and any identity; a listing that goes past it before ``selected``'s session appears is 503
    ``NATIVE_ENVIRONMENT_UNAVAILABLE``."""
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import VersionRefIndicator

    found = {}
    try:
        with AIProjectClient(endpoint=client.target.project_endpoint, credential=client.credential,
                             **PLATFORM_READ_RETRY,
                             connection_timeout=5, read_timeout=10) as project:
            for index, session in enumerate(project.agents.list_sessions(client.target.agent_name, limit=100, order="desc")):
                if index >= 100:
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the native locator is outside the bounded session page")
                indicator = session.version_indicator
                if session.status not in {"active", "idle"} or not isinstance(indicator, VersionRefIndicator):
                    continue
                version = indicator.agent_version
                if not isinstance(version, str) or re.fullmatch(r"[1-9][0-9]{0,15}", version) is None:
                    continue
                if session.agent_session_id == native_session_id(client.target, client_id, version=version):
                    found[version] = session.agent_session_id
                    if version == selected:
                        break
    except AzureError as error:
        status = getattr(error, "status_code", None)
        raise Problem(status if status in {401, 403} else 503, "NATIVE_ENVIRONMENT_UNAVAILABLE",
                      "the original native environment locator could not be read") from None
    if not found:
        return None
    version = selected if selected in found else next(iter(found))
    return _target(client.target, session_id=found[version], agent_version=version)


def _require_selected_version(found, selected):
    """Refuse before invoking a session that platform metadata places on another version than the selected one.

    Invoking it would make the platform bring it back (possibly as yet another version); the follow, an
    agent-versions sync or the ADR-0003 replacement creates the selected version's session instead."""
    if found.agent_version != selected:
        raise Problem(503, "AGENT_VERSION_FOLLOWING",
                      "the selected version has no session yet; no other version's session is invoked")


def _metadata_record(session):
    from azure.ai.projects.models import VersionRefIndicator

    try:
        if not isinstance(session.version_indicator, VersionRefIndicator):
            raise ValueError("a concrete observed version is required")
        return NativeSessionMetadata(
            hosted_session_id=session.agent_session_id,
            hosted_agent_version=session.version_indicator.agent_version, status=session.status)
    except (AttributeError, TypeError, ValueError):
        raise Problem(502, "NATIVE_STATE_INVALID", "the platform session metadata is invalid") from None


def _metadata_sessions(client):
    from azure.ai.projects import AIProjectClient

    observed = {}
    truncated = False
    try:
        with AIProjectClient(endpoint=client.target.project_endpoint, credential=client.credential,
                             **PLATFORM_READ_RETRY,
                             connection_timeout=5, read_timeout=10) as project:
            for index, session in enumerate(project.agents.list_sessions(
                    client.target.agent_name, limit=100, order="desc")):
                if index == 100:
                    truncated = True
                    break
                if not isinstance(session.agent_session_id, str):
                    raise Problem(502, "NATIVE_STATE_INVALID", "a platform session has no valid identity")
                if not session.agent_session_id.startswith("fa-native-"):
                    continue
                item = _metadata_record(session)
                if item.hosted_session_id in observed and observed[item.hosted_session_id] != item:
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the platform session listing changed")
                observed[item.hosted_session_id] = item
    except AzureError as error:
        status = getattr(error, "status_code", None)
        logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_METADATA_READ status=%s",
                                           status if type(status) is int else None)
        raise Problem(status if status in {401, 403} else 503, "NATIVE_ENVIRONMENT_UNAVAILABLE",
                      "the platform session metadata could not be read") from None
    return observed, truncated


class NativeSessions:
    def __init__(self, composition):
        self.composition = composition
        self.lock = RLock()
        self.prepared = None
        self.binding = None
        self.environment = None
        self.environment_creates = set()
        self.following_candidate = None
        self.following = None
        self.desired_version = None
        self.admission_request_id = None
        self._reader = None
        self._selector = None
        self._reader_generation = 0
        self._reader_lock = Lock()
        # ADR-0009 phase 3: the last binding row read (scope, usable row or None, monotonic start). The row locates the
        # Agent's session; None (no row, unreadable, unsupported profile) keeps the legacy selector locator.
        self._bound_row = None
        # (scope, SID, confirmed, used) of the row's SID that platform metadata last confirmed active or idle (or, on a
        # warm process, did not answer about): when that metadata read started, reused for ``METADATA_REUSE_SECONDS``,
        # and its last use, which keeps the process warm for ``READER_IDLE_SECONDS`` (ADR-0009 §9).
        self._row_checked = None
        # ADR-0009 §7a: first retired read per compatible source (m); first active read and the overdue log per
        # incompatible source (l). Both are in-process observations only, never a decision.
        self._retired_seen, self._active_seen, self._overdue_logged = {}, {}, set()
        # Agent ID -> (admitting RID, first seen) for the background cut's stuck-admission bound.
        self._admitting_seen = {}
        # (Agent ID, version) whose definition declares no profile, logged once (``_correct_profile``).
        self._profile_undeclared = set()
        # Fence RID -> [first failed drive, first unmovable read or None] (``_drive_failed``); in memory, as above.
        self._drive_failing = {}
        # (fence RID, version) pairs whose wake already cleared the drive clocks in this process (``_drive``).
        self._woken_cleared = set()
        self._deciding_abandon = False
        # Destination SIDs whose image rejected ``settled_session_ids`` (``_follow_invoke``).
        self._settled_unsupported = set()
        # (agent ID, SID, version) crash-loops decided here whose own veto write failed (``_crash_loop``).
        self._veto_unrecorded = set()
        self._project_vetoed = set()  # (agent_name, version) whose project veto row was confirmed written
        # ADR-0009 5b (``upgrade_snapshot_flow``): Agent ID -> (source SID, upgrade ID, (root mode, entries)) of its last
        # published snapshot, the next one's incremental base and the tree a kept destination may already hold; and
        # the versions whose image refused the 5b fields (422), not asked again; (Agent ID, target) -> consecutive failed
        # restore switches; (destination SID, staging path) of restore PUTs whose ACK was lost and not yet read back.
        self._snapshot_bases, self._snapshot_unsupported, self._restore_failures = {}, set(), {}
        self._restore_unconfirmed = set()
        # U3: how often each (agent ID, CID, target) answered 403 NATIVE_STATE_SCOPE; skipped once it repeats
        # (shortcut: in process memory, so a restarted API asks once more before skipping it; fine for a read-only
        # check).
        self._scope_refused = {}
        # The zero-call guard every Foundry client of this composition (and each one derived from it) checks.
        self.guard = SessionGuard(lambda: binding_store(composition), enforce=session_guard_enforced())
        # ADR-0009 phase 4b (``FA_NATIVE_IDLE_UPGRADE``): U0's conditions in ``cut``.
        self.idle_upgrade = idle_upgrade_enabled()
        for client in getattr(composition, "clients", ()) or ():
            slot = getattr(client, "_session_guard", None)
            if isinstance(slot, list) and len(slot) == 1:
                slot[0] = self.guard

    def forget(self):
        """The next read selects the version and session from the platform again."""
        with self._reader_lock:
            self._reader_generation += 1
            self._reader = None
            self._selector = None
            self._bound_row = None
            self._row_checked = None

    def reader_cached(self):
        return self._reader is not None or self._bound_row is not None and self._bound_row[1] is not None

    def bound_row(self, client, checked_since=None, *, fresh=False):
        """ADR-0009 §2/§4 step 1: the Agent's binding row, the locator of its session (no session is called).

        Read from the Table unless a read that started at or after ``checked_since`` (default
        ``SELECTOR_CHECK_SECONDS`` ago, which only reads may use) is kept; a submit passes its own start, so the row it
        sends by was read in that request. None when there is no row, it can not be read (logged; the legacy selector
        locator stands in, never a refusal) or its recorded profile is not supported (ADR-0009 §9 phase 3: logged,
        and the request goes the legacy way). ``fresh``: always read (ADR-0009 §4 steps 5-6 read the row again)."""
        scope = _scope(client.target)
        started = monotonic()
        if checked_since is None:
            checked_since = started - SELECTOR_CHECK_SECONDS
        with self._reader_lock:
            kept, generation = self._bound_row, self._reader_generation
        if not fresh and kept is not None and kept[0] == scope and kept[2] >= checked_since:
            return kept[1]
        row = self._binding("locate", lambda store: store.read(client.target.agent_id))
        if (row is not None and row.agent_id == client.target.agent_id and not row.supported()
                and (row.drift or row.profile_unread)):
            # 2d-ii: a row that drifted in place onto v2 before #927 recorded it is healed here, not sent the
            # incompatible way (a new SID without its files). A supported row never gets here.
            row = self._correct_profile(client, row, send=True)
        if row is not None and (row.agent_id != client.target.agent_id or not row.supported()):
            if row.agent_id == client.target.agent_id:
                self.guard.mark_incompatible(row.agent_id, row.hosted_session_id)
                logging.getLogger(__name__).warning(
                    "NATIVE_SESSION_CALL_INCOMPATIBLE operation=locate session_id=%s enforced=False",
                    row.hosted_session_id)
            row = None
        elif row is not None and (row.agent_id, row.hosted_session_id) in self.guard.refused:
            # A SID this process found incompatible (e.g. the source of an abandon whose row write was lost) is
            # never the locator: the legacy locator stands in, as before phase 3 (ADR-0009 §7a zero-call need).
            logging.getLogger(__name__).warning("NATIVE_BINDING_LOCATOR_INCOMPATIBLE session_id=%s",
                                                row.hosted_session_id)
            row = None
        with self._reader_lock:
            kept = self._bound_row
            if self._reader_generation == generation and (kept is None or kept[0] != scope or started >= kept[2]):
                self._bound_row = (scope, row, started)
        return row

    def _keep_row(self, client, row):
        """A row this process just wrote (drift record, repaired switch) is the next locator."""
        with self._reader_lock:
            self._bound_row = (_scope(client.target), row, monotonic())

    def keep_written(self, row):
        """``_keep_row`` for a write made without a client in hand (``clear_waiting``): the row replaces the kept row
        of the same Agent, so the next submit does not reuse the old one. An Agent with no kept row keeps nothing."""
        with self._reader_lock:
            kept = self._bound_row
            if kept is not None and kept[1] is not None and kept[1].agent_id == row.agent_id:
                self._bound_row = (kept[0], row, monotonic())

    def _bound_client(self, client, row, *, version=None):
        """``client`` aimed at the binding row's current SID, expecting its recorded actual version (V_rec)."""
        located = client.for_target(_target(client.target, session_id=row.hosted_session_id,
                                            agent_version=version or row.observed.agent_version))
        located._binding_row = row
        return located

    @staticmethod
    def binding_moved(error):
        """True when the refusal says the Agent's binding moved (reads nothing and forgets nothing).

        An unreadable selector says nothing about the binding, so the kept reader stays."""
        if isinstance(error, SelectorUnreadable):
            return False
        detail = error.body["error"]
        return detail["code"] in _MOVED or (
            detail["code"] == "FOUNDRY_INVOCATION_UNKNOWN" and detail.get("upstream_status") == 404)

    def moved(self, error):
        """Forget the cached reader after a refusal; True when the refusal says the Agent's binding moved."""
        moved = self.binding_moved(error)
        if moved or error.body["error"]["code"].startswith("FOUNDRY_"):
            self.forget()
        return moved

    @contextmanager
    def _held(self, fenced=False):
        """``self.lock``. ADR-0009 2d-i P6 (flag on): with a files fence the binding row records (``fenced``), a send
        never waits behind the fence's follow step (it holds this lock up to ~10 s each): it is answered at once with
        503 ``AGENT_VERSION_FOLLOWING`` (nothing sent), which the in-request wait resends."""
        if fenced and self.guard.enforce:
            if not self.lock.acquire(blocking=False):
                raise Problem(503, "AGENT_VERSION_FOLLOWING", "this Agent's files fence is moving; nothing was sent",
                              not_dispatched=True)
        else:
            self.lock.acquire()
        try:
            yield
        finally:
            self.lock.release()

    @staticmethod
    def _has_fence(row):
        return row is not None and row.upgrade is not None

    @contextmanager
    def _changing(self, fenced=False):
        """One environment operation; reads meanwhile and afterwards select the binding from the platform again."""
        with self._held(fenced):
            # A cached reader may be the source this operation stops; a read must not resume it.
            self.forget()
            try:
                yield
            finally:
                self.forget()

    def _binding(self, operation, call):
        """ADR-0009 phase 1 only records, so a Table failure never changes the request's outcome; the next
        observation writes the same CAS again."""
        try:
            return call(binding_store(self.composition))
        except Exception as error:  # recording only; see the docstring
            code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
            logging.getLogger(__name__).warning("NATIVE_BINDING_UNRECORDED operation=%s code=%s", operation, code)
            return None

    def observed_binding(self, client, *, version=None, claimed_version=None, created_at=None):
        """Claim ``client``'s SID if unclaimed, then record ``version``. Only facts already in hand, so nothing here
        calls a session: ``version`` the VM accepted as expected or reported (``vm``); ``claimed_version`` platform
        metadata a claim read (``claim``: no VM confirms it)."""
        target = client.target

        def claim_profile():
            # A drift the VM reported records that image's profile; unconfirmed metadata (``claimed_version``), or
            # a reported version whose definition is unreadable or undeclared, keeps the creation version's.
            if claimed_version is None and version not in (None, target.agent_version):
                try:
                    return self._row_profile(target, version)
                except Exception:  # noqa: BLE001 - the claim falls back as before; the turn end's heal reads again
                    pass
            return self._row_profile(target, target.agent_version)

        def observing(store):
            row = store.claim(target.agent_id, hosted_session_id=target.session_id,
                              created_agent_version=target.agent_version,
                              observed_version=claimed_version or version,
                              source="claim" if claimed_version else "vm", created_at=created_at,
                              # Read only to insert: an existing row (every bound send) costs no get_version.
                              state_profile=claim_profile)
            if version is None or row is None or row.observed.agent_version == version:
                return row
            # A drift no bound path recorded (a reported version, a legacy-located send): its profile too, as
            # ``_drifted``'s CAS does (one ``get_version``, only when this CAS is about to write).
            profile = self._version_profile(client, version) if row.hosted_session_id == target.session_id else None
            return store.record_observed(target.agent_id, hosted_session_id=target.session_id,
                                         agent_version=version, source="vm", state_profile=profile)

        return self._binding("observe", observing)

    def _row_profile(self, target, version):
        """ADR-0009 2d-i: a binding row records the profile ``version``'s definition declares. An offline harness
        without a version resolver keeps the API's own profile, as ``current_hosted_version`` keeps the configured
        version. Unreadable or undeclared raises, so the row is not written (a claim never overwrites one)."""
        from .behavior_agent_versions import _resolver
        resolver = _resolver(self.composition.runtime)
        if resolver is None:
            return NATIVE_STATE_PROFILE
        profile = resolver.profile(target, version)
        if profile is None:
            raise Problem(409, "NATIVE_TURNS_DISABLED", "the version's definition declares no native state profile")
        return profile

    def _record_switch(self, client, value, *, repairing=False):
        """``repairing`` (M3, a send waits on it): an unreadable profile writes the send profile rather than leave
        the send refused, marked ``profile_unread`` so the background cut's next definition read corrects it; the
        deploy gate reports the row meanwhile. A follow writes nothing then, and its rerun reads the definition again."""
        def switching(store):
            try:
                profile, unread = self._row_profile(client.target, value.hosted_agent_version), False
            except Exception as error:  # noqa: BLE001 - only the repair falls back; see the docstring
                if not repairing:
                    raise
                code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
                logging.getLogger(__name__).warning("NATIVE_BINDING_SWITCH_PROFILE_UNREAD code=%s", code)
                profile, unread = NATIVE_STATE_PROFILE, True
            # A new row field only with 2d-i on: an older API revision decodes rows strictly (extra="forbid").
            return store.record_switch(client.target.agent_id, value, state_profile=profile,
                                       profile_unread=unread and self.guard.enforce, idle_upgrade=self.idle_upgrade,
                                       rollback=_ROLLBACK.get() == value.hosted_agent_version)

        return self._binding("switch", switching)

    def _switched(self, client, value):
        """A rerun's same ``ready`` result writes too, repairing a crash after the commit (ADR-0009 §7a (j))."""
        if value.state == "ready":
            self._record_switch(client, value)
        return value

    def _switched_and_checked(self, client, value):
        """``_switched``, then, on an Agent's first switch, the one read-back of unfinished receipts (§7a (f))."""
        if value.state == "ready":
            row = self._record_switch(client, value)
            if row is not None and row.hosted_session_id == value.hosted_session_id and row.unfinished_checked is None:
                self._binding("unfinished", lambda store: self._backfill_unfinished(store, client, value))
        return value

    @staticmethod
    def _backfill_unfinished(store, client, value):
        """One bounded read through the new session (it reads the same ``fa/v1/turns``; no idle session is called).
        An image without the read, or a failed read, is recorded as unverified."""
        from .behavior_native_turn_api import NativeTurnGateway
        try:
            found, _ = NativeTurnGateway._invoke(client, NativeUnfinishedRead())
            sessions, complete = found.session_ids, found.complete and found.hosted_session_id == value.hosted_session_id
        except Problem as error:
            logging.getLogger(__name__).warning("NATIVE_BINDING_UNFINISHED_UNREAD code=%s", error.body["error"]["code"])
            sessions, complete = (), False
        return store.record_unfinished(client.target.agent_id, hosted_session_id=value.hosted_session_id,
                                       session_ids=sessions, complete=complete)

    def candidate(self, client, *, version=None, row=None, located=False):
        """The session for ``version``: with no version, the legacy identity session; otherwise
        ``destination_session``'s SID (the binding row's ``row``, read when not given; ``located=True`` means the
        caller already read it, None included). A destination other than the row's current SID carries ``_fresh`` =
        (row, attempt): a fresh (M5) SID, or generation 0 as attempt 0."""
        client_id = self.composition.config.identity.managed_identity_client_id
        if version is None:
            return client.for_target(_target(client.target, session_id=native_session_id(client.target, client_id)))
        if not located:
            row = self.bound_row(client)
        sid, attempt = self.destination_session(client, version, row)
        selected = client.for_target(_target(client.target, session_id=sid, agent_version=version))
        selected._binding_row = row
        if attempt is None and row is not None and sid != row.hosted_session_id:
            # Generation 0 as the row's attempt 0 (``destination_session``): unusable, it raises the attempt too.
            attempt = 0
        if attempt is not None:
            selected._fresh = (row, attempt)
        return selected

    def destination_session(self, client, version, row):
        """(SID, attempt) of ``version``'s session for this Agent; attempt is None unless the SID is fresh (M5).

        The one destination function every locator and follow step uses, so they never disagree (ADR-0009 M5):
        - no binding row: generation 0, derived from the version (``native_session_id``), as before;
        - the row's current SID was created on ``version`` and is not being abandoned: that SID;
        - generation 0 of ``version`` is neither the current nor a previous SID and no attempt was raised for
          ``version``: generation 0 (a first visit to a version keeps its old SID, so existing sessions and follows
          read the same);
        - otherwise a fresh SID from the current SID, ``version`` and the row's attempt (``fresh_session_id``): the
          same version gets a new session (the drifted, retired or incompatible SID of the selected version)."""
        client_id = self.composition.config.identity.managed_identity_client_id
        generation0 = native_session_id(client.target, client_id, version=version)
        if row is None:
            return generation0, None
        if version == row.created_agent_version and row.abandon is None:
            return row.hosted_session_id, None
        if generation0 != row.hosted_session_id and not row.earlier(generation0) and row.attempt(version) == 0:
            # Attempt 0 of this version: a generation 0 that reads back retired (a previous SID the row trimmed or
            # never recorded) raises the attempt, and the next destination is fresh (``candidate`` marks it).
            return generation0, None
        attempt = row.attempt(version)
        return fresh_session_id(client.target, client_id, current_session_id=row.hosted_session_id,
                                version=version, attempt=attempt), attempt

    def selected_version(self, client):
        from .behavior_agent_versions import current_hosted_version
        if _ROLLBACK.get() is not None:
            return _ROLLBACK.get()
        self.desired_version = current_hosted_version(self.composition.runtime, client.target)
        return self.desired_version

    def _checked_version(self, client, checked_since=None, *, stale_read=False):
        """The selected version from a selector read started after ``checked_since`` (default:
        ``SELECTOR_CHECK_SECONDS`` ago), reading it now if none is that recent.

        "After" is strict: with a coarse clock (about 16 ms on Windows) an earlier request's read can share the
        tick in which a submit starts. ``stale_read``: see ``reader``."""
        if _ROLLBACK.get() is not None:
            return _ROLLBACK.get()  # a rollback's own reads never enter the shared selector cache
        scope = _scope(client.target)
        started = monotonic()
        if checked_since is None:
            checked_since = started - SELECTOR_CHECK_SECONDS
        with self._reader_lock:
            selector, generation = self._selector, self._reader_generation
            if selector is not None and selector[0] == scope and selector[2] > checked_since:
                return selector[1]
        try:
            version = self.selected_version(client)
        except SelectorUnreadable:
            if not (stale_read and selector is not None and selector[0] == scope
                    and monotonic() - selector[2] < SELECTOR_STALE_READ_SECONDS):
                raise
            logging.getLogger(__name__).warning(
                "NATIVE_SELECTOR_UNREADABLE kept_version=%s confirmed_seconds_ago=%d",
                selector[1], monotonic() - selector[2])
            return selector[1]
        with self._reader_lock:
            kept = self._selector
            # A concurrent read that started later saw a newer selector; an earlier-started one never replaces it.
            if self._reader_generation == generation and (kept is None or kept[0] != scope or started > kept[2]):
                self._selector = (scope, version, started)
        return version

    def _scoped(self, client, target, *, row=None):
        """``target`` is one of this API identity's sessions of ``client``'s Agent: a generation 0 SID (legacy or
        version-derived), or, ADR-0009 phase 3, a SID the binding row names (current, previous, or the destination
        it derives). The row is read only when the SID is not generation 0."""
        fields = ("project_endpoint", "agent_id", "agent_name", "agent_oid", "executor_id")
        client_id = self.composition.config.identity.managed_identity_client_id
        if any(getattr(client.target, name) != getattr(target, name) for name in fields):
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the native locator is outside the original server scope")
        if target.session_id in {native_session_id(client.target, client_id),
                                 native_session_id(client.target, client_id, version=target.agent_version)}:
            return client.for_target(target)
        if row is None:
            row = self.bound_row(client)
        if row is None or not (target.session_id == row.hosted_session_id or row.earlier(target.session_id)
                               or target.session_id == self.destination_session(client, target.agent_version, row)[0]):
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the native locator is outside the original server scope")
        return client.for_target(target)

    @staticmethod
    def _state(client, *, environment_request_id=None):
        from .behavior_native_turn_api import NativeTurnGateway
        value, _ = NativeTurnGateway._invoke(client, NativeSessionRead(environment_request_id=environment_request_id))
        return value

    def _remember(self, client, state):
        value = state.preparation
        if (value is None or state.hosted_session_id != client.target.session_id
                or value.agent_id != client.target.agent_id
                or value.hosted_session_id != client.target.session_id
                or value.hosted_agent_version != client.target.agent_version):
            raise Problem(409, "NATIVE_SESSION_CHANGED", "explicit preparation of the original native session is required")
        self.prepared = value
        self.binding = client.target
        return value

    def _remember_state(self, client, state):
        if state.agent_id != client.target.agent_id or state.preparation is None:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the original prepared Agent binding is unavailable")
        value = state.environment
        if value is None:
            sid, version = state.preparation.hosted_session_id, state.preparation.hosted_agent_version
        elif value.state == "ready":
            sid, version = value.hosted_session_id, value.hosted_agent_version
        else:
            sid, version = value.previous_hosted_session_id, value.previous_agent_version
        if sid != state.hosted_session_id:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the published Agent environment differs")
        self.prepared, self.environment, self.following = state.preparation, value, state.following
        self.admission_request_id = state.admission_request_id
        self.binding = _target(client.target, session_id=sid, agent_version=version)
        return self._scoped(client, self.binding)

    def current(self, client, *, refresh=False, checked_since=None):
        """``checked_since``: see ``reader``. With the flag, the binding row is read before the lock (the same one
        read, which ``_current`` reuses), so a send never queues behind a files fence's step (``_held``)."""
        since = row = None
        if self.guard.enforce:
            since = checked_since if checked_since is not None else monotonic()
            row = self.bound_row(client, since)
        with self._held(self._has_fence(row)):
            return self._current(client, checked_since, since)

    def _current(self, client, checked_since=None, row_since=None):
        row = self.bound_row(client, row_since if row_since is not None else
                             checked_since if checked_since is not None else monotonic())
        if row is not None:
            return self._current_bound(client, checked_since, row)
        retry = self.reader_cached()
        while True:
            try:
                reader = self.reader(client, checked_since=checked_since)
            except Problem as error:
                row = self._claim_located(client, error, checked_since)
                if row is None:
                    raise
                return self._current_bound(client, checked_since, row)
            try:
                current = self._remember_state(reader, self._state(reader))
            except Problem as error:
                if self.moved(error) and retry:
                    retry = False
                    continue
                raise
            if (current.target.session_id, current.target.agent_version) == (
                    reader.target.session_id, reader.target.agent_version):
                self.following_candidate = None
                return current
            if retry:
                # A cached reader that is no longer the Agent's environment: read the platform once more.
                self.forget()
                retry = False
                continue
            self.following_candidate = reader.target
            raise Problem(409, "NATIVE_SESSION_CHANGED",
                          "the published version has not become the Agent's current environment")

    def _claim_located(self, client, error, checked_since):
        """ADR-0009 2d-i P7 (``FA_NATIVE_SESSION_GUARD_ENFORCE`` only): a rowless Agent whose selected version has no
        session is claimed on the session the version listing locates (metadata only; nothing is invoked), so the
        row path serves it instead of a refusal. Only a compatible one (its version's definition declares a
        supported profile) is returned; anything else (none listed, the selected version's own, an incompatible or
        undeclared profile, an unwritable row) gives None, and the submit's follow bootstraps, follows or abandons
        (§7a) as before. Without the flag the same Agent is followed by the submit (``_prepared_before``)."""
        if not self.guard.enforce or error.status != 409 or error.body["error"]["code"] != "NATIVE_SESSION_CHANGED":
            return None
        try:
            selected = self._checked_version(client, checked_since)
            located = _latest_version_reader(
                client, self.composition.config.identity.managed_identity_client_id, selected)
        except Problem as unread:
            # The submit's own locate (``_prepared_before``) reports it with ``not_dispatched``.
            logging.getLogger(__name__).warning("NATIVE_BINDING_CLAIM_UNLOCATED code=%s", unread.body["error"]["code"])
            return None
        # The submit's ``_prepared_before`` reuses this listing when nothing is claimed (one listing per locate).
        error.located = (selected, located)
        if located is None or located.agent_version == selected:
            return None
        found = client.for_target(located)
        # The same definition check as the §7a decision (``_source_supported``), but an unreadable one claims nothing.
        try:
            profile = _native_profile(found, floor=True)
        except Problem as unread:
            logging.getLogger(__name__).warning("NATIVE_BINDING_CLAIM_PROFILE_UNREAD code=%s", unread.body["error"]["code"])
            return None
        if profile not in SUPPORTED_STATE_PROFILES:
            # Never invoked: the submit's follow decides the §7a abandon and claims the row as incompatible.
            self.guard.mark_incompatible(located.agent_id, located.session_id)
            logging.getLogger(__name__).warning("NATIVE_BINDING_CLAIM_INCOMPATIBLE session_id=%s version=%s",
                                                located.session_id, located.agent_version)
            return None
        row = self.observed_binding(found, claimed_version=located.agent_version)
        if row is None or row.hosted_session_id != located.session_id or not row.supported():
            return None
        self._keep_row(client, row)
        return row

    def _selected_or_none(self, client, checked_since, stale_read=False):
        """The selected version, or None when the selector is unreadable or names a version this API build does not
        serve: with a binding row the send goes on by the row (ADR-0009 §1: the selector only picks the next new
        session's version), so neither refuses it."""
        try:
            return self._checked_version(client, checked_since, stale_read=stale_read)
        except Problem as error:
            if error.body["error"]["code"] != "AGENT_VERSION_FOLLOWING":
                raise
            logging.getLogger(__name__).warning("NATIVE_SELECTOR_UNSERVED_BOUND code=%s", error.body["error"]["code"])
            return None

    def _located(self, client, row, checked_since, *, stale_read=False):
        """(selected version or None, usable) before anything calls the row's SID; this invokes no session.

        ADR-0009 §10 phase 0 guarantee: a SID the selector no longer picks is never called before the follow decides
        (with ``FA_NATIVE_SESSION_GUARD_ENFORCE`` it is, and the background cut moves it after a turn end: 2d-i P7),
        and a non-active one (404, failed, deleted, expired) never at all. The selector read and the SID's platform
        metadata (``get_session``) run in parallel, so the session call after them waits no longer than on main, where
        they ran one after the other. Every caller (each invokes the SID next: send, read, poll) reads the
        metadata unless a read started within ``METADATA_REUSE_SECONDS``. ``usable``: the row has no abandon decision,
        the selector (when this build serves it) names the version the SID was created on (with the flag: any
        version), and the metadata says active or idle. A 401/403 is raised. Otherwise a failed read (5xx, 429,
        timeout) or a creating/updating SID is raised as 503 on a cold process, as on main; a warm one (it used the SID
        within ``READER_IDLE_SECONDS``, when main read nothing) reads once without retry, waits at most
        ``METADATA_BUDGET_SECONDS`` and, failing that, logs and keeps the SID as main did."""
        scope, sid = _scope(client.target), row.hosted_session_id
        with self._reader_lock:
            checked = self._row_checked
        now = monotonic()
        ours = checked is not None and checked[:2] == (scope, sid)
        kept = ours and now - checked[2] < METADATA_REUSE_SECONDS
        if kept:
            selected, live = self._selected_or_none(client, checked_since, stale_read), True
        else:
            warm = ours and now - checked[3] < READER_IDLE_SECONDS
            budget = METADATA_BUDGET_SECONDS
            options = {**PLATFORM_NO_RETRY, "connection_timeout": budget, "read_timeout": budget} if warm else {}
            reading = _LOCATE.submit(copy_context().run, _read_platform_session, self._bound_client(client, row),
                                     **options)
            selected = self._selected_or_none(client, checked_since, stale_read)
            try:
                live = reading.result(timeout=max(0.0, now + budget - monotonic()) if warm else None) is not None
            except _RetiredSession:
                live = False
            except (_MetadataUnconfirmed, TimeoutError) as error:
                if not warm:
                    raise
                logging.getLogger(__name__).warning(
                    "NATIVE_BINDING_METADATA_UNCONFIRMED session_id=%s code=%s kept=True", sid,
                    error.body["error"]["code"] if isinstance(error, Problem) else "budget_exceeded")
                live = True
        # 2d-i (``FA_NATIVE_SESSION_GUARD_ENFORCE``): a selector move no longer makes the row's SID unusable; the
        # background cut moves the Agent after a turn end instead (plan F6, landed with P7's claim, kept by P6).
        usable = live and row.abandon is None and (
            selected is None or selected == row.created_agent_version or self.guard.enforce)
        with self._reader_lock:
            if live:
                # A use keeps the process warm; only a metadata read (answered or, when warm, not) restarts the window.
                self._row_checked = (scope, sid, checked[2] if kept else now, monotonic())
            elif checked is not None and checked[:2] == (scope, sid):
                self._row_checked = None
        if not live:
            logging.getLogger(__name__).warning("NATIVE_BINDING_SESSION_RETIRED session_id=%s invoked=False", sid)
        return selected, usable

    def _current_bound(self, client, checked_since, row):
        """ADR-0009 §4 steps 1 and 3: the binding row names the Agent's current SID and its recorded actual version
        V_rec; the session is called with expected = V_rec, never the selector's version. Before that call,
        ``_located`` decides whether the retained submit-time follow (``_follow_before_admission``) runs instead,
        without calling the SID: the selector names another version than the SID was created on, an abandon decision
        is pending, or platform metadata says the SID is retired or gone. A compatible drift found on the session read
        records the version and goes on (§4 step 6); a row behind Hosted's own record is repaired silently (M3)."""
        selected, usable = self._located(client, row, checked_since)
        if not usable:
            self.following_candidate = self.candidate(
                client, version=selected or row.created_agent_version, row=row, located=True).target
            raise Problem(409, "NATIVE_SESSION_CHANGED",
                          "the selected version is not yet the Agent's current environment; nothing was called")
        try:
            reader, state, row = self._bound_state(client, self._bound_client(client, row), row)
        except Problem as failure:
            if self.binding_moved(failure) and failure.body["error"]["code"] != "NATIVE_SESSION_CHANGED":
                raise Problem(409, "NATIVE_SESSION_CHANGED",
                              "the Agent's recorded session did not answer as its environment") from None
            raise
        self._remember_state(reader, state)
        self.following_candidate = None
        self.binding = reader.target
        return reader

    def _bound_state(self, client, reader, row):
        """One session read by the row, with ADR-0009 §4's drift and divergence handling (no RID is involved, so
        nothing can run twice): returns (client, state, row)."""
        try:
            state = self._state(reader)
        except Problem as error:
            drifted = self._drifted(reader, error, row)
            if drifted is None:
                raise
            reader, row, source = drifted
            state = self._state(reader)
            if source == "metadata-active":
                # The VM just accepted expected = V_obs: only now is the metadata's version a fact (§4 step 6).
                row = self._observe_drift(reader, row, source)
        if state.hosted_session_id != reader.target.session_id:
            repaired = self._repair(reader, state, row)
            if repaired is None:
                raise Problem(409, "NATIVE_SESSION_CHANGED", "the Agent record names another session")
            reader, row = repaired
            state = self._state(reader)
            if state.hosted_session_id != reader.target.session_id:
                raise Problem(409, "NATIVE_SESSION_CHANGED", "the Agent record names another session")
        return reader, state, row

    def _observe_drift(self, reader, row, source):
        """The drift CAS of ADR-0009 §4 step 6 for ``reader``'s SID and version; the written row (or ``row``)."""
        target = reader.target
        # The profile ``_drifted`` (or the 403 path) read for this version, carried on ``reader`` to the CAS.
        written = self._binding("observe", lambda store: store.record_observed(
            target.agent_id, hosted_session_id=target.session_id, agent_version=target.agent_version, source=source,
            state_profile=getattr(reader, "_drift_profile", None)))
        if written is None or written.hosted_session_id != target.session_id:
            return row
        self._keep_row(reader, written)
        reader._binding_row = written
        return written

    def _version_profile(self, client, version):
        """ADR-0009 §3: the profile ``version``'s definition enables (one ``get_version`` read, only on the drift
        path), ``INCOMPATIBLE_STATE_PROFILE`` when it enables none. None: unreadable, which decides nothing (counted
        as supported, and the row keeps its profile)."""
        try:
            profile = _native_profile(client.for_target(_target(client.target, agent_version=version)))
        except Exception as error:  # noqa: BLE001 - an unreadable definition decides nothing
            code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
            logging.getLogger(__name__).warning("NATIVE_VERSION_PROFILE_UNREAD code=%s version=%s", code, version)
            return None
        return INCOMPATIBLE_STATE_PROFILE if profile is None else profile

    def observed_version(self, reader, error):
        """(V_obs, source) behind a ``NATIVE_SESSION_CHANGED`` from ``reader``'s SID, or None when it is not a version
        refusal. A newer image names it (``actual_version``, a VM fact); an older one only lets the API read the
        session's metadata (it just answered, so it is active), a guess until the VM accepts it as expected."""
        detail = error.body["error"]
        if error.status != 409 or detail["code"] != "NATIVE_SESSION_CHANGED" or detail.get("reason") == "replaced":
            return None
        actual = detail.get("actual_version")
        if actual is not None:
            return actual, "vm"
        if detail.get("not_dispatched") is True and detail.get("reason") is None:
            # Hosted's admission check (its Agent record names another SID), not a version refusal.
            return reader.target.agent_version, "vm"
        try:
            metadata = _session_metadata(reader)
        except Exception as unread:  # noqa: BLE001 - an unread version is "not known", never a new failure
            code = unread.body["error"]["code"] if isinstance(unread, Problem) else type(unread).__name__
            logging.getLogger(__name__).warning("NATIVE_VERSION_METADATA_UNREAD code=%s", code)
            return None
        if metadata is None or metadata.status != "active":
            return None
        return metadata.version_indicator.agent_version, "metadata-active"

    def _drifted(self, reader, error, row):
        """(client at V_obs, row, source) when ``error`` is a compatible same-SID drift; None otherwise. A VM-reported
        version is recorded now; a metadata guess only after the VM accepts it (the caller's next call). An
        incompatible one is logged and marked (ADR-0009 §9 phase 3: no refusal is added; the caller's follow handles
        it) and gives None. The drift CAS also records the profile V_obs declares: the SID now runs that image, so the
        row's profile no longer says what it was created on (an unreadable definition keeps the row's)."""
        observed = self.observed_version(reader, error)
        if observed is None or observed[0] == reader.target.agent_version:
            return None
        version, source = observed
        profile = self._version_profile(reader, version)
        moved = reader.for_target(_target(reader.target, agent_version=version))
        moved._binding_row, moved._drift_profile = row, profile
        if source == "vm":
            row = self._observe_drift(moved, row, source)
        if profile is not None and profile not in SUPPORTED_STATE_PROFILES:
            self.guard.mark_incompatible(reader.target.agent_id, reader.target.session_id)
            logging.getLogger(__name__).warning(
                "NATIVE_SESSION_CALL_INCOMPATIBLE operation=drift session_id=%s version=%s enforced=False",
                reader.target.session_id, version)
            return None
        return moved, row, source

    def _repair(self, reader, state, row):
        """ADR-0009 §4 step 6 "inconsistent" / M3: Hosted's Agent record already names another SID with a committed
        (``ready``) environment the binding row has not caught up with. ``record_switch`` (idempotent) writes it, with
        the switch notice of phase 2c (§8), and the new SID becomes the locator: (client, row), the row being the
        post-switch row the response's notice comes from, or None when the state confirms no committed switch."""
        environment = state.environment
        if (environment is None or environment.state != "ready"
                or environment.hosted_session_id != state.hosted_session_id
                or environment.agent_id != reader.target.agent_id):
            return None
        logging.getLogger(__name__).warning("NATIVE_BINDING_DIVERGED session_id=%s binding_session_id=%s repaired=True",
                                            environment.hosted_session_id, reader.target.session_id)
        written = self._record_switch(reader, environment, repairing=True)
        if written is None or written.hosted_session_id != environment.hosted_session_id:
            return None
        self._keep_row(reader, written)
        return self._bound_client(reader, written), written

    def relocate(self, client, error):
        """After a refusal from the row's SID: the client to send the same command to once, or None. A compatible
        drift goes to V_obs; a stale row (``replaced``, or V_obs = V_rec) is repaired from that SID's own session state
        (``session_read`` is not a record read, so a replaced session still answers it; it is active, it just
        answered). A refusal naming ``current_hosted_session_id`` is repaired to that SID or to nothing. Never raises:
        the original refusal stands when nothing is confirmed."""
        row = getattr(client, "_binding_row", None)
        if row is None:
            return None
        named = error.body["error"].get("current_hosted_session_id")
        try:
            if named is not None:
                # ADR-0009 4c-2: a ``replaced`` 409 names the Agent's current SID; the repair must land exactly there.
                repaired = self._repair(client, self._state(client), row)
                if repaired is not None and repaired[0].target.session_id == named:
                    return repaired[0]
                logging.getLogger(__name__).warning("NATIVE_RELOCATE_NAMED_UNCONFIRMED session_id=%s", named)
                return None
            drifted = self._drifted(client, error, row)
            if drifted is not None:
                return drifted[0]
            observed = self.observed_version(client, error)
            detail = error.body["error"]
            if not (error.status == 409 and detail["code"] == "NATIVE_SESSION_CHANGED"
                    and (detail.get("reason") == "replaced" or observed is not None
                         and observed[0] == client.target.agent_version)):
                return None
            repaired = self._repair(client, self._state(client), row)
            return None if repaired is None else repaired[0]
        except Problem as unconfirmed:
            logging.getLogger(__name__).warning("NATIVE_RELOCATE_UNCONFIRMED code=%s", unconfirmed.body["error"]["code"])
            return None

    def turn_session(self, client):
        """The session the Agent record last named, while platform metadata says it is active; else None.

        Only a cancel or a delivery acknowledgement uses it, after ``current`` refused because the selected version
        has no session yet or pending work keeps the Agent on that earlier session: the turn lives there. A running
        turn keeps its session active; an idle or reclaimed one is never invoked, as invoking it makes the platform
        bring it back, possibly as another version. A denied metadata read (401/403) is raised unchanged.
        The session can still go idle between the metadata read and the call, which may then wake it: accepted, as it
        is bounded to an explicit user Stop or acknowledgement on a session that was active a moment ago."""
        binding = self.binding
        if binding is None or _scope(binding) != _scope(client.target):
            logging.getLogger(__name__).warning("NATIVE_TURN_SESSION_UNKNOWN")
            return None
        try:
            holder = self._scoped(client, binding)
            if _active_session(holder):
                return holder
        except Problem as error:
            if error.status in {401, 403}:
                raise  # A denied platform read is reported as is, not as a stop that may simply be sent again.
            logging.getLogger(__name__).warning("NATIVE_TURN_SESSION_UNCONFIRMED code=%s", error.body["error"]["code"])
            return None
        logging.getLogger(__name__).warning("NATIVE_TURN_SESSION_NOT_ACTIVE version=%s", binding.agent_version)
        return None

    def admission(self, client, *, request_id=None, resolved=False):
        """``resolved=True``: this request just got ``client`` from ``current()``; it is not read again."""
        with self._held(self._has_fence(getattr(client, "_binding_row", None))):
            current = client if resolved else self._current(client)
            if request_id is not None and self.admission_request_id == request_id:
                return current
            if (self.environment is not None and self.environment.state == "pending"
                    or self.following is not None and self.following.state == "pending"
                    or self.following_candidate is not None
                    and current.target.session_id != self.following_candidate.session_id):
                raise Problem(503, "AGENT_VERSION_FOLLOWING",
                              "the entire Agent is changing environments; this new request was not accepted")
            return current

    def reader(self, client, *, checked_since=None, stale_read=False):
        """The selected version's existing session, kept while the endpoint selector still names its version.

        Another process can move the selector at any time, so the selected version comes from a selector read
        started after ``checked_since`` (default ``SELECTOR_CHECK_SECONDS`` ago, which only reads may use; work
        delivery passes its own start). A kept reader of another version is dropped before anything is invoked.
        ``stale_read`` (reads only): while the selector cannot be read, a version it confirmed within
        ``SELECTOR_STALE_READ_SECONDS`` is kept; otherwise ``SelectorUnreadable`` is raised and the reader stays.

        ADR-0009 phase 3: with a binding row the row is the locator (its current SID at V_rec), read at most once per
        ``SELECTOR_CHECK_SECONDS`` for reads and in-request for ``checked_since``, under the same selector and metadata
        checks as main's reader (``_located``, ADR-0009 §10). When the selector moved or an abandon decision is
        pending, the selected version's destination is read only if it exists, as on main; the row's SID never is."""
        row = self.bound_row(client, checked_since)
        if row is not None:
            selected, usable = self._located(client, row, checked_since, stale_read=stale_read)
            if usable:
                return self._bound_client(client, row)
            if row.abandon is None and selected in {None, row.created_agent_version}:
                raise Problem(409, "NATIVE_SESSION_CHANGED",
                              "the Agent's recorded session is retired; this read invokes no session")
            return self._platform_reader(client, version=selected or row.created_agent_version)
        scope = _scope(client.target)
        with self._reader_lock:
            cached, generation = self._reader, self._reader_generation
            if not (cached is not None and cached[0] == scope and monotonic() - cached[2] < READER_IDLE_SECONDS):
                cached = None
        version = self._checked_version(client, checked_since, stale_read=stale_read and cached is not None)
        if cached is not None:
            if version == cached[1].agent_version:
                with self._reader_lock:
                    if self._reader_generation == generation:
                        self._reader = (scope, cached[1], monotonic())
                return client.for_target(cached[1])
            logging.getLogger(__name__).warning(
                "NATIVE_READER_VERSION_MOVED kept_version=%s selected_version=%s", cached[1].agent_version, version)
            with self._reader_lock:
                # The selector read just now stays; only the reader of the unselected version is dropped.
                self._reader_generation += 1
                self._reader = None
                generation = self._reader_generation
        selected = self._platform_reader(client, version=version)
        with self._reader_lock:
            if self._reader_generation == generation:
                self._reader = (scope, selected.target, monotonic())
        return selected

    def _platform_reader(self, client, *, version):
        selected = self.candidate(client, version=version)
        actual = _read_platform_session(selected)
        if actual is not None:
            if (actual.session_id, actual.agent_version) != (selected.target.session_id, version):
                raise Problem(409, "NATIVE_SESSION_CHANGED",
                              "the current reader's platform version differs; this read invokes no old session")
            return selected.for_target(actual)
        raise Problem(409, "NATIVE_SESSION_CHANGED",
                      "the current version needs a reader; this read creates nothing and invokes no old session")

    def environment_view(self, client):
        version = self.selected_version(client)
        row = self.bound_row(client, monotonic())
        destination = self.candidate(client, version=version, row=row, located=True)
        observed, truncated = _metadata_sessions(client)
        actual = _session_metadata(destination)
        observed.pop(destination.target.session_id, None)
        item = None if actual is None else _metadata_record(actual)
        if item is not None:
            observed[item.hosted_session_id] = item
        # ADR-0009 §1: the row's current SID created on the selected version needs no follow when it woke on another
        # version (a runtime fact the send records and runs on); only its status matters.
        current = row is not None and destination.target.session_id == row.hosted_session_id
        return NativeEnvironmentMetadata(
            agent_id=client.target.agent_id, hosted_session_id=destination.target.session_id,
            hosted_agent_version=version, observed_sessions=tuple(observed.values()), truncated=truncated,
            state="unverified" if item is not None and (current or item.hosted_agent_version == version)
            and item.status in {"active", "idle"} else "needs_follow",
        )

    def _confirm_environment(self, client, value):
        state = self._state(client)
        if state.environment != value or state.hosted_session_id != value.hosted_session_id:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original environment result did not read back")
        self._remember_state(client, state)
        return self._switched_and_checked(client, value)

    def observe_environment(self, client, command):
        reader = self.candidate(client, version=command.expected_agent_version)
        actual = _read_platform_session(reader)
        if actual is None:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original destination reader is unavailable; nothing was created")
        if actual.agent_version != command.expected_agent_version:
            raise Problem(409, "NATIVE_SESSION_CHANGED",
                          "the original destination version changed; this reconciliation invokes no original session")
        reader = reader.for_target(actual)
        state = self._state(reader)
        if (state.environment is not None and state.environment.state == "pending"
                and state.environment.hosted_session_id != reader.target.session_id):
            state = self._state(reader, environment_request_id=command.request_id)
        target = self.candidate(client, version=command.expected_agent_version).target
        values = ((state.environment, state.following) if state.environment is not None
                  and state.environment.state == "ready" else (state.following, state.environment))
        for value in values:
            if (value is not None and value.request_id == command.request_id
                    and value.hosted_session_id == target.session_id
                    and value.hosted_agent_version == command.expected_agent_version
                    and (command.expected_session_id is None
                         or value.previous_hosted_session_id == command.expected_session_id)):
                if (value.state == "pending" and value.files_manifest_sha256 is not None
                        and value.reason in {"files_pending", "source_stop_required"}):
                    from .behavior_session_files import FollowFiles
                    files = FollowFiles(reader, value, lambda: None).observe()
                    state = self._state(reader)
                    observed = state.following or state.environment
                    if observed is None or any(getattr(observed, name) != getattr(value, name) for name in (
                            "agent_id", "request_id", "previous_hosted_session_id", "hosted_session_id",
                            "hosted_agent_version")):
                        raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original file operation changed during readback")
                    if (observed.state == "pending" and files is not None and files.sending is None
                            and (not files.publishing or files.published)):
                        source = client.for_target(_target(
                            client.target, session_id=value.previous_hosted_session_id,
                            agent_version=value.previous_agent_version))
                        metadata = _session_metadata(source)
                        if (metadata is not None
                                and metadata.version_indicator.agent_version != value.previous_agent_version):
                            return observed.model_copy(update={"reason": "source_version_changed"})
                        return observed.model_copy(update={"reason": "files_pending"})
                    return observed
                return value
        current = self._current_environment(reader, state, command)
        if current is not None:
            return current
        raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN",
                      "the original environment operation has no confirmed observation; do not resubmit it")

    @staticmethod
    def _current_environment(client, state, command):
        if (command.expected_session_id is None and state.environment is None and state.following is None
                and state.preparation is not None and state.hosted_session_id == client.target.session_id
                and client.target.agent_version == command.expected_agent_version
                and state.preparation.hosted_agent_version == command.expected_agent_version):
            return NativeEnvironmentView(
                agent_id=client.target.agent_id, request_id=command.request_id,
                previous_hosted_session_id=state.hosted_session_id,
                previous_agent_version=command.expected_agent_version,
                hosted_session_id=state.hosted_session_id, hosted_agent_version=command.expected_agent_version,
                state="current")
        return None

    def _destination(self, client, expected_version, before_send, *, mark=True, row=None):
        """``mark=False`` (the background cut): ``following_candidate`` is left alone, so a concurrent send is not
        refused by ``admission`` while the cut only probes. ``row``: the binding row the caller just read."""
        version = self.selected_version(client)
        if version != expected_version:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the server-selected version changed")
        destination = (self.candidate(client, version=version) if row is None
                       else self.candidate(client, version=version, row=row, located=True))
        fresh = getattr(destination, "_fresh", None)
        try:
            actual = _read_platform_session(destination)
        except _RetiredSession:
            self._raise_attempt(destination, fresh)
            raise
        if actual is None:
            _require_native_version(destination)
            if destination.target.session_id in self.environment_creates:
                raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original version session create is unconfirmed")

            def creating():
                before_send()
                self.environment_creates.add(destination.target.session_id)

            try:
                destination.create_session(before_send=creating)
            except Problem as error:
                if type(error.body["error"].get("upstream_status")) is int:
                    # The platform answered with a refusal (401/403/4xx/5xx): a retry may create this same SID again
                    # once the cause is fixed. No answer (a timeout) keeps the latch: never resend an unknown.
                    self.environment_creates.discard(destination.target.session_id)
                if error.body["error"].get("upstream_status") in {401, 403}:
                    raise Problem(error.body["error"]["upstream_status"], "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE",
                                  "the original API identity cannot create this session") from None
                if error.body["error"]["code"] not in {"FOUNDRY_SESSION_UNKNOWN", "FOUNDRY_SESSION_UNAVAILABLE"}:
                    raise
                actual = _read_platform_session(destination)
                if actual is None:
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "read the same version session after creation") from None
            if actual is None:
                actual = _read_platform_session(destination)
        if actual is None:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original version session create has no confirmed readback")
        row = getattr(destination, "_binding_row", None)
        if actual.agent_version != version and not (row is not None and row.hosted_session_id == actual.session_id):
            # The Agent's own current SID may have woken on another version (a runtime fact, ADR-0009 §1); any other
            # destination must be the version it was created on. A fresh one that is not gets the next attempt.
            self._raise_attempt(destination, fresh)
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the original version session did not read back")
        if mark:
            self.following_candidate = destination.target
        return destination

    def _raise_attempt(self, destination, fresh):
        """ADR-0009 M5: a fresh destination that is retired or woke on another version is never reused; the next
        destination for this version takes the next attempt (the row's ``fresh``, CAS)."""
        if fresh is None:
            return
        row, attempt = fresh
        target = destination.target
        self._binding("fresh", lambda store: store.raise_attempt(
            target.agent_id, hosted_session_id=row.hosted_session_id, agent_version=target.agent_version,
            attempt=attempt))
        self.forget()

    def retarget(self, client, command, *, before_send):
        from .behavior_native_turn_api import NativeTurnGateway

        with self._changing():
            destination = self._destination(client, command.expected_agent_version, before_send)
            state = self._state(destination)
            before_send()
            value, _ = NativeTurnGateway._invoke(destination, NativeSessionRetarget(
                request_id=command.request_id, expected_session_id=command.expected_session_id,
                expected_etag=state.etag, expected_agent_version=command.expected_agent_version,
                pending_request_id=command.pending_request_id,
                pending_session_id=command.pending_session_id, idle_only=command.idle_only))
            if value.state == "ready":
                return self._confirm_environment(destination, value)
            if value.reason != "source_stop_required":
                return value
            return self.follow(client, command, before_send=before_send)

    def follow(self, client, command, *, before_send, destination=None):
        """Only the automatic ``advance`` (``_deciding_abandon``, set under ``self.lock``) and a marked fence's drive
        (``_drive``, a retired source) may decide an ADR-0009 §7a abandon-source switch from metadata; every other
        caller only carries out a decision already written.
        ``destination``: the background cut's fence after FA's stop of the source, driven at its own session whatever
        the selector names now (no ``_destination`` read)."""
        from .behavior_native_turn_api import NativeTurnGateway
        from .behavior_session_files import FollowFiles
        from .native_files_wire import FILES_PROFILE

        with self._changing():
            if destination is None:
                destination = self._destination(client, command.expected_agent_version, before_send)
            state = self._state(destination)
            existing = state.environment
            if (existing is not None and existing.hosted_session_id == destination.target.session_id
                    and existing.state == "ready" and (command.expected_session_id is None
                    or existing.previous_hosted_session_id == command.expected_session_id)):
                self._remember_state(destination, state)
                # A rerun after a crash between the commit and the binding write completes that write.
                return self._switched_and_checked(destination, existing)
            current = self._current_environment(destination, state, command)
            if current is not None:
                return current
            found = self._binding_row(client.target.agent_id)
            switched = self._abandon_source(client, command, destination, state, before_send,
                                            decide=self._deciding_abandon,
                                            found=found)
            if switched is not None:
                return switched
            omitted = state.following
            confirmed_omission = (omitted is not None and omitted.request_id == command.request_id
                                  and omitted.hosted_session_id == destination.target.session_id
                                  and omitted.files_migrated is False and omitted.files_skip_request_id is not None)
            if state.files_profile != FILES_PROFILE and not confirmed_omission:
                raise Problem(409, "NATIVE_FILES_UNSUPPORTED", "the selected environment cannot safely migrate workspace files")
            before_send()
            internal = NativeSessionFollow(
                request_id=command.request_id,
                expected_session_id=command.expected_session_id or state.hosted_session_id,
                expected_etag=state.etag, stage="prepare", idle_only=command.idle_only, files_required=True,
                settled_session_ids=self._settled(found, destination))
            try:
                value, _ = self._follow_invoke(destination, internal)
            except Problem as error:
                if (not command.idle_only or error.status != 409
                        or error.body["error"]["code"] != "NATIVE_ENVIRONMENT_CONFLICT"
                        or error.body["error"].get("files_check") is not None or state.following is not None
                        or state.environment is not None and state.environment.state != "ready"):
                    raise
                observed = self._state(destination)
                # Admission may mark and clear the Agent ETag after the read; no environment intent may change.
                fields = {"etag", "admission_request_id"}
                if (observed.etag == state.etag or observed.hosted_session_id != internal.expected_session_id
                        or observed.model_dump(exclude=fields) != state.model_dump(exclude=fields)
                        or self.selected_version(client) != command.expected_agent_version):
                    raise
                before_send()
                internal = internal.model_copy(update={"expected_etag": observed.etag})
                value, _ = self._follow_invoke(destination, internal)
            if value.state == "ready":
                return self._confirm_environment(destination, value)
            # Hosted's view names the version the source SID was created on; a drift the binding row recorded for that
            # SID (ADR-0009 §2, §9) is the same source, any other version is still refused before the source is called.
            row = None if found is None else found[1]
            source_version = (row.observed.agent_version if row is not None
                              and row.hosted_session_id == value.previous_hosted_session_id else None)
            if value.reason == "files_pending":
                FollowFiles(destination, value, before_send, source_version=source_version).copy_step()
                before_send()
                value, _ = self._follow_invoke(destination, internal)
            if value.reason != "source_stop_required":
                return value
            skip_files = value.files_migrated is False and value.files_skip_request_id is not None
            if value.files_manifest_sha256 is None and not skip_files:
                raise Problem(409, "NATIVE_FILES_UNSUPPORTED", "the original follow has no verified workspace manifest")
            files = None if skip_files else FollowFiles(destination, value, before_send, source_version=source_version)
            stop_recorded = False
            if files is not None:
                if not isinstance(before_send, _CutFence):
                    # Today's follow outside a fence, unchanged: a source on another version is refused (the owner's
                    # omission, the source drift exit).
                    try:
                        files.require_source_version(active=False)
                    except Problem as error:
                        # A denied (fixable role assignment) or unconfirmed metadata read waits like `source_status`;
                        # a proven drift (409) is still refused.
                        if error.status not in {401, 403} and not isinstance(error, _MetadataUnconfirmed):
                            raise
                        logging.getLogger(__name__).warning(
                            "NATIVE_ENVIRONMENT_PENDING stage=precheck status=%s code=%s rid=%s",
                            error.status, error.body["error"]["code"], timing_id(command.request_id))
                        return value.model_copy(update={"reason": "source_unavailable"})
                # The fence: before FA's recorded stop `before_stop` refuses a source not active on its version; after
                # it a source awake on any version was woken, so it is stopped again below and the files are
                # published from the verified staging (ADR-0009 §10 check ②, dev 2026-10-16).
                stop_recorded = files.before_stop()
            source = client.for_target(_target(
                client.target, session_id=value.previous_hosted_session_id, agent_version=value.previous_agent_version))
            before_send()
            phase = "source_status"
            try:
                # The source is the Agent's current SID until this follow commits; an unclaimed Agent claims it
                # with the version this metadata read already shows (ADR-0009 §2), outside any owner request.
                status = _source_session_status(source, phase, observe=lambda session: self.observed_binding(
                    source, claimed_version=session.version_indicator.agent_version,
                    created_at=getattr(session, "created_at", None)))
                # ADR-0009 §10: stop only an active source. Idle or terminal has no compute to stop; creating,
                # updating or deleting is left alone and reported below as `source_active` for a later retry.
                if status == "active":
                    if isinstance(before_send, _CutFence):
                        # The background cut's reserve and durable marker, before every stop until it is marked.
                        before_send.stopping()
                    phase = "stop_session"
                    accepted = source.stop_session()
                    # `is not False` on purpose: only an explicit False (`session_already_stopped`, not FA's stop) skips the record;
                    # clients and test doubles that return None are treated as accepted.
                    if files is not None and not stop_recorded and accepted is not False:
                        # `stop_started` is written only after the platform accepted FA's own stop: a source found
                        # inactive without it was reclaimed and its staging is not published.
                        phase = "stop_record"
                        files.record_stop()
                        stop_recorded = True
                phase = "source_readback"
                status = _source_session_status(source, phase)
            except Problem as error:
                upstream = error.body["error"].get("upstream_status")
                logging.getLogger(__name__).warning(
                    "NATIVE_ENVIRONMENT_PENDING stage=%s status=%s code=%s upstream_status=%s",
                    phase, error.status, error.body["error"]["code"], upstream if type(upstream) is int else None)
                return value.model_copy(update={"reason": "source_unavailable"})
            if status not in {"idle", "failed", "deleted", "expired"}:
                return value.model_copy(update={"reason": "source_active"})
            if files is not None and not stop_recorded:
                # Inactive although FA never had its own stop accepted: the platform reclaimed it (ADR-0009 §10).
                raise Problem(409, "NATIVE_FILES_SOURCE_UNAVAILABLE",
                              "the original source became inactive without FA's stop; its files are not read")
            if files is not None and not files.stopped_step():
                return value.model_copy(update={"reason": "files_pending"})
            before_send()
            value, _ = self._follow_invoke(destination, internal.model_copy(update={"stage": "commit"}))
            if value.state == "ready":
                return self._confirm_environment(destination, value)
            return value

    def _automatic_decision(self, client, command, destination, state):
        from .behavior_native_turn_wire import NativeEnvironmentWithoutFiles, automatic_omission_id

        original = state.following or state.environment
        if original is not None and original.state == "pending":
            follow = NativeEnvironmentCommand(
                request_id=original.request_id, expected_session_id=original.previous_hosted_session_id,
                expected_agent_version=original.hosted_agent_version,
                idle_only=original.operation_idle_only if original.operation_idle_only is not None else command.idle_only)
        else:
            follow = NativeEnvironmentCommand(
                request_id=command.request_id, expected_session_id=state.hosted_session_id,
                expected_agent_version=command.expected_agent_version, idle_only=command.idle_only)
        if command.expected_session_id is not None and command.expected_session_id != follow.expected_session_id:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the requested original source changed")
        existing = (original.files_confirmation_saved if original is not None
                    and original.hosted_session_id == destination.target.session_id else None)
        return NativeEnvironmentWithoutFiles(
            request_id=existing or automatic_omission_id(
                client.target.agent_id, follow.expected_session_id, destination.target.session_id, command.expected_agent_version),
            follow=follow, expected_agent_version=command.expected_agent_version,
            confirmation="do-not-transfer-workspace-files")

    def advance(self, client, command, *, before_send):
        """The automatic follow (deploy-time ``follow_all`` and the submit-time follow). A source that can not be
        reached is abandoned up front (ADR-0009 §7a (i)/(ii)); a drifted idle source is abandoned only once the follow
        is left waiting on its own unfinished work (§7a (iii)). A background cut's fence the binding row records is
        settled first, by the same routine as ``cut`` (``_converge``, one pass); while it stays, this send waits.
        With ``FA_NATIVE_SESSION_GUARD_ENFORCE`` a bound Agent is never followed from here (``_advance_bound``), and
        a fence step another thread runs is never waited for (``_held``)."""
        early = self._binding_row(client.target.agent_id) if self.guard.enforce else None
        with self._changing(self._has_fence(None if early is None else early[1])):
            found = self._binding_row(client.target.agent_id)
            if found is not None and found[1] is not None and found[1].upgrade is not None:
                fence = found[1].upgrade
                settled, _ = self._converge(client, found[1], once=True)
                if settled == "fence_broken":
                    raise Problem(503, "AGENT_VERSION_FOLLOWING", "this Agent's files fence does not answer; nothing "
                                  "was sent", not_dispatched=True)
                if settled not in {None, "withdrawn", "pending_other", "conflict", "fence_lost", "abandoned"}:
                    # One batch: the fence's own outcome (pending while it stays, ready once committed); no other
                    # policy path may commit or withdraw it.
                    view = self._state(self._fence_reader(client, fence)).environment
                    if view is not None:
                        return view
                found = None  # the fence settled: read the row again
            if self.guard.enforce:
                found = found or self._binding_row(client.target.agent_id)
                if found is not None and found[1] is not None:
                    return self._advance_bound(client, command, found, before_send)
            current = self._same_version_decision(client, command, found)
            if current is not None:
                return current
            try:
                value = self._advance(client, command, before_send=before_send)
            except Problem as error:
                detail = error.body["error"]
                if (detail.get("files_check") or {}).get("category") not in _WORK_WAIT_CHECKS:
                    raise
                switched = self._abandon_waiting(client, command, before_send)
                if switched is None:
                    raise
                return switched
            if isinstance(value, NativeEnvironmentView) and value.state == "pending" and value.reason in _WORK_WAITS:
                switched = self._abandon_waiting(client, command, before_send)
                if switched is not None:
                    return switched
            return value

    def _same_version_decision(self, client, command, found=None):
        """ADR-0009 M5 with §7a (i)/(ii): the binding row's current SID was created on the selected version, so no
        follow is due, but the SID itself may be unusable (retired, or incompatible and idle). The abandon decision is
        made from metadata (``get_session``, no invocation) *before* any destination exists; once written, the
        destination is a fresh SID of the same version (``destination_session``) and ``follow`` carries it out
        (None: go on). Without a decision there is nothing to follow, and the ``current`` view is returned without
        calling any session: the Agent's own SID is the selected version's session (calling it as a "destination"
        could wake, or recreate, a retired SID). ``found``: the caller's ``_binding_row`` read, if any."""
        found = found or self._binding_row(client.target.agent_id)
        row = None if found is None else found[1]
        if (row is None or row.abandon is not None or row.created_agent_version != command.expected_agent_version
                or command.expected_session_id not in {None, row.hosted_session_id}):
            return None
        source = client.for_target(_target(client.target, session_id=row.hosted_session_id,
                                           agent_version=row.observed.agent_version))
        if self._abandon_decision(found[0], row, source, waiting=False) is not None:
            self.forget()
            return None
        return NativeEnvironmentView(
            agent_id=client.target.agent_id, request_id=command.request_id,
            previous_hosted_session_id=row.hosted_session_id, previous_agent_version=row.created_agent_version,
            hosted_session_id=row.hosted_session_id, hosted_agent_version=row.created_agent_version, state="current")

    def _advance_bound(self, client, command, found, before_send):
        """ADR-0009 2d-i (``FA_NATIVE_SESSION_GUARD_ENFORCE``, M1 step 2: every follow entry is off). A send, routine,
        Agent relay or deploy follow that meets a refusal of a bound Agent never starts a follow; by the binding row's
        current SID only what the ADR names happens here, and the background cut moves the Agent to the selected
        version after a turn end:
        - a written §7a decision, or one platform metadata makes now (a retired SID on its second read at least
          ``RETIRED_CONFIRM_SECONDS`` after the first, inside the request's wait; an incompatible one at once,
          M1 "收斂後還剩的情況"), is carried out to the selected version's session (``_carry_out``); the current SID
          is never invoked, read through Files or stopped;
        - a ``pending`` no fence of this API recorded (an older follow's) goes through ``_settle``: withdrawn by an
          H2d session, or driven to its commit once its files progress records FA's stop (ADR "例外才接手");
        - otherwise nothing is due: the ``current`` view, and the send goes on by the row.
        A wait raises 503 ``AGENT_VERSION_FOLLOWING`` (``not_dispatched``; ``retry_after_ms`` for the second read)."""
        store, row = found
        sid = row.hosted_session_id
        decision = row.abandon if row.abandon is not None and row.abandon.hosted_session_id == sid else None
        if decision is None:
            source = client.for_target(_target(client.target, session_id=sid, agent_version=row.observed.agent_version))
            try:
                metadata = _session_metadata(source)
            except Problem as error:
                if error.status in {401, 403}:
                    raise
                raise Problem(503, "AGENT_VERSION_FOLLOWING", "the Agent's session metadata is unconfirmed; nothing "
                              "was sent", not_dispatched=True) from None
            status = "missing" if metadata is None else metadata.status
            decision = self._abandon_decision(store, row, source, waiting=False, status=status)
            if decision is None and status in _RETIRED:
                raise Problem(503, "AGENT_VERSION_FOLLOWING", "the Agent's session read retired once; it is read "
                              "again before it is left", not_dispatched=True,
                              retry_after_ms=RETIRED_CONFIRM_SECONDS * 1000)
            if decision is None and status not in {"active", "idle"}:
                raise Problem(503, "AGENT_VERSION_FOLLOWING", "the Agent's session is " + str(status) +
                              "; nothing was sent", not_dispatched=True)
        if decision is not None:
            try:
                switched = self._carry_out(client, command.expected_agent_version, before_send)
            except Problem as error:
                if error.status < 500:
                    raise
                # The new session is not confirmed yet (M1 step 5): the same RID is resent, the next try goes on.
                logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_SWITCH_PENDING code=%s",
                                                    error.body["error"]["code"])
                switched = None
            if switched is None:
                raise Problem(503, "AGENT_VERSION_FOLLOWING", "the decided switch is not confirmed yet; nothing was "
                              "sent", not_dispatched=True)
            return switched
        state = self._state(self._bound_client(client, row))
        pending = state.environment
        if state.hosted_session_id == sid and pending is not None and pending.state == "pending":
            # Any H2d session withdraws it (H0 has no withdraw): the selected version's, as the background cut's.
            executor = self._destination(client, command.expected_agent_version, before_send, mark=False, row=row)
            settled = self._settle(client, executor, self._state(executor), row)
            if settled not in {None, "withdrawn", "conflict", "fence_lost"}:
                view = self._state(executor).environment  # the shared record: the drive's pending or its commit
                if view is not None:
                    return view
        return NativeEnvironmentView(
            agent_id=client.target.agent_id, request_id=command.request_id,
            previous_hosted_session_id=sid, previous_agent_version=row.created_agent_version,
            hosted_session_id=sid, hosted_agent_version=row.created_agent_version, state="current")

    def _carry_out(self, client, version, before_send):
        """The binding row's written §7a decision, carried out by any caller at ``version``'s destination (a fresh SID
        when that is the current SID's own version, M5), never invoking, reading from or stopping the current SID; a
        commit the row missed is recorded (§7a (j)). The switched view, or None when the row holds no decision or
        Hosted's record names another source. ``version`` is one this Agent vetoed (4d: it rolled back from it) only
        when the current version is incompatible: ``_unvetoed`` names the version."""
        found = self._binding_row(client.target.agent_id)
        row = None if found is None else found[1]
        if row is None or row.abandon is None or row.abandon.hosted_session_id != row.hosted_session_id:
            return None
        version = _unvetoed(row, version)
        destination = self._destination(client, version, before_send, mark=False, row=row)
        state = self._state(destination)
        existing = state.environment
        if (existing is not None and existing.state == "ready"
                and existing.hosted_session_id == destination.target.session_id == state.hosted_session_id
                and existing.previous_hosted_session_id == row.hosted_session_id):
            return self._switched_and_checked(destination, existing)
        command = NativeEnvironmentCommand(
            request_id=abandon_request_id(client.target.agent_id, row.hosted_session_id, destination.target.session_id),
            expected_session_id=row.hosted_session_id, expected_agent_version=version, idle_only=True)
        return self._abandon_source(client, command, destination, state, before_send, decide=False, found=found)

    def _abandon_waiting(self, client, command, before_send):
        destination = self._destination(client, command.expected_agent_version, before_send)
        state = self._state(destination)
        return self._abandon_source(client, command, destination, state, before_send, decide=True, waiting=True,
                                    found=self._binding_row(client.target.agent_id))

    def _follow_invoke(self, destination, command):
        """A follow (or files-omission) invocation. ``settled_session_ids`` (ADR-0009 §7a (i)) is new: an older
        destination image rejects the unknown field (422 ``NATIVE_REQUEST_INVALID``, before it reads or writes
        anything), so the command is sent once more without it and the field is left out for that destination from
        then on; that image then waits for those receipts, as it always did."""
        from .behavior_native_turn_api import NativeTurnGateway

        sid = destination.target.session_id
        plain = _without_settled(command)
        if plain is command or sid in self._settled_unsupported:
            return NativeTurnGateway._invoke(destination, plain)
        try:
            return NativeTurnGateway._invoke(destination, command)
        except Problem as error:
            if error.status != 422 or error.body["error"]["code"] != "NATIVE_REQUEST_INVALID":
                raise
        self._settled_unsupported.add(sid)
        logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_SETTLED_UNSUPPORTED session_id=%s", sid)
        return NativeTurnGateway._invoke(destination, plain)

    @staticmethod
    def _settled(found, destination):
        """ADR-0009 §7a (i): the binding row's previous SIDs; a follow never waits for their unfinished receipts."""
        row = None if found is None else found[1]
        if row is None:
            return ()
        return tuple(item.hosted_session_id for item in row.previous_sessions
                     if item.hosted_session_id != destination.target.session_id)

    def _binding_row(self, agent_id):
        """(store, row) for the abandon decision; None when the Table can not be read (nothing is decided then)."""
        try:
            store = binding_store(self.composition)
            return store, store.read(agent_id)
        except Exception as error:  # noqa: BLE001 - an unreadable row only means no abandon decision
            code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
            logging.getLogger(__name__).warning("NATIVE_BINDING_UNRECORDED operation=abandon code=%s", code)
            return None

    def _source_supported(self, source, row):
        """ADR-0009 §3 before any call: the binding row's recorded profile and the source version's definition
        (``get_version``, no session call). An unreadable definition says nothing: only the row decides then."""
        if row is not None and row.hosted_session_id == source.target.session_id and not row.supported():
            self.guard.mark_incompatible(source.target.agent_id, source.target.session_id)
            return False
        try:
            profile = _native_profile(source, floor=True)
        except Exception as error:  # noqa: BLE001 - an unreadable definition decides nothing
            code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
            logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_SOURCE_PROFILE_UNREAD code=%s", code)
            return True
        if profile in SUPPORTED_STATE_PROFILES:
            return True
        self.guard.mark_incompatible(source.target.agent_id, source.target.session_id)
        return False

    def _idle_timeout(self, source):
        try:
            return source.session_idle_timeout()
        except Exception:  # noqa: BLE001 - the documented maximum stands in for an unreadable setting
            return _IDLE_TIMEOUT_FALLBACK_SECONDS

    def _observe_active(self, source, status, now):
        """ADR-0009 §7a (h)/(l), observation and logging only (Neal 2026-10-13): an active incompatible source never
        causes a user-visible refusal, and the ADR revision makes it impossible by deploy ordering (expand/contract).
        Nothing is decided or refused here; the existing follow goes on. This is the integration point if that
        revision ever needs more: past the version's idle timeout the dedicated log names the source once."""
        sid = source.target.session_id
        logging.getLogger(__name__).warning(
            "NATIVE_ENVIRONMENT_SOURCE_INCOMPATIBLE_ACTIVE session_id=%s version=%s status=%s",
            sid, source.target.agent_version, status)
        first = self._active_seen.setdefault(sid, now)
        if sid not in self._overdue_logged and now - first > self._idle_timeout(source):
            self._overdue_logged.add(sid)
            logging.getLogger(__name__).warning(
                "NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE session_id=%s version=%s waited_seconds=%d",
                sid, source.target.agent_version, now - first)

    def _abandon_decision(self, store, row, source, *, waiting, status=None):
        """ADR-0009 §7a applicability from platform metadata (``get_session``, never an invocation). Returns the
        written decision, or None (the existing follow goes on). ``status``: the metadata the caller just read."""
        sid, agent_id = source.target.session_id, source.target.agent_id
        if status is None:
            try:
                metadata = _session_metadata(source)
            except Problem as error:
                # 5xx, timeout, 401/403 or an invalid record: not 404 or retired (ADR-0009 §9); nothing is decided.
                logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_SOURCE_UNREAD code=%s",
                                                    error.body["error"]["code"])
                return None
            status = "missing" if metadata is None else metadata.status
        now = monotonic()
        current = row is not None and row.hosted_session_id == sid
        if status not in _RETIRED:
            self._retired_seen.pop(sid, None)
            if current:
                self._stop_durable(row, "retired")
        if status != "active":
            self._active_seen.pop(sid, None)
        if not self._source_supported(source, row):
            if status not in _RETIRED and status != "idle":
                self._observe_active(source, status, now)
                if not self.guard.enforce:
                    return None
                # 2d-i (M1 "收斂後還剩的情況"): never waited for; the switch never invokes, reads or stops it.
                status = "active"
            basis = "incompatible"
        elif status in _RETIRED:
            # Durable with the flag (any replica's first read pairs with this one), else this process's (main).
            waited = self._durable(agent_id, sid, "retired", sid, restart_after=RETIRED_PAIR_SECONDS) if current else None
            if waited is None:
                first = self._retired_seen.setdefault(sid, now)
                if now - first > RETIRED_PAIR_SECONDS:
                    self._retired_seen[sid] = first = now
                waited = now - first
            if waited < RETIRED_CONFIRM_SECONDS:
                logging.getLogger(__name__).warning(
                    "NATIVE_ENVIRONMENT_SOURCE_RETIRED_UNCONFIRMED session_id=%s status=%s", sid, status)
                return None
            basis = "retired"
        elif (waiting and status == "idle" and row is not None and row.hosted_session_id == sid and row.drift):
            basis = "drifted"
        else:
            return None
        # Written only when this decision also claims the row. Best effort, unlike ``claim``: the decided switch's
        # ``record_switch`` replaces the whole row, and a source that declares nothing must still be abandoned. An
        # incompatible source is never recorded as supported, so an enforcing guard elsewhere never calls it.
        profile = INCOMPATIBLE_STATE_PROFILE if basis == "incompatible" else NATIVE_STATE_PROFILE
        if row is None:
            try:
                declared = self._row_profile(source.target, source.target.agent_version)
            except Problem as error:
                logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_SOURCE_PROFILE_UNREAD code=%s",
                                                    error.body["error"]["code"])
            else:
                if basis != "incompatible" or declared not in SUPPORTED_STATE_PROFILES:
                    profile = declared
        return store.decide_abandon(agent_id, hosted_session_id=sid, basis=basis, source_status=status,
                                    created_agent_version=source.target.agent_version, state_profile=profile)

    def _abandon_source(self, client, command, destination, state, before_send, *, decide, found, waiting=False):
        """ADR-0009 §7a: switch to ``destination`` without invoking, reading from or stopping the Agent's current
        session. A decision already in the binding row is carried out by any caller; a new one only when ``decide``.
        ``found``: ``_binding_row``'s (store, row). None: the existing follow goes on."""
        from .behavior_native_turn_api import NativeTurnGateway

        sid = state.hosted_session_id
        if sid == destination.target.session_id or command.expected_session_id not in {None, sid}:
            return None
        environment = state.environment
        if environment is not None:
            version = environment.previous_agent_version if environment.state == "pending" else environment.hosted_agent_version
        elif state.preparation is not None:
            version = state.preparation.hosted_agent_version
        else:
            return None
        source = client.for_target(_target(client.target, session_id=sid, agent_version=version))
        if found is None:
            return None
        store, row = found
        decision = row.abandon if row is not None and row.abandon is not None and row.hosted_session_id == sid else None
        if decision is None:
            if not decide:
                return None
            decision = self._abandon_decision(store, row, source, waiting=waiting)
            if decision is None:
                return None
        if decision.basis == "incompatible":
            self.guard.mark_incompatible(client.target.agent_id, sid)
        before_send()
        value, _ = NativeTurnGateway._invoke(destination, NativeSessionAbandon(
            request_id=abandon_request_id(client.target.agent_id, sid, destination.target.session_id),
            expected_session_id=sid, expected_etag=state.etag,
            # An ``active`` source (a stuck admission) still runs: Hosted waits for its started receipts, as unset does.
            source_status=None if decision.source_status == "active" else decision.source_status))
        if value.state != "ready":
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original abandon-source switch did not commit")
        return self._confirm_environment(destination, value)

    def _advance(self, client, command, *, before_send):
        from .native_files_wire import FILES_ERRORS

        with self._changing():
            deciding, self._deciding_abandon = self._deciding_abandon, True
            try:
                try:
                    value = self.follow(client, command, before_send=before_send)
                finally:
                    self._deciding_abandon = deciding
                if getattr(value, "state", None) == "ready":
                    self._drive_failing.pop(value.request_id, None)  # only an end clears the transient clock
                return value
            except Problem as error:
                code = error.body["error"]["code"]
                if code not in FILES_ERRORS | {"NATIVE_SESSION_CHANGED", "NATIVE_ENVIRONMENT_CONFLICT"}:
                    raise
                failure = error
            destination = self._destination(client, command.expected_agent_version, before_send)
            state = self._state(destination)
            if state.following is not None and state.following.files_reconcile_required:
                raise failure
            pending = state.environment
            if (pending is not None and pending.state == "pending"
                    and pending.hosted_session_id != destination.target.session_id
                    and int(pending.hosted_agent_version) < int(command.expected_agent_version)
                    and command.request_id != pending.request_id):
                source = _session_metadata(client.for_target(_target(
                    client.target, session_id=pending.previous_hosted_session_id,
                    agent_version=pending.previous_agent_version)))
                # Retargeting keeps the files transfer, which calls the source: only an active one (ADR-0009 §10).
                if (source is not None and source.status == "active" and not pending.source_invocation_refused
                        and source.version_indicator.agent_version == pending.previous_agent_version):
                    retarget = NativeEnvironmentRetargetCommand(
                        request_id=command.request_id, expected_agent_version=command.expected_agent_version,
                        expected_session_id=command.expected_session_id or pending.previous_hosted_session_id,
                        pending_request_id=pending.request_id, pending_session_id=pending.hosted_session_id,
                        idle_only=command.idle_only)
                    try:
                        return self.retarget(client, retarget, before_send=before_send)
                    except Problem as error:
                        code = error.body["error"]["code"]
                        if code not in FILES_ERRORS or code == "NATIVE_FILES_UNKNOWN":
                            raise
                        failure, state = error, self._state(destination)
            pending = state.environment
            if (self.guard.enforce and code in FILES_ERRORS and code not in _UNMOVABLE and pending is not None
                    and pending.state == "pending"):
                # 2d-i on: a slow or denied transfer keeps its pending for the next send, as a fence's drive does
                # (``CUT_TRANSIENT_SECONDS``); flag off, today's follow commits without the files (unchanged). The
                # caller gets the pending view (a send: ``server_following``), never the transient error (P6).
                if self._drive_failed(failure, pending) is None:
                    logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_FILES_WAITING code=%s session_id=%s",
                                                        code, pending.hosted_session_id)
                    return pending.model_copy(update={"reason": "files_pending"})
                self._drive_failing.pop(pending.request_id, None)  # overdue: the no-files commit below ends it
            decision = self._automatic_decision(client, command, destination, state)
            try:
                return self.without_files(client, decision, before_send=before_send,
                                          refusal_code=code if code in FILES_ERRORS else None, system_policy=True)
            except Problem as error:
                category = error.body["error"].get("files_check", {}).get("category")
                waiting = {"active_waiting": "accepted_or_started", "admission_pending": "admission_pending",
                           "execution_active": "accepted_or_started", "task_active": "task_active",
                           "task_unconfirmed": "task_unconfirmed"}
                original = state.following or state.environment
                if category in waiting and original is not None:
                    return original.model_copy(update={"reason": waiting[category]})
                raise

    def observe_automatic(self, client, command, *, before_send):
        with self._changing():
            try:
                value = self.observe_environment(client, command)
                if value.files_reconcile_required:
                    raise Problem(503, "NATIVE_FILES_UNKNOWN", "the original file outcome is still unconfirmed")
                if value.state in {"ready", "current"} or value.files_migrated is False or value.progress_token is not None:
                    return self._observe_source_progress(client, value)
            except Problem as error:
                if error.body["error"]["code"] not in {"NATIVE_ENVIRONMENT_UNKNOWN", "NATIVE_SESSION_CHANGED"}:
                    raise
            destination = self.candidate(client, version=command.expected_agent_version)
            actual = _read_platform_session(destination)
            if actual is None or actual.agent_version != command.expected_agent_version:
                raise Problem(409, "NATIVE_SESSION_CHANGED", "the original automatic reader is unavailable; nothing was created")
            state = self._state(destination)
            decision = self._automatic_decision(client, command, destination, state)
            value = self.without_files(client, decision, before_send=before_send, reconcile=True, observe_only=True,
                                       system_policy=True)
            return self._observe_source_progress(client, value)

    @staticmethod
    def _observe_source_progress(client, value):
        if value.state != "pending":
            return value
        source = client.for_target(_target(client.target, session_id=value.previous_hosted_session_id,
                                           agent_version=value.previous_agent_version))
        metadata = _session_metadata(source)
        state = None if metadata is None else {
            "version": metadata.version_indicator.agent_version, "status": metadata.status}
        return value.model_copy(update={"progress_token": material_digest({
            "original": value.progress_token or material_digest(value.model_dump(mode="json")), "source": state})})

    def without_files(self, client, command, *, before_send, reconcile=False, preview=False, resume=False,
                      observe_only=False, refusal_code=None, system_policy=False):
        from .behavior_native_turn_api import NativeTurnGateway, _OmissionBeforeDispatch
        from .native_files_wire import FILES_PROFILE

        with self._changing():
            follow = command.follow
            target_version = command.expected_agent_version or follow.expected_agent_version
            if (preview or resume) and self.selected_version(client) != target_version:
                raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the displayed destination version changed")
            original_destination_changed = False
            if not reconcile and target_version != follow.expected_agent_version:
                original = self.candidate(client, version=follow.expected_agent_version)
                metadata = _session_metadata(original)
                original_destination_changed = (metadata is not None
                    and metadata.version_indicator.agent_version != follow.expected_agent_version)
                if preview and metadata is not None and (
                        _read_platform_session(self.candidate(client, version=target_version)) is None):
                    return NativeEnvironmentReplacementPreview(
                        agent_id=client.target.agent_id,
                        hosted_session_id=self.candidate(client, version=target_version).target.session_id,
                        hosted_agent_version=target_version, original_request_id=follow.request_id,
                        original_hosted_session_id=original.target.session_id,
                        original_hosted_agent_version=follow.expected_agent_version,
                        observed_agent_version=metadata.version_indicator.agent_version,
                        reason="destination_version_changed" if original_destination_changed else "reader_required")
            if reconcile or preview or resume:
                destination = self.candidate(client, version=target_version)
                actual = _read_platform_session(destination)
                if actual is None or actual.agent_version != target_version:
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "read the original omission destination; nothing was created")
                destination = destination.for_target(actual)
            else:
                destination = self._destination(client, target_version, before_send)
            state = self._state(destination)
            if state.files_omission_profile != "fa-files-omission-v1":
                raise Problem(409, "NATIVE_FILES_CONFIRMATION_UNSUPPORTED",
                              "this destination image predates explicit file omission; use a compatible image")
            changed_target = target_version != follow.expected_agent_version
            original_target_sid = self.candidate(client, version=follow.expected_agent_version).target.session_id
            fresh_request_id = material_digest({
                "kind": "files-omission-follow", "confirmation": command.request_id,
                "original": follow.request_id, "source": original_target_sid, "target": destination.target.session_id})
            original_ready = (changed_target and state.environment is not None and state.environment.state == "ready"
                and state.environment.request_id == follow.request_id
                and state.environment.hosted_session_id == original_target_sid
                and state.environment.hosted_agent_version == follow.expected_agent_version)
            fresh_follow = changed_target and (original_ready or any(
                value is not None and value.request_id == fresh_request_id
                and value.previous_hosted_session_id == original_target_sid
                and value.hosted_session_id == destination.target.session_id
                and value.hosted_agent_version == target_version
                for value in (state.environment, state.following)))
            if original_ready and follow.expected_session_id is not None and (
                    follow.expected_session_id != state.environment.previous_hosted_session_id):
                raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the referenced ready operation has another source")
            source_sid = follow.expected_session_id or state.hosted_session_id
            retained = state.following or state.environment
            if fresh_follow:
                source_sid = original_target_sid
            elif retained is not None and (
                    retained.request_id == follow.request_id or retained.files_skip_request_id == command.request_id):
                if follow.expected_session_id is not None and follow.expected_session_id != retained.previous_hosted_session_id:
                    raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the displayed source differs from the original file operation")
                source_sid = retained.previous_hosted_session_id
            pending_request_id = pending_session_id = None
            if changed_target:
                if not fresh_follow:
                    pending_request_id = follow.request_id
                    pending_session_id = original_target_sid
                follow = NativeEnvironmentCommand(
                    request_id=fresh_request_id if fresh_follow else material_digest({
                        "kind": "files-omission-retarget", "confirmation": command.request_id, "original": follow.request_id,
                        "source": source_sid, "target": destination.target.session_id}),
                    expected_session_id=source_sid, expected_agent_version=target_version, idle_only=True)
                if state.environment is not None and state.environment.request_id == follow.request_id:
                    source_sid = state.environment.previous_hosted_session_id
            if source_sid == destination.target.session_id:
                raise Problem(409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "there is no original environment transition to confirm")
            already_ready = (retained is not None and retained.state == "ready" and retained.request_id == follow.request_id
                             and retained.files_skip_request_id == command.request_id)
            missing = None
            valid_reasons = set()
            if not reconcile and not already_ready:
                source = client.for_target(_target(client.target, session_id=source_sid))
                actual = _session_metadata(source)
                original = state.environment or state.preparation
                pending = state.environment is not None and state.environment.state == "pending"
                profile = (original.previous_files_profile if pending else original.files_profile) if original is not None else None
                version = (original.previous_agent_version if pending else original.hosted_agent_version) if original is not None else None
                refused = (state.source_invocation_refused and state.hosted_session_id == source_sid or
                           retained is not None and retained.previous_hosted_session_id == source_sid
                           and retained.source_invocation_refused)
                if actual is not None and version is not None and actual.version_indicator.agent_version != version:
                    valid_reasons.add("source_changed")
                if original_destination_changed and not fresh_follow:
                    valid_reasons.add("original_destination")
                if state.files_profile != FILES_PROFILE:
                    valid_reasons.add("destination")
                if not (profile == FILES_PROFILE and not refused and actual is not None
                        and actual.status in {"active", "idle"} and actual.version_indicator.agent_version == version):
                    valid_reasons.add("source")
                if system_policy and refusal_code is not None:
                    valid_reasons.add("source_not_migratable")
                if "source_changed" in valid_reasons:
                    missing = "source_changed"
                elif "original_destination" in valid_reasons:
                    missing = "original_destination"
                elif "destination" in valid_reasons:
                    missing = "destination"
                elif "source" in valid_reasons:
                    missing = "source"
                elif "source_not_migratable" in valid_reasons:
                    missing = "source_not_migratable"
                else:
                    if not preview and not resume:
                        raise _OmissionBeforeDispatch(
                            command.request_id, "NATIVE_FILES_CONFLICT",
                            "both helpers are available; no omission confirmation was dispatched")
                    raise Problem(409, "NATIVE_FILES_CONFLICT", "both helpers are available; keep the original migration")
            before_send()
            internal = NativeSessionFilesOmission(
                request_id=command.request_id,
                follow=NativeSessionFollow(
                    request_id=follow.request_id, expected_session_id=source_sid, expected_etag=state.etag,
                    stage="prepare", idle_only=follow.idle_only, files_required=True,
                    settled_session_ids=self._settled(self._binding_row(client.target.agent_id), destination)),
                stage="read" if reconcile or already_ready else "check" if preview else "activate" if resume else "confirm",
                missing=missing,
                pending_request_id=pending_request_id, pending_session_id=pending_session_id,
                confirmation_digest=None if system_policy else material_digest(command),
                decision_basis="system_policy" if system_policy else None)

            def original_reason(observed):
                reason = observed.files_confirmation_reason
                if observed.files_confirmation_saved != command.request_id or reason not in valid_reasons:
                    raise Problem(409, "NATIVE_FILES_CONFLICT", "the saved omission reason is no longer confirmed")
                return internal.model_copy(update={"missing": reason})

            if internal.stage == "activate":
                observed, _ = self._follow_invoke(
                    destination, internal.model_copy(update={"stage": "read", "missing": None}))
                if observed.files_migrated is False and observed.files_skip_request_id == command.request_id:
                    return self.follow(client, follow, before_send=before_send)
                internal = original_reason(observed)
            if internal.stage == "confirm":
                try:
                    checked, _ = self._follow_invoke(destination, internal.model_copy(update={"stage": "check"}))
                    if checked.files_migrated is False and checked.files_skip_request_id == command.request_id:
                        return self.follow(client, follow, before_send=before_send)
                    if checked.reason == "scan_pending":
                        return checked.model_copy(update={
                            "files_confirmation_saved": None, "files_confirmation_reason": None,
                            "files_confirmation_absent": None,
                            "files_confirmation_pending": command.request_id})
                    if system_policy and checked.reason in {
                            "accepted_or_started", "admission_pending", "task_active", "task_unconfirmed", "task_access_denied"}:
                        return checked
                    if checked.files_confirmation_saved is not None:
                        internal = original_reason(checked)
                except Problem as error:
                    if error.status == 409 and error.body["error"]["code"] in {
                            "NATIVE_FILES_CONFLICT", "NATIVE_FILES_CONFIRMATION_REQUIRED", "NATIVE_ENVIRONMENT_CONFLICT"}:
                        raise _OmissionBeforeDispatch(command.request_id, error.body["error"]["code"],
                                                     "the read-only check refused; no omission confirmation was dispatched",
                                                     files_check=error.body["error"].get("files_check")) from None
                    raise
                before_send()
            value, _ = self._follow_invoke(destination, internal)
            if observe_only:
                return value
            if reconcile:
                if value.state == "ready":
                    return value
                if command.request_id in {value.files_confirmation_absent, value.files_confirmation_saved}:
                    before_send()
                    return self.without_files(client, command, before_send=before_send, system_policy=system_policy,
                                              refusal_code=refusal_code)
                if value.files_migrated is False and value.files_skip_request_id == command.request_id:
                    return self.follow(client, follow, before_send=before_send)
                return value
            if preview or already_ready or value.files_confirmation_pending == command.request_id:
                return value
            return self.follow(client, follow, before_send=before_send)

    def cut(self, client, *, stop=None, target=None):
        """ADR-0009 M1 2d-i step 2, run in the background after a turn end (``native_cut``): move the Agent from the
        binding row's SID to the selected version's session while the shared store is quiet. Returns
        (outcome, reason, fence seconds or None) for the ``NATIVE_UPGRADE_CUT_API`` line.

        Probe, list, cut (#856): the destination's ``NativeSessionCut`` probe answers E0 when quiet; only then are the
        source's workspace files listed (Session Files, only an ``active`` source); the cut carries ``expected_etag=E0``
        so a turn admitted meanwhile makes it busy. An ``idle`` source (read before the destination and after the
        probe) is ``source_idle``: nothing moves until a turn end finds it active. Without files Hosted commits
        directly. With files the row records
        the fence (``upgrade``), Hosted writes the H0-shaped ``pending``, and today's follow steps move the files, stop
        the source and commit within ``CUT_FENCE_SECONDS``; an overrun or a failure before FA's stop of the source
        withdraws the fence, and after its marker (``stop_started``) the fence is only ever driven forward, at its own
        destination whatever the selector names by then. Busy waits for the next turn end: nothing here loops or polls
        outside that fence. Only 5b's snapshot (both flags on, an ``active`` source; ``upgrade_snapshot_flow``)
        invokes the source; with it the files are restored on the destination before a no-fence commit that records
        them. A fence the row records (left by a crash or an ended job) is
        settled at its own destination before the selector is read (``_converge``, as every send's ``advance`` does
        first), and a commit the row missed is
        recorded (§7a (j)); an ``admitting`` seen for ``STUCK_ADMISSION_SECONDS`` is abandoned (§7a). ``self.lock`` is
        held only by the fence's follow steps, so no send waits behind a probe. Waiting for a new destination to be
        ready, the sends after it (the probe, 5b's carry, the no-fence switch) end at one ``CUT_DESTINATION_SECONDS``
        from the cut's start or on shutdown: ``_FenceOverrun`` is raised (5b's carry falls back to the fence instead).

        ``target``: 4d's rollback (``_crash_loop``), the version this cut goes to instead of the selector's."""
        from .behavior_native_turn_api import NativeTurnGateway
        from .behavior_native_turn_wire import NativeSessionCut
        from .behavior_session_files import SessionFiles

        if target is not None:
            token = _ROLLBACK.set(target)
            try:
                return self.cut(client, stop=stop)
            finally:
                _ROLLBACK.reset(token)
        agent_id = client.target.agent_id
        found = self._binding_row(agent_id)
        row = None if found is None else found[1]
        # A recorded fence settles first, as every send's ``advance`` does: a heal that finds V_rec unsupported (the
        # row then ``unbound``) must not leave it unsettled.
        if row is not None and row.upgrade is not None:
            settled, row = self._converge(client, row, stop=stop)
            if settled is not None:
                return settled, None, None
        if row is not None and (row.profile_unread or row.drift):
            row = self._correct_profile(client, row)
        if row is None or not row.supported() and row.abandon is None:
            return "unbound", None, None
        if self.idle_upgrade and row.abandon is None and _ROLLBACK.get() is None:
            rolled = self._crash_loop(client, row, stop)
            if rolled is not None:
                return rolled
        selected = self.selected_version(client)
        # The wait for a new destination (``create_session`` polls while it is creating/updating) and any send after
        # it end at the bound or on shutdown: ``_FenceOverrun`` leaves the cut, the next trigger tries again.
        bound = _CutFence(CUT_DESTINATION_SECONDS, stop or Event())
        if row.abandon is not None:
            # ADR-0009 M1 step 4(b) (P8 gap): a decided Agent with no sends (the patrol's retired SID, an
            # incompatible claim) is switched by the background job, so it never holds the 2d-ii gate.
            switched = self._carry_out(client, selected, bound)
            self.forget()
            return ("abandoned" if switched is not None else "abandon_pending"), None, None
        if row.observed.agent_version == selected:
            return "current", None, None
        if self.idle_upgrade:
            # Before the source and target are read (no extra read without the flag); the deferral start is recorded
            # only once both pass (U0).
            refused = self._idle_due(client, row, selected)
            if refused is not None:
                return refused
        source_sid = row.hosted_session_id
        source = client.for_target(_target(client.target, session_id=source_sid, agent_version=row.observed.agent_version))

        def source_status():
            metadata = _session_metadata(source)
            return "missing" if metadata is None else metadata.status

        status = source_status()
        # ADR-0009 U0 (Neal decision 5): with the idle upgrade a compatible source moves only while ``active``.
        if status == "idle" or self.idle_upgrade and status != "active":
            # Coordinator 2026-10-16: an idle source's files can not be copied without waking it, so no session is
            # created and the source is not read; the Agent moves at its next real turn end (a stale receipt read is
            # harmless). What an existing destination holds (a legacy pending, a commit the row missed) still settles.
            existing = self.candidate(client, version=selected, row=row, located=True)
            metadata = None if existing.target.session_id == source_sid else _session_metadata(existing)
            if metadata is not None and metadata.status in {"active", "idle"}:
                settled = self._settle(client, existing, self._state(existing), row)
                if settled is not None:
                    return settled, None, None
            return ("source_idle", None, None) if status == "idle" else ("source_unavailable", status, None)
        try:
            profile = _native_profile(client.for_target(_target(client.target, agent_version=selected)))
        except Problem as error:
            return "target_unread", error.body["error"]["code"], None
        if profile not in SUPPORTED_STATE_PROFILES:
            return "target_incompatible", profile, None
        if self.idle_upgrade:
            self._record_due(row, selected)  # U0 held: target compatible, compatible source active, not vetoed
            waiting = self._health_wait(row, selected)  # before any destination is created or woken
            if waiting is not None:
                return waiting
        destination = self._destination(client, selected, bound, mark=False, row=row)
        sid = destination.target.session_id
        if sid == source_sid:
            # The row's own SID was created on the selected version and woke on another: the §7a drift path.
            return "same_session", None, None
        state = self._state(destination)
        settled = self._settle(client, destination, state, row)
        if settled is not None:
            if settled == "repaired" and self.idle_upgrade:
                self._finish(source, destination, bound=bound)  # U9 for a commit the row missed, once recorded
            return settled, None, None
        if state.admission_request_id is None:
            self._admitting_seen.pop(agent_id, None)
            self._stop_durable(row, "admitting")
        else:
            if self._admitting_for(agent_id, row, state.admission_request_id) < STUCK_ADMISSION_SECONDS:
                return "busy", "admission_pending", None
            status = source_status()
            if status not in {"active", "idle"}:
                return "source_unavailable", status, None
            return self._abandon_stuck(client, row, status, selected, bound), "stuck_admission", None
        if self.idle_upgrade:
            unhealthy = self._health(destination, row, selected, bound)  # U3, before the probe
            if unhealthy is not None:
                return unhealthy
            self._prerestore(destination, row, bound)  # U9's pre-restore: after U3, before U5
        request_id = cut_request_id(agent_id, source_sid, sid)
        settled_ids = self._settled(found, destination)
        asked = NativeSessionCut(request_id=request_id, expected_session_id=source_sid, mode="probe",
                                 settled_session_ids=settled_ids)
        probe, _ = NativeTurnGateway._invoke(destination, asked, timeout=_left(bound))
        if probe.quiet_etag is None:
            return "busy", probe.reason, None
        # Read after the probe (probe, then list): a source that turned active before the quiet point is listed.
        status = source_status()
        if status not in {"active", "idle"}:
            return "source_unavailable", status, None
        if status == "idle":
            return "source_idle", None, None
        # ADR-0009 5b U1/U4 (flags off: no call): the workspace snapshot and its restore, between E0 and the cut, within
        # what is left of the cut's ``bound``.
        carried = upgrade_snapshot_flow.carry(self, client, source, destination, asked, probe.quiet_etag, bound)
        if isinstance(carried, tuple):
            return carried
        try:
            # ADR-0009 §10: only an active source is listed; an idle one was reclaimed and is never woken.
            files = carried is None and SessionFiles(client).has_files(source_sid)
        except Problem as error:
            return "files_unknown", error.body["error"]["code"], None
        command = NativeSessionCut(request_id=request_id, expected_session_id=source_sid,
                                   expected_etag=probe.quiet_etag, files_required=files,
                                   settled_session_ids=settled_ids, files_restore=carried)
        if files:
            fenced = self._fenced(client, destination, command, row, selected, stop)
            if fenced[0] == "ready":
                upgrade_snapshot_flow.switched(self, destination, failed=False)
            if self.idle_upgrade and fenced[0] == "ready":
                self._finish(None, destination, bound=bound)  # the fence already stopped the source
            return fenced
        try:
            value, _ = NativeTurnGateway._invoke(destination, command, timeout=_left(bound))
        except Problem as error:
            if carried is None:
                raise
            code = error.body["error"]["code"]
            if code == "FOUNDRY_INVOCATION_UNKNOWN":
                # Unknown, not failed (rule 7): read back; a commit the row missed is recorded (§7a (j)), never counted.
                settled = self._settle(client, destination, self._state(destination), row)
                if settled == "repaired":
                    upgrade_snapshot_flow.switched(self, destination, failed=False)
                    self._finish(source, destination, bound=bound)  # U9, after U8 (carried implies idle_upgrade)
                return settled or "busy", code, None
            # The staged restore did not commit (a staged file differs, or the publish is unconfirmed): no switch; the
            # third such failure in a row for this target sends the next attempt through the fence.
            upgrade_snapshot_flow.switched(self, destination, failed=True)
            return "busy", code, None
        if value.state != "ready":
            return "busy", value.reason, None
        self._confirm_environment(destination, value)
        self.forget()
        upgrade_snapshot_flow.switched(self, destination, failed=False)
        if self.idle_upgrade:
            self._finish(source, destination, bound=bound)  # U9, after U8
        return "ready", None if carried is None else "snapshot", None

    def _idle_due(self, client, row, selected):
        """ADR-0009 U0 (``FA_NATIVE_IDLE_UPGRADE``): ``cut``'s outcome when ``selected`` is vetoed (this Agent's list on
        the row, or the project-level row, which a 4d rollback does not read) or the veto can not be read; else None.
        Nothing is written here."""
        from .native_version_veto import veto_store

        kept = row.idle_upgrade
        if kept is not None and selected in kept.vetoed_versions:
            return "vetoed", "agent", None
        if _ROLLBACK.get() is not None:
            # Coordinator 2026-10-19: the current version's crash-loop outweighs another Agent's veto of the target.
            return None
        store = veto_store(self.composition)  # a misconfigured store raises: the cut fails visibly (outcome=error)
        try:
            vetoed = store.read(client.target.agent_name, selected) is not None
        except Problem as error:  # the Table read (Azure errors wrapped) or an invalid row: the next turn end reads again
            return "veto_unread", error.body["error"]["code"], None
        return ("vetoed", "project", None) if vetoed else None

    def _crash_loop(self, client, row, stop):
        """ADR-0009 §5 / 4d (``FA_NATIVE_IDLE_UPGRADE``): within ``CRASH_LOOP_SECONDS`` of the row's last switch, the
        current SID's ``recent_crashes`` (Hosted counts a RID once and never a ``deferred:`` exit or a ``suspended``
        chain) after that switch, plus the post-switch start failures, decide a crash-loop at ``CRASH_LOOP_COUNT``
        distinct CIDs and RIDs. Then the version is vetoed (the project row, then this Agent's list, neither blocking
        the other, both before the rollback, so the next turn end's U0 never moves back) and the Agent goes back
        through ``cut`` to the newest previous version: its destination is derived from the current SID, never a previous one (§10). None: nothing
        decided. Outside the window nothing is read; inside it, one metadata read and, only while ``active``, one
        state read. A current version already in this Agent's vetoes is a rollback decided before (busy, or its fence
        overran): it is retried at every turn end until done, whatever the window. The project veto is not read for
        this: it also names versions other Agents vetoed, and a healthy Agent on that version is not rolled back (Neal
        decision 3: 本 Agent 退回). A decision whose own veto was not written is retried the same way while this
        process runs. A rollback target this Agent vetoed itself, or one ``cut`` finds incompatible, leaves no version
        to go to (``_rollback_blocked``). A same-SID drift onto another version goes to ``_in_place_crash_loop`` instead
        (a drift back to the creation version is judged here, its window opening at the later of the switch and the
        drift back)."""
        kept = row.idle_upgrade
        if row.drift and row.observed.agent_version != row.created_agent_version:
            return self._in_place_crash_loop(client, row, kept)
        if kept is None:
            return None
        version = row.created_agent_version
        # A SID backfilled from an unfinished receipt has no known version (``record_unfinished``): never a target.
        back = next((item.agent_version for item in reversed(row.previous_sessions)
                     if item.agent_version not in (None, version)), None)
        if back is None:
            return None
        decided = (row.agent_id, row.hosted_session_id, version)
        if version in kept.vetoed_versions:
            if kept.rollback_blocked:
                return None  # recorded once: the Agent stays on this version and the selector still applies
            # Decided earlier, the rollback busy or overrun: finish it whatever the window, with no reads or judging.
            self._project_veto(client, version)  # again: the first write may have failed
            return self._decided(client, row, stop, version, back)
        # A drift back to the creation version: crashes before it were another version's (``NativeCrash`` has none).
        since = None
        if row.drift and kept.switched_at is not None:
            since = max(kept.switched_at, row.observed.observed_at, key=datetime.fromisoformat)
        if decided not in self._veto_unrecorded and not self._crash_looping(client, row, kept, version, back,
                                                                            since=since):
            return None
        # First, and on its own: durable even while this Agent's own veto keeps failing, so a restart still keeps other
        # Agents off ``version``.
        self._project_veto(client, version)
        vetoed = self._own_veto(row, version)
        if vetoed is None:
            # This Agent's veto is what finishes the rollback after the window, so none starts without it; until it is
            # written every turn end tries again, whatever the window (shortcut: in process memory, so a restart
            # after the window leaves the Agent on this version; upgrade once a second durable record exists).
            self._veto_unrecorded.add(decided)
            return "crash_loop", "veto_unrecorded", None
        self._veto_unrecorded.discard(decided)
        return self._decided(client, vetoed, stop, version, back)

    def _in_place_crash_loop(self, client, row, kept):
        """Neal 2026-10-20 (ADR-0009 4d): the platform resumed the current SID on another version (the drift
        ``record_observed`` wrote), so FA's ``switched_at`` and ``previous_sessions`` say nothing about it. The window
        opens at the recorded version's ``observed_at`` (rewritten at each in-place move, so each move opens one for
        the version now observed) and the same counters judge it. A crash-loop writes only the project veto, so U0 and
        the cut move no other Agent onto that version; there is no rollback (the platform does not reliably honour a
        new SID pinned to the old version) and no own veto. The operator reads the warning and the deploy gate's
        ``vetoed_versions``; the owner's notices are unchanged. A drift onto a version this API cannot speak never gets
        here: ``cut`` returns ``unbound`` first (#927 records the drift's profile) and §7a's follow handles that SID; its
        state is not readable on this wire, and ``cut`` never moves an Agent onto an incompatible version anyway. shortcut: a restart inside the window judges again
        and logs the same decision once more (the Insert reads the row back); read the project veto first if the repeat
        line misleads operators."""
        version = row.observed.agent_version
        if (client.target.agent_name, version) in self._project_vetoed:
            return None  # decided and written: nothing is read again, the cut goes on to the selector
        decided = (row.agent_id, row.hosted_session_id, version)
        if decided not in self._veto_unrecorded and not self._crash_looping(client, row, kept, version, None,
                                                                            since=row.observed.observed_at):
            return None
        self._project_veto(client, version)
        if (client.target.agent_name, version) in self._project_vetoed:
            self._veto_unrecorded.discard(decided)
            return "crash_loop", "in_place_veto", None
        # Written at the next turn end, whatever the window (same in-process shortcut as the own veto above).
        self._veto_unrecorded.add(decided)
        return "crash_loop", "veto_unrecorded", None

    def _own_veto(self, row, version, notice=False):
        """This Agent's veto of ``version`` (``record_veto``, with its "沒有更新" notice for ``notice``), read back: the
        row that holds it, or None when it is not written (a failure only logs)."""
        vetoed = self._binding("veto", lambda store: store.record_veto(
            row.agent_id, hosted_session_id=row.hosted_session_id, agent_version=version, notice=notice))
        held = vetoed is not None and vetoed.idle_upgrade is not None and version in vetoed.idle_upgrade.vetoed_versions
        return vetoed if held else None

    def _project_veto(self, client, version):
        """The project row of ``version`` (an Insert, kept as written); a failure only logs, so it never blocks this
        Agent's own veto or rollback, and the next turn end writes it again. Once written it is not written again
        (shortcut: in process memory, so a restart writes it once more; fine while an Insert is idempotent)."""
        from .native_version_veto import veto_store

        key = (client.target.agent_name, version)
        if key in self._project_vetoed:
            return
        store = veto_store(self.composition)
        if self._binding("project_veto", lambda _binding: store.veto(*key)) is not None:
            self._project_vetoed.add(key)

    def _decided(self, client, row, stop, version, back):
        """A crash-loop of ``version`` this Agent's vetoes hold: the rollback to ``back`` unless this Agent vetoed
        ``back`` too."""
        if back in row.idle_upgrade.vetoed_versions:
            return self._rollback_blocked(row, version, back)
        return self._rollback(client, row, stop, version, back)

    def _rollback_blocked(self, row, version, back, why="vetoed=agent"):
        """Coordinator 2026-10-19: no usable earlier version. Logged and recorded once (``rollback_blocked``); the
        Agent keeps serving on ``version`` and ``cut`` goes on to the selector (None)."""
        logging.getLogger(__name__).warning(
            "NATIVE_CRASH_LOOP_NO_ROLLBACK agent_id=%s session_id=%s version=%s rollback_to=%s %s",
            row.agent_id, row.hosted_session_id, version, back, why)
        self._binding("rollback_blocked", lambda store: store.record_rollback_blocked(
            row.agent_id, hosted_session_id=row.hosted_session_id))
        return None

    def _crash_looping(self, client, row, kept, version, back, *, since=None):
        """The window judgement of ``_crash_loop``: True when the current SID crash-loops (logged). The window opens
        at ``since`` (an in-place drift's time), else at the row's last switch."""
        since = since or (kept.switched_at if kept is not None else None)
        if since is None:
            return False
        switched = datetime.fromisoformat(since).timestamp()
        now = binding_store(self.composition).clock()
        if now is None or now.timestamp() - switched > CRASH_LOOP_SECONDS:
            return False
        current = client.for_target(_target(client.target, session_id=row.hosted_session_id,
                                            agent_version=row.observed.agent_version))
        try:
            metadata = _session_metadata(current)
            if metadata is None or metadata.status != "active":
                return False  # §10: never woken for this; a later turn end reads it while it runs
            crashes = [item for item in self._state(current).recent_crashes if item.at > switched]
        except Problem as error:
            logging.getLogger(__name__).warning("NATIVE_CRASH_LOOP_UNREAD code=%s session_id=%s",
                                                error.body["error"]["code"], row.hosted_session_id)
            return False
        # shortcut: a start failure has no CID/RID, so each counts as its own; give them IDs once a writer records them.
        # shortcut: nor a time, so an in-place window also counts ones from an earlier switch of this SID; fine while
        # nothing writes them.
        failures = kept.start_failures if kept is not None else 0
        if min(len({item.conversation_id for item in crashes}),
               len({item.request_id for item in crashes})) + failures < CRASH_LOOP_COUNT:
            return False
        if back is None:  # in place (``_in_place_crash_loop``): one line, as no rollback follows
            logging.getLogger(__name__).warning(
                "NATIVE_CRASH_LOOP_IN_PLACE agent=%s agent_id=%s session_id=%s version=%s crashes=%d start_failures=%d "
                "action=project_veto suggest=selector_rollback", client.target.agent_name, row.agent_id,
                row.hosted_session_id, version, len(crashes), failures)
            return True
        logging.getLogger(__name__).warning(
            "NATIVE_CRASH_LOOP agent_id=%s session_id=%s version=%s crashes=%d start_failures=%d rollback_to=%s",
            row.agent_id, row.hosted_session_id, version, len(crashes), failures, back)
        return True

    def _rollback(self, client, row, stop, version, back):
        outcome, reason, fence = self.cut(client, stop=stop, target=back)
        if outcome == "target_incompatible":  # a version's profile never changes: retrying can never move the Agent
            self._rollback_blocked(row, version, back, f"target_incompatible={reason}")
        return "crash_loop", outcome if reason is None else f"{outcome}:{reason}", fence

    def _recent_conversations(self, agent_id, limit):
        """The Agent's most recent conversations this process kept from owner submits, newest first (shortcut: in
        process memory only, so a restarted API names none and U3 checks no checkpoint; upgrade once a durable
        recent-conversation list exists)."""
        from .behavior_native_turn_api import _kept_conversations

        kept, lock = _kept_conversations(self.composition)
        with lock:
            return tuple([cid for agent, cid in reversed(kept) if agent == agent_id][:limit])

    def _health_wait(self, row, selected):
        """ADR-0009 §5 "間隔加隨機抖動": a target whose last check failed or answered nothing is checked again only at
        a turn end ``HEALTH_RETRY_SECONDS`` plus a random part of it after that attempt (the row's
        ``last_attempt_at``), so replicas and Agents do not retry in step. Never for a rollback, whose U3 counts
        nothing."""
        kept = row.idle_upgrade
        if (_ROLLBACK.get() is not None or kept is None or kept.target_version != selected
                or kept.last_attempt_at is None):
            return None
        now = binding_store(self.composition).clock()
        since = datetime.fromisoformat(kept.last_attempt_at).timestamp()
        if now is not None and now.timestamp() - since < HEALTH_RETRY_SECONDS * (1 + random.random()):
            return "health_wait", None, None
        return None

    def _health(self, destination, row, selected, before_send):
        """ADR-0009 U3 (``FA_NATIVE_IDLE_UPGRADE``): the destination reads back the latest checkpoint of the Agent's
        recent conversations (``NativeSessionCheckpointCheck``, #906) before the probe. None: healthy, the cut goes
        on (no conversation known: nothing to check; an image before #906, its G0b refusal: unchecked, as every cut
        before U3; a rollback cut: logged only). An ``unreadable`` or ``unsupported`` conversation is one failure of
        this attempt; the third failure of the same target is this Agent's veto (``record_veto``, the one veto writer;
        the project row stays crash-loop only). Anything else raised (5xx, a timeout, 403 ``NATIVE_STATE_SCOPE``)
        answered nothing: no failure is counted, only the attempt's time, and a turn end after the retry interval
        checks again. Either way nothing has moved: the Agent keeps serving on its SID. One conversation per call,
        newest first, within ``HEALTH_CHECK_SECONDS`` (fewer are checked when time runs out); a CID that repeats
        NATIVE_STATE_SCOPE for this target is skipped, so it never keeps U3 unanswered. Every conversation is still
        asked: when every one refuses NATIVE_STATE_SCOPE the image's own scope is wrong, one failure of this attempt;
        when none answered otherwise, nothing passes (``health_unread``)."""
        from .behavior_native_turn_api import NativeTurnGateway
        from .behavior_native_turn_wire import NativeSessionCheckpointCheck

        log = logging.getLogger(__name__)
        conversations = self._recent_conversations(row.agent_id, HEALTH_CHECK_CONVERSATIONS)
        sid = destination.target.session_id
        # Coordinator 2026-10-19: a 4d rollback cut only logs U3 (never counted, never a veto): the version it leaves
        # crash-loops, which is worse than one conversation's unreadable checkpoint, and Hosted still checks each
        # conversation's codec when it loads it (CTX-03).
        rollback = _ROLLBACK.get() is not None
        before = row.idle_upgrade
        read = None if before is None or before.target_version != selected else before.last_attempt_at

        def attempt(failed):
            """This attempt on the row (``record_health_failure``): its time, one failure when ``failed``; the count."""
            written = self._binding("health", lambda store: store.record_health_failure(
                row.agent_id, hosted_session_id=row.hosted_session_id, target_version=selected, failed=failed,
                last_attempt_at=read))
            kept = None if written is None else written.idle_upgrade
            return 0 if kept is None or kept.target_version != selected else kept.health_failures

        def unread(code):
            log.warning("NATIVE_UPGRADE_HEALTH_UNREAD code=%s session_id=%s version=%s rollback=%s", code, sid,
                        selected, rollback)
            if rollback:
                return None
            attempt(False)  # no failure, but the retry interval and its jitter apply
            return "health_unread", code, None

        end, failed, checked, refused, unconfirmed = monotonic() + HEALTH_CHECK_SECONDS, [], 0, [], False
        for cid in conversations:
            left = end - monotonic()
            if left < HEALTH_CALL_MIN_SECONDS:
                log.warning("NATIVE_UPGRADE_HEALTH_PARTIAL checked=%d of=%d session_id=%s", checked,
                            len(conversations), sid)
                break
            try:
                before_send()
                value, _ = NativeTurnGateway._invoke(destination, NativeSessionCheckpointCheck(
                    conversation_ids=(cid,)), timeout=left)
                found = value.checkpoints or {}
                if set(found) != {cid}:
                    raise Problem(502, "NATIVE_STATE_INVALID", "the checkpoint check did not answer its conversation")
            except Problem as error:
                code = error.body["error"]["code"]
                if error.status == 422 and error.body["error"].get("reserved") is True:
                    # An image older than #906 does not run the check (its G0b refusal only; any other 422 answered
                    # nothing): unchecked, as every cut before U3.
                    log.warning("NATIVE_UPGRADE_HEALTH_UNCHECKED code=%s session_id=%s version=%s", code, sid,
                                selected)
                    return None
                if code == "NATIVE_STATE_SCOPE":
                    key = (row.agent_id, cid, selected)
                    self._scope_refused[key] = self._scope_refused.get(key, 0) + 1
                    refused.append(cid)
                    if self._scope_refused[key] >= HEALTH_SCOPE_REPEATS:
                        log.warning("NATIVE_UPGRADE_HEALTH_SKIPPED code=%s conversation_id=%s session_id=%s", code,
                                    cid, sid)
                    else:
                        unconfirmed = True  # asked once more before it is skipped
                    continue
                return unread(code)
            checked += 1
            failed += [cid] if found[cid] != "ok" else []
        if refused and len(refused) == len(conversations) >= 2:  # one CID (after an API restart) proves no image-wide scope
            failed = refused  # every recent conversation refused for this target: the image's scope, one failure
            log.warning("NATIVE_UPGRADE_HEALTH_SCOPE_REFUSED session_id=%s version=%s conversations=%d", sid, selected,
                        len(refused))
        elif not failed and (conversations and not checked or unconfirmed):
            return unread("NATIVE_STATE_SCOPE" if refused else "NATIVE_UPGRADE_HEALTH_PARTIAL")
        if not failed:
            return None
        if rollback:
            log.warning("NATIVE_UPGRADE_HEALTH_FAILED agent_id=%s session_id=%s version=%s rollback=True failed=%s",
                        row.agent_id, sid, selected, ",".join(failed))
            return None
        count = attempt(True)
        log.warning("NATIVE_UPGRADE_HEALTH_FAILED agent_id=%s session_id=%s version=%s failures=%d failed=%s",
                    row.agent_id, sid, selected, count, ",".join(failed))
        if count < HEALTH_VETO_COUNT:
            return "health_failed", str(count) if count else "unrecorded", None
        if self._own_veto(row, selected, notice=True) is None:
            return "health_failed", "veto_unrecorded", None  # the next failure writes it again
        log.warning("NATIVE_UPGRADE_HEALTH_VETO agent_id=%s version=%s staying_on=%s", row.agent_id, selected,
                    row.created_agent_version)
        return "vetoed", "health", None

    def _prerestore(self, destination, row, bound):
        """ADR-0009 U9 (``FA_NATIVE_IDLE_UPGRADE``): the destination restores the latest checkpoint of the Agent's 1-2
        most recent conversations into its hot copy (``NativeSessionPrerestore``, #906), so their first turn after the
        switch is not a cold restore. Only a speed-up: a failure logs and the cut goes on; past the cut's ``bound`` (or
        on shutdown) it leaves the cut before the probe, as every other step does."""
        from .behavior_native_turn_api import NativeTurnGateway
        from .behavior_native_turn_wire import NativeSessionPrerestore

        conversations = self._recent_conversations(row.agent_id, PRERESTORE_CONVERSATIONS)
        if not conversations:
            return
        try:
            NativeTurnGateway._invoke(destination, NativeSessionPrerestore(conversation_ids=conversations),
                                      timeout=_left(bound))
        except Problem as error:
            logging.getLogger(__name__).warning("NATIVE_UPGRADE_PRERESTORE_FAILED code=%s session_id=%s",
                                                error.body["error"]["code"], destination.target.session_id)

    def _finish(self, source, destination, *, bound):
        """ADR-0009 U9 (``FA_NATIVE_IDLE_UPGRADE``), once the switch is on the binding row (U8, read back: a failed
        ``record_switch`` only logs): stop the old SID ``source`` (None: already stopped by the files fence) to save
        its compute (U5-U6 found no turn running there; Learn: stop keeps the file system), then ``prewarm`` the new
        SID, each within what is left of U9's own ``FINISH_SECONDS`` from its start (the files fence before it may use
        most of the cut's ``bound``; shutdown still ends it). Neither can undo the commit: any failure (also past that
        bound or on shutdown) logs, the cut keeps its switch outcome, and an old SID left running is reclaimed at its
        idle timeout."""
        from .behavior_native_turn_api import NativeTurnGateway, _prewarm_toolboxes
        from .behavior_native_turn_wire import NativePrewarm

        log = logging.getLogger(__name__)
        sid, own = destination.target.session_id, _CutFence(FINISH_SECONDS, bound.stop)

        def logged(step, call):
            try:
                return call()
            except Exception as error:  # noqa: BLE001 - after the commit nothing here may fail the cut
                code = (error.body["error"]["code"] if isinstance(error, Problem) else
                        "fence_overrun" if isinstance(error, _FenceOverrun) else type(error).__name__)
                log.warning("NATIVE_UPGRADE_FINISH_FAILED step=%s code=%s session_id=%s", step, code, sid,
                            exc_info=True)

        found = self._binding_row(destination.target.agent_id)  # a failed read logs there and returns None
        if found is None or found[1] is None or found[1].hosted_session_id != sid:
            reason = "binding_unread" if found is None else "switch_unrecorded"
            log.warning("NATIVE_UPGRADE_FINISH_SKIPPED reason=%s session_id=%s", reason, sid)
            return
        if source is not None:
            logged("stop", lambda: source.stop_session(timeout=_left(own, 10)))
        logged("prewarm", lambda: NativeTurnGateway._invoke(destination, NativePrewarm(
            toolboxes=_prewarm_toolboxes(self.composition.runtime)), timeout=_left(own)))

    def _record_due(self, row, selected):
        """ADR-0009 U0's write, once every U0 condition held: the deferral start, once per target (a Table failure
        only logs)."""
        kept = row.idle_upgrade
        if kept is None or kept.target_version != selected:
            self._binding("idle_upgrade", lambda store: store.record_due(
                row.agent_id, hosted_session_id=row.hosted_session_id, target_version=selected))

    def _fenced(self, client, destination, command, row, selected, stop):
        """The files mode of ``cut``: the fence, then today's follow steps (copy, stop the source, commit)."""
        from .behavior_native_turn_api import NativeTurnGateway
        from .native_binding import NativeBindingUpgrade

        agent_id, sid = client.target.agent_id, destination.target.session_id
        own = []

        def recording(store):
            own.append(NativeBindingUpgrade(hosted_session_id=sid, agent_version=destination.target.agent_version,
                                            request_id=command.request_id, started_at=store._now(),
                                            rollback=_ROLLBACK.get() == destination.target.agent_version))
            return store.record_upgrade(agent_id, hosted_session_id=row.hosted_session_id, upgrade=own[-1])

        written = self._binding("upgrade", recording)
        upgrade = None if written is None else written.upgrade
        if upgrade is None or (upgrade.hosted_session_id, upgrade.request_id) != (sid, command.request_id):
            return "busy", "upgrade_unrecorded", None
        if upgrade != own[-1]:
            # Another replica's job holds this same fence: only that one drives it. shortcut: two jobs that build their
            # record in the same microsecond both drive; add a nonce to NativeBindingUpgrade if that is ever seen.
            return "fence_live", None, None

        def mark():
            marked = self._binding("upgrade", lambda store: store.mark_stop(
                agent_id, hosted_session_id=row.hosted_session_id, upgrade=upgrade))
            fence_ = None if marked is None else marked.upgrade
            return fence_ is not None and fence_.stop_started and (
                fence_.hosted_session_id, fence_.request_id) == (sid, command.request_id)

        fence = _CutFence(CUT_FENCE_SECONDS, stop or Event(), mark)
        value, _ = NativeTurnGateway._invoke(destination, command)
        if value.state != "pending":
            # Another replica's same fence (same RID) may hold the pending: its record is cleared only once it is gone.
            if self._settle(client, destination, self._state(destination), written, stop=stop) is None:
                self._binding("upgrade", lambda store: store.record_upgrade(
                    agent_id, hosted_session_id=row.hosted_session_id, upgrade=None))
            return "busy", value.reason, None
        follow = NativeEnvironmentCommand(request_id=command.request_id, expected_session_id=row.hosted_session_id,
                                          expected_agent_version=selected, idle_only=True)
        try:
            if os.environ.get(CUT_FAULT_ENV) == "after_pending":
                fence.stop.wait(max(0.0, fence.deadline - monotonic()))
                raise _FenceOverrun()
            outcome, reason = self._steps(client, follow, fence, destination)
            return outcome, reason, monotonic() - fence.started
        except (_FenceOverrun, Problem) as error:
            reason = "fence_overrun" if isinstance(error, _FenceOverrun) else error.body["error"]["code"]
        # Read again: another replica running the same fence may have marked its stop meanwhile.
        found = self._binding_row(agent_id)
        latest = written if found is None or found[1] is None else found[1]
        settled = self._settle(client, destination, self._state(destination), latest, own=True,
                               marked=fence.stopped, stop=stop)
        if settled is None:
            # The fence is already gone (withdrawn by another path): only the row's record of it is left.
            self._binding("upgrade", lambda store: store.record_upgrade(
                agent_id, hosted_session_id=row.hosted_session_id, upgrade=None))
        return settled or "withdrawn", reason, monotonic() - fence.started

    def _steps(self, client, follow, fence, destination, *, once=False):
        """Today's follow steps under the fence until the commit: (outcome, reason). Before FA's stop of the source is
        marked the fence raises ``_FenceOverrun`` past its bound or on shutdown (the caller withdraws it). After it the
        steps go to the fence's own ``destination`` whatever the selector names now (the Agent's sends meet the
        pending, so no turn end would come to resume it); past the bound they warn once and, ``CUT_DRIVE_SECONDS``
        later or on shutdown, leave the pending in place for the next trigger, never withdrawn."""
        overdue = False
        while True:
            fence()
            if fence.stopped:
                if fence.stop.is_set():
                    return "stopping", "shutdown"
                late = monotonic() - fence.deadline
                if late > 0 and not overdue:
                    overdue = True
                    logging.getLogger(__name__).warning(
                        "NATIVE_UPGRADE_CUT_OVERDUE session_id=%s request_id=%s late_seconds=%d",
                        destination.target.session_id, follow.request_id, late)
                if late > CUT_DRIVE_SECONDS:
                    return "stopping", "drive_capped"
            value = self.follow(client, follow, before_send=fence, destination=destination if fence.stopped else None)
            if value.state == "ready":
                return "ready", None
            if value.reason not in _FENCE_STEPS:
                raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the files fence stopped at " + str(value.reason))
            if once:
                return "stopping", value.reason  # a send's pass; its resend drives the next
            if value.reason == "source_active":
                fence.stop.wait(1)  # the source's own stop is still settling

    @staticmethod
    def _fence_reader(client, fence):
        """The session a fence (``NativeBindingUpgrade``) or a ``pending`` view names as its target."""
        version = fence.agent_version if isinstance(fence, NativeBindingUpgrade) else fence.hosted_agent_version
        return client.for_target(_target(client.target, session_id=fence.hosted_session_id, agent_version=version))

    def _converge(self, client, row, *, stop=None, once=False):
        """The entry for a fence the binding row records (``upgrade``), called by ``cut`` and by every send's
        ``advance`` before anything else decides: ``_settle`` (the one fence policy; ``_fenced`` enters it for the
        job's own fence) at the fence's own destination. (outcome, row); outcome None when the fence is gone, and then
        the row's record of it is cleared. ``once``: a send drives one pass."""
        reader = self._fence_reader(client, row.upgrade)
        try:
            state = self._state(reader)
        except Problem as error:
            if not self.guard.enforce or error.status in {401, 403}:
                raise
            return self._fence_broken(client, row, error, reader), row
        self._stop_durable(row, "fence_broken")
        settled = self._settle(client, reader, state, row, stop=stop, once=once)
        if settled is not None:
            return settled, row
        # The fence is gone (withdrawn or never written): only the row's record of it is left.
        return None, self._binding("upgrade", lambda store: store.record_upgrade(
            row.agent_id, hosted_session_id=row.hosted_session_id, upgrade=None)) or row

    def clear_fence(self, client):
        """Operator path for a stuck files fence (ADR-0009 2d-i P9 gate; the runbook step is in the Shared Context
        README): the one fence policy (``_settle``), with the fence-age guard lifted (``own``). A fence without
        FA's recorded stop is withdrawn (the old SID admits again, its files untouched); one with it is driven, up to
        ``CLEAR_DRIVE_PASSES`` passes ``CLEAR_PASS_SECONDS`` apart while it stays ``stopping``; a marked fence still
        left after them (source metadata unsettled or unreadable, files that will not move) is committed without its
        files by the §7a abandon path, with the honest notice. A broken destination: ``_fence_broken`` (an unmarked
        fence withdrawn by a fresh session; a marked one left without files only on a definite failure). A record
        whose ``pending`` is gone is cleared. Returns the outcome; ``no_fence`` when the row records none."""
        found = self._binding_row(client.target.agent_id)
        row = None if found is None else found[1]
        if row is None or row.upgrade is None:
            return "no_fence"
        fence = row.upgrade
        reader = self._fence_reader(client, fence)
        with self._changing():
            try:
                state = self._state(reader)
            except Problem as error:
                if error.status in {401, 403}:
                    raise
                return self._fence_broken(client, row, error, reader, force=True)
            settled = self._settle(client, reader, state, row, own=True)
            for _ in range(CLEAR_DRIVE_PASSES - 1):
                if settled != "stopping":
                    break
                _clear_sleep(CLEAR_PASS_SECONDS)
                found = self._binding_row(row.agent_id)
                if found is None or found[1] is None or found[1].upgrade != fence:
                    break  # committed, or another path settled it: the checks below read what is left
                row = found[1]
                settled = self._settle(client, reader, self._state(reader), row, own=True)
            if settled is None:
                self._binding("upgrade", lambda store: store.record_upgrade(
                    row.agent_id, hosted_session_id=row.hosted_session_id, upgrade=None))
                return "cleared"
            pending = self._state(reader).environment
            if settled != "stopping" or pending is None or pending.state != "pending" or (
                    pending.hosted_session_id, pending.request_id) != (fence.hosted_session_id, fence.request_id):
                return settled
            source = client.for_target(_target(client.target, session_id=pending.previous_hosted_session_id,
                                               agent_version=pending.previous_agent_version))
            try:
                metadata = _session_metadata(source)
                status = "missing" if metadata is None else metadata.status
            except Problem:
                status = "active"
            if status in _RETIRED:
                return settled  # the drive's own §7a path switches a retired source (its retired pair)
            marker = _CutFence(CUT_FENCE_SECONDS, Event())
            marker.stopped = True
            follow = NativeEnvironmentCommand(
                request_id=pending.request_id, expected_session_id=pending.previous_hosted_session_id,
                expected_agent_version=pending.hosted_agent_version, idle_only=True)
            return self._without_moved_files(
                client, reader, pending, marker, follow, "files_overdue",
                status if status in {"idle", "active"} else "deleted" if status == "deleting" else "active",
                "NATIVE_UPGRADE_OPERATOR_CLEAR")

    def _fence_broken(self, client, row, error, reader, *, force=False):
        """P9 gate, limit ④ (flag on): the fence's destination session (``reader``) does not answer, so every send
        would meet it; the send waits (``fence_broken``). Only a definite failure (``_definitely_broken``) starts or
        keeps the durable clock; a 5xx, timeout or throttle (a platform outage) clears it and is waited out. Past
        ``FENCE_BROKEN_SECONDS`` of definite failures, or at once for the operator (``force``), the fence is left: one
        without FA's recorded stop is withdrawn by the selected version's fresh H2d session (``_withdraw_broken``: the
        old SID admits again with its files); only a marked one (``stop_started``, or Hosted refusing that withdraw)
        on a definite failure takes the §7a abandon path to a fresh session of the selected version (never the broken
        one): no file moves, the ④ notice (or "舊環境已失效" when the source is gone), and that commit replaces the
        fence's ``pending`` (Hosted's abandon CAS)."""
        fence = row.upgrade
        definite = self._definitely_broken(reader, error)
        waited = None
        if definite:
            waited = self._durable(row.agent_id, row.hosted_session_id, "fence_broken", fence.request_id)
        else:
            self._stop_durable(row, "fence_broken")
        logging.getLogger(__name__).warning(
            "NATIVE_UPGRADE_CUT_FENCE_BROKEN agent_id=%s session_id=%s request_id=%s code=%s definite=%s "
            "waited_seconds=%s", row.agent_id, fence.hosted_session_id, fence.request_id, error.body["error"]["code"],
            definite, None if waited is None else int(waited))
        if not force and (not definite or waited is None or waited < FENCE_BROKEN_SECONDS):
            return "fence_broken"
        if not fence.stop_started:
            withdrawn = self._withdraw_broken(client, row, fence)
            if withdrawn != "refused":
                return withdrawn
        if not definite:
            return "fence_broken"  # a marked fence's files may still move once the outage ends
        source = client.for_target(_target(client.target, session_id=row.hosted_session_id,
                                           agent_version=row.observed.agent_version))
        try:
            metadata = _session_metadata(source)
            status = "missing" if metadata is None else metadata.status
        except Problem:
            status = "active"  # unread: not known gone
        if status not in {"idle", "active", "failed", "deleted", "expired", "missing"}:
            status = "deleted" if status == "deleting" else "active"
        try:
            store = binding_store(self.composition)
            store.decide_abandon(row.agent_id, hosted_session_id=row.hosted_session_id, basis="files_overdue",
                                 source_status=status, created_agent_version=row.created_agent_version,
                                 state_profile=row.state_profile)
            selected = self.selected_version(client)
            decided = store.read(row.agent_id)
            going = None if decided is None else _unvetoed(decided, selected)  # where ``_carry_out`` goes
            if going is not None and self.destination_session(client, going, decided)[0] == fence.hosted_session_id:
                store.raise_attempt(row.agent_id, hosted_session_id=row.hosted_session_id, agent_version=going,
                                    attempt=decided.attempt(going))
            switched = self._carry_out(client, selected, lambda: None)
        except Problem as failed:
            logging.getLogger(__name__).warning("NATIVE_UPGRADE_CUT_FENCE_BROKEN_UNSWITCHED code=%s",
                                                failed.body["error"]["code"])
            switched = None
        self.forget()
        return "fence_broken" if switched is None else "abandoned"

    @staticmethod
    def _definitely_broken(reader, error):
        """Limit ④ counts only a definite failure of the fence's destination: a 4xx refusal (not a timeout or
        throttle), an invoke that reads 404, or platform metadata reading the session retired. Anything else (a 5xx
        outage, an unreadable metadata) may clear by itself."""
        detail = error.body["error"]
        if 400 <= error.status < 500 and error.status not in {408, 429} or detail.get("upstream_status") == 404:
            return True
        try:
            metadata = _session_metadata(reader)
        except Problem:
            return False
        return metadata is None or metadata.status in _RETIRED

    def _withdraw_broken(self, client, row, fence):
        """An unmarked fence whose destination does not answer is withdrawn by the selected version's H2d session,
        as an older follow's ``pending`` is (``_settle``; Hosted refuses the withdraw once the files progress records
        FA's stop), never by the broken SID: when the selector still names it, the M5 attempt is raised first (the
        next cut goes to that fresh SID too). "withdrawn" (the old SID is current again, its files untouched; also
        when the ``pending`` was already gone), "refused" (FA's stop is recorded: the caller's marked path), or
        "fence_broken"/"conflict" (left for the next read)."""
        try:
            selected = self.selected_version(client)
            found = self._binding_row(row.agent_id)
            latest = None if found is None else found[1]
            if latest is None or latest.hosted_session_id != row.hosted_session_id or latest.upgrade != fence:
                return "fence_broken"  # the row moved meanwhile: the next read settles what it records
            if self.destination_session(client, selected, latest)[0] == fence.hosted_session_id:
                latest = self._binding("fresh", lambda store: store.raise_attempt(
                    row.agent_id, hosted_session_id=row.hosted_session_id, agent_version=selected,
                    attempt=latest.attempt(selected))) or latest
            executor = self._destination(client, selected, lambda: None, mark=False, row=latest)
            if executor.target.session_id == fence.hosted_session_id:
                return "fence_broken"
            settled = self._settle(client, executor, self._state(executor), latest, own=True, broken=True)
        except Problem as failed:
            if failed.status in {401, 403}:
                raise
            logging.getLogger(__name__).warning("NATIVE_UPGRADE_CUT_FENCE_BROKEN_UNWITHDRAWN code=%s",
                                                failed.body["error"]["code"])
            return "fence_broken"
        if settled is None:
            # Hosted holds no pending any more: only the row's record of the fence is left.
            self._binding("upgrade", lambda store: store.record_upgrade(
                row.agent_id, hosted_session_id=row.hosted_session_id, upgrade=None))
        if settled in {None, "withdrawn"}:
            logging.getLogger(__name__).warning(
                "NATIVE_UPGRADE_CUT_FENCE_BROKEN_WITHDRAWN agent_id=%s session_id=%s request_id=%s",
                row.agent_id, fence.hosted_session_id, fence.request_id)
            return "withdrawn"
        return settled if settled in {"refused", "conflict"} else "conflict" if settled == "repaired" else "fence_broken"

    def _settle(self, client, destination, state, row, *, own=False, marked=False, stop=None, once=False,
                broken=False):
        """The one fence policy: before a cut or a send's ``advance`` (``_converge``), and after a fence that did not
        commit (``own``: the caller's fence; ``destination``: the
        fence's own session). A ``pending`` that is the row's recorded fence (``upgrade``) is driven to its commit
        (``_drive``) once FA's stop of the source is marked (``stop_started``, or ``marked`` by the caller) or recorded
        in its files progress (ADR-0009 "例外才接手": never withdrawn then); without either this H2d destination
        withdraws it when it is the caller's own or older than ``CUT_FENCE_SECONDS`` (a fence whose job ended), after
        reading the row once more; Hosted itself refuses to withdraw a fence whose files progress records FA's stop,
        and a refused withdraw is driven forward too, so no replica's stale read withdraws after a stop. Any other
        ``pending`` belongs to the follow that wrote it. A commit the row missed is recorded (§7a (j)). A marked fence
        whose ``pending`` is gone while the source is still current never committed ("fence_lost"): an anomaly, since
        Hosted refuses that withdraw; only here is its record cleared, with no other change. None: nothing to settle."""
        from .behavior_native_turn_api import NativeTurnGateway
        from .behavior_native_turn_wire import NativeSessionWithdraw

        pending = state.environment
        if state.hosted_session_id == row.hosted_session_id and (pending is None or pending.state != "pending"):
            fence = row.upgrade
            if fence is None or not (marked or fence.stop_started):
                return None
            logging.getLogger(__name__).warning(
                "NATIVE_UPGRADE_CUT_FENCE_LOST session_id=%s request_id=%s source_session_id=%s",
                fence.hosted_session_id, fence.request_id, row.hosted_session_id)
            self._binding("upgrade", lambda store: store.record_upgrade(
                row.agent_id, hosted_session_id=row.hosted_session_id, upgrade=None, lost=fence))
            return "fence_lost"
        if state.hosted_session_id == row.hosted_session_id:
            fence = row.upgrade
            # 2d-i P6: with every follow entry off, a pending no fence recorded is an older follow's that nothing
            # moves on any more (ADR M1 "舊式 follow 留下的 pending"): withdrawn by default, driven to its commit at
            # its own target once its files progress records FA's stop (Hosted refuses that withdraw too).
            legacy = fence is None and self.guard.enforce
            if not legacy and (fence is None or (fence.hosted_session_id, fence.request_id) != (
                    pending.hosted_session_id, pending.request_id)):
                return "pending_other"
            try:
                stopped = (self._stop_started(destination, pending)
                           if legacy or not (marked or fence.stop_started or broken) else False)
            except Problem as error:
                if error.status != 409 or error.body["error"]["code"] != "NATIVE_ENVIRONMENT_CONFLICT":
                    raise
                # The files progress names no pending: another job withdrew or committed it after ``state`` was read
                # (dev 2026-10-18, a send's pass against the job's withdraw at its bound). Settled again from a fresh
                # read; the same pending still there is a conflict of its own.
                fresh = self._state(destination)
                left = fresh.environment
                if left is not None and left.state == "pending" and (left.hosted_session_id, left.request_id) == (
                        pending.hosted_session_id, pending.request_id):
                    raise
                logging.getLogger(__name__).warning(
                    "NATIVE_UPGRADE_CUT_FENCE_SETTLED_MEANWHILE session_id=%s request_id=%s",
                    pending.hosted_session_id, pending.request_id)
                return self._settle(client, destination, fresh, row, own=own, marked=marked, stop=stop, once=once,
                                    broken=broken)
            if legacy:
                if stopped:
                    logging.getLogger(__name__).warning(
                        "NATIVE_UPGRADE_LEGACY_PENDING_DRIVEN session_id=%s", pending.hosted_session_id)
                    return self._drive(client, self._fence_reader(client, pending), pending, stop, once=once)
            elif marked or fence.stop_started or stopped:
                # ``broken``: that progress is read through the fence's own SID, which does not answer; Hosted's
                # withdraw CAS below reads the same progress.
                return "refused" if broken else self._drive(client, destination, pending, stop, once=once)
            if not own and not legacy and (binding_store(self.composition).clock() - datetime.fromisoformat(
                    fence.started_at)).total_seconds() < CUT_FENCE_SECONDS:
                return "fence_live"
            # Another replica may have marked its stop since ``row`` was read: read the row right before the withdraw.
            found = self._binding_row(row.agent_id)
            latest = None if found is None or found[1] is None else found[1].upgrade
            if latest is not None and latest.stop_started and (latest.hosted_session_id, latest.request_id) == (
                    pending.hosted_session_id, pending.request_id):
                return "refused" if broken else self._drive(client, destination, pending, stop, once=once)
            if latest != fence or legacy and found is None:
                return "fence_live"  # unreadable, or another job's record of it: that job settles it
            state, _ = NativeTurnGateway._invoke(destination, NativeSessionWithdraw(
                request_id=material_digest({"kind": "fa-native-withdraw-v1", "agent": row.agent_id,
                                            "pending": pending.request_id, "target": pending.hosted_session_id}),
                expected_etag=state.etag, pending_request_id=pending.request_id,
                pending_session_id=pending.hosted_session_id))
            self.forget()
            left = state.environment
            if left is not None and left.state == "pending" and (left.hosted_session_id, left.request_id) == (
                    pending.hosted_session_id, pending.request_id):
                # Hosted refused: the fence's files progress records FA's stop of the source (any replica's).
                if broken:
                    return "refused"
                return self._drive(client, self._fence_reader(client, pending) if legacy else destination, pending,
                                   stop, once=once)
            if state.hosted_session_id == row.hosted_session_id:
                if legacy:
                    logging.getLogger(__name__).warning("NATIVE_UPGRADE_LEGACY_PENDING_WITHDRAWN session_id=%s",
                                                        pending.hosted_session_id)
                else:
                    self._binding("upgrade", lambda store: store.record_upgrade(
                        row.agent_id, hosted_session_id=row.hosted_session_id, upgrade=None))
                return "withdrawn"
        repaired = self._repair(destination, state, row)
        self.forget()
        return "conflict" if repaired is None else "repaired"

    def _drive(self, client, destination, pending, stop, *, once=False):
        """A fence after FA's stop of the source: driven to its commit at its own destination and version (the
        ``pending``'s), never withdrawn. By the source's platform status: ``active`` goes on with today's follow (which
        stops it again); ``idle`` (or awake on another version) first records the stop if that record was lost (FA
        marked its own stop); then the files are published from the verified staging, never read from the stopped
        source again (ADR-0009 §10 check ②); a retired source
        (failed, deleted, expired, 404) has no files left and is switched by the §7a abandon path (retired pair, the
        "舊環境已失效" notice); anything else waits. A ``_WOKEN`` code is a wake only when the source then reads
        ``active``; otherwise ``_drive_failed`` bounds it. "ready", "abandoned", or "stopping": left in place for the next
        trigger (a failure is logged, never raised). A failure that can never clear (``_drive_failed``) commits the
        fence without its files instead (``_without_moved_files``), so no Agent waits on it forever."""
        from .behavior_session_files import FollowFiles

        fence = _CutFence(CUT_FENCE_SECONDS, stop or Event())
        fence.stopped = True
        follow = NativeEnvironmentCommand(
            request_id=pending.request_id, expected_session_id=pending.previous_hosted_session_id,
            expected_agent_version=pending.hosted_agent_version, idle_only=True)
        status = progress = None
        reader = destination.for_target(_target(destination.target, session_id=pending.previous_hosted_session_id,
                                                agent_version=pending.previous_agent_version))
        try:
            source = _session_metadata(reader)
            status = "missing" if source is None else source.status
            if status in _RETIRED:
                switched = self._abandon_source(client, follow, destination, self._state(destination), fence,
                                                decide=True, found=self._binding_row(client.target.agent_id))
                outcome = "stopping" if switched is None else "abandoned"
            elif status not in {"active", "idle"}:
                outcome = "stopping"
                if self.guard.enforce:
                    # P9 gate, limit ③: creating/updating/deleting waits under the same durable transient bound.
                    raise Problem(503, "NATIVE_ENVIRONMENT_SOURCE_UNSETTLED", "the fence's source is " + str(status))
            else:
                woken = (pending.request_id, source.version_indicator.agent_version)
                if woken[1] != pending.previous_agent_version and woken not in self._woken_cleared:
                    # On another version: woken after FA's stop (a drive only runs past it, so the staging is
                    # verified). Cleared before any destination call: errors before the wake do not count toward the
                    # bound; once per process and wake, so a source that stays there still reaches it.
                    if not self._clear_drive_clocks(pending):
                        return "stopping"  # unwritten: no clock may be read; the next pass clears again first
                    self._woken_cleared.add(woken)
                files = FollowFiles(destination, pending, fence)
                progress = files.step("read").progress
                # Idle, or awake on another version (only a wake does that): FA's marked stop, its record lost.
                if (progress is not None and progress.source_verified and not progress.stop_started and (
                        status == "idle" or source.version_indicator.agent_version != pending.previous_agent_version)):
                    files.record_stop()
                outcome = self._steps(client, follow, fence, destination, once=once)[0]
        except Problem as error:
            if error.body["error"]["code"] in _WOKEN and progress is not None and progress.source_verified:
                # The code alone does not say who: the destination's own (a stuck `admitting`, an OSError) is bounded.
                try:
                    again = _session_metadata(reader)
                    again = "missing" if again is None else again.status
                except Problem as unread:
                    again, error = None, unread  # unread: a transient error under the bound
                if again == "active":
                    # FA's stop is marked and the staging verified: the source was woken (ADR-0009 §10 check ②).
                    # Never without files and no clock: the next pass stops it again and publishes from the staging.
                    logging.getLogger(__name__).warning(
                        "NATIVE_UPGRADE_CUT_SOURCE_WOKEN code=%s session_id=%s source_session_id=%s",
                        error.body["error"]["code"], pending.hosted_session_id, pending.previous_hosted_session_id)
                    return "stopping"
                if again not in {None, "idle"}:
                    return "stopping"  # retired or unsettled: the next pass's metadata read takes that exit
            basis = self._drive_failed(error, pending)
            if basis is None:
                return "stopping"
            if status not in {"active", "idle"}:
                if not self.guard.enforce:
                    return "stopping"
                # Limit ③ past its bound: the same honest no-files commit; a source that is not (yet) gone keeps the
                # ④ notice, one being deleted the "舊環境已失效" one.
                logging.getLogger(__name__).warning(
                    "NATIVE_UPGRADE_CUT_SOURCE_UNSETTLED_OVERDUE agent_id=%s session_id=%s request_id=%s status=%s",
                    pending.agent_id, pending.hosted_session_id, pending.request_id, status)
                status = "deleted" if status == "deleting" else "active"
            return self._without_moved_files(client, destination, pending, fence, follow, basis, status,
                                             error.body["error"]["code"])
        if outcome != "stopping":
            # Only an end clears the clocks: a pass that waits (creating, updating, a capped drive) is no progress.
            self._drive_failing.pop(pending.request_id, None)
        return outcome

    def _drive_failed(self, error, pending):
        """Classifies a drive error. None: wait for the next trigger (a turn end or a send). Otherwise the abandon
        basis: ``files_unmovable`` once an ``_UNMOVABLE`` error is read again at least ``RETIRED_CONFIRM_SECONDS``
        after the first (any transient error between restarts the pair); ``files_overdue`` once transient errors have
        lasted ``CUT_TRANSIENT_SECONDS`` (that long a wait is its own confirmation; a durable clock found past twice
        that starts again). Only a drive that ends the fence
        (``ready`` or ``abandoned``), or the first pass that sees the source woken on another version, clears both
        clocks; a ``stopping`` pass without an error keeps them."""
        code, now, log = error.body["error"]["code"], monotonic(), logging.getLogger(__name__)
        agent, sid, rid = pending.agent_id, pending.previous_hosted_session_id, pending.request_id
        # Durable with the flag (the row still names the source until the commit, which rebuilds it without clocks).
        # Past twice the bound the clock saw no failing pass after the bound (one takes the no-files path): a gap with
        # no attempts (a freeze, a deploy), not 30 minutes of failing, so it starts again.
        failed = self._durable(agent, sid, "drive", rid, restart_after=2 * CUT_TRANSIENT_SECONDS)
        if failed is None:
            failing = self._drive_failing.setdefault(rid, [now, None])
            failed = now - failing[0]
        # A 503 woken code is the destination's OSError (staging, workspace): it may clear, so the 30-minute bound.
        if code not in _UNMOVABLE or (code in _WOKEN and error.status == 503):
            # A transient error between two unmovable reads restarts their pair.
            if self.guard.enforce:
                self._binding("clock", lambda store: store.record_clock(
                    agent, hosted_session_id=sid, kind="unmovable", subject=None))
            if rid in self._drive_failing:
                self._drive_failing[rid][1] = None
            if failed <= CUT_TRANSIENT_SECONDS:
                log.warning("NATIVE_UPGRADE_CUT_DRIVE_PENDING code=%s session_id=%s agent_id=%s waited_seconds=%d", code,
                            pending.hosted_session_id, agent, failed)
                return None
            return "files_overdue"
        unmovable = self._durable(agent, sid, "unmovable", rid)
        if unmovable is None:
            failing = self._drive_failing.setdefault(rid, [now, None])
            if failing[1] is None:
                failing[1] = now
            unmovable = now - failing[1]
        if unmovable < RETIRED_CONFIRM_SECONDS:
            log.warning("NATIVE_UPGRADE_CUT_FILES_UNMOVABLE_UNCONFIRMED code=%s session_id=%s", code,
                        pending.hosted_session_id)
            return None
        return "files_unmovable"

    def _clear_drive_clocks(self, pending):
        """Clear the fence's ``drive`` and ``unmovable`` clocks (durable and in this process), as a commit does. False
        when a Table write failed (``_binding`` swallows it), so the caller does not count the clear as done."""
        self._drive_failing.pop(pending.request_id, None)
        if self.guard.enforce:
            for kind in ("drive", "unmovable"):
                if self._binding("clock", lambda store, kind=kind: store.record_clock(
                        pending.agent_id, hosted_session_id=pending.previous_hosted_session_id, kind=kind,
                        subject=None) or True) is None:
                    return False
        return True

    def _without_moved_files(self, client, destination, pending, fence, follow, basis, status, code):
        """The fence's files can not be moved: it commits at its own destination without them, by the §7a abandon path
        (decision written first, so any caller finishes it), with the ④ "without files" notice since the source still
        exists (``status`` idle or active; ``deleted`` for one being deleted past limit ③, "舊環境已失效"). The binding
        row records ``files_migrated=False`` and ``basis``."""
        agent_id = client.target.agent_id
        found = self._binding_row(agent_id)
        row = None if found is None else found[1]
        if row is None or row.hosted_session_id != pending.previous_hosted_session_id:
            return "stopping"
        logging.getLogger(__name__).warning(
            "NATIVE_UPGRADE_CUT_FILES_UNMOVABLE code=%s basis=%s session_id=%s source_session_id=%s source_status=%s",
            code, basis, pending.hosted_session_id, row.hosted_session_id, status)
        self._binding("abandon", lambda store: store.decide_abandon(
            agent_id, hosted_session_id=row.hosted_session_id, basis=basis, source_status=status,
            created_agent_version=row.created_agent_version, state_profile=row.state_profile))
        try:
            switched = self._abandon_source(client, follow, destination, self._state(destination), fence,
                                            decide=False, found=self._binding_row(agent_id))
        except Problem as error:
            logging.getLogger(__name__).warning("NATIVE_UPGRADE_CUT_DRIVE_PENDING code=%s session_id=%s",
                                                error.body["error"]["code"], pending.hosted_session_id)
            return "stopping"
        if switched is None:
            return "stopping"
        self._drive_failing.pop(pending.request_id, None)
        return "abandoned"

    @staticmethod
    def _stop_started(destination, pending):
        """The fence's files progress records FA's own stop of the source. Read through the session the ``pending``
        names, which may not be today's destination once the selector moved."""
        from .behavior_session_files import FollowFiles

        reader = destination.for_target(_target(destination.target, session_id=pending.hosted_session_id,
                                                agent_version=pending.hosted_agent_version))
        files = FollowFiles(reader, pending, lambda: None).step("read").progress
        return files is not None and files.stop_started

    def _durable(self, agent_id, sid, kind, subject, *, restart_after=None):
        """ADR-0009 2d-i P6 (``FA_NATIVE_SESSION_GUARD_ENFORCE`` only): seconds since the binding row's durable
        ``kind`` clock started for ``subject``, started now when it runs for another subject or none (or, with
        ``restart_after``, ran longer than that). So a bound holds across replicas and restarts. None without the flag,
        or when the row (an unclaimed Agent, another SID, an unreadable Table) can not keep it: the caller then keeps
        this process's memory, as before."""
        if not self.guard.enforce:
            return None
        try:
            store = binding_store(self.composition)
            for restart in (False, True):
                now = _wall(store)
                row = store.record_clock(agent_id, hosted_session_id=sid, kind=kind, subject=subject,
                                         since=now.astimezone(timezone.utc).isoformat(), restart=restart)
                kept = None if row is None else row.clock(kind)
                if kept is None or kept.subject != subject:
                    return None
                waited = (now - datetime.fromisoformat(kept.since)).total_seconds()
                if restart_after is None or waited <= restart_after:
                    return waited
        except Exception as error:  # noqa: BLE001 - an unrecorded clock falls back to memory; see the docstring
            code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
            logging.getLogger(__name__).warning("NATIVE_BINDING_CLOCK_UNRECORDED kind=%s code=%s", kind, code)
        return None

    def _stop_durable(self, row, kind):
        """Clear the row's ``kind`` clock; reads nothing when ``row`` (as read by the caller) has none."""
        if self.guard.enforce and row is not None and row.clock(kind) is not None:
            self._binding("clock", lambda store: store.record_clock(
                row.agent_id, hosted_session_id=row.hosted_session_id, kind=kind, subject=None))

    def _admitting_for(self, agent_id, row, request_id):
        """Seconds the ``admitting`` RID has been seen stuck: durable with the flag, else in this process."""
        waited = self._durable(agent_id, row.hosted_session_id, "admitting", request_id)
        if waited is not None:
            return waited
        seen = self._admitting_seen.get(agent_id)
        if seen is None or seen[0] != request_id:
            self._admitting_seen[agent_id] = seen = (request_id, monotonic())
        return monotonic() - seen[1]

    def _correct_profile(self, client, row, *, send=False):
        """The row's profile is what the version its SID runs (V_rec) declares: this replaces the repair fallback's v1
        (``profile_unread``) and the stale profile of a row that drifted before the drift CAS recorded it (its V_rec
        already equals what the VM runs, so no 409 comes to record it). ``HostedVersions.profile`` keeps each
        version's profile, so this is one ``get_version`` per version and process; no session is called. Unreadable
        or undeclared: the row is kept; an unreadable one is read and logged again next time, an undeclared one
        (kept by ``HostedVersions``: a definition never changes) is logged once per Agent, version and process.
        ``send`` (the locate before a send, 2d-ii): unreadable, or a correction not written, is a 503 the client
        resends with the same RID, never the incompatible path (``guard.refused`` would keep the SID for good). It is
        the version-follow 503 without ``not_dispatched``: the submit answers it as sent nowhere and resends it in
        the request, then ``server_following`` (the workbench's same-RID resend), and never starts a switch for it."""
        unhealed = Problem(503, "AGENT_VERSION_FOLLOWING", "this Agent's session version could not be confirmed yet")
        from .behavior_agent_versions import _resolver
        resolver = _resolver(self.composition.runtime)
        if resolver is None:
            return row  # an offline harness: ``_row_profile``'s send-profile stand-in is no definition read
        version = row.observed.agent_version
        try:
            profile = resolver.profile(client.target, version)
        except Exception as error:  # noqa: BLE001 - still unreadable: the next attempt reads again
            code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
            logging.getLogger(__name__).warning("NATIVE_BINDING_PROFILE_UNREAD code=%s version=%s", code, version)
            if send:
                raise unhealed from None
            return row
        if profile is None:
            if (row.agent_id, version) not in self._profile_undeclared:
                self._profile_undeclared.add((row.agent_id, version))
                logging.getLogger(__name__).warning(
                    "NATIVE_BINDING_PROFILE_UNREAD code=NATIVE_TURNS_DISABLED version=%s", version)
            return row
        if not row.profile_unread and _storable(profile) == row.state_profile:
            return row  # already right (as stored: a bad pattern is kept as incompatible): no Table call
        corrected = self._binding("profile", lambda store: store.record_profile(
            row.agent_id, hosted_session_id=row.hosted_session_id, agent_version=version, state_profile=profile))
        if corrected is None and send:
            raise unhealed
        return corrected or row

    def admission_stuck(self, client, *, before_send):
        """ADR-0009 "共用 store 一直不安靜", request-triggered (2d-i P6, flag on): a send that another RID's
        ``admitting`` keeps out (503 ``NATIVE_ADMISSION_PENDING``) starts or continues the durable bound, which the
        background cut shares; past ``STUCK_ADMISSION_SECONDS`` this request takes the §7a abandon path. True when it
        switched; never raises (the send's own answer stands)."""
        try:
            found = self._binding_row(client.target.agent_id)
            row = None if found is None else found[1]
            if not self.guard.enforce or row is None or row.upgrade is not None or row.abandon is not None:
                return False
            state = self._state(self._bound_client(client, row))
            rid = state.admission_request_id
            if rid is None or state.hosted_session_id != row.hosted_session_id:
                return False
            if self._admitting_for(client.target.agent_id, row, rid) < STUCK_ADMISSION_SECONDS:
                return False
            metadata = _session_metadata(self._bound_client(client, row))
            status = "missing" if metadata is None else metadata.status
            if status not in {"active", "idle"}:
                return False
            return self._abandon_stuck(client, row, status, self.selected_version(client), before_send) == "abandoned"
        except Problem as error:
            logging.getLogger(__name__).warning("NATIVE_ADMISSION_STUCK_UNCONFIRMED code=%s", error.body["error"]["code"])
            return False

    def _abandon_stuck(self, client, row, status, selected, before_send=lambda: None):
        """ADR-0009 "共用 store 一直不安靜": the §7a abandon path (no invoke, read or stop of the source; ``admitting``
        is not carried over), with the ④ notice since the source still exists; to the selected version's session (a
        fresh SID when that is the current SID's own version)."""
        store, _ = self._binding_row(client.target.agent_id) or (None, None)
        if store is None:
            return "busy"
        logging.getLogger(__name__).warning(
            "NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE session_id=%s version=%s admission=stuck",
            row.hosted_session_id, row.observed.agent_version)
        store.decide_abandon(client.target.agent_id, hosted_session_id=row.hosted_session_id, basis="stuck_admission",
                             source_status=status, created_agent_version=row.created_agent_version,
                             state_profile=row.state_profile)
        switched = self._carry_out(client, selected, before_send)
        self._admitting_seen.pop(client.target.agent_id, None)
        self.forget()
        return "conflict" if switched is None else "abandoned"

    def prepare(self, client, command, *, before_send):
        from .behavior_native_turn_api import NativeTurnGateway

        with self._changing():
            version = self.selected_version(client)
            selected = self.candidate(client)
            if self.prepared is not None:
                self.current(client)
                return self.prepared
            if self.bound_row(client) is not None:
                # ADR-0009 phase 3: an Agent with a binding row was prepared; the row locates its session, never
                # the version listing (``_latest_version_reader``).
                self.current(client)
                return self.prepared
            version_reader = self.candidate(client, version=version)
            current_version = _read_platform_session(version_reader)
            if current_version is not None:
                if current_version.agent_version != version:
                    raise Problem(409, "NATIVE_SESSION_CHANGED", "the version reader is bound to another image")
                selected = version_reader
            else:
                actual = _read_platform_session(selected)
                if actual is None:
                    reader = _latest_version_reader(
                        client, self.composition.config.identity.managed_identity_client_id, version)
                    if reader is not None:
                        _require_selected_version(reader, version)
                        selected = self._scoped(client, reader)
                        actual = _read_platform_session(selected)
                        if actual is None:
                            raise Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "the original version reader disappeared")
                        if actual.agent_version != reader.agent_version:
                            raise Problem(409, "NATIVE_SESSION_CHANGED", "the original version reader has another version")
                if actual is not None:
                    _require_selected_version(actual, version)
                    selected = selected.for_target(actual)
                    state = self._state(selected)
                    if state.preparation is not None:
                        self._remember_state(selected, state)
                        return self.prepared
                try:
                    selected = self._destination(client, version, before_send)
                except Problem as error:
                    if error.body["error"]["code"] == "NATIVE_ENVIRONMENT_UNKNOWN":
                        raise Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "read the original version session after creation") from None
                    raise
            state = self._state(selected)
            if state.preparation is not None:
                self._remember_state(selected, state)
                return self.prepared
            before_send()
            prepared, _ = NativeTurnGateway._invoke(selected, NativeSessionPrepare(
                request_id=command.request_id, expected_session_id=state.hosted_session_id,
                expected_etag=state.etag,
            ))
            state = self._state(selected)
            if state.preparation != prepared:
                raise Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "the original preparation did not read back")
            return self._remember(selected, state)


def native_sessions(composition):
    value = getattr(composition, "_native_sessions", None)
    if value is None:
        value = composition.__dict__.setdefault("_native_sessions", NativeSessions(composition))
    return value
