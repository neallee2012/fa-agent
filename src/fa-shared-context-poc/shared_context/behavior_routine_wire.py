"""Routine configuration and attribution without API or SDK dependencies."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator

from .behavior_wire import Digest, Identifier, WireModel


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


class RoutineTrigger(WireModel):
    scheduled_for: datetime


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
