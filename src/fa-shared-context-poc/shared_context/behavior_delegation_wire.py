"""SDK-independent delegation commands; authority comes from the original session ticket."""

import hashlib
from typing import Annotated, Literal
import zlib

from pydantic import Field, field_validator, model_validator

from .behavior_wire import Command, Digest, Identifier, WireModel
from .errors import Problem

CAPABILITY_ID = CHILD_AGENT_TYPE = "fa-delegate-v1"
DelegationStatus = Literal["unknown", "running", "completed", "failed", "cancelled", "incomplete"]
DELEGATION_PREPARE_BYTES = 192 * 1024
DELEGATION_SIZE_REFUSAL = "DELEGATION_PREPARE_TOO_LARGE"
# Full stored text may need six JSON bytes per original control character.
MAX_DELEGATION_BYTES = 6 * 4 * 1024 * 1024 + 16384


class DelegationTask(WireModel):
    agent_type: Literal["fa-delegate-v1"]
    mode: Literal["sync"] = "sync"
    name: Annotated[str, Field(min_length=1, max_length=128)]
    description: Annotated[str, Field(min_length=1)]
    prompt: Annotated[str, Field(min_length=1)]

    @field_validator("name", "description", "prompt")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("nonempty original task text is required")
        return value


def child_configuration(*, read_user_files=False):
    value = {
        "name": CHILD_AGENT_TYPE,
        "description": "One synchronous read-only comparison of the supplied material.",
        "prompt": ("Use only the supplied task and material. You have zero tools. "
                   "Return the complete public result and required references, or an explicit failure. "
                   "Do not claim an action was executed. Stop after the result."),
        "tools": [], "mcp_servers": {}, "skills": [],
    }
    if read_user_files:
        value.update(
            description="One synchronous comparison using original user files in this conversation.",
            prompt=("Use the supplied task and read_user_file for completed human turns in this conversation. "
                    "You have no write, messaging, filesystem or MCP tools. Return the complete public result "
                    "with its original turn and filename references, or an explicit failure. Stop after the result."),
            tools=["read_user_file"])
    return value


class DelegationPrepare(Command):
    request_id: Literal["delegation-prepare"] = "delegation-prepare"
    tool_call_id: Identifier
    arguments: DelegationTask


class DelegationObservation(Command):
    request_id: Literal["delegation-observe"] = "delegation-observe"
    event_id: Identifier
    tool_call_id: Identifier
    kind: Literal["started", "result", "cancel", "parent_idle", "unknown"]
    child_agent_id: Identifier | None = None
    outcome: Literal["completed", "failed", "cancelled"] | None = None
    text: str | None = None
    ended_in_tool_call: bool = False
    failure_code: Identifier | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.kind in {"started", "result", "parent_idle"} and self.child_agent_id is None:
            raise ValueError("the observed original child identity is required")
        if (self.kind == "result") != (self.outcome is not None):
            raise ValueError("only a result observation carries an outcome")
        if self.kind != "result" and (self.text is not None or self.ended_in_tool_call):
            raise ValueError("only a result observation carries public text")
        if (self.kind == "unknown" or self.outcome == "failed") != (self.failure_code is not None):
            raise ValueError("an unresolved or failed observation requires its public code")
        return self


class DelegationModelRef(WireModel):
    tool_call_id: Identifier
    child_agent_id: Identifier


class DelegationReply(WireModel):
    schema_version: Literal["fa-delegation-reply-v1"] = "fa-delegation-reply-v1"
    parent_job_id: Identifier
    sdk_session_id: Identifier
    delegation_id: Identifier
    tool_call_id: Identifier
    input_sha256: Digest
    child_agent_id: Identifier | None
    status: DelegationStatus
    result: str | None
    failure_code: Identifier | None
    fresh: bool = False
    cancel_requested: bool = False
    child_cancelled: bool = False
    parent_idle: bool = False
    stop_confirmed: bool = False

    @model_validator(mode="after")
    def complete(self):
        validate_delegation_outcome(self)
        return self


def validate_delegation_outcome(value):
    if value.fresh and (value.status != "unknown" or value.child_agent_id is not None or value.result is not None):
        raise ValueError("only the original pre-dispatch reservation can be fresh")
    if value.status == "completed" and (not value.result or not value.result.strip() or value.child_agent_id is None):
        raise ValueError("completed must retain a full public child result")
    if value.stop_confirmed != (value.status == "cancelled"):
        raise ValueError("a confirmed stop must be the original cancelled outcome")
    if value.stop_confirmed and not (value.child_cancelled and value.parent_idle):
        raise ValueError("stop confirmation requires both original observations")


class DelegationPublicText(WireModel):
    data: Annotated[bytes, Field(max_length=32768)]
    sha256: Digest
    byte_length: Annotated[int, Field(ge=0, le=4 * 1024 * 1024)]


def pack_delegation_text(text: str | None) -> DelegationPublicText | None:
    if text is None:
        return None
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        raise Problem(422, "DELEGATION_RESULT_INVALID", "child result must be complete UTF-8 text") from None
    if len(raw) > 4 * 1024 * 1024:
        raise Problem(413, "DELEGATION_RESULT_LIMIT", "complete child result exceeds its storage envelope; nothing is truncated")
    data = zlib.compress(raw)
    if len(data) > 32768:
        raise Problem(413, "DELEGATION_RESULT_LIMIT", "complete child result exceeds its storage envelope; nothing is truncated")
    return DelegationPublicText(data=data, sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw))


def unpack_delegation_text(value: DelegationPublicText) -> str:
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(value.data, 4 * 1024 * 1024 + 1)
        text = raw.decode("utf-8")
    except (zlib.error, UnicodeError):
        raise Problem(409, "DELEGATION_RESULT_INVALID", "complete child result bytes are unavailable") from None
    if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or len(raw) != value.byte_length or hashlib.sha256(raw).hexdigest() != value.sha256):
        raise Problem(409, "DELEGATION_RESULT_INVALID", "complete child result bytes differ")
    return text
