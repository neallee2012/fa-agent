"""ADR-0009 §2 Agent binding row: where an Agent's session is and which version it actually ran.

One CAS item per Agent in the existing Context Table, written only by the API. It never calls a session and holds
no work, lease or upgrade state (those belong to the ADR-0009 draft); SDK-independent, so the API can import it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Annotated, Callable, Literal

from pydantic import Field, model_validator

from .behavior_native_turn_wire import NATIVE_STATE_PROFILE, NativeEnvironmentNotice, material_digest
from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .storage import Repository, TableRepository, serialize

AgentVersion = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
# Every write is a whole-row replace guarded by the ETag just read; a conflict reads again and recomputes.
WRITE_ATTEMPTS = 4
MAX_BINDING_BYTES = 16 * 1024
MAX_PREVIOUS_SESSIONS = 64
log = logging.getLogger(__name__)


class NativeBindingSession(WireModel):
    hosted_session_id: Identifier
    agent_version: AgentVersion


class NativeBindingObservation(WireModel):
    """``vm``: the VM accepted the version as expected or reported it. ``claim``: platform metadata a claim read,
    which no VM confirms. ``metadata-active`` is ADR-0009 §4 step 6's label; phase 1 never writes it."""
    agent_version: AgentVersion
    observed_at: Annotated[str, Field(min_length=20, max_length=40)]
    source: Literal["claim", "vm", "metadata-active"]


class NativeBindingSwitch(WireModel):
    reason: Literal["version_follow"]
    previous_hosted_session_id: Identifier
    files_migrated: bool | None = None


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
    observed: NativeBindingObservation
    # Only from ``get_session`` (never ``list_sessions``) and only when an existing flow already read it.
    created_at: Annotated[str, Field(max_length=40)] | None = None
    state_profile: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")] = NATIVE_STATE_PROFILE
    previous_sessions: Annotated[tuple[NativeBindingSession, ...], Field(max_length=MAX_PREVIOUS_SESSIONS)] = ()
    last_switch: NativeBindingSwitch | None = None

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
        return self

    def notice(self) -> NativeEnvironmentNotice | None:
        """ADR-0009 §8 "平台把助理的執行環境換成了另一個版本（vX→vY）" while the notice is pending."""
        if not self.notice_pending:
            return None
        return NativeEnvironmentNotice(from_version=self.created_agent_version, to_version=self.notice_to_version)


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
              created_at=None) -> NativeBindingRow:
        """Insert once; an existing row is read back, never overwritten. An ``observed_version`` that already differs
        from the creation version sets the drift flag in this insert: otherwise every later observation would equal
        the recorded drifted version and the flag would never be set (ADR-0009 §2)."""

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
                created_at=_timestamp(created_at)))
            return written[0]

        row = self._update(agent_id, claiming)
        if written and row == written[0]:
            log.warning("NATIVE_BINDING_CLAIMED session_id=%s created_version=%s observed_version=%s drift=%s",
                        hosted_session_id, row.created_agent_version, row.observed.agent_version, row.drift)
        return row

    def record_observed(self, agent_id: str, *, hosted_session_id: str, agent_version: str,
                        source: Literal["vm", "metadata-active"]) -> NativeBindingRow | None:
        """ADR-0009 §4 step 6 drift CAS. Another SID is a binding divergence, not drift. Once the flag is set, later
        versions (a drift back included) change only the recorded version: version numbers are not monotonic."""
        moved, written = [], []

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
            written.append(row.model_copy(update={
                "observed": NativeBindingObservation(agent_version=agent_version, observed_at=self._now(), source=source),
                "drift": row.drift or first, "notice_pending": row.notice_pending or first,
                "notice_to_version": agent_version if first else row.notice_to_version}))
            return written[0]

        row = self._update(agent_id, recording)
        if moved:
            log.warning("NATIVE_BINDING_DIVERGED session_id=%s binding_session_id=%s", hosted_session_id, moved[-1])
        elif written and row == written[0]:
            log.warning("NATIVE_VERSION_OBSERVED source=%s session_id=%s created_version=%s observed_version=%s drift=%s",
                        source, hosted_session_id, row.created_agent_version, agent_version, row.drift)
        return row

    def record_switch(self, agent_id: str, view) -> NativeBindingRow:
        """ADR-0009 §7a (f)/(j). Idempotent, so a crash between the commit and this write is repaired by the next
        follow's rerun; when the commit's previous SID is not the row's (the selector moved before the rerun), both
        are kept as previous SIDs."""

        def switching(row):
            if (row is not None and row.hosted_session_id == view.hosted_session_id
                    and row.created_agent_version == view.hosted_agent_version):
                return None
            previous = list(row.previous_sessions) if row is not None else []
            sources = []
            if row is not None and row.hosted_session_id not in {view.previous_hosted_session_id, view.hosted_session_id}:
                sources.append(NativeBindingSession(
                    hosted_session_id=row.hosted_session_id, agent_version=row.created_agent_version))
            sources.append(NativeBindingSession(
                hosted_session_id=view.previous_hosted_session_id, agent_version=view.previous_agent_version))
            for item in sources:
                if all(kept.hosted_session_id != item.hosted_session_id for kept in previous):
                    previous.append(item)
            previous = [item for item in previous if item.hosted_session_id != view.hosted_session_id]
            if len(previous) > MAX_PREVIOUS_SESSIONS:
                log.warning("NATIVE_BINDING_PREVIOUS_TRIMMED agent_id=%s dropped=%d",
                            agent_id, len(previous) - MAX_PREVIOUS_SESSIONS)
                previous = previous[-MAX_PREVIOUS_SESSIONS:]
            return NativeBindingRow(
                agent_id=agent_id, hosted_session_id=view.hosted_session_id,
                created_agent_version=view.hosted_agent_version,
                observed=NativeBindingObservation(
                    agent_version=view.hosted_agent_version, observed_at=self._now(), source="vm"),
                previous_sessions=tuple(previous),
                last_switch=NativeBindingSwitch(
                    reason="version_follow", previous_hosted_session_id=view.previous_hosted_session_id,
                    files_migrated=view.files_migrated))

        return self._update(agent_id, switching)


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
