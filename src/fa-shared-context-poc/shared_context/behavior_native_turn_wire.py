"""ADR-0007 wire contracts. No SDK imports, execution identity or caller credentials."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator, model_validator

from .behavior_agent_message_wire import PeerTeamReference
from .behavior_native_agent_wire import NativeRelayAcceptance, NativeRelayQueued
from .behavior_agent_role_prompt import RolePromptCapture, role_text_mismatch
from .behavior_agent_template import AgentTemplate
from .behavior_assistant_wire import InstructionSubmit
from .behavior_attachments import AttachedTexts, TextAttachments, validate_attachments
from .behavior_capabilities import CAPABILITY_BASE_PROFILES, CapabilityCapture, check_captured, turn_grants
from .behavior_instruction_models import InstructionModel
from .behavior_model_capacity import ModelCapacitySnapshot
from .behavior_turn_usage import TurnModelUsage
from .behavior_text_files import UserTextFiles, validate_user_files
from .behavior_wire import Command, Digest, Identifier, ModelDeployment, WireModel
from .errors import Problem
from .native_skill_draft_wire import NativeSkillDraftIntentRead
from .behavior_skill_draft import SKILL_DRAFT_ERRORS
from .assistant_image_wire import IMAGE_DELIVERY_ERRORS
from .native_image_wire import NativeImageRead, NativeImagesRead
from .behavior_user_images import NativeUserImageUpload, USER_IMAGE_ERRORS
from .native_responses_wire import (
    NATIVE_RESPONSES_PROFILE, NATIVE_STATE_PROFILE, NativeResponseDispatch, NativeResponseId,
    NativeStateProfile, channel, check_response_state, unlabeled_profile,
)
from .native_append_wire import NativeAppend, NativeAppendRead
from .native_progress_wire import NativeProgressRead, NativeProgressSnapshot, NativeTurnStatusRead
from .native_assembly import NativeAssembly
from .native_files_wire import FILES_ERRORS, MAX_FILE_ENTRIES, NativeFileEntry, NativeFilesManifest, NativeSessionFiles
from .native_continuation_wire import CONTINUATION_ERRORS, NativeContinuation, NativeContinuationRead, NativeContinuationReady
from .native_routine_wire import NATIVE_ROUTINE_ERRORS, NativeRoutineOrigin
from .team_connector_wire import ConnectorCapture

NATIVE_TURN_HEADER = "x-client-fa-state-profile"
NATIVE_EXPECTED_VERSION_HEADER = "x-client-fa-expected-version"
# ADR-0009 §2 phase 3: the API located this session by its binding row, so Hosted's stricter ``replaced()`` applies.
NATIVE_LOCATOR_HEADER = "x-client-fa-locator"
NATIVE_LOCATOR_BINDING_ROW = "binding-row"
AUTOMATIC_ENVIRONMENT_POLICY = "fa-environment-system-policy-20261004"
# ADR-0009 §4: why Hosted refused before dispatching anything (with ``not_dispatched`` and the VM's ``actual_version``).
PRE_DISPATCH_REASONS = frozenset({"profile", "runtime", "version", "replaced"})


def before_dispatch(refusal: Problem, reason: str, actual_version: str | None) -> Problem:
    """Hosted's refusal before anything was read or dispatched says so, why, and which version this VM runs. Its code
    stays; the API keeps today's handling of these refusals until its send flow uses them (ADR-0009 phase 3)."""
    if reason not in PRE_DISPATCH_REASONS:
        raise ValueError("an unknown pre-dispatch reason")
    refusal.body["error"].update(not_dispatched=True, reason=reason,
                                 **({} if actual_version is None else {"actual_version": actual_version}))
    return refusal


# The Teams relay's request ids start with this (native_teams_relay): a human turn nobody can press a card in.
RELAY_PREFIX = "tr-"
# Hosted ENV (deployment overlay, behavior_agent_versions._ENVIRONMENT_FLAGS). The API reads the same key off the
# selected Hosted definition, so the workbench offers a switch only where the Hosted session accepts one.
MODEL_SWITCH_ENV = "FA_NATIVE_MODEL_SWITCH"


def native_model_switch_enabled(environ=None):
    """``FA_NATIVE_MODEL_SWITCH=1`` (default off): a conversation may take another model after completed turns.

    The turn then restores the published checkpoint cold with the new model (README#native-model-switch)."""
    value = (os.environ if environ is None else environ).get(MODEL_SWITCH_ENV)
    if value not in (None, "0", "1"):
        raise Problem(503, "NATIVE_PROFILE_UNSUPPORTED", "FA_NATIVE_MODEL_SWITCH must be 0 or 1")
    return value == "1"


def automatic_omission_id(agent_id, source_sid, target_sid, target_version):
    return material_digest({"policy": AUTOMATIC_ENVIRONMENT_POLICY, "agent": agent_id,
                            "source": source_sid, "target": target_sid, "version": target_version})


def require_expected_version(expected: str | None, actual: str) -> None:
    """FA precondition only; this header cannot change Foundry's sandbox routing."""
    if expected is None:
        return
    if (not isinstance(expected, str) or not expected.isascii() or not expected.isdigit()
            or expected.startswith("0") or len(expected) > 16):
        raise Problem(422, "NATIVE_REQUEST_INVALID", "the expected concrete agent version is invalid")
    if expected != actual:
        raise Problem(409, "NATIVE_SESSION_CHANGED", "the actual Hosted version differs; no operation was dispatched")


NATIVE_TURN_ERRORS = SKILL_DRAFT_ERRORS | IMAGE_DELIVERY_ERRORS | USER_IMAGE_ERRORS | FILES_ERRORS | CONTINUATION_ERRORS | NATIVE_ROUTINE_ERRORS | frozenset({
    "NATIVE_TURNS_DISABLED", "NATIVE_ROUTINES_DISABLED", "NATIVE_REQUEST_INVALID", "NATIVE_REQUEST_TOO_LARGE",
    "NATIVE_STATE_STORE_MISSING", "NATIVE_STATE_STORE_TTL", "NATIVE_STATE_STORE_UNKNOWN",
    "NATIVE_STATE_SCOPE", "NATIVE_STATE_INVALID", "NATIVE_RID_MISSING",
    "NATIVE_RID_CONFLICT", "NATIVE_CONVERSATION_MISSING", "NATIVE_CAPTURE_CHANGED", "NATIVE_MODEL_CONTEXT_EXCEEDED",
    "NATIVE_RESULT_MISSING", "NATIVE_RESULT_INCOMPLETE", "NATIVE_RESULT_TOO_LARGE",
    "NATIVE_DELIVERY_CONFLICT", "NATIVE_TURN_UNKNOWN", "NATIVE_TURN_NOT_STARTED", "NATIVE_TURN_CANCELLED",
    "NATIVE_MODEL_REFUSED",
    "NATIVE_TURN_TIMEOUT", "NATIVE_TURN_BUSY", "NATIVE_EXECUTION_UNAVAILABLE",
    "NATIVE_TURN_QUEUED", "NATIVE_TURN_NOT_TAKEN", "NATIVE_RESPONSE_UNCONFIRMED",
    "NATIVE_APPEND_MISSING", "NATIVE_APPEND_UNKNOWN", "NATIVE_PROGRESS_UNCONFIRMED",
    "NATIVE_PROFILE_UNSUPPORTED", "SDK_STATE_INCOMPLETE", "SDK_STATE_UPLOAD_UNKNOWN",
    "NATIVE_SESSION_CHANGED", "AGENT_RETIRED",
    "NATIVE_BOOTSTRAP_HAS_WORK", "NATIVE_BOOTSTRAP_SOURCE_ACTIVE", "NATIVE_BOOTSTRAP_UNKNOWN",
    "NATIVE_BOOTSTRAP_CONFLICT", "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE",
    "AGENT_VERSION_FOLLOWING", "NATIVE_ENVIRONMENT_CONFLICT", "NATIVE_ENVIRONMENT_UNKNOWN",
    "NATIVE_ENVIRONMENT_UNAVAILABLE", "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE",
    "NATIVE_ADMISSION_PENDING",
    "NATIVE_FILES_CONFIRMATION_UNSUPPORTED", "NATIVE_FILES_CONFIRMATION_REQUIRED",
    "NATIVE_AGENT_SOURCE_INVALID", "NATIVE_POST_TURN_UNKNOWN", "NATIVE_USAGE_CONFLICT",
    "SDK_STATE_UNSUPPORTED_FORMAT", "SDK_STATE_PRIVATE_MATERIAL",
    "SDK_STATE_SOURCE_UNVERIFIED", "ASSISTANT_CHECKPOINT_SCOPE",
    "SDK_START_GATE_CODE_CHANGED", "SDK_START_GATE_FENCED", "SDK_START_GATE_REPLAY",
    "NATIVE_EFFECT_UNKNOWN", "NATIVE_EFFECT_CONFLICT",
    "MEMORY_CANDIDATE_INVALID",
})
TurnNumber = Annotated[int, Field(ge=1, le=2**53 - 1)]
Cursor = Annotated[int, Field(ge=0, le=2**53 - 1)]
StateStoreCursor = Annotated[str, Field(min_length=1, max_length=2048)]
MAX_HITL_REVIEW_PAGE = 5
FailureCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,95}$")]


