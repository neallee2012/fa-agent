"""SDK-independent checkpoint DTOs shared by the API and Hosted client."""
import hashlib
import json
import platform
import sys
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .backup_models import MAX_BACKUP_BYTES
from .behavior_wire import WireModel, Identifier, Digest, Command, AssistantSdkStop
from .m3_models import BlobReceipt
from .behavior_checkpoint_privacy import checkpoint_events, validate_checkpoint_files

DIAG_CODEC = "session-fs-diag-empty-v1"
DIAG_ARCHIVE_CODEC = "artifact-session-fs-diag-empty-v1"
RESPONSES_CODEC = "session-fs-responses-v1"
RESPONSES_DIAG_CODEC = "session-fs-responses-diag-v1"
DIAG_CODECS = frozenset({DIAG_CODEC, RESPONSES_DIAG_CODEC})
RESPONSES_CODECS = frozenset({RESPONSES_CODEC, RESPONSES_DIAG_CODEC})
ARCHIVE_CODECS = {
    "session-fs-v1": "artifact-session-fs-v1",
    DIAG_CODEC: DIAG_ARCHIVE_CODEC,
    RESPONSES_CODEC: "artifact-session-fs-responses-v1",
    RESPONSES_DIAG_CODEC: "artifact-session-fs-responses-diag-v1",
}
DIAG_ARCHIVE_CODECS = frozenset(ARCHIVE_CODECS[name] for name in DIAG_CODECS)
RESPONSES_ARCHIVE_CODECS = frozenset(ARCHIVE_CODECS[name] for name in RESPONSES_CODECS)
SDK_STATE_HTTP_BYTES = MAX_BACKUP_BYTES + 4096
SDK_STATE_ERRORS = frozenset({
    "SDK_STATE_INCOMPLETE", "SDK_STATE_UPLOAD_UNKNOWN", "SDK_STATE_PRIVATE_MATERIAL",
    "SDK_STATE_UNSUPPORTED_FORMAT", "SDK_STATE_SOURCE_UNVERIFIED",
})
ModelProvider = Literal["openai-responses"]


AUTO_PROFILE = "auto-no-custom-v1"


class CheckpointProfile(WireModel):
    # manual: one FA-requested compaction (CTX-03). auto (native lane only): the pinned runtime's own threshold
    # compactions, saved as ordinary state; they write only their start/complete events and the summary.
    name: Literal["manual-no-custom-v1", "auto-no-custom-v1"] = "manual-no-custom-v1"
    platform: Literal["win32-x64", "linux-x64"] = "win32-x64"
    provider: Literal["openai-completions", "openai-responses"] = "openai-completions"


def local_platform():
    """This process's checkpoint platform, or None where none is supported (SdkSessionFiles uses the same mapping)."""
    return {
        ("win32", "amd64"): "win32-x64",
        ("linux", "x86_64"): "linux-x64",
        ("linux", "amd64"): "linux-x64",
    }.get((sys.platform, platform.machine().lower()))


def native_default_checkpoint(model_provider, worker_profile=None):
    """B1 TC: since 2026-10-03, a new native conversation (and a new Agent's first selection) that chose no
    checkpoint profile uses the runtime's own compaction. Explicit choices are never replaced.

    The platform is this server's: the deployed API and Foundry Hosted both run linux-x64, and local runs keep both
    on one machine. A mismatch is refused by the Hosted preflight before any model call. None where none may be set.
    """
    current = local_platform()
    if worker_profile is not None or current is None:
        return None
    return CheckpointProfile(name=AUTO_PROFILE, platform=current, provider=model_provider or "openai-completions")


def validate_provider_checkpoint(provider, checkpoint):
    if checkpoint is not None and checkpoint.provider != (provider or "openai-completions"):
        raise ValueError("Checkpoint provider must match the original conversation provider")


class CompactionExecution(WireModel):
    """Hosted observation, not an attestation or a substitute for job authority."""
    job_id: Identifier
    conversation_id: Identifier
    sdk_session_id: Identifier
    profile_digest: Digest
    base_state_sha256: Digest
    model_request_sha256: Digest
    runtime_pid: Annotated[int, Field(gt=0)]
    rpc_success: Literal[True]
    trigger: Literal["manual"]
    start_event_id: Identifier
    complete_event_id: Identifier
    checkpoint_number: Annotated[int, Field(gt=0)]
    summary_sha256: Digest
    messages_removed: Annotated[int, Field(ge=0)]
    tokens_removed: int


class CheckpointSource(WireModel):
    profile: CheckpointProfile
    execution: CompactionExecution
    previous_job_id: Identifier
    previous_sha256: Digest


def checked_path(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or value.startswith("/") or any(p in {"", ".", ".."} or ":" in p for p in value.split("/"))):
        raise ValueError("invalid session-relative path")
    return value


