"""Owner-approved native routine definitions on the existing metadata transaction store."""

import json
import logging
from datetime import datetime
from time import monotonic
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_native_turn_api import NativeTurnGateway, older_image
from .behavior_native_capture import pinned_to, resolve_routine_capture
from .behavior_native_responses_api import submit_native_turn, turn_profile
from .behavior_native_turn_wire import (
    NativeAgentCommand, NativeConversationRead, NativeRead, NativeRoutineSubmit, material_digest,
)
from .behavior_operations import control_key
from .behavior_routine import _occurrence, latest_occurrence
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_team_registry import IdentityRegistration, identity_key
from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .native_agent_api import NativeAgentRelayGateway
from .native_routine_auth import (
    NativeRoutineApiClock, routine_clock_enabled,
)
from .native_routine_wire import (
    NativeOnceSchedule, NativeReminderQuery, NativeRoutineChange, NativeRoutineCheck, NativeRoutineCurrent, NativeRoutineDefinition,
    NativeRoutineDispatch, NativeRoutineDue, NativeRoutineDuePage,
    NativeRoutineIssue, NativeRoutineList, NativeRoutineOrigin,
    NativeRoutineTrigger, NativeRoutineView, NativeWorkReminder, NativeWorkReminders,
    native_routine_request_id, native_routines_enabled, routine_origin_for,
)


class NativeRoutineRecord(WireModel):
    scope_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=2)]
    agent_id: Identifier

    @model_validator(mode="after")
    def original_scope(self):
        if len(self.scope_ids) == 2 and self.scope_ids[1] != self.agent_id:
            raise ValueError("routine metadata must retain its original Agent scope")
        return self


class NativeRoutineIndex(NativeRoutineRecord):
    kind: Literal["native_routine_index"] = "native_routine_index"
    routine_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def unique(self):
        if self.routine_ids != tuple(sorted(set(self.routine_ids))):
            raise ValueError("routine names must be unique and sorted")
        return self


class NativeRoutineHead(NativeRoutineRecord):
    kind: Literal["native_routine_head"] = "native_routine_head"
    routine_id: Identifier
    version_id: Identifier
    pending: tuple[Identifier, ...] = Field(default=(), exclude_if=lambda value: not value)
    last_scheduled_for: datetime | None = Field(default=None, exclude_if=lambda value: value is None)


class NativeRoutineVersion(NativeRoutineRecord):
    kind: Literal["native_routine_version"] = "native_routine_version"
    definition: NativeRoutineDefinition


class NativeRoutineIntent(NativeRoutineRecord):
    kind: Literal["native_routine_intent"] = "native_routine_intent"
    submission: NativeRoutineSubmit


def _same_occurrence(original, submission):
    """``original`` is the receipt of this intent. The SDK start pin is not part of the occurrence: the intent keeps
    the pin it was resolved with, and ``submit_native_turn`` fills the receiving SID's version's at dispatch
    (ADR-0009 2d-i). So the intent is compared with the receipt's pin and every other byte of its submission."""
    capture = pinned_to(submission.capture, original.capture.sdk_start_code_sha256)
    return (original.submit_digest == submission.model_copy(update={"capture": capture}).sha256
            and original.routine == submission.routine)


def routine_key(control, kind, identifier):
    return control_key("native-routine-" + kind, *control.policy.scope_ids, identifier)


def _read(record, model):
    if record is None:
        return None
    try:
        return model.model_validate_json(json.dumps(record.payload["data"], allow_nan=False))
    except (ValueError, KeyError, TypeError):
        raise Problem(409, "NATIVE_ROUTINE_INVALID", "the original routine metadata is invalid") from None


def _scoped(control, agent_id, record, model):
    value = _read(record, model)
    if value is not None and (value.scope_ids, value.agent_id) != (control.policy.scope_ids, agent_id):
        raise Problem(403, "NATIVE_ROUTINE_SCOPE", "the saved routine metadata belongs to another Agent")
    return value


def _write(key, value, record):
    return ControlWrite(row_id=key, mode="create" if record is None else "replace",
                        data=value.model_dump(mode="json"))


