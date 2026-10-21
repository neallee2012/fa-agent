"""ADR-0009 §2 Agent binding row: where an Agent's session is and which version it actually ran.

One CAS item per Agent in the existing Context Table, written only by the API. It never calls a session and holds
no work, lease or upgrade state (those belong to the ADR-0009 draft); SDK-independent, so the API can import it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import re
from threading import Lock
from time import monotonic
from typing import Annotated, Callable, Literal

from pydantic import Field, model_validator

from .behavior_native_turn_wire import NATIVE_STATE_PROFILE, NativeEnvironmentNotice, material_digest
from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .native_responses_wire import NATIVE_V2_PROFILE, UNLABELED_STATE_PROFILE
from .storage import Repository, TableRepository, serialize

# ADR-0009 2d-i parts this API code carries, read by the deploy's switch check (ci_deploy.API_2DI_SCHEMA, by AST):
# 1 = P5's background cut; 2 = also P6's in-request wait and verify-only follow. A plain int literal.
NATIVE_2DI_SCHEMA = 2
# ADR-0009 phase 4 parts this API code carries, read the same way (ci_deploy.API_IDLE_SCHEMA): 1 = 4b-1's binding row
# ``idle_upgrade`` sub-object (an API without it answers 409 NATIVE_BINDING_INVALID on such a row); 2 = 6a (c)'s
# ``idle_upgrade.waiting``; 3 = also 4d's ``rolled_back``/``rolled_back_without_files`` switch notices and the
# fence's ``rollback`` (the same 409 for an API without them); 4 = also 4b-4's ``vetoed`` slot notice (the same 409).
# A plain int literal.
NATIVE_IDLE_SCHEMA = 4
AgentVersion = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
# Every write is a whole-row replace guarded by the ETag just read; a conflict reads again and recomputes.
WRITE_ATTEMPTS = 4
MAX_BINDING_BYTES = 16 * 1024
MAX_PREVIOUS_SESSIONS = 64
# One row per Agent of one Team; the deploy gate refuses rather than stopping its read early.
MAX_BINDING_ROWS = 10000
# ADR-0009 §3: from 2d-ii the supported set is only v2 (2d-i's M1 relaxation accepted {v1, v2} while the API sent
# v1). A SID whose recorded profile is outside it is never invoked, read through Session Files or
# stopped by this API (§7a zero-call need, ``SessionGuard``).
SUPPORTED_STATE_PROFILES = frozenset({NATIVE_V2_PROFILE})
# Recorded for a source found incompatible whose own profile is undeclared or unreadable: in no supported set, so
# every guard and the deploy gate treat the SID as incompatible.
INCOMPATIBLE_STATE_PROFILE = "incompatible"


def _storable(profile):
    """A declared profile outside the row field's pattern is recorded as incompatible: ``model_copy`` skips the
    pattern, and a stored bad value fails every later read."""
    if profile is not None and not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", profile):
        return INCOMPATIBLE_STATE_PROFILE
    return profile


# ``active``: only a source that still runs when 2d-i abandons it: an ``admitting`` stuck past its bound (ADR-0009 §9
# last row), or a background cut's fence whose files can not be moved.
SourceStatus = Literal["idle", "failed", "deleted", "expired", "missing", "active"]
# ``files_unmovable``/``files_overdue``: a background cut's fence after FA's stop whose files can not be moved (an
# error no retry clears, or transient errors past their bound); it commits without them.
AbandonBasis = Literal["incompatible", "retired", "drifted", "stuck_admission", "files_unmovable", "files_overdue"]
# Source statuses whose environment still exists: an abandon of one carries the ④ notice, not "舊環境已失效".
_SOURCE_KEPT = frozenset({"idle", "active"})
# ``rolled_back``: ADR-0009 4d's crash-loop rollback (``FA_NATIVE_IDLE_UPGRADE`` only); ``rolled_back_without_files``:
# that, with an undelivered "沒帶工作檔" (of the rollback or of a switch before or after it), kept visible (M4 #1).
# ``vetoed``: 4b-4's "助理這次沒有更新" after U3's third failure; not a switch, so written only into an empty slot and
# replaced by any later switch.
SwitchNotice = Literal["updated", "without_files", "source_retired", "rolled_back", "rolled_back_without_files",
                       "vetoed"]
# M4 severity inside the slot: a later switch never replaces a more severe undelivered one (已退回 outranks all).
_SWITCH_RANK = {"vetoed": -1, "updated": 0, "without_files": 1, "source_retired": 2, "rolled_back": 3,
                "rolled_back_without_files": 4}
_SWITCH_KINDS = {"updated": "environment_updated", "without_files": "environment_switched_without_files",
                 "source_retired": "environment_source_retired", "rolled_back": "environment_rolled_back",
                 "rolled_back_without_files": "environment_rolled_back", "vetoed": "environment_upgrade_vetoed"}
_WITHOUT_FILES = frozenset({"without_files", "source_retired"})
_ROLLED_BACK = frozenset({"rolled_back", "rolled_back_without_files"})
# ADR-0009 §5 / Neal decision 4: a deferral this long gets the one status-line note, never a forced upgrade.
DEFERRAL_NOTICE_SECONDS = 6 * 3600
log = logging.getLogger(__name__)


class NativeBindingSession(WireModel):
    hosted_session_id: Identifier
    # None only for a SID the first switch's backfill found on an unfinished receipt (ADR-0009 §7a (f)): (e) only
    # asks whether a SID is listed, not its version.
    agent_version: AgentVersion | None
    # ADR-0009 §7a (e): the platform had retired this SID (failed/deleted/expired/404) when the Agent abandoned it,
    # so what it left unfinished can never complete. False also when unknown (an ordinary follow, a backfilled SID):
    # Hosted then adopts what it never started and marks what stalls, and the API reports those as they are.
    retired: bool = Field(default=False, exclude_if=lambda value: not value)


class NativeBindingObservation(WireModel):
    """``vm``: the VM accepted the version as expected or reported it. ``claim``: platform metadata a claim read,
    which no VM confirms. ``metadata-active``: ADR-0009 §4 step 6's old-image branch, the version an active session's
    metadata named, written only after the VM accepted it as expected in the same request."""
    agent_version: AgentVersion
    observed_at: Annotated[str, Field(min_length=20, max_length=40)]
    source: Literal["claim", "vm", "metadata-active"]


class NativeBindingSwitch(WireModel):
    """``abandon_source`` (ADR-0009 §7a): the source was never invoked, read or stopped and no file moved;
    ``source_status`` selects the §8 notice (``idle``: the 2026-10-04 sentence; otherwise "舊環境已失效")."""
    reason: Literal["version_follow", "abandon_source"]
    previous_hosted_session_id: Identifier
    files_migrated: bool | None = None
    basis: AbandonBasis | None = Field(default=None, exclude_if=lambda value: value is None)
    source_status: SourceStatus | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def abandoned(self):
        if (self.reason == "abandon_source") != (self.basis is not None and self.source_status is not None) or (
                self.reason == "abandon_source" and self.files_migrated is not False):
            raise ValueError("an abandoned source keeps its basis and status and never migrates files")
        return self


class NativeBindingAbandon(WireModel):
    """ADR-0009 §7a (g): the API's decision to abandon the current SID, written before the switch is sent. Every
    retry (any caller, any ``idle_only``) goes by it and never judges the metadata again, so it never flips."""
    hosted_session_id: Identifier
    basis: AbandonBasis
    source_status: SourceStatus
    decided_at: Annotated[str, Field(min_length=20, max_length=40)]


class NativeBindingFresh(WireModel):
    """ADR-0009 M5 (phase 3): the attempt number that goes into the next fresh SID for ``agent_version`` from the
    current SID. It is raised (CAS) only when a fresh destination proved unusable, so a retry never reuses it."""
    hosted_session_id: Identifier
    agent_version: AgentVersion
    attempt: Annotated[int, Field(ge=1, le=1_000_000)]


class NativeBindingUpgrade(WireModel):
    """ADR-0009 M1 2d-i step 2: the background cut's files fence, written before it asks Hosted for ``pending``, so a
    ``pending`` naming ``hosted_session_id`` is this API's own fence (bounded, withdrawn by the background job), not one
    an older follow left. ``started_at`` (the store's clock): another replica withdraws a fence without FA's recorded
    stop only once it is older than the fence bound. ``agent_version``: the destination's, so the fence is driven at
    its own session whatever the selector names later. ``stop_started``: written before every FA stop of the source
    the fence sends (``mark_stop``); from then on the fence is never withdrawn, only driven to its commit.
    ``rollback`` (4d, ``FA_NATIVE_IDLE_UPGRADE`` only): the fence is a crash-loop rollback's, so whichever path commits
    it later (a cut's or a send's ``_converge``) writes 已退回 and opens no crash-loop window."""
    hosted_session_id: Identifier
    agent_version: AgentVersion
    request_id: Identifier
    started_at: Annotated[str, Field(min_length=20, max_length=40)]
    stop_started: bool = Field(default=False, exclude_if=lambda value: not value)
    rollback: bool = Field(default=False, exclude_if=lambda value: not value)


_Timestamp = Annotated[str, Field(min_length=20, max_length=40)]


class NativeBindingIdleUpgrade(WireModel):
    """ADR-0009 phase 4b idle upgrade (``FA_NATIVE_IDLE_UPGRADE`` only; not ``upgrade``, which is 2d-i's files fence).
    U0 (4b-1) writes ``target_version`` and ``deferred_since`` (the store's clock when that target first became due),
    and a new target resets both. ``record_switch`` (U8, with the flag) keeps only the vetoes and the switch time
    (``switched_at``, the start of 4d's crash-loop window); 4d's crash-loop adds a veto. U3 (4b-2) counts the
    target's failed health checks in ``health_failures`` and vetoes it at the third. The other fields belong to later
    4b steps and stay at their defaults until then."""
    target_version: AgentVersion | None = None
    deferred_since: _Timestamp | None = None
    last_attempt_at: _Timestamp | None = Field(default=None, exclude_if=lambda value: value is None)
    health_failures: Annotated[int, Field(ge=0, le=1_000)] = Field(default=0, exclude_if=lambda value: not value)
    # This Agent's vetoed versions (3 health failures on one target, or 4d's crash-loop); the project-level ones are
    # native_version_veto's.
    vetoed_versions: Annotated[tuple[AgentVersion, ...], Field(max_length=16)] = Field(
        default=(), exclude_if=lambda value: not value)
    switched_at: _Timestamp | None = Field(default=None, exclude_if=lambda value: value is None)
    start_failures: Annotated[int, Field(ge=0, le=1_000)] = Field(default=0, exclude_if=lambda value: not value)
    # Phase 6a (c): the "有新版本 vN，會在助理閒置時更新" status-line note for ``target_version`` once its deferral passed
    # 6 hours. ``pending`` until an owner workbench response carries it, then ``shown``: it stays, so the note is made
    # once per target. A new target (``record_due``) or the switch (``record_switch``) rebuilds this object, ending it.
    waiting: Literal["pending", "shown"] | None = Field(default=None, exclude_if=lambda value: value is None)
    # 4d (coordinator 2026-10-19): the current version crash-looped and this Agent's own vetoes also name the version a
    # rollback would go to, so there is none; written once, so no turn end retries it. The switch rebuild ends it.
    rollback_blocked: bool = Field(default=False, exclude_if=lambda value: not value)


ClockKind = Literal["retired", "admitting", "drive", "unmovable", "fence_broken"]


class NativeBindingClock(WireModel):
    """ADR-0009 2d-i P6: a bound's durable start, so it holds across API replicas and restarts. ``retired``: the first
    retired read of the current SID (§7a (m)); ``admitting``: the first time ``subject`` (an ``admitting`` RID) was
    seen stuck; ``drive``/``unmovable``: a fence's (``subject``, its RID) first failed drive and first unmovable read;
    ``fence_broken``: the first time the fence's destination could not be read. One per kind, for the current SID."""
    kind: ClockKind
    subject: Identifier
    since: Annotated[str, Field(min_length=20, max_length=40)]


class NativeBindingRow(WireModel):
    schema_version: Literal["fa-native-binding-v1"] = "fa-native-binding-v1"
    agent_id: Identifier
    hosted_session_id: Identifier
    # The version the current SID was created on: generation 0 is the version that derives the SID; a switch
    # writes the commit result's ``hosted_agent_version``.
    created_agent_version: AgentVersion
    # Same-SID drift: set the first time an actual version differs from ``created_agent_version``; never cleared
    # while the SID stays (a drift back keeps it), cleared only when the current SID changes.
    drift: bool = False
    # Set together with ``drift`` (on its unset→set write only); delivered by an owner response, cleared after.
    notice_pending: bool = False
    # The version first observed when ``drift`` was set (vY of the notice): the recorded version moves on, so a
    # drift back would otherwise name the creation version on both sides.
    notice_to_version: AgentVersion | None = None
    # ADR-0009 §8 / M4 notice slot: the switch notice the current SID's commit set (``updated``: work files came
    # along; ``without_files``: not moved, the old environment is kept; ``source_retired``: not moved, the old one had
    # failed or was deleted). Delivered once by an owner response and cleared after it is written. It outranks the
    # drift notice (嚴重的蓋過輕的), and an earlier undelivered no-files kind outranks a later ``updated``, so the
    # "沒帶工作檔" fact is never hidden before it is shown. Absent from rows that never had one (bytes unchanged).
    switch_notice: SwitchNotice | None = Field(default=None, exclude_if=lambda value: value is None)
    observed: NativeBindingObservation
    # Only from ``get_session`` (never ``list_sessions``) and only when an existing flow already read it.
    created_at: Annotated[str, Field(max_length=40)] | None = None
    state_profile: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")] = UNLABELED_STATE_PROFILE
    previous_sessions: Annotated[tuple[NativeBindingSession, ...], Field(max_length=MAX_PREVIOUS_SESSIONS)] = ()
    last_switch: NativeBindingSwitch | None = None
    abandon: NativeBindingAbandon | None = Field(default=None, exclude_if=lambda value: value is None)
    # ADR-0009 §7a (f): the one-time read-back of unfinished receipts after the first switch; kept across switches.
    unfinished_checked: Literal["verified", "unverified"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    # ADR-0009 M5: a raised attempt for the next fresh SID; absent (attempt 0) on every row that never needed one.
    fresh: NativeBindingFresh | None = Field(default=None, exclude_if=lambda value: value is None)
    upgrade: NativeBindingUpgrade | None = Field(default=None, exclude_if=lambda value: value is None)
    # ``state_profile`` is the repair fallback's v1, not a definition read (``NATIVE_BINDING_SWITCH_PROFILE_UNREAD``);
    # the next successful read of the version's definition corrects it (``record_profile``).
    profile_unread: bool = Field(default=False, exclude_if=lambda value: not value)
    # Written only with FA_NATIVE_SESSION_GUARD_ENFORCE (an older API revision decodes rows strictly); a switch's
    # ``record_switch`` rebuilds the row without them.
    clocks: Annotated[tuple[NativeBindingClock, ...], Field(max_length=8)] = Field(
        default=(), exclude_if=lambda value: not value)
    # Written only with FA_NATIVE_IDLE_UPGRADE, by the same rule as ``clocks``: an API older than this field decodes a
    # row that has it as 409 NATIVE_BINDING_INVALID, so once it is on the API never rolls back below it.
    idle_upgrade: NativeBindingIdleUpgrade | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def consistent(self):
        if self.notice_pending and not self.drift:
            raise ValueError("a pending drift notice requires the same-SID drift flag")
        if (self.notice_to_version is not None) != self.drift or self.notice_to_version == self.created_agent_version:
            raise ValueError("the drift flag keeps the version it was set by, which differs from the creation version")
        if any(item.hosted_session_id == self.hosted_session_id for item in self.previous_sessions):
            raise ValueError("the current SID is never one of its previous SIDs")
        if len({item.hosted_session_id for item in self.previous_sessions}) != len(self.previous_sessions):
            raise ValueError("previous SIDs are distinct")
        if self.abandon is not None and self.abandon.hosted_session_id != self.hosted_session_id:
            raise ValueError("only the current SID can be abandoned")
        if self.fresh is not None and self.fresh.hosted_session_id != self.hosted_session_id:
            raise ValueError("a fresh-SID attempt belongs to the current SID")
        if self.upgrade is not None and self.upgrade.hosted_session_id == self.hosted_session_id:
            raise ValueError("a files fence names a destination other than the current SID")
        if len({item.kind for item in self.clocks}) != len(self.clocks):
            raise ValueError("one durable clock per kind")
        return self

    def clock(self, kind: str) -> NativeBindingClock | None:
        return next((item for item in self.clocks if item.kind == kind), None)

    def attempt(self, agent_version: str) -> int:
        """ADR-0009 M5: the attempt number of the next fresh SID for ``agent_version`` from the current SID."""
        fresh = self.fresh
        return fresh.attempt if fresh is not None and fresh.agent_version == agent_version else 0

    def supported(self) -> bool:
        return self.state_profile in SUPPORTED_STATE_PROFILES

    def earlier(self, hosted_session_id: str) -> bool:
        """ADR-0009 §7a (e): a receipt naming one of the previous SIDs was accepted by an earlier version."""
        return any(item.hosted_session_id == hosted_session_id for item in self.previous_sessions)

    def retired(self, hosted_session_id: str) -> bool:
        """A previous SID the platform had retired when the Agent left it: what it left can never complete."""
        return any(item.hosted_session_id == hosted_session_id and item.retired for item in self.previous_sessions)

    def notice(self, *, switch: bool = True, rolled_back: bool = True,
               vetoed: bool = True) -> NativeEnvironmentNotice | None:
        """The pending ADR-0009 §8 notice of the slot: the switch notice, else "平台把助理的執行環境換成了另一個版本
        （vX→vY）" while the drift notice is pending. ``switch=False``: the drift notice only (for a client that does
        not know the switch kinds, as before phase 2c); ``rolled_back=False``: the same for a slot holding 已退回 (a
        client before phase 4d); ``vetoed=False``: the same for 4b-4's "沒有更新" (a client before 4b-4)."""
        if switch and self.switch_notice is not None and (rolled_back or self.switch_notice not in _ROLLED_BACK) and (
                vetoed or self.switch_notice != "vetoed"):
            return NativeEnvironmentNotice(kind=_SWITCH_KINDS[self.switch_notice],
                                           without_files=self.switch_notice == "rolled_back_without_files")
        if not self.notice_pending:
            return None
        return NativeEnvironmentNotice(from_version=self.created_agent_version, to_version=self.notice_to_version)

    def waiting_version(self) -> str | None:
        """Phase 6a (c): the target of the 6-hour "有新版本" note while it is pending (not a slot notice: it is the
        lightest and rides on an owner workbench answer only when no slot notice does). None once the recorded version
        is the target: ``record_observed`` keeps ``idle_upgrade`` when the platform moves the VM there itself; None
        once this Agent vetoed the target (4b-4: "沒有更新" said it stays, so no later "有新版本" for it)."""
        upgrade = self.idle_upgrade
        if (upgrade is None or upgrade.waiting != "pending" or upgrade.target_version == self.observed.agent_version
                or upgrade.target_version in upgrade.vetoed_versions):
            return None
        return upgrade.target_version


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value) -> str | None:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    return None


class NativeBindingStore:
    def __init__(self, repository: Repository, *, tenant_id: str, team_id: str,
                 clock: Callable[[], datetime] = _utc_now):
        self.repository, self.clock = repository, clock
        # Its own partition: the legacy authority inventory never reads it, and no other Team's row shares it.
        self.partition_key = "native-binding:" + material_digest({"tenant": tenant_id, "team": team_id})

    @staticmethod
    def row_key(agent_id: str) -> str:
        return "agent:" + agent_id

    def _now(self) -> str:
        value = _timestamp(self.clock())
        if value is None:
            raise Problem(503, "NATIVE_BINDING_UNAVAILABLE", "trusted UTC is unavailable")
        return value

    def _read(self, agent_id):
        try:
            record = self.repository.get(self.partition_key, self.row_key(agent_id))
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                return None, None
            raise
        try:
            row = NativeBindingRow.model_validate_json(serialize(record.payload))
            if row.agent_id != agent_id or not record.etag or record.etag == "*":
                raise ValueError("another Agent's binding row")
        except (ValueError, TypeError):
            raise Problem(409, "NATIVE_BINDING_INVALID", "the original binding row differs from its fixed schema") from None
        return row, record.etag

    def read(self, agent_id: str) -> NativeBindingRow | None:
        return self._read(agent_id)[0]

    def rows(self, *, page: int = 100) -> tuple[NativeBindingRow, ...]:
        """Every binding row of this Team, read page by page to the end (the ADR-0009 phase 3b deploy gate). A row
        that differs from its fixed schema or names another Agent stops the read: the gate never skips one."""
        found, after = [], None
        while True:
            items, more = self.repository.list_prefix(self.partition_key, "agent:", page, after=after)
            for key, record in items:
                try:
                    row = NativeBindingRow.model_validate_json(serialize(record.payload))
                    if self.row_key(row.agent_id) != key:
                        raise ValueError("another Agent's binding row")
                except (ValueError, TypeError):
                    raise Problem(409, "NATIVE_BINDING_INVALID",
                                  "the original binding row differs from its fixed schema") from None
                found.append(row)
            if not more or not items:
                return tuple(found)
            if len(found) >= MAX_BINDING_ROWS:
                raise Problem(413, "NATIVE_BINDING_INVALID", "the Team has more binding rows than the gate reads")
            after = items[-1][0]

    def _update(self, agent_id, change):
        """``change(row)`` returns the new row or None; a conflict rereads and recomputes, so a stale computation
        never overwrites another writer."""
        for _ in range(WRITE_ATTEMPTS):
            row, etag = self._read(agent_id)
            updated = change(row)
            if updated is None or updated == row:
                return row
            payload = updated.model_dump(mode="json")
            if len(serialize(payload)) > MAX_BINDING_BYTES:
                raise Problem(413, "NATIVE_BINDING_INVALID", "the binding row exceeds its fixed bound")
            try:
                if row is None:
                    self.repository.create(self.partition_key, self.row_key(agent_id), payload)
                else:
                    self.repository.replace(self.partition_key, self.row_key(agent_id), payload, etag)
            except Problem as error:
                if (error.status, error.body["error"]["code"]) not in {(409, "ALREADY_EXISTS"), (412, "ETAG_CONFLICT")}:
                    raise
                continue
            return updated
        raise Problem(503, "NATIVE_BINDING_CONFLICT", "the binding row kept changing; nothing more was written")

    def claim(self, agent_id: str, *, hosted_session_id: str, created_agent_version: str,
              observed_version: str | None = None, source: Literal["claim", "vm"] = "claim",
              created_at=None, state_profile: str | Callable[[], str] = NATIVE_STATE_PROFILE) -> NativeBindingRow:
        """Insert once; an existing row is read back, never overwritten. An ``observed_version`` that already differs
        from the creation version sets the drift flag in this insert: otherwise every later observation would equal
        the recorded drifted version and the flag would never be set (ADR-0009 §2). ``state_profile``: what the
        creation version's definition declares (ADR-0009 2d-i); a callable is read only when this inserts."""

        written = []

        def claiming(row):
            written.clear()
            if row is not None:
                return None
            observed = observed_version or created_agent_version
            drifted = observed != created_agent_version
            written.append(NativeBindingRow(
                agent_id=agent_id, hosted_session_id=hosted_session_id, created_agent_version=created_agent_version,
                drift=drifted, notice_pending=drifted, notice_to_version=observed if drifted else None,
                observed=NativeBindingObservation(agent_version=observed, observed_at=self._now(), source=source),
                created_at=_timestamp(created_at),
                state_profile=state_profile() if callable(state_profile) else state_profile))
            return written[0]

        row = self._update(agent_id, claiming)
        if written and row == written[0]:
            log.warning("NATIVE_BINDING_CLAIMED session_id=%s created_version=%s observed_version=%s drift=%s",
                        hosted_session_id, row.created_agent_version, row.observed.agent_version, row.drift)
        return row

    def record_observed(self, agent_id: str, *, hosted_session_id: str, agent_version: str,
                        source: Literal["vm", "metadata-active"],
                        state_profile: str | None = None) -> NativeBindingRow | None:
        """ADR-0009 §4 step 6 drift CAS. Another SID is a binding divergence, not drift. Once the flag is set, later
        versions (a drift back included) change only the recorded version: version numbers are not monotonic.
        ``state_profile``: what ``agent_version``'s definition declares, written in the same CAS so the row's profile
        follows the version the SID now runs (a same-SID drift onto another image); None (not read) keeps it."""
        moved, written = [], []
        state_profile = _storable(state_profile)

        def recording(row):
            moved.clear()
            written.clear()
            if row is None:
                return None
            if row.hosted_session_id != hosted_session_id:
                moved.append(row.hosted_session_id)
                return None
            if row.observed.agent_version == agent_version:
                return None
            first = not row.drift and agent_version != row.created_agent_version
            profile = {} if state_profile is None else {"state_profile": state_profile, "profile_unread": False}
            written.append(row.model_copy(update={
                "observed": NativeBindingObservation(agent_version=agent_version, observed_at=self._now(), source=source),
                "drift": row.drift or first, "notice_pending": row.notice_pending or first,
                "notice_to_version": agent_version if first else row.notice_to_version, **profile}))
            return written[0]

        row = self._update(agent_id, recording)
        if moved:
            log.warning("NATIVE_BINDING_DIVERGED session_id=%s binding_session_id=%s", hosted_session_id, moved[-1])
        elif written and row == written[0]:
            log.warning("NATIVE_VERSION_OBSERVED source=%s session_id=%s created_version=%s observed_version=%s drift=%s "
                        "profile=%s", source, hosted_session_id, row.created_agent_version, agent_version, row.drift,
                        row.state_profile)
        return row

    def record_switch(self, agent_id: str, view, *, state_profile: str = NATIVE_STATE_PROFILE,
                      profile_unread: bool = False, idle_upgrade: bool = False,
                      rollback: bool = False) -> NativeBindingRow:
        """ADR-0009 §7a (f)/(j). Idempotent, so a crash between the commit and this write is repaired by the next
        follow's rerun; when the commit's previous SID is not the row's (the selector moved before the rerun), both
        are kept as previous SIDs. ``state_profile``: what the new SID's version declares (2d-i); ``profile_unread``:
        it is the repair fallback's v1, corrected by the next definition read. A row with ``upgrade`` is rebuilt
        without it: the fence ended in this commit. ``idle_upgrade`` (``FA_NATIVE_IDLE_UPGRADE``, U8): the rebuilt row
        keeps this Agent's vetoes and records the switch time; ``rollback`` (4d), or a rollback's fence this commit
        ends: the slot says 已退回 and no switch time is recorded, so the rollback's own switch is never judged."""

        def switching(row):
            if (row is not None and row.hosted_session_id == view.hosted_session_id
                    and row.created_agent_version == view.hosted_agent_version):
                return None
            fence = None if row is None else row.upgrade
            rolling = rollback or (fence is not None and fence.rollback
                                   and fence.hosted_session_id == view.hosted_session_id)
            previous = list(row.previous_sessions) if row is not None else []
            decision = None if row is None else row.abandon
            # The abandon path never moves files (Hosted's ``abandon_session`` commits ``files_migrated=False``), so a
            # commit with ``files_migrated is True`` was another switch (a fence's drive, a follow) whatever basis the
            # decision named: its moved files and "updated" notice are the truth, so any basis is dropped, not only
            # the ``files_*`` ones a failed no-files attempt wrote.
            if decision is not None and (decision.hosted_session_id != view.previous_hosted_session_id
                                         or view.files_migrated is True):
                decision = None
            sources = []
            if row is not None and row.hosted_session_id not in {view.previous_hosted_session_id, view.hosted_session_id}:
                sources.append(NativeBindingSession(
                    hosted_session_id=row.hosted_session_id, agent_version=row.created_agent_version))
            sources.append(NativeBindingSession(
                hosted_session_id=view.previous_hosted_session_id, agent_version=view.previous_agent_version,
                retired=decision is not None and decision.source_status not in _SOURCE_KEPT))
            for item in sources:
                if all(kept.hosted_session_id != item.hosted_session_id for kept in previous):
                    previous.append(item)
            previous = [item for item in previous if item.hosted_session_id != view.hosted_session_id]
            if len(previous) > MAX_PREVIOUS_SESSIONS:
                log.warning("NATIVE_BINDING_PREVIOUS_TRIMMED agent_id=%s dropped=%d",
                            agent_id, len(previous) - MAX_PREVIOUS_SESSIONS)
                previous = previous[-MAX_PREVIOUS_SESSIONS:]
            # §8 switch notice; an undelivered more severe one stays (M4). Only a confirmed move of the work files is
            # "updated": ``files_migrated=None`` (an image without workspace files) moved none, so it says so honestly.
            if decision is not None:
                notice = "without_files" if decision.source_status in _SOURCE_KEPT else "source_retired"
            else:
                notice = "updated" if view.files_migrated is True else "without_files"
            if rolling:
                notice = "rolled_back" if notice == "updated" else "rolled_back_without_files"
            kept = None if row is None else row.switch_notice
            fresh = notice
            if kept is not None and _SWITCH_RANK[kept] > _SWITCH_RANK[notice]:
                notice = kept
            if notice == "rolled_back" and (kept in _WITHOUT_FILES or fresh in _WITHOUT_FILES):
                notice = "rolled_back_without_files"  # M4 #1: 已退回 never hides an undelivered "沒帶工作檔"
            return NativeBindingRow(
                agent_id=agent_id, hosted_session_id=view.hosted_session_id,
                created_agent_version=view.hosted_agent_version,
                observed=NativeBindingObservation(
                    agent_version=view.hosted_agent_version, observed_at=self._now(), source="vm"),
                previous_sessions=tuple(previous), switch_notice=notice, state_profile=state_profile,
                profile_unread=profile_unread, unfinished_checked=None if row is None else row.unfinished_checked,
                idle_upgrade=NativeBindingIdleUpgrade(
                    vetoed_versions=() if row is None or row.idle_upgrade is None else row.idle_upgrade.vetoed_versions,
                    switched_at=None if rolling else self._now()) if idle_upgrade else None,
                last_switch=NativeBindingSwitch(
                    reason="version_follow", previous_hosted_session_id=view.previous_hosted_session_id,
                    files_migrated=view.files_migrated) if decision is None else NativeBindingSwitch(
                    reason="abandon_source", previous_hosted_session_id=view.previous_hosted_session_id,
                    files_migrated=False, basis=decision.basis, source_status=decision.source_status))

        return self._update(agent_id, switching)

    def raise_attempt(self, agent_id: str, *, hosted_session_id: str, agent_version: str,
                      attempt: int) -> NativeBindingRow | None:
        """ADR-0009 M5: the fresh SID of ``attempt`` proved unusable (retired, or woke on another version), so the next
        destination for ``agent_version`` takes ``attempt + 1``. Idempotent: an attempt already past it is kept."""

        def raising(row):
            if row is None or row.hosted_session_id != hosted_session_id or row.attempt(agent_version) != attempt:
                return None
            return row.model_copy(update={"fresh": NativeBindingFresh(
                hosted_session_id=hosted_session_id, agent_version=agent_version, attempt=attempt + 1)})

        row = self._update(agent_id, raising)
        if row is not None and row.attempt(agent_version) == attempt + 1:
            log.warning("NATIVE_BINDING_FRESH_ATTEMPT session_id=%s version=%s attempt=%d",
                        hosted_session_id, agent_version, attempt + 1)
        return row

    def record_upgrade(self, agent_id: str, *, hosted_session_id: str, upgrade: NativeBindingUpgrade | None,
                       lost: NativeBindingUpgrade | None = None) -> NativeBindingRow | None:
        """ADR-0009 M1 2d-i: set (before the files fence's ``pending``) or clear (after its withdraw) the background
        cut's fence on the row that still names ``hosted_session_id``; another SID's row is left alone. A fence whose
        stop is marked is never replaced or cleared (its commit's ``record_switch`` ends it), except the ``lost`` one
        ``_settle`` found gone from Hosted without a commit; the same fence (SID and RID) is never replaced: the
        returned row's ``started_at`` says which job's fence it is."""

        def recording(row):
            if row is None or row.hosted_session_id != hosted_session_id or row.upgrade == upgrade:
                return None
            kept = row.upgrade
            if kept is not None and (kept.stop_started and kept != lost or upgrade is not None and (
                    kept.hosted_session_id, kept.request_id) == (upgrade.hosted_session_id, upgrade.request_id)):
                return None
            if lost is not None and kept != lost:
                return None  # the lost fence was replaced meanwhile: the newer one is not cleared with it
            return row.model_copy(update={"upgrade": upgrade})

        return self._update(agent_id, recording)

    def mark_stop(self, agent_id: str, *, hosted_session_id: str, upgrade: NativeBindingUpgrade) -> NativeBindingRow | None:
        """The fence's durable "stop started" marker, written before FA's stop of the source (idempotent); only on the
        row that still records this fence (same destination SID and RID)."""

        def marking(row):
            fence = None if row is None else row.upgrade
            if (fence is None or row.hosted_session_id != hosted_session_id
                    or (fence.hosted_session_id, fence.request_id) != (upgrade.hosted_session_id, upgrade.request_id)):
                return None
            return row.model_copy(update={"upgrade": fence.model_copy(update={"stop_started": True})})

        return self._update(agent_id, marking)

    def record_clock(self, agent_id: str, *, hosted_session_id: str, kind: str, subject: str | None,
                     since: str | None = None, restart: bool = False) -> NativeBindingRow | None:
        """Start the durable clock ``kind`` for ``subject`` at ``since`` (kept when it already runs for the same
        subject, unless ``restart``), or clear it (``subject`` None), on the row that still names
        ``hosted_session_id``. Returns the row as written or read; its ``clock(kind)`` is the start that holds."""

        def recording(row):
            if row is None or row.hosted_session_id != hosted_session_id:
                return None
            kept = row.clock(kind)
            if subject is None and kept is None or kept is not None and kept.subject == subject and not restart:
                return None
            others = tuple(item for item in row.clocks if item.kind != kind)
            started = () if subject is None else (NativeBindingClock(kind=kind, subject=subject, since=since),)
            return row.model_copy(update={"clocks": others + started})

        return self._update(agent_id, recording)

    def record_due(self, agent_id: str, *, hosted_session_id: str, target_version: str) -> NativeBindingRow | None:
        """ADR-0009 U0 (``FA_NATIVE_IDLE_UPGRADE`` only): ``target_version`` is due on the row that still names
        ``hosted_session_id``. The first time it is due its deferral start is written; the same target again writes
        nothing, so the start holds; another target (the selector moved) starts again, keeping the later steps' vetoes."""

        def recording(row):
            if row is None or row.hosted_session_id != hosted_session_id:
                return None
            kept = row.idle_upgrade or NativeBindingIdleUpgrade()
            if kept.target_version == target_version:
                return None
            return row.model_copy(update={"idle_upgrade": NativeBindingIdleUpgrade(
                target_version=target_version, deferred_since=self._now(), vetoed_versions=kept.vetoed_versions,
                switched_at=kept.switched_at, start_failures=kept.start_failures,
                rollback_blocked=kept.rollback_blocked)})

        return self._update(agent_id, recording)

    def record_veto(self, agent_id: str, *, hosted_session_id: str, agent_version: str,
                    notice: bool = False) -> NativeBindingRow | None:
        """ADR-0009 4d: ``agent_version`` crash-looped on the row that still names ``hosted_session_id``; added once to
        this Agent's vetoes (the newest 16 are kept). ``notice`` (4b-4: U3's veto): the same write puts "沒有更新" into
        an empty notice slot, so the veto is never on the row without it (a slot already holding a switch keeps it)."""

        def recording(row):
            if row is None or row.hosted_session_id != hosted_session_id:
                return None
            kept = row.idle_upgrade or NativeBindingIdleUpgrade()
            if agent_version in kept.vetoed_versions:
                return None
            return row.model_copy(update={"idle_upgrade": kept.model_copy(update={
                "vetoed_versions": (*kept.vetoed_versions, agent_version)[-16:]}), **(
                {"switch_notice": "vetoed"} if notice and row.switch_notice is None else {})})

        return self._update(agent_id, recording)

    def record_health_failure(self, agent_id: str, *, hosted_session_id: str, target_version: str,
                              last_attempt_at: str | None, failed: bool = True) -> NativeBindingRow | None:
        """ADR-0009 U3: one failed health check of ``target_version`` while it is the row's due target; the count is
        per (Agent, target), so a new target (``record_due``) or the switch (``record_switch``) starts it again. The
        attempt is the one that read ``last_attempt_at`` (the previous attempt's time, written here): another replica
        that checked from the same read counts nothing more. ``failed=False``: a check that answered nothing, only its
        time (so the retry interval applies), no failure."""

        def recording(row):
            if (row is None or row.hosted_session_id != hosted_session_id or row.idle_upgrade is None
                    or row.idle_upgrade.target_version != target_version
                    or row.idle_upgrade.last_attempt_at != last_attempt_at):
                return None
            kept = row.idle_upgrade
            return row.model_copy(update={"idle_upgrade": kept.model_copy(update={
                "health_failures": min(kept.health_failures + int(failed), 1_000), "last_attempt_at": self._now()})})

        return self._update(agent_id, recording)

    def record_rollback_blocked(self, agent_id: str, *, hosted_session_id: str) -> NativeBindingRow | None:
        """ADR-0009 4d: the row that still names ``hosted_session_id`` has no version to roll back to."""

        def recording(row):
            if (row is None or row.hosted_session_id != hosted_session_id or row.idle_upgrade is None
                    or row.idle_upgrade.rollback_blocked):
                return None
            kept = row.idle_upgrade.model_copy(update={"rollback_blocked": True})
            return row.model_copy(update={"idle_upgrade": kept})

        return self._update(agent_id, recording)

    def overdue(self, row: NativeBindingRow) -> bool:
        """Phase 6a (c): ``row``'s deferral began more than ``DEFERRAL_NOTICE_SECONDS`` ago by this store's clock and no
        note was made for that target yet, nor did this Agent veto it. Pure: no read."""
        upgrade = row.idle_upgrade
        if (upgrade is None or upgrade.deferred_since is None or upgrade.waiting is not None
                or upgrade.target_version in upgrade.vetoed_versions):
            return False
        began = datetime.fromisoformat(upgrade.deferred_since)
        return (datetime.fromisoformat(self._now()) - began).total_seconds() > DEFERRAL_NOTICE_SECONDS

    def record_waiting(self, agent_id: str, *, hosted_session_id: str, target_version: str) -> NativeBindingRow | None:
        """Phase 6a (c): make the status-line note for ``target_version`` once, while the row still names this SID and
        this target, its deferral is over 6 hours old and no note was made. Metadata only: one row CAS."""

        def recording(row):
            if (row is None or row.hosted_session_id != hosted_session_id or row.idle_upgrade is None
                    or row.idle_upgrade.target_version != target_version or not self.overdue(row)):
                return None
            return row.model_copy(update={"idle_upgrade": row.idle_upgrade.model_copy(update={"waiting": "pending"})})

        return self._update(agent_id, recording)

    def record_profile(self, agent_id: str, *, hosted_session_id: str, agent_version: str,
                       state_profile: str) -> NativeBindingRow | None:
        """A definition read of the version the current SID runs (``observed``) replaces the repair fallback's profile
        (``profile_unread``), or the stale one of a row that drifted before the drift CAS recorded the profile: such a
        row's V_rec already equals what the VM runs, so no drift CAS comes again. Only while the SID and the recorded
        version are still those; a row that is neither, or already says so, is not written."""
        state_profile = _storable(state_profile)
        written = []

        def correcting(row):
            written.clear()
            if (row is None or row.hosted_session_id != hosted_session_id or row.observed.agent_version != agent_version
                    or not (row.profile_unread or row.drift and row.state_profile != state_profile)):
                return None
            written.extend((row.model_copy(update={"state_profile": state_profile, "profile_unread": False}),
                            row.state_profile))
            return written[0]

        row = self._update(agent_id, correcting)
        if written and row == written[0]:
            log.warning("NATIVE_BINDING_PROFILE_CORRECTED session_id=%s version=%s from=%s profile=%s",
                        hosted_session_id, agent_version, written[1], row.state_profile)
        return row

    def clear_notice(self, agent_id: str, delivered: NativeEnvironmentNotice) -> NativeBindingRow | None:
        """ADR-0009 §4 end: after an owner response carrying ``delivered`` was written, clear it by CAS. A conflict
        rereads and clears only when the slot still holds that same notice: a newer or more severe one written
        meanwhile was never shown and stays. Clearing a switch notice also clears a drift notice it outranked."""

        written = []

        def clearing(row):
            written.clear()
            if row is None or row.notice() != delivered:
                return None
            written.append(row.model_copy(update={"switch_notice": None, "notice_pending": False}))
            return written[0]

        row = self._update(agent_id, clearing)
        if written and row == written[0]:
            log.warning("NATIVE_ENVIRONMENT_NOTICE_CLEARED kind=%s session_id=%s", delivered.kind, row.hosted_session_id)
        return row

    def clear_waiting(self, agent_id: str, target_version: str) -> NativeBindingRow | None:
        """Phase 6a (c): after an owner answer carrying the note for ``target_version`` was written, keep it as ``shown``
        (never removed: the note is made once per target). A different or no pending note is left as it is."""

        def clearing(row):
            if row is None or row.waiting_version() != target_version:
                return None
            return row.model_copy(update={"idle_upgrade": row.idle_upgrade.model_copy(update={"waiting": "shown"})})

        return self._update(agent_id, clearing)

    def record_unfinished(self, agent_id: str, *, hosted_session_id: str, session_ids, complete: bool):
        """ADR-0009 §7a (f), once per Agent: SIDs of unfinished receipts (other than the current one) join the
        previous-SID list, version unknown; an incomplete read is recorded as unverified, never as verified."""

        def recording(row):
            if row is None or row.hosted_session_id != hosted_session_id or row.unfinished_checked is not None:
                return None
            previous = list(row.previous_sessions)
            for sid in session_ids:
                if sid != row.hosted_session_id and not row.earlier(sid) and len(previous) < MAX_PREVIOUS_SESSIONS:
                    previous.append(NativeBindingSession(hosted_session_id=sid, agent_version=None))
            return row.model_copy(update={"previous_sessions": tuple(previous),
                                          "unfinished_checked": "verified" if complete else "unverified"})

        row = self._update(agent_id, recording)
        if row is not None and row.unfinished_checked is not None:
            log.warning("NATIVE_BINDING_UNFINISHED_CHECKED result=%s sessions=%d", row.unfinished_checked,
                        len(session_ids))
        return row

    def decide_abandon(self, agent_id: str, *, hosted_session_id: str, basis: str, source_status: str,
                       created_agent_version: str, state_profile: str = NATIVE_STATE_PROFILE) -> NativeBindingAbandon | None:
        """ADR-0009 §7a (g): write the decision once for the current SID (claiming an unclaimed Agent in the same
        insert, with ``state_profile``). An existing decision is returned unchanged, never replaced; None when the row
        names another SID."""

        def deciding(row):
            if row is None:
                row = NativeBindingRow(
                    agent_id=agent_id, hosted_session_id=hosted_session_id, created_agent_version=created_agent_version,
                    state_profile=state_profile,
                    observed=NativeBindingObservation(
                        agent_version=created_agent_version, observed_at=self._now(), source="claim"))
            elif row.hosted_session_id != hosted_session_id or row.abandon is not None:
                return None
            return row.model_copy(update={"abandon": NativeBindingAbandon(
                hosted_session_id=hosted_session_id, basis=basis, source_status=source_status, decided_at=self._now())})

        row = self._update(agent_id, deciding)
        if row is None or row.hosted_session_id != hosted_session_id or row.abandon is None:
            return None
        log.warning("NATIVE_ENVIRONMENT_SOURCE_ABANDON_DECIDED session_id=%s basis=%s source_status=%s",
                    hosted_session_id, row.abandon.basis, row.abandon.source_status)
        return row.abandon


SESSION_GUARD_ENV = "FA_NATIVE_SESSION_GUARD_ENFORCE"


def session_guard_enforced(environ=None) -> bool:
    """``FA_NATIVE_SESSION_GUARD_ENFORCE=1`` (default off, observe only): the API's zero-call need is enforced.

    ADR-0009 §7a (k): it switches on at phase 2d-i, together with the in-request switch (the destination's short
    switch and in-request wait), when an incompatible session has a seamless way out; before that, blocking a call
    would only leave a refusal. Any other value than ``0``/``1`` keeps it off and is logged."""
    value = (os.environ if environ is None else environ).get(SESSION_GUARD_ENV)
    if value not in (None, "", "0", "1"):
        log.warning("NATIVE_SESSION_GUARD_SETTING_INVALID enforced=False")
    return value == "1"


IDLE_UPGRADE_ENV = "FA_NATIVE_IDLE_UPGRADE"


def idle_upgrade_enabled(environ=None) -> bool:
    """``FA_NATIVE_IDLE_UPGRADE=1`` (default off): ADR-0009 phase 4's idle upgrade between compatible new images,
    built on 2d-i's background cut. It needs ``FA_NATIVE_SESSION_GUARD_ENFORCE=1``: the API refuses to start with it
    alone (``ValueError``). Any other value than ``0``/``1`` keeps it off and is logged."""
    environ = os.environ if environ is None else environ
    value = environ.get(IDLE_UPGRADE_ENV)
    if value not in (None, "", "0", "1"):
        log.warning("NATIVE_IDLE_UPGRADE_SETTING_INVALID enabled=False")
    if value != "1":
        return False
    if not session_guard_enforced(environ):
        raise ValueError(f"{IDLE_UPGRADE_ENV}=1 needs {SESSION_GUARD_ENV}=1 (ADR-0009 phase 4 is built on 2d-i)")
    return True


class SessionGuard:
    """ADR-0009 §7a zero-call check, run before any request leaves the API for a session: invocations, Session Files
    calls, Responses calls naming a session, and ``stop_session``. It finds the binding row's current SID whose
    recorded profile is not supported.

    **Observe only by default** (Neal 2026-10-13: an active incompatible source must never cause a user-visible
    refusal; the ADR revision makes that state impossible by deploy ordering, expand/contract). It logs
    ``NATIVE_SESSION_CALL_INCOMPATIBLE`` and lets the call proceed. ``enforce=True`` is the integration point that
    refuses the call before anything is sent (503 ``AGENT_VERSION_FOLLOWING``, ``not_dispatched``); it follows
    ``session_guard_enforced()`` (default off; on at phase 2d-i with the in-request switch).

    Observing never reads the Table on a request path: it logs only SIDs the follow already found incompatible
    (``mark_incompatible``). Enforcing reads the row: a SID's recorded profile does not change while it is current,
    so an incompatible verdict is kept for the process and any other verdict is read again after ``ALLOW_SECONDS``
    (one Table read per SID per minute, not per call). An unreadable row allows and logs, like every phase 1 binding
    read."""
    ALLOW_SECONDS = 60

    def __init__(self, store: Callable[[], "NativeBindingStore"], *, clock: Callable[[], float] = monotonic,
                 enforce: bool = False):
        self.store, self.clock, self.enforce = store, clock, enforce
        self.refused: set[tuple[str, str]] = set()
        self.allowed: dict[tuple[str, str], float] = {}
        self.lock = Lock()

    def mark_incompatible(self, agent_id: str, hosted_session_id: str):
        """A SID this API found incompatible another way (its version definition's profile)."""
        with self.lock:
            self.refused.add((agent_id, hosted_session_id))
            self.allowed.pop((agent_id, hosted_session_id), None)

    def check(self, agent_id: str, hosted_session_id: str, *, operation: str):
        key = (agent_id, hosted_session_id)
        with self.lock:
            refused = key in self.refused
            fresh = not refused and self.clock() - self.allowed.get(key, float("-inf")) < self.ALLOW_SECONDS
        # Observing reads no Table on the request path: only SIDs a follow already found incompatible are logged.
        if not refused and not fresh and self.enforce:
            try:
                row = self.store().read(agent_id)
            except Exception as error:  # noqa: BLE001 - an unreadable row allows; see the class docstring
                code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
                log.warning("NATIVE_SESSION_GUARD_UNREAD operation=%s code=%s", operation, code)
                with self.lock:
                    self.allowed[key] = self.clock()
                return
            refused = row is not None and row.hosted_session_id == hosted_session_id and not row.supported()
            with self.lock:
                if refused:
                    self.refused.add(key)
                else:
                    self.allowed[key] = self.clock()
        if refused:
            log.warning("NATIVE_SESSION_CALL_INCOMPATIBLE operation=%s session_id=%s enforced=%s",
                        operation, hosted_session_id, self.enforce)
            if not self.enforce:
                return
            # The code every caller already handles while an Agent changes environments: nothing was sent, so the
            # same RID and body may be sent again once the Agent is on a compatible session.
            raise Problem(503, "AGENT_VERSION_FOLLOWING",
                          "this Agent's session is not compatible with this service; nothing was sent to it",
                          not_dispatched=True)


def check_session(client, hosted_session_id: str | None = None, *, operation: str):
    """The zero-call check for ``client`` (a Foundry client carrying its composition's guard slot)."""
    slot = getattr(client, "_session_guard", None)
    guard = None if slot is None else slot[0]
    if guard is not None:
        guard.check(client.target.agent_id, hosted_session_id or client.target.session_id, operation=operation)


def binding_store(composition) -> NativeBindingStore:
    owner = getattr(composition, "_agent_message_owner", None) or composition
    control = composition.runtime.work_policy
    if control is None:
        raise Problem(503, "NATIVE_BINDING_UNAVAILABLE", "the original Agent Team is unavailable")
    runtime = composition.runtime
    store = NativeBindingStore(TableRepository(owner.clients[0]), tenant_id=str(composition.config.identity.tenant_id),
                               team_id=control.policy.template.team_id,
                               clock=getattr(runtime, "_now", _utc_now))
    if store.partition_key == composition.config.table.scope.partition_key:
        raise Problem(503, "NATIVE_BINDING_UNAVAILABLE", "binding rows cannot share the legacy authority inventory")
    return store