def canonical_bytes(value: WireModel | dict) -> bytes:
    data = value.model_dump(mode="json") if isinstance(value, WireModel) else value
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def material_digest(value: WireModel | dict) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def continuation_request_id(agent_id, conversation_id, root_request_id, attempt):
    return "resume-" + material_digest({
        "kind": "fa-native-continuation-v1", "agent_id": agent_id, "conversation_id": conversation_id,
        "root_request_id": root_request_id, "attempt": attempt,
    })[:56]


def continuation_ready_id(environment_request_id, session_id, manifest_sha256):
    return "resume-ready-" + material_digest({
        "kind": "fa-native-continuation-ready-v1", "environment_request_id": environment_request_id,
        "hosted_session_id": session_id, "files_manifest_sha256": manifest_sha256,
    })[:48]


def team_memory_scope(tenant_id: str, team_id: str) -> str:
    """The Foundry memory scope of one Team; a partition key chosen by the API, not an ACL."""
    # The memory service accepts only [A-Za-z0-9_-] in a scope.
    return "fat1-" + hashlib.sha256(f"fa-team-memory-v1\0{tenant_id}\0{team_id}".encode("utf-8")).hexdigest()[:48]


class TeamMemoryCapture(WireModel):
    """TN-1: the target Agent's Team memory scope, resolved by the API for this turn only."""

    schema_version: Literal["fa-team-memory-v1"] = "fa-team-memory-v1"
    scope: Annotated[str, Field(pattern=r"^fat1-[0-9a-f]{48}$")]


class NativeTurnCapture(WireModel):
    """The API resolver's immutable selection, not a client-supplied permission."""

    schema_version: Literal["fa-native-turn-capture-v1"] = "fa-native-turn-capture-v1"
    template: AgentTemplate
    profile_selection_id: Identifier | None = None
    model: InstructionModel
    model_capacity: ModelCapacitySnapshot | None = None
    reasoning_summary: Literal["detailed"] | None = None
    role_prompt: RolePromptCapture | None = None
    capabilities: CapabilityCapture | None = None
    attached_text: AttachedTexts | None = None
    sdk_start_code_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    team_memory: TeamMemoryCapture | None = Field(default=None, exclude_if=lambda value: value is None)
    connectors: ConnectorCapture | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def captured_selection(self):
        settings = self.template.settings
        if self.template.unavailable_skills:
            raise ValueError("A captured selection cannot contain unavailable skills")
        if role_text_mismatch(self.template.role.profile, self.role_prompt) is not None:
            raise ValueError("The selected role profile requires its exact approved role text")
        if (self.model_capacity is not None) != (settings.model_capacity_profile is not None):
            raise ValueError("The selected capacity profile requires its original resolved facts")
        if self.model_capacity is not None and self.model_capacity != self.model.capacity_snapshot():
            raise ValueError("Capacity must belong to the captured model and its original facts")
        if self.reasoning_summary is not None and (
                settings.model_provider != "openai-responses" or self.model.supports_reasoning_summary is not True):
            raise ValueError("Public reasoning summary requires the captured provider and model support")
        try:
            check_captured(settings.capabilities, self.capabilities)
        except Problem:
            raise ValueError("Captured capability grants must remain within the original selection") from None
        if self.capabilities is not None and self.capabilities.vision and self.model.supports_image_input is not True:
            raise ValueError("Image capabilities require the selected model's explicit image support")
        if self.attached_text is not None and settings.text_attachment_profile is None:
            raise ValueError("Resolved attachments require their selected server profile")
        if self.connectors is not None:
            if self.template.role.profile not in CAPABILITY_BASE_PROFILES:
                raise ValueError("Team connectors require a profile that composes organization tools")
            try:
                self.tools
            except ValueError:
                raise ValueError("A Team connector must not repeat a selected capability or tool name") from None
        return self

    @property
    def tools(self) -> CapabilityCapture | None:
        """Every tool grant of this turn: the selected capabilities and the Team connectors."""
        return turn_grants(self.capabilities, () if self.connectors is None else self.connectors.grants)

    @property
    def sha256(self) -> str:
        return material_digest(self)


class NativeSubmit(WireModel):
    operation: Literal["submit"] = "submit"
    command: InstructionSubmit
    capture: NativeTurnCapture
    state_profile: NativeStateProfile = Field(
        default=NATIVE_STATE_PROFILE, exclude_if=unlabeled_profile)
    new_conversation_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def original_command(self):
        _check_input_capture(self.command, self.capture)
        _check_new_conversation(self)
        return self

    @property
    def sha256(self) -> str:
        return material_digest(self)


def _check_input_capture(command, capture):
    if command.model is not None and command.model != capture.model.id:
        raise ValueError("The requested model must match the original resolver result")
    entries = () if capture.attached_text is None else capture.attached_text.entries
    if tuple(e.attachment for e in entries) != command.attachments:
        raise ValueError("The captured text must cover all and only the original attachments")
    if command.files and capture.template.settings.user_text_file_profile is None:
        raise ValueError("Literal files require the original server-selected file profile")


def _check_new_conversation(request):
    new = request.state_profile == NATIVE_RESPONSES_PROFILE and request.command.conversation_id is None
    if new != (request.new_conversation_id is not None):
        raise ValueError("Only a new Responses conversation requires its server-selected CID")


class NativeRead(WireModel):
    operation: Literal["read"] = "read"
    request_id: Identifier


class NativeConversationRead(WireModel):
    operation: Literal["conversation"] = "conversation"
    conversation_id: Identifier


class NativeHistory(WireModel):
    operation: Literal["history"] = "history"
    conversation_id: Identifier
    after_turn: Cursor = 0
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class NativeHitlHistoryRead(WireModel):
    operation: Literal["hitl-reviews"] = "hitl-reviews"
    conversation_id: Identifier
    request_id: Identifier
    after: StateStoreCursor | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_HITL_REVIEW_PAGE)] = MAX_HITL_REVIEW_PAGE


MAX_RECEIVED_PAGE = 50


class NativeReceivedRead(WireModel):
    """One page of this Agent's own original receipts, newest first; only requests another Agent sent are kept."""
    operation: Literal["received"] = "received"
    after: StateStoreCursor | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_RECEIVED_PAGE)] = MAX_RECEIVED_PAGE


class NativeResult(WireModel):
    operation: Literal["result"] = "result"
    conversation_id: Identifier
    request_id: Identifier
    part: Annotated[int, Field(ge=0)] = 0


class NativeCancel(WireModel):
    operation: Literal["cancel"] = "cancel"
    conversation_id: Identifier
    request_id: Identifier


class NativeResponseNotStarted(WireModel):
    """API-verified pre-handler rejection; never a transport timeout or an unknown response."""

    operation: Literal["response_not_started"] = "response_not_started"
    conversation_id: Identifier
    request_id: Identifier
    submit_digest: Digest
    reason: Literal["invalid_request", "protocol_unavailable"]