class SdkState(WireModel):
    sdk_version: Literal["1.0.13"] = "1.0.13"
    runtime_version: Literal["1.0.83"] = "1.0.83"
    sdk_session_id: Identifier
    model_provider: ModelProvider | None = Field(default=None, exclude_if=lambda value:value is None)
    codec: Literal["session-fs-v1", "session-fs-diag-empty-v1",
                   "session-fs-responses-v1", "session-fs-responses-diag-v1"] = "session-fs-v1"
    files: dict[str, str]
    directories: tuple[str, ...] = ()
    projected_event_ids: tuple[Identifier, ...] | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def complete(self):
        if (self.codec in RESPONSES_CODECS) != (self.model_provider == "openai-responses"):
            raise ValueError("Responses requires its own versioned checkpoint codec")
        if not {"events.jsonl", "workspace.yaml"} <= self.files.keys():
            raise ValueError("required SDK files are missing")
        for path in (*self.files, *self.directories):
            checked_path(path)
        if (len(set(self.directories)) != len(self.directories) or set(self.files) & set(self.directories)
                or any(str(p) in self.files for name in (*self.files, *self.directories)
                       for p in PurePosixPath(name).parents if str(p) != ".")):
            raise ValueError("conflicting session filesystem entries")
        events = checkpoint_events(self.files["events.jsonl"])
        starts = [e for e in events if isinstance(e, dict) and e.get("type") == "session.start"]
        if (not all(isinstance(e, dict) and isinstance(e.get("type"), str) for e in events)
                or len(starts) != 1
                or not isinstance(starts[0].get("data"), dict)
                or starts[0]["data"].get("sessionId", starts[0]["data"].get("session_id")) != self.sdk_session_id):
            raise ValueError("SDK events do not belong to the expected session")
        if self.codec in DIAG_CODECS:
            from .behavior_sdk_compaction import validate_projected_state
            validate_projected_state(self)
        else:
            if self.projected_event_ids is not None:
                raise ValueError("ordinary checkpoints cannot declare diagnostic projection")
            validate_checkpoint_files(self.files, model_provider=self.model_provider)
        if len(self.encoded()) > MAX_BACKUP_BYTES:
            raise ValueError("SDK state exceeds the existing backup byte bound")
        return self

    def encoded(self):
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")

    @property
    def sha256(self):
        return hashlib.sha256(self.encoded()).hexdigest()


class SdkStatePart(WireModel):
    version_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    receipt: BlobReceipt


class StoredSdkArchive(WireModel):
    sdk_version: Literal["1.0.13"] = "1.0.13"
    runtime_version: Literal["1.0.83"] = "1.0.83"
    codec: Literal["artifact-session-fs-v1", "artifact-session-fs-diag-empty-v1",
                   "artifact-session-fs-responses-v1", "artifact-session-fs-responses-diag-v1"] = "artifact-session-fs-v1"
    sdk_session_id: Identifier
    model_provider: ModelProvider | None = Field(default=None, exclude_if=lambda value:value is None)
    conversation_id: Identifier
    job_id: Identifier
    tenant_id: Identifier
    team_id: Identifier
    agent_oid: Identifier
    artifact_id: Identifier
    sha256: Digest
    byte_length: Annotated[int, Field(gt=0, le=MAX_BACKUP_BYTES)]
    parts: Annotated[tuple[SdkStatePart, ...], Field(min_length=1, max_length=16)]
    source: CheckpointSource | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def sizes(self):
        if (self.codec in RESPONSES_ARCHIVE_CODECS) != (self.model_provider == "openai-responses"):
            raise ValueError("Responses requires its own versioned checkpoint reference")
        if (self.codec in DIAG_ARCHIVE_CODECS) != (self.source is not None):
            raise ValueError("checkpoint codec and source must be paired")
        if (sum(p.receipt.bytes for p in self.parts) != self.byte_length
                or len({p.version_id for p in self.parts}) != len(self.parts)):
            raise ValueError("checkpoint parts do not cover the complete state")
        return self

    def require_profile(self, expected, *, model_provider=None):
        if self.model_provider != model_provider:
            from .errors import Problem
            raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "the saved checkpoint belongs to another model provider")
        if self.codec in DIAG_ARCHIVE_CODECS and (expected is None or self.source.profile != expected):
            from .errors import Problem
            raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "the checkpoint consumer profile is disabled or differs")


class SaveSdkState(Command):
    state: SdkState
    sdk_stop: AssistantSdkStop
    compaction: CompactionExecution | None = Field(default=None, exclude_if=lambda v: v is None)