class NativeRoutineService:
    def __init__(self, composition, *, enabled):
        self.owner = getattr(composition, "_agent_message_owner", None) or composition
        self.enabled = enabled

    def _context(self, principal, agent_id, *, write=False):
        if not self.enabled or not native_routines_enabled():
            raise Problem(403, "NATIVE_ROUTINES_DISABLED", "native routine definitions are not enabled")
        composition = self.owner.for_agent(agent_id)
        control = composition.runtime.work_policy
        if control is None:
            raise Problem(503, "NATIVE_ROUTINES_DISABLED", "the original Agent settings are unavailable")
        control._human(principal, "approve" if write else "read")
        self._current(control, write=write)
        return composition, control

    def _owner_key(self, control):
        binding = control.policy.template
        return (None if self.owner.verifier.teams is None else
                identity_key(binding.tenant_id, binding.human_oid))

    def _authority_keys(self, control):
        owner_key = self._owner_key(control)
        return (control.policy_key, control.revocation_key, *((owner_key,) if owner_key is not None else ()))

    def _current(self, control, *, write=False, rows=None):
        if not self.enabled or not native_routines_enabled():
            raise Problem(403, "NATIVE_ROUTINES_DISABLED", "native routines are not enabled")
        policy = control.installed(rows)
        if policy != control.policy or not policy.read_enabled or write and not policy.active:
            raise Problem(403, "WORK_POLICY_REVOKED", "the current original routine authority is required")
        teams = self.owner.verifier.teams
        if teams is not None:
            binding = policy.template
            owner = (teams.owner(binding.human_oid) if rows is None else
                     _read(rows[self._owner_key(control)], IdentityRegistration))
            if owner is None and binding.human_oid == teams.legacy_owner.oid:
                owner = teams.legacy_owner
            if owner is None or not owner.active or (owner.kind, owner.tenant_id, owner.team_id, owner.oid) != (
                    "owner", binding.tenant_id, binding.team_id, binding.human_oid):
                raise Problem(403, "NATIVE_ROUTINE_INACTIVE", "the original routine owner is no longer active")
        return policy

    @staticmethod
    def _scope(control, agent_id, definition):
        binding = control.policy.template
        if (definition.tenant_id, definition.team_id, definition.owner_oid, definition.agent_id) != (
                binding.tenant_id, binding.team_id, binding.human_oid, agent_id):
            raise Problem(403, "NATIVE_ROUTINE_SCOPE", "the routine belongs to another original owner or Agent")
        return definition

    def _version(self, control, agent_id, version_id):
        saved = _scoped(control, agent_id, control.store.read_record(routine_key(control, "version", version_id)), NativeRoutineVersion)
        if saved is None:
            raise Problem(404, "NATIVE_ROUTINE_MISSING", "the original routine version is unavailable")
        definition = self._scope(control, agent_id, saved.definition)
        if definition.request_id != version_id:
            raise Problem(409, "NATIVE_ROUTINE_INVALID", "the version differs from its original key")
        return definition

    @staticmethod
    def _view(definition):
        return NativeRoutineView(definition=definition, definition_sha256=definition.sha256)

    def _current_definition(self, control, agent_id, routine_id):
        head = _scoped(control, agent_id, control.store.read_record(routine_key(control, "head", routine_id)), NativeRoutineHead)
        if head is None:
            raise Problem(404, "NATIVE_ROUTINE_MISSING", "the original routine is unavailable")
        definition = self._version(control, agent_id, head.version_id)
        if (head.routine_id, definition.routine_id) != (routine_id, routine_id):
            raise Problem(409, "NATIVE_ROUTINE_INVALID", "the routine differs from its original head")
        return definition

    def read(self, principal, agent_id, routine_id):
        _, control = self._context(principal, agent_id)
        return control.store.consistent_read(
            lambda: self._view(self._current_definition(control, agent_id, routine_id)))

    def list(self, principal, agent_id, after=None):
        _, control = self._context(principal, agent_id)

        def read():
            index = _scoped(control, agent_id, control.store.read_record(routine_key(control, "index", "definitions")), NativeRoutineIndex)
            identifiers = () if index is None else tuple(key for key in index.routine_ids if after is None or key > after)
            values = tuple(self._view(self._current_definition(control, agent_id, key)) for key in identifiers[:20])
            return NativeRoutineList(definitions=values, next_after=identifiers[19] if len(identifiers) > 20 else None)

        return control.store.consistent_read(read)

    def publish(self, principal, agent_id, command):
        command = NativeRoutineChange.model_validate_json(command.model_dump_json())
        composition, control = self._context(principal, agent_id, write=True)
        version_key = routine_key(control, "version", command.request_id)
        prior = _scoped(control, agent_id, control.store.read_record(version_key), NativeRoutineVersion)
        if prior is not None:
            original = self._scope(control, agent_id, prior.definition)
            if original.model_dump(mode="json", include=set(NativeRoutineChange.model_fields)) != command.model_dump(mode="json"):
                raise Problem(409, "NATIVE_ROUTINE_CONFLICT", "the original definition request already names different content")
            return self._view(original)
        gateway = NativeTurnGateway(composition, enabled=self.enabled)
        gateway.read(principal, NativeConversationRead(conversation_id=command.conversation_id))
        if command.related_request_id is not None:
            related, _ = gateway.read(principal, NativeRead(request_id=command.related_request_id))
            if related.conversation_id != command.conversation_id:
                raise Problem(403, "NATIVE_ROUTINE_SCOPE", "a reminder or related work must stay in its original conversation")
        now = control.runtime._now()
        if command.enabled and isinstance(command.schedule, NativeOnceSchedule) and command.schedule.scheduled_for < now:
            raise Problem(409, "ROUTINE_OCCURRENCE_STALE", "an unregistered past reminder cannot be backfilled")
        binding = control.policy.template
        definition = NativeRoutineDefinition(
            **command.model_dump(), tenant_id=binding.tenant_id, team_id=binding.team_id,
            agent_id=agent_id, owner_oid=principal.oid, approved_at=now)
        head_key = routine_key(control, "head", command.routine_id)
        index_key = routine_key(control, "index", "definitions")
        scope = {"scope_ids": control.policy.scope_ids, "agent_id": agent_id}
        request = ControlRequest(
            request_id=routine_key(control, "publish", command.request_id),
            request_digest=material_digest({"owner": principal.oid, "agent": agent_id, "command": command.model_dump(mode="json")}),
            read_set=(*self._authority_keys(control), head_key, version_key, index_key))

        def changes(rows):
            control._human(principal, "approve")
            self._current(control, write=True, rows=rows)
            head = _scoped(control, agent_id, rows[head_key], NativeRoutineHead)
            if (None if head is None else head.version_id) != command.expected_version_id:
                raise Problem(409, "NATIVE_ROUTINE_CHANGED", "read the current routine version before changing it")
            if rows[version_key] is not None:
                raise Problem(409, "NATIVE_ROUTINE_CONFLICT", "this immutable definition request already exists")
            index = _scoped(control, agent_id, rows[index_key], NativeRoutineIndex) or NativeRoutineIndex(**scope)
            if (head is not None) != (command.routine_id in index.routine_ids):
                raise Problem(409, "NATIVE_ROUTINE_INVALID", "the original routine head and index differ")
            index = NativeRoutineIndex(**scope, routine_ids=tuple(sorted(set((*index.routine_ids, command.routine_id)))))
            return ControlChanges(
                writes=(_write(version_key, NativeRoutineVersion(**scope, definition=definition), None),
                        _write(head_key, (NativeRoutineHead(**scope, routine_id=command.routine_id, version_id=command.request_id)
                                          if head is None else head.model_copy(update={"version_id": command.request_id})),
                               rows[head_key]), _write(index_key, index, rows[index_key])),
                result={"routine_id": command.routine_id, "version_id": command.request_id})

        control.store.transact(request, changes)
        return self._view(self._version(control, agent_id, command.request_id))

    def _clock(self, clock):
        if (not self.enabled or not isinstance(clock, NativeRoutineApiClock) or not routine_clock_enabled()
                or clock.tenant_id != self.owner.verifier.auth.tenant_id):
            raise Problem(403, "NATIVE_ROUTINE_PRODUCER_DENIED", "only the API's own routine clock may dispatch")

    def _active_definition(self, control, agent_id, definition, *, rows=None):
        self._current(control, write=True, rows=rows)
        key = routine_key(control, "head", definition.routine_id)
        head = _scoped(control, agent_id, control.store.read_record(key) if rows is None else rows[key], NativeRoutineHead)
        if head is None or head.version_id != definition.request_id or not definition.enabled:
            raise Problem(403, "NATIVE_ROUTINE_INACTIVE", "the original approved routine version is not active")
        self._scope(control, agent_id, definition)
        return head

    def _intent(self, control, agent_id, request_id):
        value = _scoped(control, agent_id, control.store.read_record(routine_key(control, "intent", request_id)), NativeRoutineIntent)
        if value is not None:
            source = self._scope(control, agent_id, value.submission.routine)
            if source.request_id != request_id:
                raise Problem(409, "NATIVE_ROUTINE_INVALID", "the occurrence differs from its original key")
        return value

    @staticmethod
    def _original(client, rid):
        try:
            return NativeTurnGateway._invoke(client, NativeRead(request_id=rid))[0]
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NATIVE_RID_MISSING":
                return None
            raise

    @staticmethod
    def _dispatch(receipt):
        occurrence = receipt.routine.occurrence
        return NativeRoutineDispatch(
            routine_id=occurrence.routine_id, scheduled_for=occurrence.scheduled_for,
            request_id=receipt.request_id, conversation_id=receipt.conversation_id,
            status=receipt.status, failure_code=receipt.failure_code, cancel_requested=receipt.cancel_requested)

    def _reserve(self, control, agent_id, definition, submission):
        rid = submission.command.request_id
        intent_key = routine_key(control, "intent", rid)
        head_key = routine_key(control, "head", definition.routine_id)
        request = ControlRequest(
            request_id=routine_key(control, "reserve", rid), request_digest=submission.sha256,
            read_set=(*self._authority_keys(control), head_key, intent_key))

        def changes(rows):
            head = self._active_definition(control, agent_id, definition, rows=rows)
            previous = _scoped(control, agent_id, rows[intent_key], NativeRoutineIntent)
            if previous is not None:
                if previous.submission != submission:
                    raise Problem(409, "NATIVE_ROUTINE_CONFLICT", "the occurrence already names different submitted input")
                return ControlChanges(result={"request_id": rid})
            scheduled = submission.routine.occurrence.scheduled_for
            head = head.model_copy(update={
                "pending": tuple(sorted(set((*head.pending, rid)))),
                "last_scheduled_for": max(scheduled, head.last_scheduled_for or scheduled),
            })
            return ControlChanges(
                writes=(_write(intent_key, NativeRoutineIntent(
                    scope_ids=control.policy.scope_ids, agent_id=agent_id, submission=submission), None),
                        _write(head_key, head, rows[head_key])), result={"request_id": rid})

        control.store.transact(request, changes)
        retained = self._intent(control, agent_id, rid)
        if retained is None:
            raise Problem(503, "NATIVE_ROUTINE_UNCONFIRMED", "the original occurrence intent is unconfirmed", request_id=rid)
        return retained

    def _settle(self, control, receipt):
        if receipt.status not in {"completed", "unknown", "busy_refused"}:
            return
        rid = receipt.request_id
        head_key = routine_key(control, "head", receipt.routine.occurrence.routine_id)
        request = ControlRequest(
            request_id=routine_key(control, "settle", rid),
            request_digest=material_digest({"request_id": rid, "submit_digest": receipt.submit_digest}),
            read_set=(head_key,))

        def changes(rows):
            head = _scoped(control, receipt.agent_id, rows[head_key], NativeRoutineHead)
            if head is None:
                raise Problem(409, "NATIVE_ROUTINE_INVALID", "the original occurrence has no routine head")
            writes = ()
            if rid in head.pending:
                writes = (_write(head_key, head.model_copy(update={
                    "pending": tuple(item for item in head.pending if item != rid)}), rows[head_key]),)
            return ControlChanges(writes=writes, result={"request_id": rid})

        control.store.transact(request, changes)

    @staticmethod
    def _due_time(definition, now, issues):
        if isinstance(definition.schedule, NativeOnceSchedule):
            return definition.schedule.scheduled_for
        try:
            return latest_occurrence(definition.schedule, now)
        except Problem as error:
            code = error.body["error"]["code"]
            if code not in {"ROUTINE_LOCAL_TIME_MISSING", "ROUTINE_LOCAL_TIME_AMBIGUOUS"}:
                raise
            logging.getLogger(__name__).warning("FA_NATIVE_ROUTINE_TIME_REFUSED code=%s", code)
            issues.append(NativeRoutineIssue(routine_id=definition.routine_id, code=code))
            return None

    def due(self, clock, agent_id, after=None):
        self._clock(clock)
        control = self.owner.for_agent(agent_id).runtime.work_policy
        self._current(control)
        binding = control.policy.template

        def read():
            self._current(control)
            index = _scoped(control, agent_id, control.store.read_record(routine_key(control, "index", "definitions")), NativeRoutineIndex)
            values, issues = {}, []
            now = control.runtime._now()
            for identifier in (() if index is None else index.routine_ids):
                definition = self._current_definition(control, agent_id, identifier)
                head = _scoped(control, agent_id, control.store.read_record(routine_key(control, "head", identifier)), NativeRoutineHead)
                if not definition.enabled or definition.kind != "routine":
                    continue
                for rid in head.pending:
                    intent = self._intent(control, agent_id, rid)
                    if intent is None:
                        raise Problem(409, "NATIVE_ROUTINE_INVALID", "the pending occurrence has no original intent")
                    occurrence = intent.submission.routine.occurrence
                    if occurrence.approved_version == definition.request_id:
                        values[rid] = NativeRoutineDue(
                            routine_id=identifier, scheduled_for=occurrence.scheduled_for, request_id=rid, registered=True)
                scheduled = self._due_time(definition, now, issues)
                if scheduled is None or scheduled < definition.approved_at or (
                        head.last_scheduled_for is not None and scheduled <= head.last_scheduled_for):
                    continue
                rid = native_routine_request_id(binding.tenant_id, binding.team_id, agent_id, identifier, scheduled)
                values.setdefault(rid, NativeRoutineDue(
                    routine_id=identifier, scheduled_for=scheduled, request_id=rid, registered=False))
            selected = sorted(key for key in values if after is None or key > after)
            return NativeRoutineDuePage(
                occurrences=tuple(values[key] for key in selected[:20]),
                next_after=selected[19] if len(selected) > 20 else None,
                issues=tuple(issues) if after is None else ())

        return control.store.consistent_read(read)

    def reminders(self, principal, agent_id, command):
        command = NativeReminderQuery.model_validate_json(command.model_dump_json())
        composition, control = self._context(principal, agent_id)
        gateway = NativeTurnGateway(composition, enabled=self.enabled)
        gateway.read(principal, NativeConversationRead(conversation_id=command.conversation_id))
        return self._reminders(
            control, agent_id, command, lambda rid: gateway.read(principal, NativeRead(request_id=rid))[0])

    def agent_reminders(self, principal, command):
        command = NativeReminderQuery.model_validate_json(command.model_dump_json())
        relay = NativeAgentRelayGateway(self.owner, enabled=self.enabled)
        scope = relay._sender_scope(principal)
        composition, client = relay._client(scope)
        NativeTurnGateway._invoke(client, NativeConversationRead(conversation_id=command.conversation_id))
        return self._reminders(
            composition.runtime.work_policy, scope.target.agent_id, command,
            lambda rid: NativeTurnGateway._invoke(client, NativeRead(request_id=rid))[0], actionable_only=True)

    @staticmethod
    def _reminder_receipt(read_turn, routine_id, request_id, issues):
        try:
            return read_turn(request_id)
        except Problem as error:
            if error.status != 404 or error.body["error"]["code"] != "NATIVE_RID_MISSING":
                raise
            logging.getLogger(__name__).warning("FA_NATIVE_ROUTINE_RECEIPT_UNCONFIRMED code=NATIVE_RID_MISSING")
            issues.append(NativeRoutineIssue(
                routine_id=routine_id, request_id=request_id, code="NATIVE_RID_MISSING"))
            return None

    def _reminders(self, control, agent_id, command, read_turn, *, actionable_only=False):
        def definitions():
            self._current(control)
            index = _scoped(control, agent_id, control.store.read_record(routine_key(control, "index", "definitions")), NativeRoutineIndex)
            values = []
            for identifier in (() if index is None else index.routine_ids):
                definition = self._current_definition(control, agent_id, identifier)
                head = _scoped(control, agent_id, control.store.read_record(
                    routine_key(control, "head", identifier)), NativeRoutineHead)
                values.append((definition, head))
            return values

        values, turns, issues = {}, {}, []
        now = control.runtime._now()
        for definition, head in control.store.consistent_read(definitions):
            references = set(head.pending)
            if head.last_scheduled_for is not None:
                references.add(native_routine_request_id(
                    definition.tenant_id, definition.team_id, agent_id, definition.routine_id, head.last_scheduled_for))
            for rid in sorted(references):
                intent = self._intent(control, agent_id, rid)
                if intent is None:
                    raise Problem(409, "NATIVE_ROUTINE_INVALID", "the original scheduled reference has no retained intent")
                if intent.submission.command.conversation_id != command.conversation_id:
                    continue
                original = self._reminder_receipt(read_turn, definition.routine_id, rid, issues)
                if original is None:
                    continue
                if original.agent_id != agent_id or not _same_occurrence(original, intent.submission):
                    raise Problem(409, "NATIVE_ROUTINE_CONFLICT", "the scheduled observation differs from its original receipt")
                observed = self._dispatch(original)
                if not actionable_only or observed.needs_attention:
                    turns[rid] = observed
            if not definition.enabled or definition.kind != "reminder" or definition.conversation_id != command.conversation_id:
                continue
            due = self._due_time(definition, now, issues)
            if due is None or due > now or due < definition.approved_at:
                continue
            original = self._reminder_receipt(read_turn, definition.routine_id, definition.related_request_id, issues)
            if original is None:
                continue
            if (original.agent_id, original.conversation_id) != (agent_id, command.conversation_id):
                raise Problem(403, "NATIVE_ROUTINE_SCOPE", "the original reminder target belongs to another conversation")
            if (original.cancel_requested or original.failure_code == "NATIVE_TURN_CANCELLED"
                    or original.status in {"completed", "busy_refused"}):
                continue
            if original.request_id in values and values[original.request_id].scheduled_for >= due:
                continue
            values[original.request_id] = NativeWorkReminder(
                routine_id=definition.routine_id,
                occurrence_id=native_routine_request_id(
                    definition.tenant_id, definition.team_id, agent_id, definition.routine_id, due),
                scheduled_for=due, agent_id=agent_id, conversation_id=original.conversation_id, request_id=original.request_id,
                status=original.status, failure_code=original.failure_code)
        selected = sorted(key for key in values.keys() | turns.keys() if command.after is None or key > command.after)
        return NativeWorkReminders(
            reminders=tuple(values[key] for key in selected[:20] if key in values),
            routine_turns=tuple(turns[key] for key in selected[:20] if key in turns),
            next_after=selected[19] if len(selected) > 20 else None,
            issues=tuple(issues) if command.after is None else ())

    def check(self, principal, command):
        command = NativeRoutineCheck.model_validate_json(command.model_dump_json())
        scope = NativeAgentRelayGateway(self.owner, enabled=self.enabled)._sender_scope(principal)
        principal.require(scope.team_id, "execute")
        control = self.owner.for_agent(scope.target.agent_id).runtime.work_policy

        def read():
            self._current(control, write=True)
            intent = self._intent(control, scope.target.agent_id, command.request_id)
            if intent is None:
                raise Problem(403, "NATIVE_ROUTINE_SCOPE", "the original registered occurrence is required")
            source = intent.submission.routine
            if (source.conversation_id, source.occurrence.definition_sha256) != (
                    command.conversation_id, command.definition_sha256):
                raise Problem(403, "NATIVE_ROUTINE_SCOPE", "the original occurrence scope or definition differs")
            definition = self._version(control, scope.target.agent_id, source.occurrence.approved_version)
            if source != routine_origin_for(definition, source.occurrence.scheduled_for):
                raise Problem(409, "NATIVE_ROUTINE_INVALID", "the occurrence no longer matches its original definition")
            self._active_definition(control, scope.target.agent_id, definition)
            return NativeRoutineCurrent(**command.model_dump())

        return control.store.consistent_read(read)

    @staticmethod
    def _switched(sessions, raw_client, command, authorize, rid):
        """ADR-0009 2d-i (``FA_NATIVE_SESSION_GUARD_ENFORCE``): a routine never follows on its own any more (M1 step
        2; no ``_destination`` and no follow ``pending``). It waits in this request like every send, running the
        in-request switch of a bound Agent between tries (``advance``, which starts no follow then), and ends this
        occurrence with 503 ``NATIVE_ROUTINE_UNCONFIRMED`` (same RID) when the Agent's session is not current within
        the wait; the clock's next tick triggers the same occurrence again."""
        from .behavior_native_turn_api import wait_in_request

        def located():
            try:
                return sessions.current(raw_client, checked_since=monotonic())
            except Problem as error:
                if error.body["error"]["code"] not in {"NATIVE_SESSION_CHANGED", "AGENT_VERSION_FOLLOWING"}:
                    error.routine_own = True  # raised as before the switch (e.g. a revoked policy)
                    raise
                # A session read, not a dispatch: nothing was sent.
                raise Problem(503, "AGENT_VERSION_FOLLOWING", "the Agent is changing environments",
                              not_dispatched=True) from None

        def switching():
            # The raw row, as ``advance`` reads it: ``bound_row`` is None for an unsupported profile, whose
            # incompatible SID is exactly what the in-request switch leaves (§7a (k)).
            found = sessions._binding_row(raw_client.target.agent_id)
            if found is not None and found[1] is not None:
                sessions.advance(raw_client, command, before_send=authorize)

        try:
            return wait_in_request(located, enforce=True, between=switching)
        except Problem as error:
            if getattr(error, "routine_own", False):
                raise
            raise Problem(503, "NATIVE_ROUTINE_UNCONFIRMED",
                          "the original environment is not ready; its routine was not resubmitted",
                          request_id=rid, environment_code=error.body["error"]["code"]) from None

    def trigger(self, clock, agent_id, command):
        from .behavior_team_registry import resolve_agent_scope
        from .behavior_native_sessions import native_sessions
        from .behavior_native_turn_wire import NativeEnvironmentCommand

        command = NativeRoutineTrigger.model_validate_json(command.model_dump_json())
        self._clock(clock)
        scope = resolve_agent_scope(self.owner.runtime.store, self.owner.config, agent_id)
        composition, raw_client = NativeAgentRelayGateway(self.owner, enabled=self.enabled)._client(scope, prepared=False)
        control = composition.runtime.work_policy
        self._current(control)
        binding = control.policy.template
        rid = native_routine_request_id(
            binding.tenant_id, binding.team_id, agent_id, command.routine_id, command.scheduled_for)
        intent = self._intent(control, agent_id, rid)
        sessions = native_sessions(composition)
        environment_command = None
        try:
            # A routine dispatches work, so (like an owner submit) the selector is read in this request.
            client = sessions.current(raw_client, checked_since=monotonic())
        except Problem as error:
            if error.body["error"]["code"] not in {"NATIVE_SESSION_CHANGED", "AGENT_VERSION_FOLLOWING"}:
                raise
            candidate_definition = (self._current_definition(control, agent_id, command.routine_id) if intent is None
                                    else self._version(control, agent_id, intent.submission.routine.occurrence.approved_version))
            self._active_definition(control, agent_id, candidate_definition)
            version = sessions.selected_version(raw_client)
            environment_command = NativeEnvironmentCommand(
                request_id=material_digest({"routine_environment": agent_id, "version": version}),
                expected_agent_version=version, idle_only=True)

            def authorize_environment():
                self._clock(clock)
                self._active_definition(control, agent_id, candidate_definition)

            if sessions.guard.enforce:
                client = self._switched(sessions, raw_client, environment_command, authorize_environment, rid)
                environment_command = None
            else:
                client = sessions._destination(raw_client, version, authorize_environment)
        original = self._original(client, rid)
        if original is not None and (intent is None or not _same_occurrence(original, intent.submission)):
            raise Problem(409, "NATIVE_ROUTINE_CONFLICT", "the original native receipt has another source")
        if original is not None and original.status != "accepted":
            NativeTurnGateway._accounted(control, (original, None), composition)
            self._settle(control, original)
            return self._dispatch(original)
        definition = (self._current_definition(control, agent_id, command.routine_id) if intent is None else
                      self._version(control, agent_id, intent.submission.routine.occurrence.approved_version))
        if definition.kind != "routine":
            raise Problem(403, "NATIVE_ROUTINE_ATTENTION_ONLY", "reminders only project their original work; no turn is started")

        def current():
            self._clock(clock)
            self._active_definition(control, agent_id, definition)

        current()
        conversation, _ = NativeTurnGateway._invoke(
            client, NativeConversationRead(conversation_id=definition.conversation_id))
        if environment_command is None:
            NativeTurnGateway._same_session(client, conversation)
        if intent is None:
            scheduled = _occurrence(definition.schedule, command.scheduled_for, control.runtime._now())
            if scheduled < definition.approved_at:
                raise Problem(409, "ROUTINE_OCCURRENCE_STALE", "an unregistered occurrence predating approval cannot be backfilled")
            # The conversation's current model, not the project default: a default that differs from a conversation
            # with completed turns is a changed capture (NATIVE_CAPTURE_CHANGED) when model switching is off.
            submitted = NativeAgentCommand(
                request_id=rid, conversation_id=definition.conversation_id, content=definition.content,
                model=conversation.capture.model.id)
            capture = resolve_routine_capture(
                control, composition.assistant.authority, submitted, conversation=conversation, authorize=current)
            source = routine_origin_for(definition, scheduled)
            intent = self._reserve(control, agent_id, definition, NativeRoutineSubmit(
                command=submitted, capture=capture, routine=source, state_profile=turn_profile(capture, conversation=conversation)))
        submission = intent.submission
        if environment_command is not None:
            try:
                environment = sessions.advance(raw_client, environment_command, before_send=current)
                if environment.state not in {"ready", "current"}:
                    # The next minute's tick re-sends this occurrence; nothing reads a progress token any more.
                    return NativeRoutineDispatch(
                        purpose="original-routine-environment-progress", routine_id=command.routine_id,
                        scheduled_for=command.scheduled_for, request_id=rid,
                        conversation_id=submission.command.conversation_id, status="environment_pending")
            except Problem as error:
                raise Problem(error.status, "NATIVE_ROUTINE_UNCONFIRMED",
                              "the original environment is not ready; its routine was not resubmitted",
                              request_id=rid, environment_code=error.body["error"]["code"]) from None
            client = sessions.current(raw_client, checked_since=monotonic())
            conversation, _ = NativeTurnGateway._invoke(
                client, NativeConversationRead(conversation_id=definition.conversation_id))
            NativeTurnGateway._same_session(client, conversation)
            original = self._original(client, rid)
            if original is not None and not _same_occurrence(original, submission):
                raise Problem(409, "NATIVE_ROUTINE_CONFLICT", "the occurrence changed while its environment was being prepared")
            if original is not None and original.status != "accepted":
                NativeTurnGateway._accounted(control, (original, None), composition)
                self._settle(control, original)
                return self._dispatch(original)
        try:
            receipt, _ = submit_native_turn(
                composition, client, submission.command, submission.capture if original is None else original.capture,
                routine=submission.routine, original=original, conversation=conversation, before_send=current)
        except Problem as error:
            if error.status < 500 and not older_image(composition, control, client, error):
                raise
            raise Problem(503, "NATIVE_ROUTINE_UNCONFIRMED",
                          "read the original occurrence; do not replace its RID or replay unknown effects",
                          request_id=rid) from None
        NativeTurnGateway._accounted(control, (receipt, None), composition)
        self._settle(control, receipt)
        return self._dispatch(receipt)