class NativeDelivered(WireModel):
    operation: Literal["delivered"] = "delivered"
    conversation_id: Identifier
    request_id: Identifier
    result_digest: Digest


class NativeCandidateRead(WireModel):
    """ADR-0007 C5: the original human reads this turn's inactive memory suggestion; never shares or activates it."""
    operation: Literal["memory_candidate"] = "memory_candidate"
    conversation_id: Identifier
    request_id: Identifier


class NativeSessionRead(WireModel):
    operation: Literal["session_read"] = "session_read"
    environment_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    # ADR-0009 5b: also answer this session's workspace tree (``workspace_*``), after the Agent ETag (U1 on the source;
    # on the destination before a fallback to the fence). An image before 5b refuses the field (422): no helper, so
    # the API keeps its existing files path.
    workspace: bool = Field(default=False, exclude_if=lambda value: not value)


# A toolbox version the Hosted process may list ahead of a turn (README#typing-prewarm); the toolbox provider keeps
# at most this many listed versions (native_toolbox.MAX_TOOLBOXES).
MAX_PREWARM_TOOLBOXES = 32


class NativePrewarmToolbox(WireModel):
    toolbox: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")]
    # None: the toolbox's current default version, read the way a turn reads it.
    version: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)


class NativePrewarm(WireModel):
    """The owner is typing (README#typing-prewarm): reaching the session wakes its compute, and the process lists
    these toolbox versions into its kept lists. Never a turn: no RID, record, queue position or model call."""
    operation: Literal["prewarm"] = "prewarm"
    toolboxes: Annotated[tuple[NativePrewarmToolbox, ...], Field(max_length=MAX_PREWARM_TOOLBOXES)] = ()

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.toolboxes)) != len(self.toolboxes):
            raise ValueError("each prewarm toolbox version is named once")
        return self


class NativePrewarmView(WireModel):
    """Hosted took the prewarm: ``toolboxes`` versions are being listed in the background; nothing was recorded.
    Also the answer to ``NativeSessionCheckpointCheck`` / ``NativeSessionPrerestore`` (``toolboxes=0``)."""
    agent_id: Identifier
    hosted_session_id: Identifier
    toolboxes: Annotated[int, Field(ge=0, le=MAX_PREWARM_TOOLBOXES)]
    # Per named conversation: its latest checkpoint is readable on this image (or none is owed), or explicitly refused
    # (CTX-03: a missing/different segment is unreadable, an unknown codec, version or shape unsupported). An empty map
    # checked nothing; it is never a pass for conversations the request did not name.
    checkpoints: dict[Identifier, Literal["ok", "unreadable", "unsupported"]] | None = Field(
        default=None, exclude_if=lambda value: value is None)


class NativePrewarmResult(WireModel):
    """The API's answer to the workbench: ``sent`` reached Hosted, ``recent`` was skipped by the per-Agent interval,
    ``skipped`` did not reach a current session. Only a hint; the next send never depends on it."""
    state: Literal["sent", "recent", "skipped"]
    toolboxes: Annotated[int, Field(ge=0, le=MAX_PREWARM_TOOLBOXES)] = 0


class NativeSessionPrepare(Command):
    operation: Literal["session_prepare"] = "session_prepare"
    expected_session_id: Identifier
    expected_etag: Annotated[str, Field(min_length=1, max_length=256)]


class NativeSessionPreparation(WireModel):
    schema_version: Literal["fa-native-session-preparation-v1"] = "fa-native-session-preparation-v1"
    agent_id: Identifier
    request_id: Identifier
    previous_hosted_session_id: Identifier | None
    hosted_session_id: Identifier
    hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    files_profile: Literal["fa-workspace-files-v1"] | None = Field(default=None, exclude_if=lambda value: value is None)
    state: Literal["ready"] = "ready"
    model_started: Literal[False] = False
    original_work_replayed: Literal[False] = False

    @model_validator(mode="after")
    def distinct_source(self):
        if self.previous_hosted_session_id == self.hosted_session_id:
            raise ValueError("an unchanged bootstrap session has no previous session")
        return self


class NativeEnvironmentCommand(Command):
    expected_session_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    expected_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    idle_only: bool = Field(default=False, exclude_if=lambda value: not value)


class NativeEnvironmentWithoutFiles(Command):
    follow: NativeEnvironmentCommand
    confirmation: Literal["do-not-transfer-workspace-files"]
    expected_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")] | None = None


class NativeEnvironmentRetargetCommand(NativeEnvironmentCommand):
    expected_session_id: Identifier
    pending_request_id: Identifier
    pending_session_id: Identifier

    @model_validator(mode="after")
    def new_operation(self):
        if self.request_id == self.pending_request_id:
            raise ValueError("retargeting requires a new explicit operation ID")
        return self


class NativeSessionFollow(Command):
    operation: Literal["session_follow"] = "session_follow"
    expected_session_id: Identifier
    expected_etag: Annotated[str, Field(min_length=1, max_length=256)]
    stage: Literal["prepare", "commit"]
    idle_only: bool = Field(default=False, exclude_if=lambda value: not value)
    files_required: bool = Field(default=False, exclude_if=lambda value: not value)
    # ADR-0009 §7a (i): SIDs the API's binding row lists as previous. Their unfinished receipts will not continue
    # (the API answers them with the earlier-version notice), so the follow does not wait for them.
    settled_session_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)] = Field(
        default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def source_not_settled(self):
        return _source_not_settled(self)


def _source_not_settled(command):
    # The source's own open receipts are the work a switch waits for; settling them would skip that wait.
    if command.expected_session_id in command.settled_session_ids:
        raise ValueError("settled_session_ids can not name the source session")
    return command


class NativeSessionAbandon(Command):
    """ADR-0009 §7a: make this (destination) session the Agent's, abandoning ``expected_session_id`` without invoking,
    reading or stopping it. The API decided it from platform metadata and its binding row; Hosted only commits."""
    operation: Literal["session_abandon"] = "session_abandon"
    expected_session_id: Identifier
    expected_etag: Annotated[str, Field(min_length=1, max_length=256)]
    # The source status the API's decision saw. A retired source (the platform can not run it) lets the new session
    # go on at once past receipts left ``started`` on it (ADR-0009 §7a "(r)(s) 的競爭"); ``idle`` waits for them.
    source_status: Literal["idle", "failed", "deleted", "expired", "missing"] | None = Field(
        default=None, exclude_if=lambda value: value is None)


class NativeSessionCut(Command):
    """ADR-0009 M1 2d-i: this (destination) session takes the Agent from ``expected_session_id`` only while the shared
    store is quiet; when it is not, the Agent record is not written (``state="current"`` with the reason) and the API
    tries again at the next turn end. Never invokes, reads from or stops the source.

    Two steps: ``mode="probe"`` runs the same busy checks and, when quiet, answers E0 (``quiet_etag``, the Agent ETag
    the store was quiet at) and commits nothing; the API then lists the source's files and sends ``mode="cut"`` with
    ``expected_etag=E0``, which is busy if the ETag moved (a turn admitted after the listing started)."""
    operation: Literal["session_cut"] = "session_cut"
    expected_session_id: Identifier
    mode: Literal["probe", "cut"] = Field(default="cut", exclude_if=lambda value: value == "cut")
    # E0 from the probe: any later write (an admission) makes this attempt busy. Required for a no-files cut, whose
    # commit is safe only if nothing ran between the API's file listing and the commit.
    expected_etag: Annotated[str, Field(min_length=1, max_length=256)] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    # The API listed workspace files on the active source (``SessionFiles``): they move under an H0-shaped ``pending``
    # fence through the existing follow file stages. False: a direct commit, no fence.
    files_required: bool = Field(default=False, exclude_if=lambda value: not value)
    # As ``NativeSessionFollow``: previous SIDs whose unfinished receipts are not waited for.
    settled_session_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)] = Field(
        default=(), exclude_if=lambda value: not value)
    # ADR-0009 5b U4: the 5b snapshot, restored through this session's staging (``NativeWorkspaceFiles``): a probe
    # prepares it (refused when the live root holds something else); the cut confirms every staged file, publishes the
    # tree and commits ``files_migrated`` with its digest. An image before 5b refuses the field (422).
    files_restore: NativeFilesManifest | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def listed_at_e0(self):
        if self.mode == "cut" and not self.files_required and self.expected_etag is None:
            raise ValueError("a cut without files requires the probe's expected_etag")
        if self.files_restore is not None and (self.files_required or self.files_restore.source_session_id
                                               != self.expected_session_id):
            raise ValueError("a restore replaces the files fence and names this cut's source")
        return _source_not_settled(self)


