"""Explicit API-MI bootstrap; normal turn traffic never creates or changes a session."""

from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import copy_context
import hashlib
import json
import logging
import re
from threading import Lock, RLock
from time import monotonic

from azure.core.exceptions import AzureError, ResourceNotFoundError

from .behavior_agent_versions import SelectorUnreadable
from .behavior_foundry import PLATFORM_READ_RETRY, FoundryTarget
from .behavior_native_turn_wire import (
    NATIVE_STATE_PROFILE, NativeEnvironmentMetadata, NativeEnvironmentReplacementPreview, NativeEnvironmentView, NativeSessionMetadata,
    NativeSessionAbandon, NativeSessionFollow, NativeSessionRead, NativeSessionPrepare, NativeSessionRetarget,
    NativeSessionFilesOmission, NativeUnfinishedRead,
    NativeEnvironmentCommand, NativeEnvironmentRetargetCommand, material_digest,
)
from .errors import Problem
from .native_binding import SUPPORTED_STATE_PROFILES, SessionGuard, binding_store, session_guard_enforced


class _RetiredSession(Problem):
    """A known terminal platform session is not a candidate for new execution."""


# Foundry deletes a session only after 30 days without activity (Learn: manage-hosted-sessions); a cached binding
# unused for a day is read again, so a kept ID never invokes an expired session.
READER_IDLE_SECONDS = 24 * 3600
# Another process (agent-versions sync, a follow, a deploy) can move the endpoint selector while a reader is kept.
# Invoking a session of an unselected version makes the platform bring it back (possibly as another version), so a
# kept reader is used only after a selector read that started at most this long ago still names its version.
# Only reads and polls use this window; anything that delivers work (submit, owner writes, routine, Agent relay,
# prewarm) uses a selector read started within its own request.
SELECTOR_CHECK_SECONDS = 10
# A read or status poll keeps using a reader whose version the selector confirmed at most this long ago while the
# selector itself cannot be read (SelectorUnreadable); work delivery never does.
SELECTOR_STALE_READ_SECONDS = 120
# Refusals meaning the cached binding moved: the platform is read once more (reads are idempotent).
_MOVED = frozenset({"NATIVE_SESSION_CHANGED", "AGENT_VERSION_FOLLOWING", "FOUNDRY_NATIVE_BINDING_MISMATCH"})
# ADR-0009 §7a (m): a compatible source must read retired twice, at least this far apart (across follow attempts,
# never by sleeping in a request), before it is abandoned. Kept in memory: a restart starts the pair again.
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


def _session_metadata(client):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import VersionRefIndicator

    try:
        with AIProjectClient(endpoint=client.target.project_endpoint, credential=client.credential,
                             **PLATFORM_READ_RETRY,
                             connection_timeout=5, read_timeout=10) as project:
            session = project.agents.get_session(client.target.agent_name, client.target.session_id)
    except ResourceNotFoundError:
        return None
    except AzureError as error:
        status = getattr(error, "status_code", None)
        logging.getLogger(__name__).warning("NATIVE_BOOTSTRAP_PLATFORM_READ status=%s",
                                           status if type(status) is int else None)
        denied = status in {401, 403}
        raise Problem(status if denied else 503,
                      "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE" if denied else "NATIVE_BOOTSTRAP_UNKNOWN",
                      "the original platform session could not be read") from None
    indicator = session.version_indicator
    if (session.agent_session_id != client.target.session_id or not isinstance(indicator, VersionRefIndicator)
            or not isinstance(indicator.agent_version, str)
            or re.fullmatch(r"[1-9][0-9]{0,15}", indicator.agent_version) is None):
        raise Problem(409, "NATIVE_SESSION_CHANGED", "the platform session identity or concrete version differs")
    return session


def _read_platform_session(client):
    session = _session_metadata(client)
    if session is None:
        return None
    if session.status in {"failed", "deleted", "expired"}:
        raise _RetiredSession(503, "NATIVE_BOOTSTRAP_UNKNOWN",
                              "the original session is retired; no replacement was created")
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


def _native_profile(client):
    """The native state profile ``client``'s version definition enables (None when it enables none). One
    ``get_version`` read: no session is called (ADR-0009 §3)."""
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
    profile = environment.get("FA_NATIVE_TURNS")
    return profile if isinstance(profile, str) else None


