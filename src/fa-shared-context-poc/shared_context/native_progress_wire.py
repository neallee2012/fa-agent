"""SDK-independent public activity; never part of a native receipt or checkpoint."""

import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_assistant_wire import PublicProgress
from .behavior_wire import Digest, Identifier, WireModel
from .native_continuation_wire import NativeContinuation


MAX_PROGRESS_BYTES = 8192


def encoded(value):
    # Match the pinned State store's JSON encoding, including ASCII escapes for Unicode.
    return json.dumps(value, allow_nan=False).encode("utf-8")


class NativeProgressRead(WireModel):
    operation: Literal["progress"] = "progress"
    conversation_id: Identifier
    request_id: Identifier


class NativeTurnStatusRead(WireModel):
    """One RID's receipt, public progress and (once completed) its first reply page, in one read."""

    operation: Literal["turn_status"] = "turn_status"
    conversation_id: Identifier
    request_id: Identifier


class NativeProgressSnapshot(WireModel):
    schema_version: Literal["fa-native-public-progress-v1"] = "fa-native-public-progress-v1"
    agent_id: Identifier
    hosted_session_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    submit_digest: Digest
    progress: PublicProgress
    recorded: bool
    finished: bool
    truncated: bool
    unconfirmed: bool = False
    byte_length: Annotated[int, Field(ge=1, le=MAX_PROGRESS_BYTES)]
    continuation: NativeContinuation | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def bounded(self):
        if len(encoded(self.model_dump(mode="json"))) != self.byte_length:
            raise ValueError("The complete public progress envelope byte count differs")
        if not self.recorded and (self.finished or self.progress.events
                                 or self.progress.reasoning_summary.availability == "provided"):
            raise ValueError("Missing progress cannot claim recorded steps or completion")
        if [event.seq for event in self.progress.events] != sorted({event.seq for event in self.progress.events}):
            raise ValueError("Public progress sequences must increase without duplicates")
        summaries = [event.reasoning_summary for event in self.progress.events if event.reasoning_summary is not None]
        if summaries and self.progress.reasoning_summary.text != summaries[-1]:
            raise ValueError("The summary metadata differs from the last public summary")
        if self.finished and self.unconfirmed:
            raise ValueError("A verified finished snapshot cannot remain unconfirmed")
        return self


def progress_snapshot(receipt, progress, *, recorded=True, finished=False, truncated=False, unconfirmed=False):
    value = {
        "schema_version": "fa-native-public-progress-v1",
        "agent_id": receipt.agent_id, "hosted_session_id": receipt.hosted_session_id,
        "conversation_id": receipt.conversation_id, "request_id": receipt.request_id,
        "submit_digest": receipt.submit_digest, "progress": progress.model_dump(mode="json"),
        "recorded": recorded, "finished": finished, "truncated": truncated,
        "unconfirmed": unconfirmed, "byte_length": 1,
    }
    continuation = getattr(receipt, "continuation", None)
    if continuation is not None:
        value["continuation"] = continuation.model_dump(mode="json")
    while True:
        while (size := len(encoded(value))) != value["byte_length"]:
            value["byte_length"] = size
        if size <= MAX_PROGRESS_BYTES:
            return NativeProgressSnapshot.model_validate_json(encoded(value))
        events = value["progress"]["events"]
        if not events:
            raise ValueError("The public progress identity envelope exceeds its fixed bound")
        del events[0]
        value["truncated"] = True