class NativeSessionWithdraw(Command):
    """ADR-0009 lifecycle "pending 切換用法": undo the ``pending`` (cut or older follow) named here with one CAS
    (If-Match ``expected_etag``); a committed switch answers as committed. Runs on an H2d session, not the current one."""
    operation: Literal["session_withdraw"] = "session_withdraw"
    expected_etag: Annotated[str, Field(min_length=1, max_length=256)]
    pending_request_id: Identifier
    pending_session_id: Identifier


class NativeUnfinishedRead(WireModel):
    """ADR-0009 §7a (f): one bounded read-only scan of this Agent's ``fa/v1/turns`` receipts for unfinished ones
    accepted by another SID than the current one (the first switch backfills them into the previous-SID list)."""
    operation: Literal["session_unfinished"] = "session_unfinished"


class NativeSessionCheckpointCheck(WireModel):
    """ADR-0009 phase 4 U3 (reserved in the release 31 wire, which refuses it): the destination session reads back
    from the store the latest checkpoint of each conversation the API names (its recent ones) before a switch. Not in
    ``_RECORD_READS``: the destination is not yet the current SID. Writes nothing, starts no SDK."""
    operation: Literal["session_checkpoint_check"] = "session_checkpoint_check"
    conversation_ids: Annotated[tuple[Identifier, ...], Field(max_length=5)] = Field(
        default=(), exclude_if=lambda value: not value)


class NativeSessionPrerestore(WireModel):
    """ADR-0009 phase 4 U9, reserved like ``NativeSessionCheckpointCheck``: the destination session restores the
    latest checkpoint of 1-2 named conversations into its local hot copy before a switch, so their next turn there
    is not a cold restore. Reads the shared store only, writes no receipt, starts no SDK; repeating it is harmless."""
    operation: Literal["session_prerestore"] = "session_prerestore"
    conversation_ids: Annotated[tuple[Identifier, ...], Field(max_length=2)] = Field(
        default=(), exclude_if=lambda value: not value)


class NativeUnfinishedSessions(WireModel):
    schema_version: Literal["fa-native-unfinished-sessions-v1"] = "fa-native-unfinished-sessions-v1"
    agent_id: Identifier
    hosted_session_id: Identifier
    session_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)] = ()
    # False: the bounded scan stopped early (deadline or too many SIDs); the API records the check as unverified.
    complete: bool


NativeFilesOmissionReason = Literal["source", "source_changed", "source_not_migratable", "destination", "original_destination"]


class NativeSessionFilesOmission(Command):
    operation: Literal["session_files_omission"] = "session_files_omission"
    follow: NativeSessionFollow
    stage: Literal["check", "confirm", "read", "activate"] = "confirm"
    missing: NativeFilesOmissionReason | None = None
    pending_request_id: Identifier | None = None
    pending_session_id: Identifier | None = None
    confirmation_digest: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    decision_basis: Literal["system_policy"] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if self.follow.stage != "prepare" or (self.stage in {"check", "confirm", "activate"}) != (self.missing is not None):
            raise ValueError("file omission must name the original prepare and confirmed missing helper")
        if (self.pending_request_id is None) != (self.pending_session_id is None):
            raise ValueError("retargeting a blocked file operation needs its original request and destination")
        if self.missing == "original_destination" and self.pending_request_id is None:
            raise ValueError("an unavailable destination must retain its original pending operation")
        if self.missing == "source_not_migratable" and self.decision_basis != "system_policy":
            raise ValueError("a known migration refusal is handled only by the system policy")
        return self


class NativeSessionRetarget(Command):
    operation: Literal["session_retarget"] = "session_retarget"
    expected_session_id: Identifier
    expected_etag: Annotated[str, Field(min_length=1, max_length=256)]
    expected_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    pending_request_id: Identifier
    pending_session_id: Identifier
    idle_only: bool = Field(default=False, exclude_if=lambda value: not value)


NativeStorageErrorCode = Literal[
    "BadRequest", "InvalidRequest", "InvalidArgument", "InvalidParameter", "InvalidCursor",
    "InvalidContinuationToken", "Unauthorized", "Forbidden", "NotFound", "ResourceNotFound",
    "Conflict", "PreconditionFailed", "TooManyRequests", "RateLimitExceeded",
    "InternalServerError", "ServiceUnavailable", "GatewayTimeout",
    "bad_request", "invalid_request", "invalid_request_error", "invalid_argument", "invalid_parameter",
    "invalid_cursor", "invalid_continuation_token", "unauthorized", "forbidden", "not_found",
    "resource_not_found", "conflict", "precondition_failed", "rate_limit_exceeded",
    "internal_error", "server_error", "service_unavailable", "UNCLASSIFIED",
]


class NativeFilesCheckDetail(WireModel):
    category: Literal[
        "scan_pending", "active_waiting", "admission_pending", "execution_active",
        "task_active", "task_unconfirmed", "task_access_denied", "checkpoint_unconfirmed",
        "storage_read_failed", "page_changed",
    ]
    stage: Literal["open", "list", "read", "close", "write"] | None = Field(default=None, exclude_if=lambda value: value is None)
    key_category: Literal[
        "agent", "following", "confirmation", "check_progress", "receipt",
        "conversation", "turn", "checkpoint",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    failure_kind: Literal["deadline", "backend"] | None = Field(default=None, exclude_if=lambda value: value is None)
    read_attempt: Annotated[int, Field(ge=1, le=2)] | None = Field(default=None, exclude_if=lambda value: value is None)
    read_elapsed_ms: Annotated[int, Field(ge=0)] | None = Field(default=None, exclude_if=lambda value: value is None)
    backend_status: Annotated[int, Field(strict=True, ge=100, le=599)] | None = Field(default=None, exclude_if=lambda value: value is None)
    backend_code: NativeStorageErrorCode | None = Field(default=None, exclude_if=lambda value: value is None)
    cursor_source: Literal["page_last_id", "none"] | None = Field(default=None, exclude_if=lambda value: value is None)
    request_id_prefix: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,7}$")] | None = None
    conversation_id_prefix: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,7}$")] | None = None


