"""Owner-triggered daily occurrences; timing and scheduling stay outside FA."""

from datetime import datetime, time, timedelta, timezone
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .behavior_assistant_wire import InstructionSubmit
from .behavior_operations import control_key
from .behavior_routine_wire import RoutineOccurrence
from .errors import Problem
from .source_materials import digest


def _instants(local, zone):
    return tuple(sorted({
        instant
        for fold in (0, 1)
        for instant in (local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc),)
        if instant.astimezone(zone).replace(tzinfo=None) == local
    }))


def _unique_instant(local, zone):
    candidates = _instants(local, zone)
    if not candidates:
        raise Problem(422, "ROUTINE_LOCAL_TIME_MISSING", "the scheduled local time does not exist")
    if len(candidates) != 1:
        raise Problem(422, "ROUTINE_LOCAL_TIME_AMBIGUOUS", "the scheduled local time is ambiguous")
    return candidates[0]


def _zone(name):
    try:
        return timezone.utc if name == "UTC" else ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise Problem(503, "ROUTINE_TIME_ZONE_UNAVAILABLE", "the configured time zone is unavailable") from None


def latest_occurrence(definition, now):
    zone = _zone(definition.time_zone)
    local_now = now.astimezone(zone)
    hour, minute = map(int, definition.local_time.split(":"))
    latest = datetime.combine(local_now.date(), time(hour, minute))
    candidates = _instants(latest, zone)
    if (candidates and now < candidates[0]) or (
        not candidates and local_now.replace(tzinfo=None) < latest
    ):
        latest -= timedelta(days=1)
    return _unique_instant(latest, zone)


def _occurrence(definition, supplied, now):
    zone = _zone(definition.time_zone)
    hour, minute = map(int, definition.local_time.split(":"))
    expected = latest_occurrence(definition, now)
    local = supplied if supplied.tzinfo is None else supplied.astimezone(zone).replace(tzinfo=None)
    actual = _unique_instant(local, zone)
    if supplied.tzinfo is not None and supplied.astimezone(timezone.utc) != actual:
        raise Problem(422, "ROUTINE_OCCURRENCE_INVALID", "the submitted time does not match its local occurrence")
    if actual > now:
        raise Problem(409, "ROUTINE_OCCURRENCE_FUTURE", "the occurrence is not due")
    if local.time() != time(hour, minute):
        raise Problem(422, "ROUTINE_OCCURRENCE_INVALID", "the submitted time is not on the approved schedule")
    if actual != expected:
        raise Problem(409, "ROUTINE_OCCURRENCE_STALE", "only the most recent due occurrence can be triggered")
    return expected


def _definition(policy, owner_oid, routine_id):
    if not policy.routines:
        raise Problem(503, "ROUTINE_PRODUCER_DISABLED", "routine production is not enabled")
    matches = [routine for routine in policy.routines if routine.routine_id == routine_id]
    if len(matches) != 1 or matches[0].owner_oid != owner_oid:
        raise Problem(403, "ROUTINE_OWNER_DENIED", "one approved routine owned by this caller is required")
    definition = matches[0]
    if not definition.enabled:
        raise Problem(503, "ROUTINE_PRODUCER_DISABLED", "routine production is not enabled")
    return definition


def _request_id(policy, routine_id, scheduled):
    return control_key("routine-occurrence", *policy.scope_ids, routine_id, scheduled.isoformat())


def _submission(policy, definition, scheduled):
    request_id = _request_id(policy, definition.routine_id, scheduled)
    trigger_data = {
        "routine_id": definition.routine_id, "approved_version": definition.approved_version,
        "time_zone": definition.time_zone, "scheduled_for": scheduled.isoformat(),
        "notification_policy": definition.notification_policy, "data": definition.trigger_data,
    }
    content = definition.content + "\n\nRoutine trigger data (not operation authority):\n" + json.dumps(
        trigger_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )
    occurrence = RoutineOccurrence(
        routine_id=definition.routine_id, approved_version=definition.approved_version,
        time_zone=definition.time_zone, scheduled_for=scheduled,
        definition_sha256=digest(definition.model_dump(mode="json")),
        notification_policy=definition.notification_policy,
    )
    return InstructionSubmit(request_id=request_id, content=content), occurrence


def validate_submission(policy, owner_oid, command, occurrence, *, now=None):
    definition = _definition(policy, owner_oid, occurrence.routine_id)
    scheduled = occurrence.scheduled_for
    if now is not None:
        scheduled = _occurrence(definition, scheduled, now)
    expected_command, expected_occurrence = _submission(policy, definition, scheduled)
    if occurrence != expected_occurrence or command != expected_command:
        raise Problem(403, "ROUTINE_SOURCE_CHANGED", "the original approved routine input differs")


def trigger(control, principal, routine_id, command):
    from .behavior_work_policy import registered_instruction

    control._human(principal, "submit")
    policy = control.installed()
    _, record = _original_occurrence(control, policy, routine_id, command)
    if record is not None:
        original = registered_instruction(record)
        if original is None:
            raise Problem(403, "ROUTINE_SOURCE_CHANGED", "the original routine instruction is unavailable")
        return control.submit(principal, original, routine=record.routine)
    definition = _definition(policy, principal.oid, routine_id)
    scheduled = _occurrence(definition, command.scheduled_for, control.runtime._now())
    submitted, occurrence = _submission(policy, definition, scheduled)
    return control.submit(principal, submitted, routine=occurrence)


def _original_occurrence(control, policy, routine_id, command):
    from .behavior_work_policy import derive_work_binding

    if not policy.active or not policy.read_enabled:
        raise Problem(403, "WORK_POLICY_REVOKED", "current original read rights are revoked")
    supplied = command.scheduled_for
    if supplied.tzinfo is None:
        definitions = [item for item in policy.routines if item.routine_id == routine_id]
        if len(definitions) != 1:
            raise Problem(403, "ROUTINE_SOURCE_CHANGED", "the original local occurrence requires its time zone")
        supplied = _unique_instant(supplied, _zone(definitions[0].time_zone))
    scheduled = supplied.astimezone(timezone.utc)
    request_id = _request_id(policy, routine_id, scheduled)
    _, binding = derive_work_binding(policy, request_id)
    control.refresh(binding.binding_id)
    record = control.records.get(binding.binding_id)
    if record is None:
        return scheduled, None
    if (record.routine is None or record.routine.routine_id != routine_id
            or record.routine.scheduled_for != scheduled):
        raise Problem(403, "ROUTINE_SOURCE_CHANGED", "the original work has a different occurrence source")
    return scheduled, record


def reconcile(control, principal, routine_id, command):
    from .behavior_work_policy import registered_instruction

    control._human(principal)
    policy = control.installed()
    _, record = _original_occurrence(control, policy, routine_id, command)
    if record is None:
        definition = _definition(policy, principal.oid, routine_id)
        scheduled = _occurrence(definition, command.scheduled_for, control.runtime._now())
        original, _ = _submission(policy, definition, scheduled)
        return control.submit_reconcile(principal, original)
    original = registered_instruction(record)
    if original is None:
        raise Problem(403, "ROUTINE_SOURCE_CHANGED", "the original routine instruction is unavailable")
    return control.submit_reconcile(principal, original)
