"""Routine configuration and attribution without API or SDK dependencies."""

from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, JsonValue, field_validator

from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem


class DailyRoutineDefinition(WireModel):
    routine_id: Identifier
    owner_oid: Identifier
    approved_version: Identifier
    time_zone: Annotated[str, Field(min_length=1, max_length=64)]
    local_time: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
    content: Annotated[str, Field(min_length=1)]
    trigger_data: dict[str, JsonValue] = Field(default_factory=dict)
    notification_policy: Literal["original-conversation"] = "original-conversation"
    enabled: bool = False


class RoutineOccurrence(WireModel):
    routine_id: Identifier
    approved_version: Identifier
    time_zone: str
    scheduled_for: datetime
    definition_sha256: Digest
    notification_policy: Literal["original-conversation"] = "original-conversation"

    @field_validator("scheduled_for")
    @classmethod
    def utc_occurrence(cls, value):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("a stored occurrence requires its unique UTC instant")
        return value


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