class NativeEnvironmentView(WireModel):
    schema_version: Literal["fa-native-environment-v1"] = "fa-native-environment-v1"
    agent_id: Identifier
    request_id: Identifier | None = None
    previous_hosted_session_id: Identifier
    previous_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    hosted_session_id: Identifier
    hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    state: Literal["available", "current", "pending", "ready"]
    reason: Literal[
        "accepted_or_started", "source_stop_required", "source_active", "source_unavailable",
        "admission_pending", "scan_pending", "task_active", "task_unconfirmed", "task_access_denied",
        "files_pending", "files_unsupported", "unresolved_work",
        "source_version_changed",
    ] | None = None
    files_manifest_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    files_migrated: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    files_skip_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    files_omission_basis: Literal["system_policy"] | None = Field(default=None, exclude_if=lambda value: value is None)
    files_omission_reason: NativeFilesOmissionReason | None = Field(default=None, exclude_if=lambda value: value is None)
    progress_token: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    operation_idle_only: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    files_reconcile_required: bool = Field(default=False, exclude_if=lambda value: not value)
    files_confirmation_saved: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    files_confirmation_reason: NativeFilesOmissionReason | None = Field(default=None, exclude_if=lambda value: value is None)
    files_confirmation_absent: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    files_confirmation_pending: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    previous_files_profile: Literal["fa-workspace-files-v1"] | None = Field(default=None, exclude_if=lambda value: value is None)
    files_profile: Literal["fa-workspace-files-v1"] | None = Field(default=None, exclude_if=lambda value: value is None)
    source_invocation_refused: bool = Field(default=False, exclude_if=lambda value: not value)
    files_check: NativeFilesCheckDetail | None = Field(default=None, exclude_if=lambda value: value is None)
    # ADR-0009 M7, reserved for the phase 4 cut ``pending`` (Hosted clock, epoch seconds); never written before 2d-ii,
    # since H0 decodes ``agent.environment`` strictly.
    cut_deadline: Annotated[float, Field(ge=0)] | None = Field(default=None, exclude_if=lambda value: value is None)
    # A quiet ``NativeSessionCut`` probe's answer only (E0), never stored.
    quiet_etag: Annotated[str, Field(min_length=1, max_length=256)] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    model_started: Literal[False] = False
    original_work_replayed: Literal[False] = False
    scope: Literal["entire-agent"] = "entire-agent"

    @model_validator(mode="after")
    def original_transition(self):
        if self.previous_hosted_session_id == self.hosted_session_id and self.state != "current":
            raise ValueError("version following requires a distinct execution session")
        if self.state not in {"available", "current"} and self.request_id is None:
            raise ValueError("a persisted transition retains its original request")
        if self.state == "ready" and self.reason is not None:
            raise ValueError("a ready environment has no unresolved transition reason")
        if self.files_check is not None and (
                self.state != "pending" or self.reason != "scan_pending" or self.files_check.category != "scan_pending"):
            raise ValueError("an environment check detail is only progress, not a ready or confirmed result")
        if self.files_migrated is False:
            if self.files_skip_request_id is None or self.files_manifest_sha256 is not None:
                raise ValueError("omitted files require the original explicit confirmation, not a migration manifest")
        elif self.files_skip_request_id is not None:
            raise ValueError("only an explicit file omission names its confirmation")
        if self.files_omission_basis is not None or self.files_omission_reason is not None:
            if self.files_migrated is not False or self.files_omission_basis is None or self.files_omission_reason is None:
                raise ValueError("an automatic omission retains its policy basis, reason and original decision")
        if self.files_migrated is True and self.files_manifest_sha256 is None:
            raise ValueError("migrated files require their original manifest")
        if self.files_confirmation_saved is not None and (
                self.state != "pending" or self.files_migrated is not None or self.files_skip_request_id is not None):
            raise ValueError("a saved but inactive confirmation cannot claim migration or an active omission")
        if self.files_confirmation_reason is not None and self.files_confirmation_saved is None:
            raise ValueError("an original omission reason requires its saved confirmation reference")
        if self.files_confirmation_absent is not None and (
                self.state != "pending" or self.files_migrated is not None
                or self.files_confirmation_saved is not None
                or self.files_confirmation_pending is not None):
            raise ValueError("an absent confirmation observation cannot grant or claim file migration")
        if self.files_confirmation_pending is not None and (
                self.state != "pending" or self.reason not in {
                    "scan_pending", "accepted_or_started", "admission_pending",
                    "task_active", "task_unconfirmed"}
                or self.reason == "scan_pending" and self.files_check is None
                or self.files_migrated is not None or self.files_confirmation_saved is not None):
            raise ValueError("a pending confirmation is unactivated progress, not an omission or ready state")
        return self


class NativeSessionMetadata(WireModel):
    hosted_session_id: Identifier
    hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    status: Literal["active", "idle", "creating", "updating", "deleting", "failed", "deleted", "expired"]


class NativeEnvironmentMetadata(WireModel):
    schema_version: Literal["fa-native-environment-metadata-v1"] = "fa-native-environment-metadata-v1"
    agent_id: Identifier
    hosted_session_id: Identifier
    hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    observed_sessions: Annotated[tuple[NativeSessionMetadata, ...], Field(max_length=101)] = ()
    state: Literal["needs_follow", "unverified"]
    truncated: bool = False
    state_scope: Literal["platform-metadata-only"] = "platform-metadata-only"
    preparation_verified: Literal[False] = False
    model_started: Literal[False] = False
    original_work_replayed: Literal[False] = False
    scope: Literal["entire-agent"] = "entire-agent"


class NativeEnvironmentReplacementPreview(WireModel):
    schema_version: Literal["fa-native-environment-replacement-preview-v1"] = "fa-native-environment-replacement-preview-v1"
    agent_id: Identifier
    hosted_session_id: Identifier
    hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    original_request_id: Identifier
    original_hosted_session_id: Identifier
    original_hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    observed_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    state: Literal["confirmation_required"] = "confirmation_required"
    reason: Literal["destination_version_changed", "reader_required"] = "destination_version_changed"
    state_scope: Literal["platform-metadata-only"] = "platform-metadata-only"
    original_work_checked: Literal[False] = False
    model_started: Literal[False] = False
    original_work_replayed: Literal[False] = False
    scope: Literal["entire-agent"] = "entire-agent"

    @model_validator(mode="after")
    def confirmed_drift(self):
        if ((self.reason == "destination_version_changed") !=
                (self.observed_agent_version != self.original_hosted_agent_version)
                or self.hosted_session_id == self.original_hosted_session_id
                or int(self.hosted_agent_version) <= int(self.original_hosted_agent_version)):
            raise ValueError("replacement preview needs matching metadata and a distinct newer destination")
        return self


class NativeAdmissionObservation(WireModel):
    schema_version: Literal["fa-native-admission-observation-v1"] = "fa-native-admission-observation-v1"
    agent_id: Identifier
    request_id: Identifier
    environment_request_id: Identifier
    hosted_session_id: Identifier
    state: Literal["not_accepted"] = "not_accepted"
    state_scope: Literal["after-confirmed-environment-transition"] = "after-confirmed-environment-transition"
    read_only: Literal[True] = True
    retry_allowed: Literal[False] = False


class NativeCrash(WireModel):
    """ADR-0009 §5 crash-loop signal (M7, reserved: phase 4d writes ``crashes:<SID>`` and reports them)."""
    at: Annotated[float, Field(ge=0)]
    conversation_id: Identifier
    request_id: Identifier


