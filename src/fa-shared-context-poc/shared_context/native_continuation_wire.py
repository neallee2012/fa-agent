"""SDK-independent identities for bounded recovery and original tool operations."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from .behavior_wire import Digest, Identifier, WireModel

MAX_AUTOMATIC_CONTINUATIONS = 2
CONTINUATION_STOP_STATUS = {
    "NATIVE_CONTINUATION_CAPTURE_REVOKED": 403,
    "WORK_POLICY_REVOKED": 403,
    "NATIVE_CONTINUATION_ADVANCED": 409,
}
CONTINUATION_ERRORS = frozenset("NATIVE_CONTINUATION_" + suffix for suffix in (
    "MISSING", "CONFLICT", "UNAVAILABLE", "LIMIT", "ADVANCED", "CHECKPOINT_REQUIRED",
    "WATERMARK_UNAVAILABLE", "WORKSPACE_REQUIRED", "PEER_PENDING", "EFFECT_UNKNOWN", "CAPTURE_REVOKED", "PENDING",
    "UNCONFIRMED",
))


class NativeContinuation(WireModel):
    schema_version: Literal["fa-native-continuation-v1"] = "fa-native-continuation-v1"
    continuation_of: Identifier
    root_request_id: Identifier
    attempt: Annotated[int, Field(ge=1, le=MAX_AUTOMATIC_CONTINUATIONS)]
    source_submit_digest: Digest
    checkpoint_sha256: Digest


class NativeContinuationRead(WireModel):
    operation: Literal["continuation"] = "continuation"
    request_id: Identifier


class NativeContinuationReady(WireModel):
    operation: Literal["continuation_ready"] = "continuation_ready"
    request_id: Identifier
    environment_request_id: Identifier
    hosted_session_id: Identifier
    files_manifest_sha256: Digest


class NativeContinuationReadyReceipt(WireModel):
    agent_id: Identifier
    hosted_session_id: Identifier
    request_id: Identifier
    environment_request_id: Identifier
    pending_request_ids: tuple[Identifier, ...]
    purpose: Literal["environment-ready-not-continuation-acceptance"] = "environment-ready-not-continuation-acceptance"


class NativeContinuationView(WireModel):
    schema_version: Literal["fa-native-continuation-view-v1"] = "fa-native-continuation-view-v1"
    agent_id: Identifier
    conversation_id: Identifier
    original_request_id: Identifier
    root_request_id: Identifier
    request_id: Identifier | None = None
    continuation: NativeContinuation | None = None
    state: Literal["prepared", "admitted", "blocked"]
    failure_code: Identifier | None = None
    source_hosted_session_id: Identifier
    target_hosted_session_id: Identifier | None = None
    cancel_requested: bool = Field(default=False, exclude_if=lambda value: not value)

    @property
    def owns_outcome(self) -> bool:
        return self.continuation is not None and self.state != "blocked"


class NativeContinuationQuery(WireModel):
    section: Literal["overview", "request", "append", "effect", "human"] = "overview"
    index: Annotated[int, Field(ge=0)] = 0
    part: Annotated[int, Field(ge=0)] = 0


class NativeContinuationPage(WireModel):
    schema_version: Literal["fa-native-continuation-context-v1"] = "fa-native-continuation-context-v1"
    root_request_id: Identifier
    continuation_of: Identifier
    attempt: Annotated[int, Field(ge=1, le=MAX_AUTOMATIC_CONTINUATIONS)]
    section: Literal["overview", "request", "append", "effect", "human"]
    index: int
    part: int
    next_part: int | None
    text: Annotated[str, Field(max_length=4096)]
    sha256: Digest
    authorization: Literal[False] = False
    purpose: Literal["original-recovery-data-not-new-instruction"] = "original-recovery-data-not-new-instruction"


class NativeOperation(WireModel):
    schema_version: Literal["fa-native-operation-v1"] = "fa-native-operation-v1"
    root_request_id: Identifier
    operation_id: Digest
    tool_name: Annotated[str, Field(min_length=1, max_length=256)]
    arguments: dict[str, JsonValue]
    binding_digest: Digest
