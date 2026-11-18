"""Owner-triggered daily occurrences; timing and scheduling stay outside FA."""

import json

from .behavior_assistant_wire import InstructionSubmit
from .behavior_operations import control_key
from .behavior_routine_wire import RoutineOccurrence, _occurrence, _zone
from .errors import Problem
from .source_materials import digest


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