class NativeSessionState(WireModel):
    agent_id: Identifier
    hosted_session_id: Identifier
    etag: Annotated[str, Field(min_length=1, max_length=256)]
    preparation: NativeSessionPreparation | None = None
    environment: NativeEnvironmentView | None = Field(default=None, exclude_if=lambda value: value is None)
    following: NativeEnvironmentView | None = Field(default=None, exclude_if=lambda value: value is None)
    admission_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    files_profile: Literal["fa-workspace-files-v1"] | None = Field(default=None, exclude_if=lambda value: value is None)
    files_omission_profile: Literal["fa-files-omission-v1"] | None = Field(default=None, exclude_if=lambda value: value is None)
    source_invocation_refused: bool = Field(default=False, exclude_if=lambda value: not value)
    # ADR-0009 §1: the VM's own FOUNDRY_AGENT_VERSION. Optional and omitted when absent, so an older image's state
    # still decodes; the API never forwards it to a client whose strict decoder predates it (observe_preparation).
    actual_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    # ADR-0009 M7, reserved: phase 4d's crash-loop read; never filled before then.
    recent_crashes: Annotated[tuple[NativeCrash, ...], Field(max_length=8)] = Field(
        default=(), exclude_if=lambda value: not value)
    # ``NativeSessionRead(workspace=True)`` only: the workspace root's mode and entries (``fa-workspace-files-v1``),
    # enumerated after ``etag`` was read, so a turn admitted meanwhile moves the ETag past it.
    workspace_root_mode: Annotated[int, Field(ge=0, le=0o777)] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    workspace_entries: Annotated[tuple[NativeFileEntry, ...], Field(max_length=MAX_FILE_ENTRIES)] = Field(
        default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def original_binding(self):
        if self.workspace_entries and self.workspace_root_mode is None:
            raise ValueError("workspace entries come with their root")
        if self.preparation is not None and self.preparation.agent_id != self.agent_id:
            raise ValueError("the original preparation belongs to another Agent")
        if self.environment is not None:
            expected = (self.environment.hosted_session_id if self.environment.state == "ready"
                        else self.environment.previous_hosted_session_id)
            if (self.environment.agent_id != self.agent_id or self.environment.state not in {"pending", "ready"}
                    or expected != self.hosted_session_id):
                raise ValueError("the environment must describe the published Agent binding")
        elif self.preparation is not None and self.preparation.hosted_session_id != self.hosted_session_id:
            raise ValueError("the original preparation differs from the published session")
        if self.following is not None and self.following.agent_id != self.agent_id:
            raise ValueError("the pending operation belongs to another Agent")
        return self


class NativeResultReference(WireModel):
    turn_number: TurnNumber
    turn_key: Annotated[str, Field(pattern=r"^turn:[1-9][0-9]*$")]
    result_digest: Digest
    text_sha256: Digest
    text_bytes: Annotated[int, Field(ge=0)]
    text_parts: Annotated[int, Field(ge=1)]
    checkpoint_sha256: Digest

    @model_validator(mode="after")
    def turn_identity(self):
        if self.turn_key != f"turn:{self.turn_number}":
            raise ValueError("The result must name its original conversation turn")
        return self


class NativeTurnAddress(WireModel):
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier


class NativeOutcomeReference(WireModel):
    status: Literal["unknown", "busy_refused"]
    receipt_digest: Digest
    failure_code: FailureCode


class NativeAgentOrigin(WireModel):
    """Verified relay provenance, never a credential or a new human instruction."""

    kind: Literal["agent_request", "agent_result", "agent_notice"]
    message_kind: Literal["request", "handoff", "result", "blocked"]
    message_id: Identifier
    sender: NativeTurnAddress
    recipient_agent_id: Identifier
    root_id: Identifier
    root: NativeTurnAddress
    hop: Annotated[int, Field(ge=0)]
    in_reply_to: Identifier | None = None
    result: NativeResultReference | None = None
    outcome: NativeOutcomeReference | None = Field(default=None, exclude_if=lambda value: value is None)
    continuation: NativeTurnAddress | None = Field(default=None, exclude_if=lambda value: value is None)
    refs: Annotated[tuple[PeerTeamReference, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def request_or_result(self):
        if self.kind == "agent_request":
            if (self.message_kind not in {"request", "handoff"} or self.result is not None
                    or self.outcome is not None or self.continuation is not None):
                raise ValueError("An agent request retains its delegation depth and is not a result")
        elif self.kind == "agent_result":
            if self.message_kind != "result" or self.in_reply_to is None or self.result is None or self.outcome is not None:
                raise ValueError("An agent result references its original request and completed result")
        elif self.message_kind != "blocked" or self.in_reply_to is None or self.outcome is None or self.result is not None:
            raise ValueError("An agent notice names its unconfirmed original outcome, not proof of a stopped SDK")
        if self.continuation is not None and self.continuation.agent_id != self.recipient_agent_id:
            raise ValueError("A continuation belongs to the receiving Agent's original conversation")
        if len(set(self.refs)) != len(self.refs):
            raise ValueError("Original Team references cannot be repeated")
        return self


class NativeAgentCommand(Command):
    content: Annotated[str, Field(min_length=1)]
    authorize_own_result: Literal[False] = False
    conversation_id: Identifier | None = None
    model: ModelDeployment | None = None
    attachments: TextAttachments = ()
    files: UserTextFiles = ()

    _attachments = field_validator("attachments")(validate_attachments)

    @field_validator("content")
    @classmethod
    def bounded_brief(cls, value):
        return InstructionSubmit.transport_text(value)

    @model_validator(mode="after")
    def original_files(self):
        validate_user_files(self.content, self.files, reference_names=tuple(item.name for item in self.attachments))
        return self


class NativeAgentReceive(WireModel):
    operation: Literal["agent_receive"] = "agent_receive"
    command: NativeAgentCommand
    capture: NativeTurnCapture
    source: NativeAgentOrigin
    state_profile: NativeStateProfile = Field(
        default=NATIVE_STATE_PROFILE, exclude_if=unlabeled_profile)
    new_conversation_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def captured_source(self):
        _check_input_capture(self.command, self.capture)
        _check_new_conversation(self)
        if self.command.request_id != self.source.message_id:
            raise ValueError("An agent input uses its original scoped message or wake ID as its RID")
        return self

    @property
    def sha256(self):
        return material_digest(self)


class NativeRoutineSubmit(WireModel):
    operation: Literal["routine_receive"] = "routine_receive"
    command: NativeAgentCommand
    capture: NativeTurnCapture
    routine: NativeRoutineOrigin
    state_profile: NativeStateProfile = Field(
        default=NATIVE_STATE_PROFILE, exclude_if=unlabeled_profile)

    @model_validator(mode="after")
    def captured_routine(self):
        _check_input_capture(self.command, self.capture)
        self.routine.check_command(self.command)
        return self

    @property
    def sha256(self):
        return material_digest(self)


class NativeContinuationSubmit(WireModel):
    operation: Literal["continue"] = "continue"
    command: InstructionSubmit | NativeAgentCommand
    capture: NativeTurnCapture
    source: NativeAgentOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    routine: NativeRoutineOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    continuation: NativeContinuation
    state_profile: NativeStateProfile = NATIVE_STATE_PROFILE

    @model_validator(mode="after")
    def captured_continuation(self):
        _check_input_capture(self.command, self.capture)
        if self.command.conversation_id is None or self.command.request_id == self.continuation.continuation_of:
            raise ValueError("a continuation requires the original CID and a distinct derived RID")
        if self.routine is not None:
            if self.source is not None or not isinstance(self.command, NativeAgentCommand):
                raise ValueError("a routine cannot also claim human or Agent input")
            self.routine.check_command(self.command, continuation=True)
        return self

    @property
    def sha256(self):
        return material_digest(self)


NativeRequest = Annotated[
    NativeSubmit | NativeAgentReceive | NativeRoutineSubmit | NativeContinuationSubmit | NativeContinuationRead | NativeContinuationReady |
    NativeAppend | NativeAppendRead | NativeProgressRead | NativeTurnStatusRead | NativeRead | NativeConversationRead | NativeHistory |
    NativeResult | NativeCancel | NativeResponseNotStarted | NativeDelivered | NativeSessionRead | NativeSessionPrepare |
    NativeSessionFollow | NativeSessionRetarget | NativeSessionAbandon | NativeSessionCut | NativeSessionWithdraw |
    NativeUnfinishedRead | NativeSessionCheckpointCheck | NativeSessionPrerestore | NativeSessionFiles |
    NativeSessionFilesOmission | NativeCandidateRead |
    NativeSkillDraftIntentRead | NativeImagesRead | NativeImageRead | NativeUserImageUpload | NativeHitlHistoryRead |
    NativeReceivedRead | NativePrewarm,
    Field(discriminator="operation"),
]
NATIVE_REQUEST = TypeAdapter(NativeRequest)


# ADR-0009 §8 / M4: the kinds the binding row's per-Agent notice slot delivers to the owner once (cleared after the
# owner response carrying it is written); ``earlier_version_unfinished`` is a read-time view of one receipt instead.
SLOT_NOTICE_KINDS = frozenset({
    "platform_version_changed", "environment_updated", "environment_switched_without_files",
    "environment_source_retired", "environment_rolled_back", "environment_upgrade_vetoed",
})
# Capability signal of a workbench bridge that decodes the phase 2c notice kinds (ADR-0009 §8). A deploy updates the API
# before the console, and an older bridge refuses a receipt with a kind it does not know (502), so the owner's submit
# carries those kinds, and the API clears the slot, only for a request with this header (a header, not a body field: an
# older API ignores an unknown header). Without it the answer is exactly as before phase 2c: the drift notice, never
# cleared. The value stays "2": a phase 2c API accepts exactly that one, whichever revision goes live first.
NOTICE_KINDS_HEADER = "X-FA-Notice-Kinds"
NOTICE_KINDS_VERSION = "2"
# Phase 6a (c), its own header for the same reason: the bridge decodes ``NativeReceipt.upgrade_waiting_version``.
# The API carries it only beside NOTICE_KINDS_HEADER; an API before 6a (c) ignores this header, an older bridge never
# sends it.
UPGRADE_WAITING_HEADER = "X-FA-Upgrade-Waiting"
UPGRADE_WAITING_VERSION = "1"
# Phase 4d, its own header for the same reason: the bridge decodes ``environment_rolled_back`` (and its
# ``without_files``). The API carries 已退回 only beside NOTICE_KINDS_HEADER; to a bridge without this header it carries
# the drift notice only and clears nothing, so 已退回 is never cleared unseen. An API before 4d ignores this header.
ROLLBACK_NOTICE_HEADER = "X-FA-Rollback-Notice"
ROLLBACK_NOTICE_VERSION = "1"
# Phase 4b-4, its own header for the same reason: the bridge decodes ``environment_upgrade_vetoed``. Without it the API
# carries the drift notice only and clears nothing; an API before 4b-4 ignores this header.
UPGRADE_VETOED_HEADER = "X-FA-Upgrade-Vetoed-Notice"
UPGRADE_VETOED_VERSION = "1"


class NativeEnvironmentNotice(WireModel):
    """ADR-0009 §8 notices; never stored: the API sets them, and Hosted only projects ``earlier_version_unfinished``
    onto its read of a RID it refused by version (``refused:<RID>``, §9).

    ``platform_version_changed``: "平台把助理的執行環境換成了另一個版本（vX→vY）", the platform ran the Agent's same
    session on another version than it was created on. ``environment_updated``: the Agent moved to a new version and
    its work files came along. ``environment_switched_without_files``: it moved without its work files and the old
    environment is kept (the 2026-10-04 sentence). ``environment_source_retired``: it moved without its work files
    because the old environment had failed or been deleted (§7a). ``earlier_version_unfinished`` (§7a (e)): this
    unfinished request was accepted by a session the Agent has since left, so it will not continue; send it again as
    a new message (§8 "這則訊息是較早的版本接下的……"). ``environment_rolled_back`` (phase 4d): the new version
    crash-looped and the Agent went back to its earlier one (§5, M4 "已退回"); ``without_files``: no work files came
    along, so the no-files sentence follows it (M4 #1). ``environment_upgrade_vetoed`` (phase 4b-4): the new version
    failed its health check (U3) a third time, so this Agent vetoed it and stays on its current one (§5, M4 #6)."""
    kind: Literal[
        "platform_version_changed", "earlier_version_unfinished", "environment_updated",
        "environment_switched_without_files", "environment_source_retired", "environment_rolled_back",
        "environment_upgrade_vetoed",
    ] = "platform_version_changed"
    from_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    to_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    without_files: bool = Field(default=False, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def versions(self):
        if (self.kind == "platform_version_changed") != (self.from_version is not None and self.to_version is not None):
            raise ValueError("only the platform version change names its two versions")
        if self.without_files and self.kind != "environment_rolled_back":
            raise ValueError("only 已退回 carries the no-files sentence; the other switch kinds name it themselves")
        return self


class NativeReceipt(WireModel):
    state_profile: NativeStateProfile = NATIVE_STATE_PROFILE
    agent_id: Identifier
    hosted_session_id: Identifier
    request_id: Identifier
    conversation_id: Identifier
    submit_digest: Digest
    capture_digest: Digest
    command: InstructionSubmit | NativeAgentCommand
    capture: NativeTurnCapture
    source: NativeAgentOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    routine: NativeRoutineOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    status: Literal["accepted", "started", "completed", "unknown", "busy_refused"]
    turn_number: TurnNumber | None = None
    result: NativeResultReference | None = None
    delegation: NativeRelayAcceptance | None = Field(default=None, exclude_if=lambda value: value is None)
    queued_relay: NativeRelayQueued | None = Field(default=None, exclude_if=lambda value: value is None)
    failure_code: FailureCode | None = None
    notification_error: FailureCode | None = Field(default=None, exclude_if=lambda value: value is None)
    cancel_requested: bool = False
    delivered: bool = False
    usage: TurnModelUsage | None = None
    response_id: NativeResponseId | None = Field(default=None, exclude_if=lambda value: value is None)
    dispatch: NativeResponseDispatch | None = Field(default=None, exclude_if=lambda value: value is None)
    assembly: NativeAssembly | None = Field(default=None, exclude_if=lambda value: value is None)
    continuation: NativeContinuation | None = Field(default=None, exclude_if=lambda value: value is None)
    continuation_available: bool = Field(default=False, exclude_if=lambda value: not value)
    # Read-time only (ADR-0009 §4 end): an owner response carries the Agent's pending drift notice, and Hosted's read
    # of a RID refused by version carries ``earlier_version_unfinished`` (§9); absent otherwise.
    environment_notice: NativeEnvironmentNotice | None = Field(default=None, exclude_if=lambda value: value is None)
    # Read-time only (ADR-0009 §12 6a (c)): the owner's workbench answer carries the version the Agent has been waiting
    # on for over 6 hours ("有新版本 vN，會在助理閒置時更新", status line only, once per target). Not a notice kind: the
    # v2 wire may not grow an enum value, but may grow an optional field a dump leaves out.
    upgrade_waiting_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)

    @property
    def human_origin(self) -> bool:
        return self.source is None and self.routine is None

    @property
    def observed_assembly(self) -> NativeAssembly:
        # Read-time interpretation only: never backfill an old receipt with today's configuration.
        return self.assembly or NativeAssembly(state="unknown", reason="not_recorded")

    @model_validator(mode="after")
    def original_material(self):
        check_response_state(self.state_profile, self.status, self.request_id, self.response_id, self.dispatch)
        new_cid = (self.conversation_id if channel(self.state_profile) == "responses"
                   and self.command.conversation_id is None else None)
        if self.continuation is not None:
            original = NativeContinuationSubmit(
                command=self.command, capture=self.capture, source=self.source, routine=self.routine,
                state_profile=self.state_profile,
                continuation=self.continuation)
        elif self.routine is not None:
            original = NativeRoutineSubmit(
                command=self.command, capture=self.capture, routine=self.routine, state_profile=self.state_profile)
        elif self.source is None:
            original = NativeSubmit(command=self.command, capture=self.capture, state_profile=self.state_profile,
                                    new_conversation_id=new_cid)
        else:
            original = NativeAgentReceive(
                command=self.command, capture=self.capture, source=self.source, state_profile=self.state_profile,
                new_conversation_id=new_cid)
            if self.source.recipient_agent_id != self.agent_id:
                raise ValueError("The saved agent message must name this receiving Agent")
        if self.source is not None and self.source.recipient_agent_id != self.agent_id:
            raise ValueError("The saved source must belong to this receiving Agent")
        if self.routine is not None and (
                self.source is not None or (self.routine.agent_id, self.routine.conversation_id)
                != (self.agent_id, self.conversation_id)):
            raise ValueError("The saved routine must belong only to this receiving Agent and conversation")
        if (self.request_id != self.command.request_id
                or self.command.conversation_id not in (None, self.conversation_id)
                or self.capture_digest != self.capture.sha256
                or self.submit_digest != original.sha256):
            raise ValueError("The receipt must retain the exact original request and capture")
        if (self.status == "completed") != (self.result is not None):
            raise ValueError("Completion and its durable result reference must be atomic")
        if self.result is not None and self.turn_number != self.result.turn_number:
            raise ValueError("Completion must refer to this original turn")
        if self.delivered and self.result is None:
            raise ValueError("Delivery acknowledges only a completed result")
        if self.delegation is not None and self.status == "accepted":
            raise ValueError("A delegation acknowledgment requires the original started SDK turn")
        if self.queued_relay is not None and (
                self.status == "accepted" or (
                    self.queued_relay.agent_id, self.queued_relay.conversation_id, self.queued_relay.request_id)
                != (self.agent_id, self.conversation_id, self.request_id)):
            raise ValueError("A queued relay must belong to its original started sender")
        return self


class NativeConversation(WireModel):
    state_profile: NativeStateProfile = NATIVE_STATE_PROFILE
    agent_id: Identifier
    hosted_session_id: Identifier
    execution_hosted_session_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    continuation_blocker: FailureCode | None = Field(default=None, exclude_if=lambda value: value is None)
    conversation_id: Identifier
    last_turn: Cursor
    last_request_id: Identifier | None
    capture: NativeTurnCapture
    capture_digest: Digest
    inflight_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    last_response_id: NativeResponseId | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def captured_digest(self):
        if self.capture_digest != self.capture.sha256:
            raise ValueError("Conversation capture must retain its original digest")
        if channel(self.state_profile) == "invocations" and (
                self.inflight_request_id is not None or self.last_response_id is not None):
            raise ValueError("An Invocations conversation cannot acquire Responses routing state")
        return self


def native_turn_root(receipt):
    if receipt.source is not None:
        return receipt.source.root
    return NativeTurnAddress(
        agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
        request_id=receipt.request_id if receipt.continuation is None else receipt.continuation.root_request_id)


def check_continuation_link(receipt, original):
    ref = receipt.continuation
    previous = original.continuation
    if (ref is None or original.status != "unknown"
            or (original.agent_id, original.conversation_id, original.request_id, original.submit_digest) != (
                receipt.agent_id, receipt.conversation_id, ref.continuation_of, ref.source_submit_digest)
            or ref.attempt != (1 if previous is None else previous.attempt + 1)
            or ref.root_request_id != (original.request_id if previous is None else previous.root_request_id)
            or receipt.request_id != continuation_request_id(
                receipt.agent_id, receipt.conversation_id, ref.root_request_id, ref.attempt)
            or receipt.command != original.command.model_copy(update={
                "request_id": receipt.request_id, "conversation_id": receipt.conversation_id})
            or receipt.source != original.source or receipt.routine != original.routine):
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the original continuation relationship differs")


class NativeTurnOrigin(WireModel):
    source_kind: Literal["human", "agent", "routine", "continuation"]
    actor_role: Literal["original-requester", "sending-agent", "routine-producer", "continuing-agent"]
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    turn_number: TurnNumber
    previous_turn_number: TurnNumber | None
    capture_digest: Digest
    source: NativeAgentOrigin | None
    routine: NativeRoutineOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    continuation: NativeContinuation | None = Field(default=None, exclude_if=lambda value: value is None)
    reply_target: Literal["original-conversation"] = "original-conversation"
    authorization: Literal[False] = False

    @model_validator(mode="after")
    def source_matches(self):
        if ((self.source_kind, self.actor_role) != (
                ("continuation", "continuing-agent") if self.continuation is not None else
                ("routine", "routine-producer") if self.routine is not None else
                ("human", "original-requester") if self.source is None else ("agent", "sending-agent"))
                or self.previous_turn_number is not None and self.previous_turn_number >= self.turn_number
                or self.source is not None and self.source.recipient_agent_id != self.agent_id
                or self.routine is not None and (
                    self.source is not None or (self.routine.agent_id, self.routine.conversation_id)
                    != (self.agent_id, self.conversation_id))):
            raise ValueError("native origin must describe only this original input and saved predecessor")
        return self


class NativeInstructionContext(WireModel):
    schema_version: Literal["fa-instruction-context-origin-v1"] = "fa-instruction-context-origin-v1"
    state_profile: NativeStateProfile = NATIVE_STATE_PROFILE
    content: str
    turn_origin: NativeTurnOrigin


class NativePublicTurn(WireModel):
    conversation_id: Identifier
    request_id: Identifier
    turn_number: TurnNumber
    user_text: str | None
    agent_text: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source: NativeAgentOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    routine: NativeRoutineOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    routine_text: str | None = Field(default=None, exclude_if=lambda value: value is None)
    continuation: NativeContinuation | None = Field(default=None, exclude_if=lambda value: value is None)
    continuation_text: str | None = Field(default=None, exclude_if=lambda value: value is None)
    result: NativeResultReference
    usage: TurnModelUsage | None = None
    delegation: NativeRelayAcceptance | None = Field(default=None, exclude_if=lambda value: value is None)
    queued_relay: NativeRelayQueued | None = Field(default=None, exclude_if=lambda value: value is None)
    notification_error: FailureCode | None = Field(default=None, exclude_if=lambda value: value is None)
    # Read-time only: the official State store created_at of this turn's immutable result item.
    finished_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def speaker(self):
        if self.routine is not None and (
                self.source is not None or self.routine.conversation_id != self.conversation_id):
            raise ValueError("routine history must keep its original conversation and source")
        if self.continuation is not None:
            if (self.user_text is not None or self.agent_text is not None or self.routine_text is not None
                    or self.continuation_text is None):
                raise ValueError("automatic continuation must not be a new human or peer message")
        elif self.routine is not None:
            if (self.user_text is not None or self.agent_text is not None or self.continuation_text is not None
                    or self.routine_text is None or self.request_id != self.routine.request_id
                    or hashlib.sha256(self.routine_text.encode("utf-8")).hexdigest() != self.routine.content_sha256):
                raise ValueError("a scheduled occurrence is neither a human instruction nor a peer message")
        elif (self.routine_text is not None or self.continuation_text is not None
                or (self.source is None) != (self.user_text is not None)
                or (self.source is not None) != (self.agent_text is not None)):
            raise ValueError("Agent input must never be published as a human's original wording")
        return self


class NativeHistoryPage(WireModel):
    conversation: NativeConversation
    turns: tuple[NativePublicTurn, ...]
    next_after_turn: Cursor | None


class NativeReceivedItem(WireModel):
    """A conversation another Agent opened in this Agent; metadata only, its text stays in the original receipt."""
    request_id: Identifier
    conversation_id: Identifier
    state_profile: NativeStateProfile
    status: Literal["accepted", "started", "completed", "unknown"]
    sender_agent_id: Identifier
    message_kind: Literal["request", "handoff"]


class NativeReceivedPage(WireModel):
    agent_id: Identifier
    items: Annotated[tuple[NativeReceivedItem, ...], Field(max_length=MAX_RECEIVED_PAGE)]
    next_after: StateStoreCursor | None


class NativeResultPage(WireModel):
    conversation_id: Identifier
    request_id: Identifier
    result: NativeResultReference
    part: Annotated[int, Field(ge=0)]
    text: str
    next_part: Annotated[int, Field(ge=0)] | None
    complete: bool

    @model_validator(mode="after")
    def explicit_pagination(self):
        if (self.part >= self.result.text_parts
                or self.next_part != (self.part + 1 if self.part + 1 < self.result.text_parts else None)
                or self.complete != (self.result.text_parts == 1)):
            raise ValueError("A partial result must explicitly expose the next part")
        return self


class NativeTurnStatus(WireModel):
    """``fa-native-turn-status-v1``: a completed single-page reply is inline; a longer one names part 0.

    ``preview`` is the reply a still-started turn is about to save, for display only: not a result, not delivered.
    ``finished_at`` is the official State store created_at of a completed turn's result item, for display only.
    """

    schema_version: Literal["fa-native-turn-status-v1"] = "fa-native-turn-status-v1"
    receipt: NativeReceipt
    progress: NativeProgressSnapshot | None = None
    text: str | None = None
    next_part: Literal[0] | None = None
    preview: str | None = Field(default=None, exclude_if=lambda value: value is None)
    finished_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def one_reply_form(self):
        receipt = self.receipt
        if self.progress is not None and (
                (self.progress.agent_id, self.progress.conversation_id, self.progress.request_id,
                 self.progress.submit_digest) != (
                    receipt.agent_id, receipt.conversation_id, receipt.request_id, receipt.submit_digest)):
            raise ValueError("The progress belongs to another original request")
        if self.preview is not None and receipt.status != "started":
            raise ValueError("Only a started turn shows a reply preview")
        completed = receipt.status == "completed" and receipt.result is not None
        if not completed and (self.text is not None or self.next_part is not None or self.finished_at is not None):
            raise ValueError("Only a completed original result carries a reply")
        if completed and (self.text is None) == (self.next_part is None):
            raise ValueError("A completed reply is either inline or names its first page")
        if self.text is not None and receipt.result.text_parts != 1:
            raise ValueError("Only a single-page reply is inline")
        return self