def validate_native_routine_history(inventory, config):
    """Recognize original routine metadata during existing startup/rotation reads; never dispatch."""
    from .behavior_team_registry import TeamOwnership, team_key

    models = {model.model_fields["kind"].default: model for model in (
        NativeRoutineIndex, NativeRoutineHead, NativeRoutineVersion, NativeRoutineIntent)}
    values = {}

    def key(value, kind, identifier):
        return control_key("native-routine-" + kind, *value.scope_ids, identifier)

    def invalid():
        raise Problem(409, "NATIVE_ROUTINE_INVALID", "retained routine metadata differs from its original scope or reference")

    def subject(value, source):
        if source.agent_id != value.agent_id or source.tenant_id != config.identity.tenant_id:
            invalid()
        if source.team_id == config.identity.team_id:
            owner = config.identity.human_oid
        else:
            ownership = _read(inventory.rows.get(team_key(source.tenant_id, source.team_id)), TeamOwnership)
            if ownership is None or (ownership.tenant_id, ownership.team_id) != (source.tenant_id, source.team_id):
                invalid()
            owner = ownership.owner_oid
        if source.owner_oid != owner:
            invalid()

    for identifier, row in inventory.rows.items():
        kind = row.payload["data"].get("kind")
        if not isinstance(kind, str) or not kind.startswith("native_routine_"):
            continue
        if kind not in models:
            invalid()
        value = _read(row, models[kind])
        if (value.scope_ids[0] != config.table.scope.authority_domain
                or len(value.scope_ids) == 1 and value.agent_id not in {target.agent_id for target in config.targets}):
            invalid()
        if isinstance(value, NativeRoutineVersion):
            subject(value, value.definition)
            expected = key(value, "version", value.definition.request_id)
        elif isinstance(value, NativeRoutineIntent):
            subject(value, value.submission.routine)
            expected = key(value, "intent", value.submission.command.request_id)
        elif isinstance(value, NativeRoutineHead):
            expected = key(value, "head", value.routine_id)
        else:
            expected = key(value, "index", "definitions")
        if identifier != expected:
            invalid()
        values[identifier] = value
    for value in values.values():
        if isinstance(value, NativeRoutineIndex):
            for routine_id in value.routine_ids:
                head = values.get(key(value, "head", routine_id))
                if not isinstance(head, NativeRoutineHead) or head.agent_id != value.agent_id:
                    invalid()
        elif isinstance(value, NativeRoutineHead):
            version = values.get(key(value, "version", value.version_id))
            index = values.get(key(value, "index", "definitions"))
            if (not isinstance(version, NativeRoutineVersion) or version.agent_id != value.agent_id
                    or version.definition.routine_id != value.routine_id
                    or not isinstance(index, NativeRoutineIndex) or value.routine_id not in index.routine_ids):
                invalid()
            for rid in value.pending:
                intent = values.get(key(value, "intent", rid))
                if (not isinstance(intent, NativeRoutineIntent) or intent.agent_id != value.agent_id
                        or intent.submission.routine.occurrence.routine_id != value.routine_id):
                    invalid()
        elif isinstance(value, NativeRoutineIntent):
            source = value.submission.routine
            version = values.get(key(value, "version", source.occurrence.approved_version))
            if (not isinstance(version, NativeRoutineVersion) or version.agent_id != value.agent_id
                    or source != routine_origin_for(version.definition, source.occurrence.scheduled_for)):
                invalid()
    return values