# Native references never stand in for a legacy job-bound StoredSdkArchive.
NATIVE_CODECS = {
    "session-fs-v1": "native-session-fs-v1", RESPONSES_CODEC: "native-session-fs-responses-v1",
    DIAG_CODEC: "native-session-fs-diag-empty-v1", RESPONSES_DIAG_CODEC: "native-session-fs-responses-diag-v1",
}
NATIVE_DIAG_CODECS = frozenset(NATIVE_CODECS[name] for name in DIAG_CODECS)
NATIVE_RESPONSES_CODECS = frozenset(NATIVE_CODECS[name] for name in RESPONSES_CODECS)
NATIVE_SEGMENT_BYTES = 512 * 1024  # base64 of one segment plus its envelope stays below the 1 MB item limit
NATIVE_MAX_SEGMENTS = -(-MAX_BACKUP_BYTES // NATIVE_SEGMENT_BYTES)


class NativeCheckpointScope(WireModel):
    """Whose checkpoint this is; taken from trusted Hosted state, never from model or request text."""
    agent_id: Identifier
    conversation_id: Identifier
    sdk_session_id: Identifier
    model_provider: ModelProvider | None = Field(default=None, exclude_if=lambda value: value is None)


class NativeCompactionExecution(WireModel):
    agent_id: Identifier
    request_id: Identifier
    conversation_id: Identifier
    sdk_session_id: Identifier
    capture_digest: Digest
    base_state_sha256: Digest
    model_request_sha256: Digest
    runtime_pid: Annotated[int, Field(gt=0)]
    rpc_success: Literal[True]
    trigger: Literal["manual"]
    start_event_id: Identifier
    complete_event_id: Identifier
    checkpoint_number: Annotated[int, Field(gt=0)]
    summary_sha256: Digest
    messages_removed: Annotated[int, Field(ge=0)]
    tokens_removed: int


class NativeCheckpointSource(WireModel):
    profile: CheckpointProfile
    execution: NativeCompactionExecution


class NativeCheckpointLineage(WireModel):
    request_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    capture_digest: Digest
    previous_sha256: Digest | None = None
    previous_request_id: Identifier | None = None
    previous_turn_number: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def predecessor(self):
        if ((self.previous_request_id is None) != (self.previous_turn_number is None)
                or self.previous_request_id is not None and (
                    self.previous_sha256 is None or self.previous_request_id == self.request_id
                    or self.previous_turn_number >= self.turn_number)):
            raise ValueError("checkpoint lineage must name an earlier original turn")
        return self


class NativeCheckpointRef(WireModel):
    """The complete manifest of one checkpoint stored as ``ckpt:<sha256>:<index>`` in ``fa/v1/conv/<CID>``."""
    kind: Literal["fa-native-checkpoint-v1"] = "fa-native-checkpoint-v1"
    sdk_version: Literal["1.0.13"] = "1.0.13"
    runtime_version: Literal["1.0.83"] = "1.0.83"
    codec: Literal["native-session-fs-v1", "native-session-fs-responses-v1",
                   "native-session-fs-diag-empty-v1", "native-session-fs-responses-diag-v1"]
    model_provider: ModelProvider | None = Field(default=None, exclude_if=lambda value: value is None)
    agent_id: Identifier
    conversation_id: Identifier
    sdk_session_id: Identifier
    sha256: Digest
    byte_length: Annotated[int, Field(gt=0, le=MAX_BACKUP_BYTES)]
    segments: Annotated[int, Field(ge=1, le=NATIVE_MAX_SEGMENTS)]
    lineage: NativeCheckpointLineage | None = Field(default=None, exclude_if=lambda value: value is None)
    source: NativeCheckpointSource | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def complete(self):
        if (self.codec in NATIVE_RESPONSES_CODECS) != (self.model_provider == "openai-responses"):
            raise ValueError("Responses requires its own native checkpoint codec")
        if (self.codec in NATIVE_DIAG_CODECS) != (self.source is not None):
            raise ValueError("native diagnostic state requires its original compaction source")
        if self.source is not None:
            execution = self.source.execution
            validate_provider_checkpoint(self.model_provider, self.source.profile)
            if (self.lineage is None or self.lineage.previous_request_id is None
                    or (execution.agent_id, execution.conversation_id, execution.sdk_session_id)
                    != (self.agent_id, self.conversation_id, self.sdk_session_id)):
                raise ValueError("native compaction must retain its exact scope and RID lineage")
        if self.segments != -(-self.byte_length // NATIVE_SEGMENT_BYTES):
            raise ValueError("native checkpoint segments must cover exactly the complete state")
        return self

    @property
    def scope(self):
        return NativeCheckpointScope(agent_id=self.agent_id, conversation_id=self.conversation_id,
                                     sdk_session_id=self.sdk_session_id, model_provider=self.model_provider)


class NativeCheckpointTurn(WireModel):
    scope: NativeCheckpointScope
    request_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    capture_digest: Digest
    profile: CheckpointProfile | None
    previous: NativeCheckpointRef | None

    @model_validator(mode="after")
    def original_predecessor(self):
        validate_provider_checkpoint(self.scope.model_provider, self.profile)
        if self.previous is not None and (self.previous.scope != self.scope or (
                self.previous.lineage is not None and (
                    self.previous.lineage.turn_number >= self.turn_number
                    or self.previous.lineage.request_id == self.request_id))):
            raise ValueError("native checkpoint input must retain its original conversation predecessor")
        return self

    @property
    def lineage(self):
        prior = None if self.previous is None else self.previous.lineage
        return NativeCheckpointLineage(
            request_id=self.request_id, turn_number=self.turn_number, capture_digest=self.capture_digest,
            previous_sha256=None if self.previous is None else self.previous.sha256,
            previous_request_id=None if prior is None else prior.request_id,
            previous_turn_number=None if prior is None else prior.turn_number,
        )