def _require_native_version(client):
    if _native_profile(client) != NATIVE_STATE_PROFILE:
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
        # (scope, SID, monotonic) of the row's SID that platform metadata last confirmed active or idle, refreshed on
        # each use: like main's kept reader, a cold process or a day unused reads the metadata again (ADR-0009 §9).
        self._row_checked = None
        # ADR-0009 §7a: first retired read per compatible source (m); first active read and the overdue log per
        # incompatible source (l). Both are in-process observations only, never a decision.
        self._retired_seen, self._active_seen, self._overdue_logged = {}, {}, set()
        self._deciding_abandon = False
        # Destination SIDs whose image rejected ``settled_session_ids`` (``_follow_invoke``).
        self._settled_unsupported = set()
        # The zero-call guard every Foundry client of this composition (and each one derived from it) checks.
        self.guard = SessionGuard(lambda: binding_store(composition), enforce=session_guard_enforced())
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

    def _bound_client(self, client, row, *, version=None):
        """``client`` aimed at the binding row's current SID, expecting its recorded actual version (V_rec)."""
        located = client.for_target(_target(client.target, session_id=row.hosted_session_id,
                                            agent_version=version or row.observed.agent_version))
        located._binding_row = row
        return located

    def cached_reader(self, client):
        """The kept reader for ``client``'s Agent without any platform read; None when none is kept or the selector
        was not read within ``SELECTOR_CHECK_SECONDS`` naming the kept reader's version. With a binding row, the row's
        SID is the kept reader only while the same checks hold for it (ADR-0009 §10 phase 0 guarantee): a row read and
        a selector read within that window, the selector naming the version the SID was created on, no abandon
        decision, and platform metadata that confirmed the SID within ``READER_IDLE_SECONDS``. A bound Agent never
        falls back to a kept legacy reader."""
        scope = _scope(client.target)
        with self._reader_lock:
            kept, selector, checked, now = self._bound_row, self._selector, self._row_checked, monotonic()
        if kept is not None and kept[0] == scope and kept[1] is not None:
            row = kept[1]
            if (now - kept[2] < SELECTOR_CHECK_SECONDS and row.abandon is None
                    and selector is not None and selector[0] == scope and now - selector[2] < SELECTOR_CHECK_SECONDS
                    and selector[1] == row.created_agent_version
                    and checked is not None and checked[:2] == (scope, row.hosted_session_id)
                    and now - checked[2] < READER_IDLE_SECONDS):
                return self._bound_client(client, row)
            return None
        with self._reader_lock:
            cached, selector, now = self._reader, self._selector, monotonic()
            if (cached is not None and cached[0] == scope and now - cached[2] < READER_IDLE_SECONDS
                    and selector is not None and selector[0] == scope and now - selector[2] < SELECTOR_CHECK_SECONDS
                    and selector[1] == cached[1].agent_version):
                return client.for_target(cached[1])
        return None

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
    def _changing(self):
        """One environment operation; reads meanwhile and afterwards select the binding from the platform again."""
        with self.lock:
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

        def observing(store):
            row = store.claim(target.agent_id, hosted_session_id=target.session_id,
                              created_agent_version=target.agent_version,
                              observed_version=claimed_version or version,
                              source="claim" if claimed_version else "vm", created_at=created_at)
            if version is None or row is None or row.observed.agent_version == version:
                return row
            return store.record_observed(target.agent_id, hosted_session_id=target.session_id,
                                         agent_version=version, source="vm")

        return self._binding("observe", observing)

    def _switched(self, client, value):
        """A rerun's same ``ready`` result writes too, repairing a crash after the commit (ADR-0009 §7a (j))."""
        if value.state == "ready":
            self._binding("switch", lambda store: store.record_switch(client.target.agent_id, value))
        return value

    def _switched_and_checked(self, client, value):
        """``_switched``, then, on an Agent's first switch, the one read-back of unfinished receipts (§7a (f))."""
        if value.state == "ready":
            row = self._binding("switch", lambda store: store.record_switch(client.target.agent_id, value))
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
        self.desired_version = current_hosted_version(self.composition.runtime, client.target)
        return self.desired_version

    def _checked_version(self, client, checked_since=None, *, stale_read=False):
        """The selected version from a selector read started after ``checked_since`` (default:
        ``SELECTOR_CHECK_SECONDS`` ago), reading it now if none is that recent.

        "After" is strict: with a coarse clock (about 16 ms on Windows) an earlier request's read can share the
        tick in which a submit starts. ``stale_read``: see ``reader``."""
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
        """``checked_since``: see ``reader``."""
        with self.lock:
            return self._current(client, checked_since)

    def _current(self, client, checked_since=None):
        row = self.bound_row(client, checked_since if checked_since is not None else monotonic())
        if row is not None:
            return self._current_bound(client, checked_since, row)
        retry = self.reader_cached()
        while True:
            reader = self.reader(client, checked_since=checked_since)
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

        ADR-0009 §10 phase 0 guarantee: a SID the selector no longer picks is never called before the follow decides,
        and a non-active one (404, failed, deleted, expired) never at all. The selector read and, on a cold process or
        after ``READER_IDLE_SECONDS`` unused, the SID's platform metadata (``get_session``) run in parallel, so the
        session call after them waits no longer than on main, where they ran one after the other. ``usable``: the row
        has no abandon decision, the selector (when this build serves it) names the version the SID was created on,
        and the metadata says active or idle; a 401/403/5xx metadata read is raised as on main."""
        scope, sid = _scope(client.target), row.hosted_session_id
        with self._reader_lock:
            checked = self._row_checked
        if checked is not None and checked[:2] == (scope, sid) and monotonic() - checked[2] < READER_IDLE_SECONDS:
            selected, live = self._selected_or_none(client, checked_since, stale_read), True
        else:
            selecting = _LOCATE.submit(copy_context().run, self._selected_or_none, client, checked_since, stale_read)
            try:
                live = _read_platform_session(self._bound_client(client, row)) is not None
            except _RetiredSession:
                live = False
            except BaseException:
                wait([selecting])
                raise
            selected = selecting.result()
        usable = live and row.abandon is None and (selected is None or selected == row.created_agent_version)
        with self._reader_lock:
            if live:
                self._row_checked = (scope, sid, monotonic())
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
        written = self._binding("observe", lambda store: store.record_observed(
            target.agent_id, hosted_session_id=target.session_id, agent_version=target.agent_version, source=source))
        if written is None or written.hosted_session_id != target.session_id:
            return row
        self._keep_row(reader, written)
        reader._binding_row = written
        return written

    def _version_supported(self, client, version):
        """ADR-0009 §3: ``version``'s definition enables a supported profile (one ``get_version`` read, only on the
        drift path). An unreadable definition decides nothing, so it counts as supported."""
        try:
            profile = _native_profile(client.for_target(_target(client.target, agent_version=version)))
        except Exception as error:  # noqa: BLE001 - an unreadable definition decides nothing
            code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
            logging.getLogger(__name__).warning("NATIVE_VERSION_PROFILE_UNREAD code=%s", code)
            return True
        return profile in SUPPORTED_STATE_PROFILES

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
        it) and gives None."""
        observed = self.observed_version(reader, error)
        if observed is None or observed[0] == reader.target.agent_version:
            return None
        version, source = observed
        moved = reader.for_target(_target(reader.target, agent_version=version))
        moved._binding_row = row
        if source == "vm":
            row = self._observe_drift(moved, row, source)
        if not self._version_supported(reader, version):
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
        written = self._binding("switch", lambda store: store.record_switch(reader.target.agent_id, environment))
        if written is None or written.hosted_session_id != environment.hosted_session_id:
            return None
        self._keep_row(reader, written)
        return self._bound_client(reader, written), written

    def relocate(self, client, error):
        """After a refusal from the row's SID: the client to send the same command to once, or None. A compatible
        drift goes to V_obs; a stale row (``replaced``, or V_obs = V_rec) is repaired from that SID's own session state
        (``session_read`` is not a record read, so a replaced session still answers it; it is active, it just
        answered). Never raises: the original refusal stands when nothing is confirmed."""
        row = getattr(client, "_binding_row", None)
        if row is None:
            return None
        try:
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
        with self.lock:
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

    def _destination(self, client, expected_version, before_send):
        version = self.selected_version(client)
        if version != expected_version:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the server-selected version changed")
        destination = self.candidate(client, version=version)
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

    def follow(self, client, command, *, before_send):
        """Only the automatic ``advance`` (``_deciding_abandon``, set under ``self.lock``) may decide an ADR-0009 §7a
        abandon-source switch from metadata; every other caller only carries out a decision already written."""
        from .behavior_native_turn_api import NativeTurnGateway
        from .behavior_session_files import FollowFiles
        from .native_files_wire import FILES_PROFILE

        with self._changing():
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
            if value.reason == "files_pending":
                FollowFiles(destination, value, before_send).copy_step()
                before_send()
                value, _ = self._follow_invoke(destination, internal)
            if value.reason != "source_stop_required":
                return value
            skip_files = value.files_migrated is False and value.files_skip_request_id is not None
            if value.files_manifest_sha256 is None and not skip_files:
                raise Problem(409, "NATIVE_FILES_UNSUPPORTED", "the original follow has no verified workspace manifest")
            files = None if skip_files else FollowFiles(destination, value, before_send)
            stop_recorded = False
            if files is not None:
                # Version only here: once FA's own stop is recorded the source is inactive by that stop; before it,
                # `before_stop` itself refuses a source that is not active.
                files.require_source_version(active=False)
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
                    phase = "stop_session"
                    accepted = source.stop_session()
                    # `is not False` on purpose: only an explicit False (`session_already_stopped`, not FA's stop) skips the record;
                    # clients and test doubles that return None are treated as accepted.
                    if files is not None and not stop_recorded and accepted is not False:
                        # `stop_started` is written only after the platform accepted FA's own stop: a source found
                        # inactive without it was reclaimed, and `stopped_step` never reads its files.
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
        is left waiting on its own unfinished work (§7a (iii))."""
        with self._changing():
            current = self._same_version_decision(client, command)
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

    def _same_version_decision(self, client, command):
        """ADR-0009 M5 with §7a (i)/(ii): the binding row's current SID was created on the selected version, so no
        follow is due, but the SID itself may be unusable (retired, or incompatible and idle). The abandon decision is
        made from metadata (``get_session``, no invocation) *before* any destination exists; once written, the
        destination is a fresh SID of the same version (``destination_session``) and ``follow`` carries it out
        (None: go on). Without a decision there is nothing to follow, and the ``current`` view is returned without
        calling any session: the Agent's own SID is the selected version's session (calling it as a "destination"
        could wake, or recreate, a retired SID)."""
        found = self._binding_row(client.target.agent_id)
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
            profile = _native_profile(source)
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

    def _abandon_decision(self, store, row, source, *, waiting):
        """ADR-0009 §7a applicability from platform metadata (``get_session``, never an invocation). Returns the
        written decision, or None (the existing follow goes on)."""
        sid, agent_id = source.target.session_id, source.target.agent_id
        try:
            metadata = _session_metadata(source)
        except Problem as error:
            # 5xx, timeout, 401/403 or an invalid record: not 404 or retired (ADR-0009 §9); nothing is decided.
            logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_SOURCE_UNREAD code=%s", error.body["error"]["code"])
            return None
        status = "missing" if metadata is None else metadata.status
        now = monotonic()
        if status not in _RETIRED:
            self._retired_seen.pop(sid, None)
        if status != "active":
            self._active_seen.pop(sid, None)
        if not self._source_supported(source, row):
            if status not in _RETIRED and status != "idle":
                self._observe_active(source, status, now)
                return None
            basis = "incompatible"
        elif status in _RETIRED:
            first = self._retired_seen.setdefault(sid, now)
            if now - first > RETIRED_PAIR_SECONDS:
                self._retired_seen[sid] = first = now
            if now - first < RETIRED_CONFIRM_SECONDS:
                logging.getLogger(__name__).warning(
                    "NATIVE_ENVIRONMENT_SOURCE_RETIRED_UNCONFIRMED session_id=%s status=%s", sid, status)
                return None
            basis = "retired"
        elif (waiting and status == "idle" and row is not None and row.hosted_session_id == sid and row.drift):
            basis = "drifted"
        else:
            return None
        return store.decide_abandon(agent_id, hosted_session_id=sid, basis=basis, source_status=status,
                                    created_agent_version=source.target.agent_version)

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
            expected_session_id=sid, expected_etag=state.etag, source_status=decision.source_status))
        if value.state != "ready":
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original abandon-source switch did not commit")
        return self._confirm_environment(destination, value)

    def _advance(self, client, command, *, before_send):
        from .native_files_wire import FILES_ERRORS

        with self._changing():
            deciding, self._deciding_abandon = self._deciding_abandon, True
            try:
                try:
                    return self.follow(client, command, before_send=before_send)
                finally:
                    self._deciding_abandon = deciding
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
