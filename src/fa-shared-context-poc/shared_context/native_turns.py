"""ADR-0007 application receipts over the official resilient chain and State store."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
from time import monotonic, perf_counter, time
from typing import Annotated, Awaitable, Callable, Literal
from uuid import uuid4

from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.core.storage import (
    FoundryStorageConflictError, FoundryStorageError, FoundryStorageNotFoundError, FoundryStoragePreconditionError,
)
from azure.ai.agentserver.core.tasks import (
    RetryPolicy, SteeringQueueFull, TaskCancelled, TaskConflictError, TaskContext, TaskDeferred, TaskFailed,
    multi_turn_task, task,
)
# core 2.2.0 still raises this outside the handler without exporting it from tasks.
from azure.ai.agentserver.core.tasks._client import TransportClassifiedError
from azure.core.exceptions import AzureError
from pydantic import Field, model_validator

from .backup_models import MAX_BACKUP_BYTES
from .behavior_timing import elapsed_timing, phase_timing, timing_id, work_timing
from .behavior_native_turn_wire import (
    Cursor, NativeAgentReceive, NativeRoutineSubmit, NativeCancel, NativeConversation, NativeConversationRead, NativeDelivered,
    NativeHistory, NativeHistoryPage, NativeHitlHistoryRead, NativePublicTurn, NativeRead, NativeReceipt, NativeResponseNotStarted,
    NativeRequest, NativeResult, NativeResultPage, NativeResultReference, NativeSubmit,
    NATIVE_REQUEST, NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER,
    NativeCrash, NativeEnvironmentNotice, NativeTurnCapture, TurnNumber, canonical_bytes, material_digest,
    NativeSessionRead, NativeSessionPrepare, NativeSessionPreparation, NativeSessionState, NativeCandidateRead,
    NativeEnvironmentView, NativeSessionFollow, NativeSessionRetarget, native_model_switch_enabled,
)
from .behavior_sdk_state import (
    NativeCheckpointRef, NativeCheckpointScope, NativeCheckpointTurn, NativeCompactionExecution, SdkState,
)
from .behavior_native_agent_wire import NativeRelayAcceptance
from .behavior_turn_usage import TurnModelUsage
from .behavior_wire import AssistantSdkStop, Digest, HostedIdentity, Identifier, WireModel
from .errors import Problem
from .native_responses_wire import (
    NATIVE_RESPONSES_PROFILE, UNLABELED_STATE_PROFILE, NativeResponseDispatch, NativeStateProfile, channel)
from .native_append_wire import NativeAppend, NativeAppendRead
from .native_progress_wire import NativeProgressRead
from .copilot_steering import SdkSteeringChannel
from .native_assembly import NativeAssembly
from .native_left_receipts import STALL_SECONDS
from .native_markers import (
    CRASHES_TTL_SECONDS, EARLIER_VERSION_UNFINISHED, MARKER_TTL_SECONDS, MARKERS_STORE, MAX_CRASHES, _Crashes,
    _Deferred, _Refused,
)

TURNS_STORE = "fa/v1/turns"
EFFECTS_STORE = "fa/v1/effects"
MAX_ITEM_BYTES = 1_000_000
TEXT_PART_BYTES = 128 * 1024
# Long-lived store clients kept per process: the fixed stores plus recently used conversations.
STORE_CLIENTS = 32
# A removal is two short State store round trips; at shutdown, what does not finish in this bound stays a stale entry.
_OPEN_REMOVALS_CLOSE_SECONDS = 2
# _observe_control: local signals (ctx cancel, timeout, shutdown) every tick, no store I/O; the RID read once per poll
# while the turn runs, less often while it only waits for the execution slot (the lock wakes it at once); a cancel saved
# by this process wakes the read at once; an unconfirmed read backs off up to the max. Append delivery (one ``appends:``
# read when nothing is pending) follows the same poll; an append saved by this process wakes it at once.
CONTROL_TICK_SECONDS = 0.1
CONTROL_POLL_SECONDS = 1.0
SLOT_WAIT_POLL_SECONDS = 2.0
CONTROL_POLL_MAX_SECONDS = 5.0
# The longest a running turn goes on with no confirmed RID read (every read a 5xx): then it is fenced and its SDK
# stopped, so it does not run on toward the point where a new session's stall rule (ADR-0009 §7a, N = STALL_SECONDS)
# may count it lost and start the conversation's next turn. Half of N, so the two can not drift apart.
CONTROL_UNCONFIRMED_FENCE_SECONDS = STALL_SECONDS / 2
# A deferral's marker write gets at most this long inside the shutdown grace period; then the deferral goes on unmarked.
_MARKER_WRITE_SECONDS = 2
# Longest a turn waits, before "started", for an earlier CLI that outlived its stop (the recovery restart it asked for
# normally ends the wait first); then the RID ends resendable (NATIVE_TURN_NOT_STARTED).
UNEXITED_WAIT_SECONDS = 60
# ``_unknown`` writes again onto a RID that another writer (cancel, append) changed meanwhile, at most this often.
UNKNOWN_MERGES = 3
# Waits before each further attempt to save a finished turn after an unconfirmed (503) step; then it is re-entered.
SAVE_RETRY_SECONDS = (0.5, 2.0)
_UNCERTAIN = (FoundryStorageError, AzureError, OSError)
_CONFLICT = (FoundryStorageConflictError, FoundryStoragePreconditionError)
_TASK_TRANSPORT_ERRORS = (AzureError, OSError, TransportClassifiedError)
# Record reads the API sends to its cached reader session (and the typing prewarm, sent to the same reader); environment
# commands are not included.
_RECORD_READS = frozenset({
    "read", "conversation", "history", "result", "hitl-reviews", "received", "memory_candidate", "append_read",
    "progress", "turn_status", "images", "image", "continuation", "prewarm"})
log = logging.getLogger(__name__)
# A read this admission has not done yet (``None`` is a real result: no conversation).
_UNREAD = object()


def _raised(value):
    """A read done earlier in this admission: its failure is raised where the ordinary order reads it."""
    if isinstance(value, BaseException):
        raise value
    return value


def _changed(model, **updates):
    return type(model).model_validate_json(model.model_copy(update=updates).model_dump_json())


def _refused_final(receipt):
    """How an accepted Invocations receipt with ``refused:<RID>`` reads (``_refused_view``); any other, unchanged
    (the same object)."""
    if (not isinstance(receipt, NativeReceipt) or receipt.status != "accepted"
            or channel(receipt.state_profile) != "invocations"):
        return receipt
    return _changed(receipt, status="unknown", failure_code=EARLIER_VERSION_UNFINISHED,
                    environment_notice=NativeEnvironmentNotice(kind="earlier_version_unfinished"))


def _created_at(item):
    """When the official State store created this item (epoch seconds), for display; no FA format change."""
    created = getattr(item, "created_at", None)
    return None if isinstance(created, bool) or not isinstance(created, int) else datetime.fromtimestamp(created, UTC)


def _json(model):
    data = model.model_dump(mode="json") if isinstance(model, WireModel) else model
    # core 2.1.0 serializes State store values with json.dumps defaults (ASCII escapes).
    raw = json.dumps(data, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_ITEM_BYTES:
        raise Problem(413, "NATIVE_REQUEST_TOO_LARGE", "the complete item exceeds the State store limit")
    return json.loads(raw)


def _fit_assembly(assembly, *required):
    if assembly is None:
        return None
    available = min(MAX_ITEM_BYTES - len(json.dumps(
        value.model_dump(mode="json"), allow_nan=False).encode("utf-8")) for value in required)
    for candidate in (assembly, NativeAssembly(state="partial", reason="item_bound",
                                               tools_reason="item_bound", sdk_binding_reason="item_bound")):
        # A nonempty object's new member uses the same bytes as this single-member envelope
        # (two braces become the existing object's comma and space). Match the SDK's ASCII encoding.
        size = len(json.dumps({"assembly": candidate.model_dump(mode="json")},
                              allow_nan=False).encode("utf-8"))
        if size <= available:
            if candidate is not assembly:
                log.warning("FA_NATIVE_ASSEMBLY_PARTIAL reason=item_bound")
            return candidate
    log.warning("FA_NATIVE_ASSEMBLY_DROPPED reason=item_bound")
    return None


def _decode(value, model):
    try:
        return model.model_validate_json(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError):
        raise Problem(409, "NATIVE_STATE_INVALID", "the saved native state has an invalid shape") from None


def _conversation_capture(capture, *, include_model=True):
    # Resolved attachments, the current capability intersection, the Team memory scope and the approved role head
    # (CAP-06: a new role version applies from the next turn of every conversation) are per-turn. So are the Team
    # connectors, within the conversation's first set (_capture_fits).
    excluded = {"attached_text", "capabilities", "sdk_start_code_sha256", "team_memory", "role_prompt", "connectors"}
    # The model choice and the facts derived from it.
    return capture.model_dump(mode="json", exclude=excluded if include_model else excluded | {
        "model", "model_capacity", "reasoning_summary"})


def _connector_ids(capture):
    return frozenset() if capture.connectors is None else capture.connectors.connector_ids


class NativeTaskInput(WireModel):
    request_id: Identifier
    accepted_etag: Annotated[str, Field(min_length=1, max_length=256)]
    call_id: Annotated[str, Field(min_length=1)] | None = None
    continuation_of: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    execution_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)


class _Rid(WireModel):
    receipt: NativeReceipt
    call_id: str | None = None
    run_token: Identifier | None = None
    # ADR-0009 M9: the SID that accepted this never-started receipt before this one adopted it. Images before this
    # field can not read the item at all (``extra="forbid"``), so an abandoned source can not start or write it.
    adopted_from: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def execution_identity(self):
        if self.receipt.dispatch is not None:
            raise ValueError("Read-time dispatch instructions are never durable RID state")
        if ((self.receipt.status in {"started", "completed"} and self.run_token is None)
                or self.receipt.status == "accepted" and self.run_token is not None):
            raise ValueError("Started and completed receipts retain their original execution token")
        return self


class _Meta(WireModel):
    agent_id: Identifier
    hosted_session_id: Identifier
    conversation_id: Identifier
    sdk_session_id: Identifier
    capture: NativeTurnCapture
    n: Cursor = 0
    rid: Identifier | None = None
    base_ckpt: NativeCheckpointRef | None = None
    # ADR-0009 §3: written explicitly from 2a on (no digest covers it); an unlabeled item is v1-era and reads as v1.
    state_profile: NativeStateProfile = UNLABELED_STATE_PROFILE
    inflight_rid: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def responses_slot(self):
        if channel(self.state_profile) == "invocations" and self.inflight_rid is not None:
            raise ValueError("An Invocations conversation retains its original queue")
        return self


class _Turn(WireModel):
    request_id: Identifier
    conversation_id: Identifier
    n: TurnNumber
    submit_digest: Digest
    text_sha256: Digest
    text_bytes: Annotated[int, Field(ge=0, le=MAX_BACKUP_BYTES)]
    text_parts: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=65)]
    checkpoints: tuple[NativeCheckpointRef]
    usage: TurnModelUsage | None = None
    delegation: NativeRelayAcceptance | None = Field(default=None, exclude_if=lambda value: value is None)
    assembly: NativeAssembly | None = Field(default=None, exclude_if=lambda value: value is None)

    def reference(self):
        return NativeResultReference(
            turn_number=self.n, turn_key=f"turn:{self.n}", result_digest=material_digest(self),
            text_sha256=self.text_sha256, text_bytes=self.text_bytes, text_parts=len(self.text_parts),
            checkpoint_sha256=self.checkpoints[0].sha256,
        )


@dataclass(frozen=True)
class _Versioned:
    value: _Rid
    etag: str


class _StoreClients:
    """One event loop's long-lived official State store clients and the credential they share."""

    def __init__(self, loop):
        self.loop, self.stores, self.users = loop, OrderedDict(), {}
        self.lock, self.credential, self.credential_scope = asyncio.Lock(), None, AsyncExitStack()
        # Review P2 on #889: a markers store this process found missing, so each RID read does not pay its 404 again.
        # shortcut: per process; a store another process created is seen after a restart, except by ``adopt`` (another
        # SID's receipt), which looks again (``fresh``); this process's own creates clear it. One Hosted session, one VM.
        self.missing = set()


class _UnchangedRidWrite(Problem):
    """A failed RID CAS read back exactly the original ETag and value."""


class _UnknownRidWrite(Problem):
    """An unconfirmed unknown transition must escape the execution error handler."""


class _PeerQueueWaiting(Exception):
    """Capacity is normal waiting for an already accepted peer message, not a failed attempt."""


@dataclass
class NativeTurnControl:
    """Cooperative SDK abort, never the steering queue's generic cancel signal."""

    cancelled: bool = False
    timeout: bool = False
    shutdown: bool = False
    fenced: bool = False
    retain_delegation: Callable[[NativeRelayAcceptance], Awaitable[None]] | None = None
    checkpoint_turn: NativeCheckpointTurn | None = None
    on_event: Callable[[dict], None] | None = None
    steering: SdkSteeringChannel | None = None
    # append ID -> (send token, outcome) of this process's own claims (native_appends ``UNSENT``/``ATTEMPTED``, then the
    # SDK message ID or None): a claim whose write was unconfirmed is still sent once, an unconfirmed ``remember`` write
    # is written again (``_remembered``). Never evidence of delivery on its own.
    sent_appends: dict[str, tuple[str, object]] = field(default_factory=dict)
    # ADR-0009 M9: the RID record names ``adopted_from``; its SDK start pin is the accepting image's (check_start_capture).
    adopted: bool = False

    def stop_requested(self):
        return self.cancelled or self.timeout or self.shutdown or self.fenced


@dataclass(frozen=True)
class NativeSdkResult:
    text: str
    state: SdkState
    sdk_stop: AssistantSdkStop
    usage: TurnModelUsage | None = None
    delegation: NativeRelayAcceptance | None = None
    compaction: NativeCompactionExecution | None = None
    assembly: NativeAssembly | None = None


class NativeSdkFailure(Problem):
    def __init__(self, code, usage, *, delegation=None, assembly=None):
        super().__init__(503, code, "the original SDK turn did not produce a publishable complete result")
        self.usage = usage
        self.delegation = delegation
        self.assembly = assembly


class NativePostTurnPending(Problem):
    def __init__(self):
        super().__init__(503, "NATIVE_POST_TURN_UNKNOWN",
                         "the turn is saved; its original downstream acknowledgment is pending")


NativeExecutor = Callable[
    [NativeReceipt, NativeCheckpointScope, SdkState | None, NativeTurnControl],
    Awaitable[NativeSdkResult],
]


class _Admission(WireModel):
    request_id: Identifier
    submit_digest: Digest


def binding_task_id(agent_id, session_id):
    return "fa-chain-" + material_digest({"agent_id": agent_id, "hosted_session_id": session_id})[:48]


class _AgentSession(WireModel):
    agent_id: Identifier
    agent_name: Identifier
    hosted_session_id: Identifier
    admission_started: bool = Field(default=False, exclude_if=lambda value: not value)
    preparation: NativeSessionPreparation | None = Field(default=None, exclude_if=lambda value: value is None)
    environment: NativeEnvironmentView | None = Field(default=None, exclude_if=lambda value: value is None)
    admitting: _Admission | None = Field(default=None, exclude_if=lambda value: value is None)
    following_supported: bool = Field(default=False, exclude_if=lambda value: not value)
    chain_task_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    # ADR-0009 design premise (a), M7: the start fence's counter, reserved in the v2 snapshot and written only after
    # 2d-ii (H0 decodes this record strictly, so until then it stays at 0, left out).
    start_count: Annotated[int, Field(ge=0)] = Field(default=0, exclude_if=lambda value: value == 0)

    @property
    def task_id(self):
        # Existing bindings keep their original Agent-scoped task until an explicit environment change.
        return self.agent_id if self.chain_task_id is None else self.chain_task_id

    @model_validator(mode="after")
    def preparation_scope(self):
        if self.chain_task_id is not None and self.chain_task_id != binding_task_id(self.agent_id, self.hosted_session_id):
            raise ValueError("the chain task must belong to the persisted Agent session binding")
        if self.preparation is not None and (self.preparation.agent_id != self.agent_id or (
                self.environment is None and self.preparation.hosted_session_id != self.hosted_session_id)):
            raise ValueError("the preparation must belong to this original Agent and session")
        if self.environment is not None:
            expected = (self.environment.hosted_session_id if self.environment.state == "ready"
                        else self.environment.previous_hosted_session_id)
            if (self.environment.state not in {"pending", "ready"} or self.environment.agent_id != self.agent_id
                    or expected != self.hosted_session_id):
                raise ValueError("the environment transition must retain the original Agent binding")
        return self


class NativeTurnRuntime:
    def __init__(self, identity: HostedIdentity, executor: NativeExecutor, *,
                 credential_factory=None, cache_root: Path | None = None, preflight=None,
                 on_terminal=None, on_completed=None, on_unconfirmed=None, bootstrap_source_inactive=None,
                 delivery_retry: RetryPolicy | None = None, task_observer=None, workspace_files=None,
                 state_profile=NATIVE_STATE_PROFILE):
        self.identity, self.executor = identity, executor
        # ADR-0009 M1 2d-i: the profile this runtime declares (FA_NATIVE_TURNS); it labels new conversations' _Meta
        # and, at v2, admits the v1 header too. Digest-bearing items keep the profile of the request that made them.
        self.state_profile = state_profile
        self.credential_factory, self.cache_root, self.preflight = credential_factory, cache_root, preflight
        self._agent_session = None
        self.on_terminal, self.on_completed = on_terminal, on_completed
        self.on_unconfirmed = on_unconfirmed
        self.bootstrap_source_inactive = bootstrap_source_inactive
        self.task_observer = task_observer
        self.workspace_files = workspace_files
        self._recovery_error = None
        self._admission_lock = asyncio.Lock()
        self._clients = None
        # Open-RID removals after a terminal write, run beside the chain instead of before its next turn (``_cas``),
        # and left turns' card closes (``_close_cards_later``); drained together at shutdown.
        self._open_removals: set[asyncio.Task] = set()
        # RIDs whose card close already started here: one per RID per process, however often the RID is read.
        self._card_closes: set[str] = set()
        self.execution_lock = asyncio.Lock()
        self.execution_owner: str | None = None
        # A started turn's execution through its save: future done when it ends -> the task running it. The executor's
        # recovery restart waits for these, with no bound, so its SIGTERM never cuts one mid-turn.
        self.executing: dict[asyncio.Future, asyncio.Task | None] = {}
        # RID -> the control of its run in this process, from "started" through its save (``append`` reads it).
        self.running_controls: dict[str, NativeTurnControl] = {}
        self.progress_recorder = None
        self.on_continuation = None
        self.on_recover_queued = None
        # ADR-0009 §7a (s): asks the API to send a waiting Responses message once it holds its slot (Hosted can not
        # send a Responses request to itself); None (offline): the API sends it at its next touch of the conversation.
        self.on_slot_taken = None
        self.responses_enabled = False
        self.responses_queued: set[str] = set()
        # RID -> the wake events of its control observers; a cancel saved here sets them (the poll stays the authority).
        self._control_wakes: dict[str, set[asyncio.Event]] = {}
        self._queue_retry = delivery_retry or RetryPolicy(
            max_attempts=10, initial_delay=1, max_delay=60,
            retry_on=(SteeringQueueFull, NativePostTurnPending))
        # Usage is recorded even when budget_enforced=false (AGENTS.md), so the post-turn steps outlast an API outage:
        # 1, 3, 9 ... s, capped at 1 h, ~1.9 h over the 10 attempts core allows (the attempt count survives restarts).
        self._notification_retry = delivery_retry or RetryPolicy(
            max_attempts=10, initial_delay=1, backoff_coefficient=3, max_delay=3600,
            retry_on=(SteeringQueueFull, NativePostTurnPending))

        @multi_turn_task(name="fa-native-agent-turn-v1", steerable=True,
                         retry=self._queue_retry)
        async def turn(ctx: TaskContext[NativeTaskInput]):
            return await self.handle_turn(ctx)

        self.chain = turn

        @task(name="fa-native-agent-enqueue-v1", retry=self._queue_retry)
        async def enqueue(ctx: TaskContext[NativeTaskInput]):
            return await self._enqueue_pending(ctx)

        self.enqueue = enqueue

        from .native_left_receipts import WAIT_TIMEOUT_SECONDS

        @task(name="fa-native-left-wait-v1", retry=self._queue_retry, timeout=timedelta(seconds=WAIT_TIMEOUT_SECONDS))
        async def left_wait(ctx: TaskContext[NativeTaskInput]):
            from .native_left_receipts import wait_turn
            return await wait_turn(self, ctx)

        self.left_wait = left_wait

        @task(name="fa-native-terminal-notification-v1", retry=self._notification_retry)
        async def notify(ctx: TaskContext[str]):
            try:
                # A RID refused by version is final only as a read-time view (#889 P3): strict, so an unconfirmed
                # markers read retries rather than delivers an ``accepted`` receipt.
                receipt = await self._refused_view(await self.read(ctx.input), strict=True)
            except Problem as error:
                if error.status >= 500:
                    raise NativePostTurnPending() from None
                raise
            if receipt.status not in {"completed", "unknown", "busy_refused"}:
                # No marker after all, or a current-version resend started it in the gap before the marker was
                # written (#915): it is not final, and its own end notifies.
                return receipt.request_id
            try:
                await self._deliver_notifications(receipt)
            except NativePostTurnPending:
                if ctx.retry_attempt + 1 == self._notification_retry.max_attempts:
                    await self._notification_status(receipt.request_id, "NATIVE_POST_TURN_UNKNOWN")
                raise
            await self._notification_status(receipt.request_id, None)
            return receipt.request_id

        self.notifications = notify

    async def _notification_status(self, rid, code):
        current = await self._rid(rid)
        if current.value.receipt.status == "accepted":
            return  # final only as the refused view (ADR-0009 §9): its receipt is never rewritten (#915)
        if code is None and current.value.receipt.notification_error != "NATIVE_POST_TURN_UNKNOWN":
            return
        if current.value.receipt.notification_error != code:
            receipt = _changed(current.value.receipt, notification_error=code)
            observed, _ = await self._cas(current, _changed(current.value, receipt=receipt))
            if observed.value.receipt.notification_error != code:
                raise NativePostTurnPending()

    async def _root_wake_failure(self, receipt):
        source = receipt.source
        if (receipt.status not in {"unknown", "busy_refused"} or source is None
                or source.kind == "agent_request" or source.continuation is None
                or receipt.agent_id != source.root.agent_id or receipt.conversation_id != source.root.conversation_id):
            return
        parent = await self.read(source.continuation.request_id)
        if (parent.conversation_id != receipt.conversation_id or parent.delegation is None
                or parent.delegation.message_id != source.in_reply_to
                or parent.delegation.recipient_agent_id != source.sender.agent_id):
            raise Problem(409, "NATIVE_STATE_INVALID", "the refused wake has no original sender delegation")
        await self._notification_status(parent.request_id, receipt.failure_code or "NATIVE_TURN_UNKNOWN")

    async def _retain_delegation(self, original, accepted, *, started_only=False):
        current = await self._rid(original.value.receipt.request_id)
        if (current.value.run_token != original.value.run_token
                or current.value.receipt.submit_digest != original.value.receipt.submit_digest
                or current.value.receipt.status == "accepted"):
            raise Problem(409, "NATIVE_TURN_UNKNOWN", "the original delegation belongs to a fenced execution")
        if current.value.receipt.delegation is not None:
            if current.value.receipt.delegation != accepted:
                raise Problem(409, "NATIVE_RID_CONFLICT", "the original turn already retained another delegation")
            return
        if started_only and current.value.receipt.status != "started":
            raise Problem(409, "NATIVE_TURN_UNKNOWN", "the queued acceptance cannot rewrite a settled sender")
        saved = _changed(current.value.receipt, delegation=accepted)
        observed, _ = await self._cas(current, _changed(current.value, receipt=saved))
        if observed.value.receipt.delegation != accepted:
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the accepted delegation receipt is not confirmed")

    async def _enqueue_pending(self, ctx):
        while True:
            try:
                return await self._enqueue_attempt(ctx)
            except _PeerQueueWaiting:
                if ctx.shutdown.is_set():
                    return await self._exit_for_recovery(ctx)
                if ctx.cancel.is_set():
                    supplied = ctx.input if isinstance(ctx.input, NativeTaskInput) else _decode(ctx.input, NativeTaskInput)
                    original = await self._rid(supplied.request_id)
                    if original.value.receipt.status == "accepted":
                        await self._notify_terminal(await self._unknown(
                            original, "NATIVE_TURN_TIMEOUT" if ctx.timeout_exceeded else "NATIVE_TURN_CANCELLED"))
                    return supplied.request_id
                signals = (asyncio.create_task(ctx.shutdown.wait()), asyncio.create_task(ctx.cancel.wait()))
                try:
                    await asyncio.wait(signals, timeout=min(2, self._queue_retry.compute_delay(1)),
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for signal in signals:
                        signal.cancel()
                    await asyncio.gather(*signals, return_exceptions=True)

    async def _enqueue_attempt(self, ctx):
        supplied = ctx.input if isinstance(ctx.input, NativeTaskInput) else _decode(ctx.input, NativeTaskInput)
        if ctx.shutdown.is_set():
            return await self._exit_for_recovery(ctx)
        if supplied.continuation_of is not None:
            if ctx.shutdown.is_set():
                return await self._exit_for_recovery(ctx)
            if ctx.cancel_requested or ctx.timeout_exceeded:
                from .native_continuations import stop_handoff
                await stop_handoff(self, await self.read(supplied.continuation_of), failure_code=(
                    "NATIVE_TURN_CANCELLED" if ctx.cancel_requested else "NATIVE_TURN_TIMEOUT"))
                raise Problem(409, "NATIVE_TURN_CANCELLED" if ctx.cancel_requested else "NATIVE_TURN_TIMEOUT",
                              "the original continuation dispatch stopped before submission")
            from .native_continuations import dispatch_continuation
            try:
                return await dispatch_continuation(self, supplied)
            except Problem as error:
                if error.status >= 500:
                    raise NativePostTurnPending() from None
                raise
        try:
            original = await self._rid(supplied.request_id)
        except Problem as error:
            if error.status >= 500:
                return await self._enqueue_unconfirmed(ctx, error.body["error"]["code"])
            raise
        receipt = original.value.receipt
        if supplied.execution_version is not None and supplied.execution_version != self.identity.agent_version:
            # Only the receipt's own session marks it (review P3 on #889): a retired SID's mark of a RID another
            # session adopted and runs would read "resend" while it runs.
            if receipt.hosted_session_id == self.identity.session_id and await self._mark_refused(supplied):
                if (viewed := _refused_final(receipt)) is not receipt:  # a finished RID already told its sender
                    await self._notify_terminal(viewed)
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the queued input belongs to another execution version")
        if receipt.status != "accepted":
            await self._notify_terminal(receipt)
            return receipt.request_id
        if receipt.source is not None and ctx.cancel.is_set():
            await self._notify_terminal(await self._unknown(
                original, "NATIVE_TURN_TIMEOUT" if ctx.timeout_exceeded else "NATIVE_TURN_CANCELLED"))
            return receipt.request_id
        try:
            binding = await self._require_original_session(receipt=receipt)
            if receipt.cancel_requested:
                await self._notify_terminal(await self._unknown(original, "NATIVE_TURN_CANCELLED"))
                return receipt.request_id
            await self.chain.start(
                task_id=binding.task_id, input_id=receipt.request_id,
                input=NativeTaskInput(request_id=receipt.request_id, accepted_etag=original.etag,
                                      call_id=original.value.call_id, execution_version=self.identity.agent_version))
        except SteeringQueueFull:
            if receipt.source is not None:
                raise _PeerQueueWaiting() from None
            if ctx.retry_attempt + 1 < self._queue_retry.max_attempts:
                raise
            refused = _changed(receipt, status="busy_refused", failure_code="NATIVE_TURN_BUSY")
            current, _ = await self._cas(original, _changed(original.value, receipt=refused))
            await self._notify_terminal(current.value.receipt)
        except Problem as error:
            if error.status >= 500:
                return await self._enqueue_unconfirmed(ctx, error.body["error"]["code"])
            raise
        except _TASK_TRANSPORT_ERRORS:
            return await self._enqueue_unconfirmed(ctx, "NATIVE_TURN_UNKNOWN")
        return receipt.request_id

    async def _exit_for_recovery(self, ctx, original=None):
        """ADR-0009 §5: a started run's graceful deferral first leaves ``deferred:<run_token>``, so its recovered
        re-entry is not counted as a crash. Nothing is marked without a started run in hand (an accepted RID, or the
        RID not read yet). A failed or slow write never holds up the shutdown: the deferral goes on unmarked."""
        token = None if original is None else original.value.run_token
        if token is not None:
            try:
                await asyncio.wait_for(self._mark_deferred(original.value.receipt.request_id, token),
                                       _MARKER_WRITE_SECONDS)
            except Exception as error:  # noqa: BLE001 - whatever the store answers, the shutdown goes on
                log.warning("FA_NATIVE_DEFERRED_MARK_UNCONFIRMED exception_type=%s", type(error).__name__)
        return await ctx.exit_for_recovery()

    async def _mark_deferred(self, rid, token):
        now = time()
        marker = _Deferred(run_token=token, request_id=rid, at=now, expires_at=now + MARKER_TTL_SECONDS)
        async with self._store(MARKERS_STORE, create=True) as store:
            try:
                await store.create_item(f"deferred:{token}", _json(marker))
            except _CONFLICT:
                pass  # an earlier lifetime of this run already deferred it; the first marker stays

    async def _mark_refused(self, supplied):
        """ADR-0009 §9 (Neal decision 8): an input pinned to another version is refused once as ``refused:<RID>``; its
        receipt is never rewritten. Best effort: the refusal stands whatever the store says. True when the marker exists
        afterwards (created now or before): the RID has ended, so the caller tells its sender, again on every re-entry,
        since a lost create reply or notification start leaves no other path (#915); the sender dedupes by RID."""
        now = time()
        marker = _Refused(request_id=supplied.request_id, execution_version=supplied.execution_version,
                          hosted_agent_version=self.identity.agent_version, at=now, expires_at=now + MARKER_TTL_SECONDS)
        try:
            async with self._store(MARKERS_STORE, create=True) as store:
                try:
                    await store.create_item(f"refused:{supplied.request_id}", _json(marker))
                except _CONFLICT:
                    pass  # refused before (a retry of the same input); the first marker stays
            return True
        except Exception as error:  # noqa: BLE001 - the refusal itself never depends on the marker
            log.warning("FA_NATIVE_REFUSED_MARK_UNCONFIRMED exception_type=%s", type(error).__name__)
            return False

    async def _refused(self, rid, *, fresh=False, strict=False):
        """True when ``refused:<RID>`` exists; a missing store is not a refusal. ``fresh``: also looks when this process
        cached the store as missing (another session may have created it since). An unconfirmed read is not a refusal
        either, except when ``strict`` (the gate before a run, review P2 on #889): then it is NativePostTurnPending."""
        try:
            async with self._store(MARKERS_STORE, fresh=fresh) as store:
                return await store.get_item(f"refused:{rid}") is not None
        except Problem as error:
            if strict and error.body["error"]["code"] != "NATIVE_STATE_STORE_MISSING":
                raise NativePostTurnPending() from None
            return False

    async def _refused_view(self, receipt, *, strict=False):
        """ADR-0009 §9: an accepted Invocations receipt with ``refused:<RID>`` reads as the API's §7a (e) view does:
        ``unknown`` / ``NATIVE_EARLIER_VERSION_UNFINISHED`` with the ``earlier_version_unfinished`` notice, final for
        every poller (the workbench and the Teams relay say "請用新訊息再送一次"). A read-time view only: the stored
        receipt keeps its bytes and status. One adopted (M9) and refused afterwards reads final the same way. A missing
        store or an unconfirmed read leaves it an ordinary unfinished receipt (``strict``: NativePostTurnPending)."""
        if (not isinstance(receipt, NativeReceipt) or receipt.status != "accepted"
                or channel(receipt.state_profile) != "invocations"):
            return receipt
        if not await self._refused(receipt.request_id, strict=strict):
            return receipt
        return _refused_final(receipt)

    async def _enqueue_unconfirmed(self, ctx, code):
        if ctx.retry_attempt + 1 < self._queue_retry.max_attempts:
            raise NativePostTurnPending() from None
        original = await self._rid(ctx.input.request_id)
        receipt = original.value.receipt
        if receipt.status == "accepted":
            receipt = await self._unknown(original, code)
        await self._notify_terminal(receipt)
        return receipt.request_id

    async def _defer_enqueue(self, original):
        receipt = original.value.receipt
        identifier = material_digest({"kind": "fa-native-agent-enqueue-v1",
                                      "agent": self.identity.agent_id, "rid": receipt.request_id})
        try:
            await self.enqueue.start(
                task_id=identifier,
                input=NativeTaskInput(request_id=receipt.request_id, accepted_etag=original.etag,
                                      call_id=original.value.call_id, execution_version=self.identity.agent_version))
        except TaskConflictError:
            log.info("FA_NATIVE_ENQUEUE_RETAINED rid=%s", receipt.request_id)
        except _TASK_TRANSPORT_ERRORS as error:
            log.warning("FA_NATIVE_ENQUEUE_START_UNCONFIRMED exception_type=%s", type(error).__name__)
            await self._close_unstarted(original)

    async def initialize(self):
        """Run before the official host's recovery scan; reads never initialize stores."""
        expected = _AgentSession(agent_id=self.identity.agent_id, agent_name=self.identity.agent_name,
                                 hosted_session_id=self.identity.session_id, following_supported=True)
        async with self._store(TURNS_STORE, create=True) as store, self._store(EFFECTS_STORE, create=True):
            item = await store.get_item("agent")
            if item is None:
                try:
                    await store.create_item("agent", _json(expected))
                except _CONFLICT:
                    pass
                item = await store.get_item("agent")
            if item is None:
                raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the original Agent session is unconfirmed")
            original = _decode(item.value, _AgentSession)
            if original.agent_id != self.identity.agent_id:
                raise Problem(403, "NATIVE_STATE_SCOPE", "the store belongs to another Agent")
            self._agent_session = original

    async def _agent_record(self):
        async with self._store(TURNS_STORE) as store:
            item = await store.get_item("agent")
        if item is None:
            raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "the original Agent session record is missing")
        value = _decode(item.value, _AgentSession)
        if (value.agent_id, value.agent_name) != (self.identity.agent_id, self.identity.agent_name):
            raise Problem(403, "NATIVE_STATE_SCOPE", "the session record belongs to another Agent")
        return value, item.etag

    def replaced(self, current, *, located=True):
        """ADR-0009 §2: neither the Agent's current SID nor the ``pending`` follow target (the record names the target's
        SID only at commit) is ever replaced. ``located``: the API located this session by its binding row
        (``NATIVE_LOCATOR_HEADER``); then any other SID is replaced, whatever the versions: platform drift makes
        version numbers non-monotonic, and a fresh session of the same version (M5) has the version of the one it
        replaced. The API reads only through the row's current SID (or a ``pending`` target), and a row behind this
        record is repaired from ``session_read`` (not a record read) instead of surfacing this refusal.

        A call that does not say so (an Agent without a usable row, which the API still reads through the selector's
        candidate SID, or an API from before phase 3) keeps phase 2a's rule: another SID is replaced only when the
        record names a newer version, so a read during a pending retarget or a rolling deploy never meets it."""
        if current.hosted_session_id == self.identity.session_id:
            return False
        environment = current.environment
        if (environment is not None and environment.state == "pending"
                and environment.hosted_session_id == self.identity.session_id):
            return False
        if located:
            return True
        if environment is None:
            version = None if current.preparation is None else current.preparation.hosted_agent_version
        else:
            version = environment.previous_agent_version if environment.state == "pending" else environment.hosted_agent_version
        return version is not None and int(version) > int(self.identity.agent_version)

    async def recover_owned(self):
        """The fixed SDK scan misses a chain whose creation SID predates this execution SID."""
        if self.task_observer is None:
            return
        current, _ = await self._agent_record()
        if current.hosted_session_id != self.identity.session_id or current.environment is None:
            return
        try:
            value = await self.task_observer.owned_input(task_id=current.task_id)
            if value is None:
                self._recovery_error = None
                return
            supplied = _decode(value, NativeTaskInput)
            original = await self._rid(supplied.request_id)
            if (original.value.receipt.hosted_session_id != self.identity.session_id
                    or original.value.call_id != supplied.call_id):
                raise Problem(409, "NATIVE_STATE_SCOPE", "the original recovery input belongs to another execution")
            run = await self.chain.start(
                task_id=current.task_id, input_id=supplied.request_id, input=supplied)
            self._recovery_error = "task_active" if run.is_queued else None
        except (Problem, *_TASK_TRANSPORT_ERRORS, TaskConflictError, SteeringQueueFull) as error:
            self._recovery_error = "task_unconfirmed"
            log.warning("FA_NATIVE_OWNED_RECOVERY_UNCONFIRMED exception_type=%s", type(error).__name__)

    async def session_state(self, *, environment_request_id=None, workspace=False):
        from .native_environment import following_view, current_source_refused
        from .native_files_wire import FILES_PROFILE
        value, etag = await self._agent_record()
        following, refused, crashes = await asyncio.gather(
            following_view(self, value, environment_request_id=environment_request_id),
            current_source_refused(self, value), self._recent_crashes())
        tree = {}
        if workspace:
            # ADR-0009 5b: after the ETag read; off the event loop, so this session's own turns are not delayed.
            if self.workspace_files is None:
                raise Problem(409, "NATIVE_FILES_UNSUPPORTED", "this session has no workspace files helper")
            root_mode, entries = await asyncio.to_thread(self.workspace_files.tree)
            tree = {"workspace_root_mode": root_mode, "workspace_entries": entries}
        return NativeSessionState(**tree, agent_id=value.agent_id, hosted_session_id=value.hosted_session_id,
                                  etag=etag, preparation=value.preparation, environment=value.environment,
                                  following=following,
                                  admission_request_id=None if value.admitting is None else value.admitting.request_id,
                                  files_profile=FILES_PROFILE if self.workspace_files is not None else None,
                                  files_omission_profile="fa-files-omission-v1",
                                  source_invocation_refused=refused,
                                  actual_version=self.identity.agent_version, recent_crashes=crashes)

    async def _crashes(self, store):
        """This session's ``crashes:<SID>`` item and its entries; an expired item has none."""
        item = await store.get_item(f"crashes:{self.identity.session_id}")
        value = None if item is None else _decode(item.value, _Crashes)
        return item, () if value is None or value.expires_at <= time() else value.crashes

    async def _recent_crashes(self):
        """ADR-0009 §5, read only: a missing store or an unconfirmed read is no crash and never fails the state read."""
        try:
            async with self._store(MARKERS_STORE) as store:
                return (await self._crashes(store))[1]
        except Problem as error:
            if error.body["error"]["code"] != "NATIVE_STATE_STORE_MISSING":
                log.warning("FA_NATIVE_CRASHES_READ_UNCONFIRMED code=%s", error.body["error"]["code"])
            return ()

    async def _count_unmarked_reentry(self, original):
        """ADR-0009 §5: a recovered re-entry of a started run that left no ``deferred:`` ended without a graceful
        deferral (crash, OOM, process exit): one crash. Best effort: the re-entry goes on whatever the store says."""
        token = original.value.run_token
        if original.value.receipt.status != "started" or token is None:
            return
        try:
            async with self._store(MARKERS_STORE) as store:
                if await store.get_item(f"deferred:{token}") is not None:
                    return
        except Problem as error:
            if error.body["error"]["code"] != "NATIVE_STATE_STORE_MISSING":
                log.warning("FA_NATIVE_CRASH_UNCONFIRMED code=%s", error.body["error"]["code"])
                return
        await self._count_crash(original.value.receipt)

    async def _count_crash(self, receipt):
        try:
            await self._record_crash(receipt)
        except Exception as error:  # noqa: BLE001 - a lost count never stops the turn or its refusal
            log.warning("FA_NATIVE_CRASH_UNCONFIRMED exception_type=%s", type(error).__name__)

    async def _record_crash(self, receipt):
        """CAS-append one entry to ``crashes:<SID>``: a RID already in it counts once, the newest 8 are kept."""
        key = f"crashes:{self.identity.session_id}"
        async with self._store(MARKERS_STORE, create=True) as store:
            for _ in range(UNKNOWN_MERGES):
                item, kept = await self._crashes(store)
                if any(crash.request_id == receipt.request_id for crash in kept):
                    return
                now = time()
                entry = NativeCrash(at=now, conversation_id=receipt.conversation_id, request_id=receipt.request_id)
                value = _json(_Crashes(hosted_session_id=self.identity.session_id,
                                       crashes=(*kept, entry)[-MAX_CRASHES:], expires_at=now + CRASHES_TTL_SECONDS))
                try:
                    if item is None:
                        await store.create_item(key, value)
                    else:
                        await store.set_item(key, value, if_match=item.etag)
                    return
                except _CONFLICT:
                    continue  # another writer appended meanwhile: read it again
        log.warning("FA_NATIVE_CRASH_UNCONFIRMED reason=conflict")

    async def prepare_session(self, command: NativeSessionPrepare):
        """Bootstrap only: original RID/task/checkpoint records are never rewritten or replayed."""
        value, etag = await self._agent_record()
        if value.preparation is not None:
            if value.hosted_session_id != self.identity.session_id:
                raise Problem(409, "NATIVE_SESSION_CHANGED", "the original preparation belongs to another session")
            return value.preparation
        if (value.hosted_session_id, etag) != (command.expected_session_id, command.expected_etag):
            raise Problem(409, "NATIVE_BOOTSTRAP_CONFLICT", "the original bootstrap comparison changed")
        if value.admission_started:
            raise Problem(409, "NATIVE_BOOTSTRAP_HAS_WORK", "an Agent with native admission cannot change its bootstrap session")
        previous = value.hosted_session_id if value.hosted_session_id != self.identity.session_id else None
        async with self._store(TURNS_STORE) as turns, self._store(EFFECTS_STORE) as effects:
            keys, pending = await turns.list_keys(limit=2), await effects.list_keys(limit=1)
            if keys.has_more or {key.key for key in keys.keys} != {"agent"} or pending.keys or pending.has_more:
                raise Problem(409, "NATIVE_BOOTSTRAP_HAS_WORK", "native work or effects already exist; bootstrap cannot migrate them")
            if self.workspace_files is not None:
                self.workspace_files.initialize()
            prepared = NativeSessionPreparation(
                agent_id=self.identity.agent_id, request_id=command.request_id,
                previous_hosted_session_id=previous, hosted_session_id=self.identity.session_id,
                hosted_agent_version=self.identity.agent_version,
                files_profile="fa-workspace-files-v1" if self.workspace_files is not None else None,
            )
            changed = _changed(value, hosted_session_id=self.identity.session_id, preparation=prepared)
            try:
                await turns.set_item("agent", _json(changed), if_match=etag)
            except _UNCERTAIN as error:
                found = await turns.get_item("agent")
                confirmed = None if found is None else _decode(found.value, _AgentSession)
                if (confirmed is None or confirmed.preparation is None
                        or confirmed.hosted_session_id != self.identity.session_id
                        or confirmed.preparation.previous_hosted_session_id != previous):
                    conflict = isinstance(error, _CONFLICT)
                    raise Problem(409 if conflict else 503,
                                  "NATIVE_BOOTSTRAP_CONFLICT" if conflict else "NATIVE_BOOTSTRAP_UNKNOWN",
                                  "bootstrap update did not confirm the original comparison; read the same session") from None
                prepared, changed = confirmed.preparation, confirmed
        self._agent_session = changed
        return prepared

    async def _require_original_session(self, *, admitting=False, receipt=None, request=None, known=None):
        return (await self._original_session(admitting=admitting, receipt=receipt, request=request, known=known))[0]

    async def _original_session(self, *, admitting=False, receipt=None, request=None, known=None):
        """The checked Agent record and its ETag (None when unknown).

        ``known``: the (record, ETag) this admission read or wrote a moment ago under the admission lock. A check that
        writes nothing uses it as is; the admission mark uses it only with its ETag, as the CAS precondition, and a
        record changed since (e.g. by an environment follow, which does not take this lock) is checked and marked
        again from a fresh read. A stale record for a check that writes nothing only delays a refusal: the mark's CAS
        and the turn start check the session again."""
        if self._agent_session is None:
            raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "native State stores have not initialized")
        if known is not None and (not admitting or known[1] is not None):
            current, etag = known
        else:
            known, (current, etag) = None, await self._agent_record()
        wanted = None if request is None else _Admission(request_id=request.command.request_id, submit_digest=request.sha256)
        retained = wanted is not None and current.admitting == wanted
        if (wanted is not None and current.admitting is not None
                and current.admitting.request_id == wanted.request_id and not retained):
            raise Problem(409, "NATIVE_RID_CONFLICT", "the original admission retains different submitted material")
        if self._recovery_error is not None and receipt is None:
            raise Problem(503, "AGENT_VERSION_FOLLOWING", "the original owned task is not yet confirmed")
        if (current.environment is not None and current.environment.state == "pending"
                and receipt is None and not retained):
            raise Problem(503, "AGENT_VERSION_FOLLOWING", "this Agent is changing environments; no new work was accepted")
        replaced = current.hosted_session_id != self.identity.session_id
        if replaced or (receipt is not None and receipt.hosted_session_id != self.identity.session_id):
            # Only a replaced session names the current SID: when just the receipt's SID differs, the current one is
            # this session, and a 4c resend there would come straight back here.
            raise Problem(409, "NATIVE_SESSION_CHANGED",
                          "read-only: the official chain belongs to the original Hosted session; do not replace its task ID",
                          **({"current_hosted_session_id": current.hosted_session_id} if replaced else {}))
        if admitting and (not current.admission_started or wanted is not None and not retained):
            if wanted is not None and current.admitting is not None:
                raise Problem(503, "NATIVE_ADMISSION_PENDING", "the original admission must be reconciled first")
            marked = _changed(current, admission_started=True,
                              admitting=current.admitting if wanted is None else wanted)
            moved = False
            async with self._store(TURNS_STORE) as store:
                try:
                    ref = await store.set_item("agent", _json(marked), if_match=etag)
                    etag = getattr(ref, "etag", None) or None
                except _UNCERTAIN as error:
                    found = await store.get_item("agent")
                    value = None if found is None else _decode(found.value, _AgentSession)
                    if value == marked:
                        etag = found.etag or None
                    elif known is not None and isinstance(error, _CONFLICT):
                        moved = True
                    else:
                        raise Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "original admission binding is unconfirmed") from None
            if moved:
                log.info("FA_NATIVE_ADMISSION_MARK_REREAD")
                return await self._original_session(admitting=admitting, receipt=receipt, request=request)
            current = marked
        self._agent_session = current
        return current, etag

    async def _settle_admission(self, *, admitted=None, recorded=False):
        """Clear a finished admission intent; returns the Agent record this leaves and its ETag (None when unknown).

        ``admitted``: the (record, ETag) this request just marked and the ``rid:<RID>`` it then created, both under the
        admission lock; the clear uses them instead of reading both again. Any failed write reads them again below.
        ``recorded``: that RID is already in the open-RID set. A RID enters the set before ``admitting`` is cleared, so
        a follow sees an open receipt in one or the other; a failed add raises and leaves ``admitting`` set."""
        from .native_environment import _save_agent
        if admitted is not None:
            (current, etag), saved = admitted
            pending = current.admitting
            if (etag is not None and pending is not None
                    and (saved.value.receipt.request_id, saved.value.receipt.submit_digest) == (
                        pending.request_id, pending.submit_digest)
                    and saved.value.receipt.hosted_session_id == current.hosted_session_id):
                if not recorded:
                    await self._record_open(saved.value.receipt)
                cleared = _changed(current, admitting=None)
                try:
                    return cleared, await _save_agent(self, current, etag, cleared)
                except Problem as error:
                    log.info("FA_NATIVE_ADMISSION_SETTLE_REREAD code=%s", error.body["error"]["code"])
        current, etag = await self._agent_record()
        pending = current.admitting
        if pending is None:
            return current, etag
        try:
            saved = await self._rid(pending.request_id)
        except Problem as error:
            if error.body["error"]["code"] == "NATIVE_RID_MISSING":
                return current, etag
            raise
        if (saved.value.receipt.submit_digest != pending.submit_digest
                or saved.value.receipt.hosted_session_id != current.hosted_session_id):
            raise Problem(409, "NATIVE_STATE_INVALID", "the admission intent differs from its saved receipt")
        await self._record_open(saved.value.receipt)
        cleared = _changed(current, admitting=None)
        return cleared, await _save_agent(self, current, etag, cleared)

    async def _record_open(self, receipt, *, fetched=None):
        """Add an admitted RID to the open-RID set (``native_open_rids``); a terminal receipt needs no entry.
        ``fetched``: the set as the admission reads returned it (``native_open_rids.fetch``)."""
        from .native_open_rids import add
        if receipt.status in {"accepted", "started"}:
            await add(self, (receipt.request_id,), responses=receipt.state_profile == NATIVE_RESPONSES_PROFILE,
                      fetched=fetched)

    async def _admission_reads(self, request):
        """The settle read, this RID, its conversation's meta and the open-RID set, read together: none depends on
        another. A failed RID or meta read is returned and raised where the ordinary order reads it; a failed set
        read only means the add reads it again (None)."""
        from .native_open_rids import fetch

        async def nothing():
            return None

        cid = request.command.conversation_id
        settled, found, meta, fetched = await asyncio.gather(
            self._settle_admission(), self._rid(request.command.request_id),
            nothing() if cid is None else self._meta(cid), fetch(self), return_exceptions=True)
        if isinstance(settled, BaseException):
            raise settled
        for value in (found, meta, fetched):
            if isinstance(value, BaseException) and not isinstance(value, Exception):
                raise value
        if isinstance(fetched, BaseException):
            log.info("FA_NATIVE_OPEN_RIDS_PREFETCH_SKIPPED exception_type=%s", type(fetched).__name__)
            fetched = None
        return settled, found, meta, fetched

    @asynccontextmanager
    async def _store(self, name, *, create=False, fresh=False):
        """One long-lived official client per store name; its TTL is checked once, when this process first opens it.

        Azure SDK clients and credentials are meant to be reused (connection pool, in-memory token cache); a store's
        TTL cannot change after creation (ADR-0007 §1), so re-reading it on every open only added a round trip.
        """
        try:
            clients = self._store_clients()
            store = await self._open_store(clients, name, create=create, fresh=fresh)
            clients.users[name] = clients.users.get(name, 0) + 1
            try:
                yield store
            except FoundryStorageNotFoundError:
                # A cached client skipped the open-time existence read; confirm it only on this error path.
                if await self._store_missing(clients, name, store):
                    raise Problem(404, "NATIVE_STATE_STORE_MISSING",
                                  "the State store does not exist; nothing was created") from None
                raise
            finally:
                clients.users[name] -= 1
                if clients.stores.get(name) is not store and not clients.users[name]:
                    await store.aclose()
                await self._trim_stores(clients)
        except _UNCERTAIN as exc:
            log.warning("FA_NATIVE_STATE_UNCONFIRMED exception_type=%s errno=%s winerror=%s",
                        type(exc).__name__, getattr(exc, "errno", None), getattr(exc, "winerror", None))
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the original State store outcome is unconfirmed") from None

    def _store_clients(self):
        loop = asyncio.get_running_loop()
        if self._clients is None or self._clients.loop is not loop:
            # Clients are bound to the loop that opened them; another loop (tests) starts its own set.
            self._clients = _StoreClients(loop)
        return self._clients

    async def _open_store(self, clients, name, *, create, fresh=False):
        from .native_checkpoint import open_durable_store

        cached = clients.stores.get(name)
        # ``fresh``: a read that decides whether work runs never trusts a miss cached earlier (review P3 on #889).
        if cached is None and not (create or fresh) and name in clients.missing:
            raise Problem(404, "NATIVE_STATE_STORE_MISSING", "the State store does not exist; nothing was created")
        if cached is None:
            async with clients.lock:
                cached = clients.stores.get(name)
                if cached is None:
                    if clients.credential is None and self.credential_factory is not None:
                        clients.credential = await clients.credential_scope.enter_async_context(
                            self.credential_factory())
                    try:
                        cached = await open_durable_store(
                            name, create=create, credential=clients.credential,
                            item_ttl_seconds=MARKER_TTL_SECONDS if name == MARKERS_STORE else -1)
                    except Problem as error:
                        if name == MARKERS_STORE and error.body["error"]["code"] == "NATIVE_STATE_STORE_MISSING":
                            clients.missing.add(name)
                        raise
                    clients.stores[name] = cached
                    # Under the lock: a miss a read cached while this create waited for it is healed here, so it never
                    # resurfaces once this client is evicted (review P3 on #889).
                    clients.missing.discard(name)
        clients.stores.move_to_end(name)
        return cached

    @staticmethod
    async def _store_missing(clients, name, store):
        try:
            await store.get()
        except FoundryStorageNotFoundError:
            if clients.stores.get(name) is store:
                clients.stores.pop(name)
            return True
        except _UNCERTAIN:
            return False
        return False

    @staticmethod
    async def _trim_stores(clients):
        while len(clients.stores) > STORE_CLIENTS:
            name = next((key for key in clients.stores if not clients.users.get(key)), None)
            if name is None:
                return
            await clients.stores.pop(name).aclose()

    def _check_rid(self, item, rid):
        if item is None:
            raise Problem(404, "NATIVE_RID_MISSING", "the RID is not visible; this does not prove nonacceptance")
        value = _decode(item.value, _Rid)
        if (item.key != f"rid:{rid}" or value.receipt.request_id != rid
                or value.receipt.agent_id != self.identity.agent_id or not item.etag):
            raise Problem(403, "NATIVE_STATE_SCOPE", "the receipt belongs to another Agent or request")
        return _Versioned(value, item.etag)

    async def _rid(self, rid):
        async with self._store(TURNS_STORE) as store:
            return self._check_rid(await store.get_item(f"rid:{rid}"), rid)

    async def _observed_read(self, rid, *, cid=None):
        """The API's read of a RID: a receipt an earlier SID left is adopted (never started, M9) or gets one pass of
        the stall rule (``started``) first (ADR-0009 §7a); any other receipt costs no extra read."""
        from .native_left_receipts import observe_left

        original = await self._rid(rid)
        if cid is not None:
            self._address(original.value.receipt, cid)
        if await observe_left(self, original):
            original = await self._rid(rid)
        return await self._refused_view(await self.read(rid, original=original))

    async def read(self, rid, *, claim=True, original=None):
        """``claim``: an accepted Responses receipt waiting in its conversation's left queue (ADR-0009 §7a (s)) takes
        the slot when its turn comes; its own wait task does the same without any read. ``original``: this RID just
        read by the caller."""
        original = original or await self._rid(rid)
        receipt = original.value.receipt
        if receipt.state_profile == NATIVE_RESPONSES_PROFILE and receipt.status == "accepted":
            if claim and self.responses_enabled and receipt.hosted_session_id == self.identity.session_id:
                from .native_left_receipts import turn_order

                order = await turn_order(self, original)
                if order.go and order.queued:
                    return await self._response_slot(original, wait=False)
            return await self._response_dispatch(receipt)
        from .native_relay_delivery import project
        return await project(self, receipt)

    async def _response_dispatch(self, receipt, *, meta=None, previous=None):
        """``meta``: the slot this request just confirmed; ``previous``: a terminal receipt it already read."""
        if meta is None:
            try:
                meta, _ = await self._meta(receipt.conversation_id)
            except Problem as error:
                if error.body["error"]["code"] not in {"NATIVE_CONVERSATION_MISSING", "NATIVE_STATE_STORE_MISSING"}:
                    raise
                return _changed(receipt, dispatch=NativeResponseDispatch(state="not_taken"))
        if channel(meta.state_profile) != channel(receipt.state_profile):
            raise Problem(409, "NATIVE_STATE_SCOPE", "the original conversation uses another state profile")
        if meta.rid is None:
            previous = None
        elif previous is None or previous.request_id != meta.rid:
            previous = (await self._rid(meta.rid)).value.receipt
        state = ("already_queued" if receipt.request_id in self.responses_queued else
                 "needs_response" if meta.inflight_rid == receipt.request_id and self.responses_enabled
                 and not (not receipt.human_origin and self.response_queue_full()) else "not_taken")
        return _changed(receipt, dispatch=NativeResponseDispatch(
            state=state, inflight_request_id=meta.inflight_rid,
            previous_response_id=None if previous is None else previous.response_id))

    def response_queue_full(self):
        # The active Responses entry stays in the duplicate set, but is not one of the nine waiters.
        capacity = 10 if self.execution_owner in self.responses_queued or not self.execution_lock.locked() else 9
        return len(self.responses_queued) >= capacity

    async def _response_slot(self, original, *, known=None, fresh=False, wait=True):
        """Take the conversation's Responses slot for this RID.

        ``known``: the (meta, ETag) this admission just checked, used as the CAS precondition; a meta changed since
        (e.g. the previous turn's save) is read again. ``fresh``: this admission just named a new CID, so its meta is
        created without reading first (an existing one fails the create and is read back). ``wait``: a message that
        must wait joins the conversation's left queue with a wait task of its own (False: the caller is that task or
        a read, which only look again)."""
        from .native_checkpoint import conversation_store_name
        from .native_left_receipts import left, turn_order

        receipt = original.value.receipt
        # The slot this request created, wrote or read with an acknowledgment; None leaves the decision to a read-back.
        confirmed = previous = waiting = None
        moved = False
        async with self._store(conversation_store_name(receipt.conversation_id),
                               create=receipt.command.conversation_id is None) as store:
            if known is not None:
                meta, etag = known
            elif fresh:
                meta = etag = None
            else:
                item = await store.get_item("meta")
                meta, etag = (None, None) if item is None else (_decode(item.value, _Meta), item.etag)
            if meta is None:
                meta = _Meta(
                    agent_id=self.identity.agent_id, hosted_session_id=receipt.hosted_session_id,
                    conversation_id=receipt.conversation_id, capture=receipt.capture,
                    sdk_session_id="sdk-" + material_digest({
                        "agent": self.identity.agent_id, "cid": receipt.conversation_id})[:48],
                    state_profile=NATIVE_RESPONSES_PROFILE, inflight_rid=receipt.request_id,
                )
                try:
                    await store.create_item("meta", _json(meta))
                    confirmed = meta
                except _UNCERTAIN as error:
                    log.warning("FA_NATIVE_SLOT_UNCONFIRMED exception_type=%s", type(error).__name__)
                    item = await store.get_item("meta")
                    if item is None:
                        return _changed(receipt, dispatch=NativeResponseDispatch(state="not_taken"))
                    meta, etag = _decode(item.value, _Meta), item.etag
            if ((meta.agent_id, meta.conversation_id, meta.state_profile) != (
                    self.identity.agent_id, receipt.conversation_id, receipt.state_profile)
                    or not await self._capture_fits(meta, receipt.capture)):
                raise Problem(409, "NATIVE_STATE_SCOPE", "the original conversation scope or capture differs")
            if meta.inflight_rid != receipt.request_id:
                holder = None if meta.inflight_rid is None else await self._rid(meta.inflight_rid)
                if holder is not None:
                    self._address(holder.value.receipt, receipt.conversation_id)
                # ADR-0009 §7a (s) and M9: a holder an earlier SID left is adopted when it never started, otherwise
                # released (this meta CAS; the receipt is not touched) once it can no longer complete. Until then, while
                # an adopted holder is not sent yet, and while older messages wait in the conversation's left queue,
                # this one is accepted and waits its turn, never refused. Only this session's own holder that was
                # sent (``slot:``) keeps the one-at-a-time rule.
                order = await turn_order(self, original, meta=meta, held=holder)
                if not order.go:
                    if order.queued or not order.behind.startswith("slot:"):
                        waiting = order.behind
                    elif receipt.human_origin:
                        refused = _changed(receipt, status="busy_refused", failure_code="NATIVE_TURN_BUSY")
                        current, _ = await self._cas(original, _changed(original.value, receipt=refused))
                        return await self._notify_terminal(current.value.receipt)
                    else:
                        return await self._response_dispatch(receipt)
                elif holder is not None:
                    # A left holder is read again after the order check (it may just have been marked); the CAS below
                    # stays on the meta ETag read before it, so a slot taken meanwhile is never overwritten.
                    previous = ((await self._rid(meta.inflight_rid)).value.receipt if left(self, holder.value.receipt)
                                else holder.value.receipt)
            if waiting is not None:
                pass
            elif meta.inflight_rid != receipt.request_id:
                replacement = _changed(meta, inflight_rid=receipt.request_id)
                try:
                    await store.set_item("meta", _json(replacement), if_match=etag)
                    confirmed = replacement
                except _UNCERTAIN as error:
                    if known is not None and isinstance(error, _CONFLICT):
                        moved = True
                    else:
                        log.warning("FA_NATIVE_SLOT_UNCONFIRMED exception_type=%s", type(error).__name__)
            else:
                confirmed = meta
        if waiting is not None:
            return await self._wait_for_slot(original, waiting, wait=wait)
        if moved:
            log.info("FA_NATIVE_SLOT_REREAD")
            return await self._response_slot(original, wait=wait)
        if confirmed is not None:
            return await self._response_dispatch(receipt, meta=confirmed, previous=previous)
        # An unacknowledged slot write: the actual read-back, not the missing ACK, decides whether step two is available.
        return await self.read(receipt.request_id, claim=False)

    async def _wait_for_slot(self, original, behind, *, wait):
        """A Responses message waiting its turn (ADR-0009 §7a (s)): accepted and ``not_taken``. When an older queued
        message is first and the slot is free, that one takes it now, so the API's advance of this send dispatches it:
        oldest first, for every caller."""
        from .native_left_receipts import defer

        if wait:
            try:
                await defer(self, original, behind)
            except _TASK_TRANSPORT_ERRORS as error:
                log.warning("NATIVE_LEFT_WAIT_START_UNCONFIRMED exception_type=%s", type(error).__name__)
                raise Problem(503, "NATIVE_TURN_UNKNOWN", "the accepted message's wait is unconfirmed; retain the RID") from None
        if behind.startswith("queue:"):
            try:
                await self._response_slot(await self._rid(behind.removeprefix("queue:")), wait=False)
            except Problem as error:
                log.info("NATIVE_LEFT_QUEUE_HEAD_UNCONFIRMED code=%s", error.body["error"]["code"])
        return await self._response_dispatch(original.value.receipt)

    async def _cas(self, original: _Versioned, replacement: _Rid):
        """A lost ACK may be our own write; never retry a write using a new ETag."""
        result = await self._cas_rid(original, replacement)
        if result[1] and replacement.receipt.status not in {"accepted", "started"}:
            # Only after the terminal write, and in the background so the chain's next turn does not wait two round
            # trips for it: a removal that fails, never runs or is cut short leaves a stale entry, never a hidden one.
            self._remove_open_later(replacement.receipt.request_id)
        return result

    def _remove_open_later(self, rid):
        from .native_open_rids import remove

        async def removal():
            try:
                await remove(self, rid)
            except Exception as error:  # noqa: BLE001 - best effort; the next follow re-checks a stale entry
                log.warning("FA_NATIVE_OPEN_RID_KEPT exception_type=%s", type(error).__name__)

        self._beside(removal())

    def _close_cards_later(self, receipt):
        """ADR-0009 gate 6: close a left turn's cards (``close_cards``) beside the admission or status read that found
        it can no longer complete, never before it answers (no API round trip on the send path)."""
        from .native_left_receipts import close_cards

        if receipt.request_id in self._card_closes:
            return
        self._card_closes.add(receipt.request_id)

        async def closing():
            try:
                await close_cards(self, receipt)
            except Exception as error:  # noqa: BLE001 - best effort; an unclosed card keeps its TTL
                log.warning("NATIVE_LEFT_RECEIPT_CARDS_UNCONFIRMED exception_type=%s", type(error).__name__)

        self._beside(closing())

    def _beside(self, work):
        task = asyncio.get_running_loop().create_task(work)
        self._open_removals.add(task)
        task.add_done_callback(self._open_removals.discard)

    async def drain_open_removals(self, timeout=None):
        """Wait for the open-RID removals and card closes in flight, and for those started meanwhile (a turn ending
        while this waits); any still running after ``timeout`` is cancelled (a stale entry, a card left to its TTL)."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        late = ()
        while running := tuple(task for task in self._open_removals if not task.done()):
            left = None if deadline is None else max(0, deadline - loop.time())
            _, late = await asyncio.wait(running, timeout=left)
            if late:
                break
        for task in late:
            task.cancel()
        await asyncio.gather(*late, return_exceptions=True)
        if late:
            log.warning("FA_NATIVE_OPEN_RID_REMOVALS_CUT count=%d", len(late))

    async def aclose(self):
        """VM shutdown: give the open-RID removals in flight a short bound to finish. Called again after the SDK
        executor closes, for the removals of turns that ended while it stopped them (each call has its own bound)."""
        await self.drain_open_removals(timeout=_OPEN_REMOVALS_CLOSE_SECONDS)

    async def _cas_rid(self, original: _Versioned, replacement: _Rid):
        payload = _json(replacement)
        rid = original.value.receipt.request_id
        async with self._store(TURNS_STORE) as store:
            try:
                ref = await store.set_item(f"rid:{rid}", payload, if_match=original.etag)
                return _Versioned(replacement, ref.etag), True
            except _UNCERTAIN as exc:
                current = self._check_rid(await store.get_item(f"rid:{rid}"), rid)
                if current.etag == original.etag and current.value == original.value:
                    log.warning("FA_NATIVE_RID_WRITE_UNCONFIRMED transition=%s exception_type=%s errno=%s winerror=%s",
                                replacement.receipt.status, type(exc).__name__,
                                getattr(exc, "errno", None), getattr(exc, "winerror", None))
                    raise _UnchangedRidWrite(
                        503, "NATIVE_STATE_STORE_UNKNOWN",
                        "the original RID transition was not confirmed; unchanged state is not completion") from None
                return current, current.value == replacement

    async def _immutable(self, store, key, value):
        payload = _json(value)
        try:
            await store.create_item(key, payload)
        except _CONFLICT:
            pass
        # Unknown transport errors propagate; no fallback provider or new key is used.
        found = await store.get_item(key)
        if found is None or canonical_bytes(found.value) != canonical_bytes(payload):
            raise Problem(409, "NATIVE_STATE_INVALID", "the immutable item is missing or differs")

    async def submit(self, request: NativeSubmit):
        request = NativeSubmit.model_validate_json(request.model_dump_json())
        return await self._submit(request)

    async def receive(self, request: NativeAgentReceive):
        request = NativeAgentReceive.model_validate_json(request.model_dump_json())
        if request.source.recipient_agent_id != self.identity.agent_id:
            raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "the resolved message recipient is not this Agent")
        return await self._submit(request)

    async def receive_routine(self, request: NativeRoutineSubmit):
        from .native_routine_wire import native_routines_enabled

        if not native_routines_enabled():
            raise Problem(403, "NATIVE_ROUTINES_DISABLED", "native routine admission is not enabled")
        request = NativeRoutineSubmit.model_validate_json(request.model_dump_json())
        if request.routine.agent_id != self.identity.agent_id:
            raise Problem(403, "NATIVE_STATE_SCOPE", "the original routine belongs to another Agent")
        return await self._submit(request)

    async def continue_turn(self, request):
        from .behavior_native_turn_wire import NativeContinuationSubmit
        from .native_continuations import check_submission

        request = NativeContinuationSubmit.model_validate_json(request.model_dump_json())
        async with self._admission_lock:
            await check_submission(self, request)
            agent = await self._settle_admission()
            return await self._submit_locked(request, agent=agent)

    async def _submit(self, request: NativeSubmit | NativeAgentReceive | NativeRoutineSubmit):
        async with self._admission_lock:
            with phase_timing("native_submit_pending", job_id=timing_id(request.command.request_id)):
                if self._recovery_error is not None:
                    await self.recover_owned()
                agent, found, meta, fetched = await self._admission_reads(request)
            return await self._submit_locked(request, agent=agent, found=found, meta=meta, fetched=fetched)

    async def _admission_checks(self, request, *, agent=None, meta=_UNREAD):
        """Checks before ``rid:<RID>`` is created; only the last one marks the admission it is about to create.

        ``agent`` is the (record, ETag) this request's settle just read and ``meta`` its conversation read (or its
        failure); returns the marked (record, ETag) and the conversation's (meta, ETag) it checked."""
        if request.state_profile == NATIVE_RESPONSES_PROFILE and not self.responses_enabled:
            raise Problem(403, "NATIVE_TURNS_DISABLED", "the Responses handler is not enabled")
        await self._require_original_session(request=request, known=agent)
        if self.preflight is not None:
            self.preflight(request.capture)
        from .native_user_images import check_user_images
        await check_user_images(self, request)
        checked = None
        if request.command.conversation_id is not None:
            checked = (await self._meta(request.command.conversation_id) if meta is _UNREAD
                       else _raised(meta))
            meta = checked[0]
            if channel(meta.state_profile) != channel(request.state_profile):
                raise Problem(409, "NATIVE_STATE_SCOPE", "the original conversation must retain its state profile")
            if not await self._capture_fits(meta, request.capture):
                raise Problem(409, "NATIVE_CAPTURE_CHANGED", "continue with the original conversation capture")
            # Only here, before rid:<RID> exists, so a refusal leaves no receipt (not_dispatched).
            await self._switch_fits(meta, request.capture)
            blocker = await self._continuation_blocker(meta, self.identity.session_id)
            if blocker is not None:
                raise Problem(409, blocker, "the original conversation has no completed checkpoint to restore")
        return await self._original_session(admitting=True, request=request, known=agent), checked

    async def _submit_locked(self, request: NativeSubmit | NativeAgentReceive | NativeRoutineSubmit, *, agent=None,
                             found=_UNREAD, meta=_UNREAD, fetched=None):
        from .sdk_telemetry import remember
        remember(request.command.request_id)
        # Content-free admission segments (README "rtt-a-timing").
        timed, started = timing_id(request.command.request_id), perf_counter()
        try:
            original = await self._rid(request.command.request_id) if found is _UNREAD else _raised(found)
        except Problem as exc:
            if exc.body["error"]["code"] != "NATIVE_RID_MISSING":
                raise
            original = None
        elapsed_timing("native_submit_rid_read", started, job_id=timed)
        marked = checked = None
        recorded = False
        if original is None:
            try:
                with phase_timing("native_submit_checks", job_id=timed):
                    marked, checked = await self._admission_checks(request, agent=agent, meta=meta)
            except Problem as refusal:
                # A definite refusal before ``rid:<RID>`` exists admitted nothing; the same RID may be sent again.
                code = refusal.body["error"]["code"]
                if (400 <= refusal.status < 500 or code == "AGENT_VERSION_FOLLOWING") and code != "NATIVE_RID_CONFLICT":
                    refusal.body["error"]["not_dispatched"] = True
                raise
            cid = request.command.conversation_id or request.new_conversation_id or "native-" + uuid4().hex
            receipt = NativeReceipt(
                state_profile=request.state_profile,
                agent_id=self.identity.agent_id, hosted_session_id=self.identity.session_id,
                request_id=request.command.request_id,
                conversation_id=cid, command=request.command, capture=request.capture,
                submit_digest=request.sha256, capture_digest=request.capture.sha256, status="accepted",
                source=getattr(request, "source", None),
                routine=getattr(request, "routine", None),
                continuation=getattr(request, "continuation", None),
                continuation_available=self.on_continuation is not None,
            )
            record = _Rid(receipt=receipt, call_id=get_request_context().call_id)
            payload = _json(record)
            started = perf_counter()
            async with self._store(EFFECTS_STORE):
                pass

            async def create():
                async with self._store(TURNS_STORE) as store:
                    try:
                        ref = await store.create_item(f"rid:{receipt.request_id}", payload)
                        return _Versioned(record, ref.etag)
                    except _UNCERTAIN as exc:
                        item = await store.get_item(f"rid:{receipt.request_id}")
                        if item is None:
                            log.warning("FA_NATIVE_ACCEPT_UNCONFIRMED exception_type=%s errno=%s winerror=%s",
                                        type(exc).__name__, getattr(exc, "errno", None),
                                        getattr(exc, "winerror", None))
                            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN",
                                          "acceptance is unconfirmed; retain the RID") from None
                        return self._check_rid(item, receipt.request_id)

            async def record_open():
                # Alongside the create and against the set the admission reads returned, so the common case adds no
                # round trip to the send path (one CAS beside one create); settling adds it again if this failed.
                try:
                    await self._record_open(receipt, fetched=fetched)
                    return True
                except Problem as error:
                    log.info("FA_NATIVE_OPEN_RID_DEFERRED code=%s", error.body["error"]["code"])
                    return False

            created, recorded = await asyncio.gather(create(), record_open(), return_exceptions=True)
            for value in (created, recorded):
                if isinstance(value, BaseException):
                    raise value
            original = created
            elapsed_timing("native_submit_create", started, job_id=timed)
        if original.value.receipt.submit_digest != request.sha256:
            raise Problem(409, "NATIVE_RID_CONFLICT", "the RID already names a different original request or capture")
        with phase_timing("native_submit_settle", job_id=timed):
            agent = await self._settle_admission(admitted=None if marked is None else (marked, original),
                                                 recorded=recorded is True)
        was_terminal = original.value.receipt.status in {"completed", "unknown", "busy_refused"}
        if original.value.receipt.status == "accepted":
            with phase_timing("native_submit_session", job_id=timed):
                binding = await self._require_original_session(receipt=original.value.receipt, known=agent)
            if original.value.receipt.cancel_requested:
                return await self._notify_terminal(await self._unknown(original, "NATIVE_TURN_CANCELLED"))
            if original.value.receipt.state_profile == NATIVE_RESPONSES_PROFILE:
                if not self.responses_enabled:
                    return await self.read(request.command.request_id)
                with phase_timing("native_submit_slot", job_id=timed):
                    return await self._response_slot(original, known=checked,
                                                     fresh=marked is not None and request.command.conversation_id is None)
            if marked is None:
                # ADR-0009 §9: a resend of a RID refused by version reads final; it never runs (``_handle_turn``).
                viewed = await self._refused_view(original.value.receipt)
                if viewed is not original.value.receipt:
                    # Told again: the refusal's own notice may have been lost (``_mark_refused``); dedupes by RID.
                    return await self._notify_terminal(viewed)
            try:
                with phase_timing("native_submit_start", job_id=timed):
                    await self.chain.start(
                        task_id=binding.task_id, input_id=request.command.request_id,
                        input=NativeTaskInput(request_id=request.command.request_id,
                                              accepted_etag=original.etag, call_id=original.value.call_id,
                                              execution_version=self.identity.agent_version),
                    )
            except SteeringQueueFull:
                if isinstance(request, (NativeAgentReceive, NativeRoutineSubmit)):
                    await self._defer_enqueue(original)
                    return await self.read(request.command.request_id)
                receipt = _changed(original.value.receipt, status="busy_refused", failure_code="NATIVE_TURN_BUSY")
                current, _ = await self._cas(original, _changed(original.value, receipt=receipt))
                return await self._notify_terminal(current.value.receipt)
            except TaskConflictError:
                raise Problem(409, "NATIVE_TURN_UNKNOWN", "the original chain is bound or active elsewhere; retain the RID") from None
            except _TASK_TRANSPORT_ERRORS as error:
                log.warning("FA_NATIVE_START_UNCONFIRMED exception_type=%s status=%s classification=%s",
                            type(error).__name__, getattr(error, "status", None),
                            getattr(error, "classification", None))
                return await self._close_unstarted(original)
        result = await self.read(request.command.request_id)
        return await self._notify_terminal(result) if was_terminal else result

    async def _notify_terminal(self, receipt, *, continuation_stopped=False):
        if receipt.status not in {"completed", "unknown", "busy_refused"}:
            return receipt
        if receipt.continuation is not None:
            from .native_continuations import pending_handoffs
            try:
                await pending_handoffs(self, remove=receipt.continuation.continuation_of)
            except Problem as error:
                log.warning("NATIVE_CONTINUATION_HANDOFF_PENDING code=%s", error.body["error"]["code"])
        if self.on_terminal is None and self.on_completed is None and self.on_unconfirmed is None:
            return receipt
        identifier = material_digest({"kind": ("fa-native-continuation-stopped-v1" if continuation_stopped
                                              else "fa-native-terminal-notification-v1"),
                                      "agent": self.identity.agent_id, "rid": receipt.request_id})
        try:
            with phase_timing("native_notify_start", job_id=timing_id(receipt.request_id)):
                await self.notifications.start(task_id=identifier, input=receipt.request_id)
        except TaskConflictError:
            log.info("FA_NATIVE_NOTIFICATION_RETAINED rid=%s", receipt.request_id)
        except _TASK_TRANSPORT_ERRORS:
            raise NativePostTurnPending() from None
        return receipt

    async def _deliver_notifications(self, receipt):
        from .native_continuations import read_continuation, schedule_continuation

        pending = False
        continuing = False
        try:
            if receipt.continuation_available:
                try:
                    view = await read_continuation(self, receipt.request_id)
                except Problem as error:
                    if error.body["error"]["code"] != "NATIVE_CONTINUATION_MISSING":
                        raise
                else:
                    continuing = view.owns_outcome
                    if continuing and view.state == "prepared":
                        await schedule_continuation(self, receipt)
        except Problem as error:
            if error.status >= 500:
                raise NativePostTurnPending() from None
            raise
        try:
            if not continuing:
                await self._root_wake_failure(receipt)
        except Problem as exc:
            pending = True
            log.warning("FA_NATIVE_WAKE_VISIBILITY_PENDING code=%s", exc.body["error"]["code"])
        if self.on_terminal is not None:
            try:
                await self.on_terminal(receipt)
            except Exception as exc:
                pending = True
                log.warning("FA_NATIVE_USAGE_PENDING exception_type=%s", type(exc).__name__)
        if receipt.status == "completed" and self.on_completed is not None:
            try:
                await self.on_completed(receipt, receipt.result)
            except Exception as exc:
                pending = True
                log.warning("FA_NATIVE_POST_TURN_UNKNOWN exception_type=%s", type(exc).__name__)
        if not continuing and receipt.status in {"unknown", "busy_refused"} and self.on_unconfirmed is not None:
            try:
                await self.on_unconfirmed(receipt)
            except Exception as exc:
                pending = True
                log.warning("FA_NATIVE_OUTCOME_NOTICE_PENDING exception_type=%s", type(exc).__name__)
        if pending:
            raise NativePostTurnPending()
        return receipt

    async def _meta(self, cid, *, store=None):
        from .native_checkpoint import conversation_store_name

        if store is None:
            async with self._store(conversation_store_name(cid)) as current:
                return await self._meta(cid, store=current)
        item = await store.get_item("meta")
        if item is None:
            raise Problem(404, "NATIVE_CONVERSATION_MISSING", "the original conversation is not visible")
        meta = _decode(item.value, _Meta)
        if (meta.agent_id, meta.conversation_id) != (self.identity.agent_id, cid):
            raise Problem(403, "NATIVE_STATE_SCOPE", "conversation belongs to another Agent")
        return meta, item.etag

    def _scope(self, meta):
        return NativeCheckpointScope(
            agent_id=meta.agent_id, conversation_id=meta.conversation_id, sdk_session_id=meta.sdk_session_id,
            model_provider=meta.capture.template.settings.model_provider,
        )

    async def _capture_fits(self, meta, capture):
        """The conversation keeps its capture; only its model may change. _allocate then adopts the new model.

        By default only before any saved SDK state: with no completed turn (e.g. the first one was refused by its
        model) the next turn starts an empty SDK session, so nothing produced by the old model is resumed.
        With ``FA_NATIVE_MODEL_SWITCH=1`` also after completed turns: the turn restores the published checkpoint
        (reasoning already stripped) cold with the new model; a kept SDK on the old model is discarded
        (NativeResidentPool.continue_live). Each RID keeps its own capture.
        """
        if not _connector_ids(capture) <= _connector_ids(meta.capture):
            return False
        if _conversation_capture(meta.capture) == _conversation_capture(capture):
            return True
        if _conversation_capture(meta.capture, include_model=False) != _conversation_capture(capture, include_model=False):
            return False
        if native_model_switch_enabled():
            return True
        return meta.base_ckpt is None and (
            meta.rid is None or (await self._rid(meta.rid)).value.receipt.status != "completed")

    async def _switch_fits(self, meta, capture):
        """A new model must hold the conversation as FA last measured it; otherwise nothing is admitted.

        The measure is the last completed turn's own SDK context-window snapshot (``session.usage_info``
        ``current_tokens``, PA-16 B ``sdk_context``); the limit is the new model's declared ``max_prompt_tokens``
        (else its context window). When the conversation's last turn did not complete, the turn restores
        ``meta.base_ckpt`` instead, so the turn that saved it (its lineage) is measured. Nothing is compacted here.
        An unmeasured turn or an undeclared limit is not a refusal: unknown is not over, and the runtime's own
        context-limit failure still applies."""
        if meta.capture.model.id == capture.model.id or meta.rid is None or capture.model_capacity is None:
            return
        limits = capture.model_capacity.limits
        limit = limits.max_prompt_tokens or limits.context_window_tokens
        if limit is None:
            return
        last = (await self._rid(meta.rid)).value.receipt
        if last.status != "completed":
            lineage = None if meta.base_ckpt is None else meta.base_ckpt.lineage
            last = None if lineage is None else (await self._rid(lineage.request_id)).value.receipt
        inference = (None if last is None or last.status != "completed" or last.assembly is None
                     else last.assembly.inference)
        measured = None if inference is None else next(
            (snapshot.current_tokens for snapshot in reversed(inference.sdk_context)
             if snapshot.current_tokens is not None), None)
        if measured is not None and measured > limit:
            raise Problem(409, "NATIVE_MODEL_CONTEXT_EXCEEDED",
                          "the conversation is longer than the selected model can hold; nothing was run",
                          model=capture.model.id)

    def _checkpoint_turn(self, receipt, meta):
        return NativeCheckpointTurn(
            scope=self._scope(meta), request_id=receipt.request_id, turn_number=meta.n,
            capture_digest=receipt.capture_digest, profile=receipt.capture.template.settings.checkpoint_profile,
            previous=meta.base_ckpt,
        )

    async def _turn(self, store, receipt, *, n=None, item=None):
        number = receipt.turn_number if n is None else n
        if item is None:
            item = await store.get_item(f"turn:{number}")
        if item is None:
            raise Problem(409, "NATIVE_RESULT_MISSING", "the original turn has no durable result")
        turn = _decode(item.value, _Turn)
        if ((turn.request_id, turn.conversation_id, turn.n, turn.submit_digest) !=
                (receipt.request_id, receipt.conversation_id, number, receipt.submit_digest)
                or receipt.result is not None and receipt.result != turn.reference()):
            raise Problem(409, "NATIVE_RESULT_INCOMPLETE", "the saved result differs from the original receipt")
        for checkpoint in turn.checkpoints:
            lineage = checkpoint.lineage
            if lineage is not None and (lineage.request_id, lineage.turn_number, lineage.capture_digest) != (
                    receipt.request_id, number, receipt.capture_digest):
                raise Problem(409, "SDK_STATE_SOURCE_UNVERIFIED", "the checkpoint lineage differs from its original turn")
        return turn

    async def _allocate_timed(self, original):
        """Turn number and checkpoint restore, timed without content (FA_TIMING)."""
        with phase_timing("native_allocate", job_id=timing_id(original.value.receipt.request_id)):
            return await self._allocate(original)

    async def _allocate(self, original):
        from .native_checkpoint import conversation_store_name, load_checkpoint

        receipt = original.value.receipt
        cid = receipt.conversation_id
        async with self._store(conversation_store_name(cid), create=receipt.command.conversation_id is None) as store:
            item = await store.get_item("meta")
            if item is None:
                meta = _Meta(
                    agent_id=self.identity.agent_id, hosted_session_id=receipt.hosted_session_id,
                    conversation_id=cid, capture=receipt.capture,
                    sdk_session_id="sdk-" + material_digest({"agent": self.identity.agent_id, "cid": cid})[:48],
                    state_profile=self.state_profile,
                )
                await self._immutable(store, "meta", meta)
                item = await store.get_item("meta")
            meta = _decode(item.value, _Meta)
            if (meta.agent_id != self.identity.agent_id or meta.conversation_id != cid
                    or channel(meta.state_profile) != channel(receipt.state_profile)
                    or not await self._capture_fits(meta, receipt.capture)):
                raise Problem(409, "NATIVE_CAPTURE_CHANGED", "the original conversation capture differs")
            if receipt.state_profile == NATIVE_RESPONSES_PROFILE and meta.inflight_rid != receipt.request_id:
                raise Problem(409, "NATIVE_TURN_NOT_TAKEN", "this RID does not own its conversation slot")
            checkpoint = meta.base_ckpt
            previous = None
            if meta.rid is not None:
                previous = await self.read(meta.rid)
                if previous.status == "completed":
                    checkpoint = (await self._turn(store, previous)).checkpoints[0]
            await self._allocation_fence(receipt, meta, previous)
            # ADR-0009 §7a (r): no turn of this conversation completed and its last one was another SID's, so this
            # session starts it without a checkpoint in a fresh, differently named SDK session (CTX-03 never starts a
            # blank session under a name that may have held state); never reported as a resume.
            fresh = (checkpoint is None and previous is not None
                     and previous.hosted_session_id != self.identity.session_id)
            # A capture that still differs here differs only in its model (_capture_fits): the conversation takes it
            # from this turn on, under the meta ETag; earlier RIDs keep their own captures.
            # meta.capture.connectors stays the ceiling: _capture_fits refuses a connector outside it, and the API's
            # team_connector_narrowing.resolve_connectors gives each turn only those the Team still grants. A model
            # adopted on a turn with fewer connectors therefore does not shrink what a re-enabled one may bring back.
            adopted = _conversation_capture(meta.capture) != _conversation_capture(receipt.capture)
            allocated = _changed(meta, n=meta.n + 1, rid=receipt.request_id, base_ckpt=checkpoint, **(
                {"capture": _changed(receipt.capture, connectors=meta.capture.connectors)} if adopted else {}), **(
                {"sdk_session_id": "sdk-" + material_digest({
                    "agent": self.identity.agent_id, "cid": cid, "fresh": receipt.request_id})[:48]} if fresh else {}))
            try:
                await store.set_item("meta", _json(allocated), if_match=item.etag)
            except _UNCERTAIN:
                found = await store.get_item("meta")
                if found is None or _decode(found.value, _Meta) != allocated:
                    raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "turn allocation is unconfirmed") from None
            scoped = self._scope(allocated)
            try:
                state = (None if checkpoint is None else
                         await load_checkpoint(store, scoped, checkpoint, cache_root=self.cache_root,
                                               checkpoint_profile=receipt.capture.template.settings.checkpoint_profile))
            except Problem as error:
                if error.status < 500:  # CTX-03 refused it explicitly (ADR-0009 §5); an unconfirmed read is no crash
                    await self._count_crash(receipt)
                raise
        updated = _changed(receipt, turn_number=allocated.n)
        current, ours = await self._cas(original, _changed(original.value, receipt=updated))
        if not ours:
            raise Problem(409, "NATIVE_TURN_UNKNOWN", "the original execution was fenced before SDK entry")
        return current, allocated, state

    async def _allocation_fence(self, receipt, meta, previous):
        """ADR-0009 §7a "(r)(s) 的競爭", checked in ``_allocate`` before the meta CAS: a late turn of a session the
        Agent has left is never numbered (the start check runs before allocation, so a source turn that passed it
        before the switch could otherwise take n+2 after the new session's turn and run a stale message). It ends
        ``NATIVE_TURN_UNKNOWN`` before the SDK. (1) this session still holds the Agent; (2) the last allocated receipt,
        when another SID's, can no longer complete. Same-SID order stays the chain's (a refusal there could wedge a
        conversation behind a receipt whose own ``unknown`` write was lost)."""
        from .native_left_receipts import can_complete, left

        current, _ = await self._agent_record()
        if current.hosted_session_id != self.identity.session_id:
            raise Problem(409, "NATIVE_TURN_UNKNOWN", "this session no longer holds the Agent; the turn was not numbered")
        if previous is not None and meta.rid != receipt.request_id and left(self, previous):
            if await can_complete(self, await self._rid(meta.rid)):
                raise Problem(409, "NATIVE_TURN_UNKNOWN", "an earlier session's turn holds this conversation")

    async def close_suspended_start(self, original, source_sid, task_id):
        receipt = original.value.receipt
        if (receipt.status != "accepted"
                or receipt.hosted_session_id != source_sid
                or original.value.run_token is not None or receipt.turn_number is not None):
            return receipt
        if receipt.state_profile == NATIVE_RESPONSES_PROFILE:
            from .native_responses_wire import responses_environment_blocker

            current, _ = await self._agent_record()
            if (responses_environment_blocker() is not None or receipt.response_id is not None
                    or current.environment is None or current.environment.state != "pending"
                    or current.environment.previous_hosted_session_id != source_sid
                    or current.hosted_session_id != source_sid):
                return receipt
            # This explicit transition's accepted ETag fences a late Responses handler before SDK entry.
            return await self._close_unstarted(original)
        if self.task_observer is None:
            return receipt
        value = await self.task_observer.suspended_input(task_id=task_id)
        if value is None:
            return receipt
        supplied = _decode(value, NativeTaskInput)
        if supplied.request_id != receipt.request_id:
            return receipt
        if supplied.accepted_etag != original.etag or supplied.call_id != original.value.call_id:
            raise Problem(409, "NATIVE_STATE_INVALID", "the saved input differs from the original unstarted receipt")
        return await self._close_unstarted(original)

    async def _settle_unstarted(self, supplied, *, best_effort=False):
        """This session's ``accepted`` RID that never started ends resendable (NATIVE_TURN_NOT_STARTED); a started or
        terminal one is returned unchanged (``_close_unstarted`` keeps a started winner it reads back). ``best_effort``:
        on the way to a recovery exit, bounded as the deferral marker and only logged when unconfirmed."""
        async def settle():
            original = await self._rid(supplied.request_id)
            receipt = original.value.receipt
            if receipt.hosted_session_id != self.identity.session_id or original.value.call_id != supplied.call_id:
                return receipt  # ADR-0009 M9: the session that adopted it settles it; never another caller's RID
            return await self._close_unstarted(original)

        if not best_effort:
            return await settle()
        try:
            return await asyncio.wait_for(settle(), _MARKER_WRITE_SECONDS)
        except Exception as error:  # noqa: BLE001 - whatever the store answers, the shutdown goes on
            log.warning("FA_NATIVE_UNSTARTED_CLOSE_UNCONFIRMED exception_type=%s", type(error).__name__)

    async def _await_runtime_exit(self, stop, conversation_id=None):
        """While an earlier turn's CLI still runs, or a stop of this conversation's own SDK that may leave one does, no
        SDK starts: True once it exited or ``stop()``, False when it still runs after UNEXITED_WAIT_SECONDS."""
        unexited = getattr(self.executor, "runtime_unexited", None)
        stopping = getattr(self.executor, "runtime_stopping", None)
        deadline = monotonic() + UNEXITED_WAIT_SECONDS
        while ((unexited is not None and unexited() or stopping is not None and stopping(conversation_id))
               and not stop()):
            if monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)
        return True

    async def _dropped_input(self, task_id):
        """The input core holds as ``steering.active_input`` of this chain task, or None."""
        record = await self.task_observer.original(task_id=task_id)
        payload = None if record is None else record.get("payload")
        steering = payload.get("steering") if isinstance(payload, dict) else None
        active = steering.get("active_input") if isinstance(steering, dict) else None
        if not isinstance(active, dict) or "__attachment_ref__" in active:
            return None
        return _decode(active, NativeTaskInput)

    async def _close_unstarted(self, original):
        receipt = original.value.receipt
        if receipt.status != "accepted" or original.value.run_token is not None or receipt.turn_number is not None:
            return receipt
        try:
            closed = await self._unknown(original, "NATIVE_TURN_NOT_STARTED", usage=receipt.usage)
        except _UnknownRidWrite:
            # A start whose answer was lost may have run: a read-back started or terminal winner is kept and returned.
            closed = (await self._rid(receipt.request_id)).value.receipt
            if closed.status == "accepted":
                raise
        if closed.status == "accepted":
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the original unstarted outcome was not confirmed")
        return await self._notify_terminal(closed)

    async def _unknown(self, original, code, *, usage=None, delegation=None, assembly=None, merges=UNKNOWN_MERGES):
        if original.value.receipt.status not in {"accepted", "started"}:
            return original.value.receipt
        retained = original.value.receipt.delegation
        if delegation is not None and retained is not None and delegation != retained:
            raise Problem(409, "NATIVE_RID_CONFLICT", "the original accepted delegation differs")
        receipt = _changed(original.value.receipt, status="unknown", failure_code=code, usage=usage,
                           delegation=retained or delegation,
                           assembly=assembly or original.value.receipt.assembly)
        required = _changed(original.value, receipt=_changed(receipt, assembly=None))
        receipt = _changed(receipt, assembly=_fit_assembly(receipt.assembly, required))
        try:
            current, _ = await self._cas(original, _changed(original.value, receipt=receipt))
        except Problem as error:
            if error.body["error"]["code"] != "NATIVE_STATE_STORE_UNKNOWN":
                raise
            raise _UnknownRidWrite(error.status, error.body["error"]["code"], error.body["error"]["message"]) from None
        if current.value.receipt.status in {"accepted", "started"}:
            # A definite conflict (another ETag, e.g. a concurrent cancel_requested write) on a RID still ours: merge
            # onto what was read back and write again, so the RID never stays ``started``.
            if (merges and current.etag != original.etag
                    and current.value.receipt.status == original.value.receipt.status
                    and current.value.run_token == original.value.run_token
                    and current.value.receipt.hosted_session_id == original.value.receipt.hosted_session_id):
                # A cancel that won the race becomes NATIVE_TURN_CANCELLED only for a code raised before the SDK
                # starts (_close_unstarted; preflight, capability binds and _run before run_copilot). Any other code
                # is kept, since the SDK may have run; cancel_requested stays on the receipt either way.
                if (current.value.receipt.cancel_requested
                        and code in {"NATIVE_TURN_NOT_STARTED", "NATIVE_EXECUTION_UNAVAILABLE"}):
                    code = "NATIVE_TURN_CANCELLED"
                return await self._unknown(current, code, usage=usage, delegation=delegation, assembly=assembly,
                                           merges=merges - 1)
            raise _UnknownRidWrite(503, "NATIVE_STATE_STORE_UNKNOWN",
                                   "the original unknown transition is unconfirmed; retain the original RID")
        return current.value.receipt

    async def _text(self, store, turn, part):
        if not 0 <= part < len(turn.text_parts):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "the result part is outside the original manifest")
        item = await store.get_item(f"text:{turn.text_sha256}:{part}")
        value = None if item is None else item.value
        if (not isinstance(value, dict) or set(value) != {"text", "sha256"}
                or not isinstance(value["text"], str) or value["sha256"] != turn.text_parts[part]
                or hashlib.sha256(value["text"].encode("utf-8")).hexdigest() != turn.text_parts[part]):
            raise Problem(409, "NATIVE_RESULT_INCOMPLETE", "a public result segment is missing or differs")
        return value["text"]

    async def _verify_result(self, store, turn):
        raw = "".join([await self._text(store, turn, i) for i in range(len(turn.text_parts))]).encode("utf-8")
        if len(raw) != turn.text_bytes or hashlib.sha256(raw).hexdigest() != turn.text_sha256:
            raise Problem(409, "NATIVE_RESULT_INCOMPLETE", "the complete result differs from its original digest")

    async def _reenter(self, original, *, failure_code=None, usage=None, delegation=None, assembly=None,
                       recovered=False):
        from .native_checkpoint import conversation_store_name, verify_checkpoint

        receipt = original.value.receipt
        if receipt.status != "started":
            return receipt
        if (recovered and not receipt.cancel_requested and receipt.delegation is None and receipt.queued_relay is None
                and self.on_recover_queued is not None):
            try:
                await self.on_recover_queued(original)
            except (TaskFailed, TaskCancelled, Problem, *_TASK_TRANSPORT_ERRORS) as error:
                code = error.body["error"]["code"] if isinstance(error, Problem) else "NATIVE_RELAY_OUTCOME_UNKNOWN"
                log.warning("NATIVE_RELAY_RECOVERY_UNCONFIRMED code=%s exception_type=%s", code, type(error).__name__)
                # Recovery only acts on a send that already dispatched, so the SDK has run: a cancel that landed since
                # the entry check stays ``cancel_requested`` and the code is kept (never rewritten to CANCELLED).
                return await self._unknown(await self._rid(receipt.request_id), code)
            original = await self._rid(receipt.request_id)
            receipt = original.value.receipt
            if receipt.status != "started":
                return receipt
        try:
            meta, _ = await self._meta(receipt.conversation_id)
            if meta.rid != receipt.request_id:
                return await self._unknown(original, failure_code or "NATIVE_TURN_UNKNOWN",
                                           usage=usage, delegation=delegation, assembly=assembly)
            async with self._store(conversation_store_name(receipt.conversation_id)) as store:
                turn = await self._turn(store, receipt, n=meta.n)
                await self._verify_result(store, turn)
                for checkpoint in turn.checkpoints:
                    await verify_checkpoint(store, self._scope(meta), checkpoint,
                                            checkpoint_profile=receipt.capture.template.settings.checkpoint_profile)
        except Problem as exc:
            if exc.status == 503 or exc.body["error"]["code"] == "NATIVE_STATE_STORE_TTL":
                raise
            if recovered:
                from .native_continuations import prepare_continuation
                return await prepare_continuation(
                    self, original, failure_code=failure_code or exc.body["error"]["code"])
            return await self._unknown(original, failure_code or exc.body["error"]["code"],
                                       usage=usage, delegation=delegation, assembly=assembly)
        if receipt.delegation is not None and turn.delegation is not None and receipt.delegation != turn.delegation:
            return await self._unknown(original, "NATIVE_RID_CONFLICT")
        completed = _changed(receipt, status="completed", turn_number=turn.n, result=turn.reference(),
                             usage=turn.usage, delegation=receipt.delegation or turn.delegation, assembly=turn.assembly)
        current, _ = await self._cas(original, _changed(original.value, receipt=completed))
        return current.value.receipt

    async def _observe_control(self, signals, original, control, *, waiting=False):
        """Stop the SDK when this run no longer owns the RID. Only a definite answer fences it: a read showing another
        status, ``run_token`` or SID, or a 4xx (missing, another scope, invalid). An unconfirmed read (5xx) is no
        answer; the next read follows after a bounded backoff, and the save's ETag CAS stays the final fence. A running
        turn with no confirmed read for ``CONTROL_UNCONFIRMED_FENCE_SECONDS`` is fenced; a turn that only waits for the
        slot is not (it has started nothing, and stopping it would lose the message).
        ``waiting``: the turn only waits for the execution slot, so its RID is read less often. Appends are delivered
        on the same poll (another process's append within one interval); one this process saves wakes it at once."""
        rid, token = original.value.receipt.request_id, original.value.run_token
        interval = SLOT_WAIT_POLL_SECONDS if waiting else CONTROL_POLL_SECONDS
        append_delivery = latest = None
        loop = asyncio.get_running_loop()
        next_read, failures = loop.time(), 0
        next_append, append_failures = loop.time(), 0
        confirmed = monotonic()  # the last definite answer: the start (or accept) CAS that this run just read back
        wake = asyncio.Event()
        self._control_wakes.setdefault(rid, set()).add(wake)
        try:
            while True:
                signals(control)
                if control.stop_requested():
                    return
                if wake.is_set():
                    wake.clear()
                    next_read = next_append = loop.time()
                if append_delivery is not None and append_delivery.done():
                    delivered, append_delivery = append_delivery, None
                    try:
                        if delivered.result():
                            next_append = loop.time()  # one append per pass: look for the next one at once
                    except Problem as error:
                        # Same split as the RID read: a 4xx is definite; a 5xx is retried after a bounded backoff.
                        # A retry never sends twice: only an ``accepted`` entry is claimed (``sending`` + token); a
                        # claimed entry is sent while this process has not started its send (``control.sent_appends``),
                        # before any later one, and an unconfirmed ``remember`` is written again from there.
                        if error.status < 500:
                            control.fenced = True
                            raise
                        append_failures += 1
                        if append_failures == 1:
                            log.warning("FA_NATIVE_APPEND_DELIVERY_UNCONFIRMED code=%s", error.body["error"]["code"])
                        next_append = loop.time() + min(CONTROL_POLL_MAX_SECONDS, interval * 2 ** append_failures)
                    else:
                        if append_failures:
                            log.info("FA_NATIVE_APPEND_DELIVERY_RECOVERED failures=%d", append_failures)
                        append_failures = 0
                if loop.time() >= next_read:
                    try:
                        current = await self._rid(rid)
                        cancelled = current.value.receipt.cancel_requested
                        if current.value.receipt.continuation is not None:
                            from .native_continuations import stop_requested
                            cancelled = cancelled or await stop_requested(self, current.value.receipt)
                    except Problem as error:
                        if error.status < 500:
                            control.fenced = True
                            raise
                        failures += 1
                        if failures == 1:
                            log.warning("FA_NATIVE_CONTROL_READ_UNCONFIRMED code=%s", error.body["error"]["code"])
                        if not waiting and monotonic() - confirmed >= CONTROL_UNCONFIRMED_FENCE_SECONDS:
                            log.warning("FA_NATIVE_CONTROL_UNCONFIRMED_FENCE failures=%d", failures)
                            control.fenced = True
                            return
                        next_read = loop.time() + min(CONTROL_POLL_MAX_SECONDS, interval * 2 ** failures)
                    else:
                        if failures:
                            log.info("FA_NATIVE_CONTROL_READ_RECOVERED failures=%d", failures)
                        failures, latest, confirmed = 0, current, monotonic()
                        next_read = loop.time() + interval
                        control.cancelled = cancelled
                        control.fenced = (
                            current.value.receipt.status != original.value.receipt.status
                            or current.value.run_token != token
                            or current.value.receipt.hosted_session_id != original.value.receipt.hosted_session_id)
                        if control.stop_requested():
                            return
                if (latest is not None and append_delivery is None and loop.time() >= next_append
                        and control.steering is not None and control.steering.ready and not control.steering.closed):
                    from .native_appends import deliver_appends
                    next_append = loop.time() + interval
                    append_delivery = asyncio.create_task(deliver_appends(self, latest.value.receipt, control))
                # SDK steering does not replace the independent cancellation observer.
                await asyncio.sleep(CONTROL_TICK_SECONDS)
        finally:
            wakes = self._control_wakes.get(rid)
            if wakes is not None:
                wakes.discard(wake)
                if not wakes:
                    del self._control_wakes[rid]
            if append_delivery is not None:
                append_delivery.cancel()
                await asyncio.gather(append_delivery, return_exceptions=True)

    def _wake_control(self, rid):
        """A cancel or append this process saved: its control observers look now (the poll stays the authority)."""
        for wake in self._control_wakes.get(rid, ()):
            wake.set()

    @asynccontextmanager
    async def execution_slot(self, original, control, signals):
        """Both native lanes acquire once; the existing control observer also covers the wait."""
        async def acquire():
            await self.execution_lock.acquire()
            self.execution_owner = original.value.receipt.request_id
            return True

        waiter = asyncio.create_task(acquire())
        watcher = asyncio.create_task(self._observe_control(signals, original, control, waiting=True))
        waiting = perf_counter()
        try:
            await asyncio.wait((waiter, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher.done():
                watcher.result()
            signals(control)
            acquired = waiter.done() and not waiter.cancelled() and waiter.result()
            if acquired:
                elapsed_timing("native_queue_wait", waiting, job_id=timing_id(original.value.receipt.request_id))
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            yield bool(acquired and not control.stop_requested())
        finally:
            watcher.cancel()
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, watcher, return_exceptions=True)
            if waiter.done() and not waiter.cancelled() and waiter.exception() is None and waiter.result():
                self.execution_owner = None
                self.execution_lock.release()

    async def _save_turn(self, original, meta, outcome):
        from .native_checkpoint import bounded, conversation_store_name, save_checkpoint

        # A resident release (ADR-0005) is published like an exit; the native checkpoint never stores sdk_stop, and
        # diagnostic compaction state still refuses a release in save_checkpoint.
        if not outcome.sdk_stop.graceful:
            raise Problem(409, "SDK_STATE_INCOMPLETE", "native checkpoint publication requires a graceful SDK end")
        raw = outcome.text.encode("utf-8")
        if len(raw) > MAX_BACKUP_BYTES:
            raise Problem(413, "NATIVE_RESULT_TOO_LARGE", "the full public result exceeds the supported segmented bound")
        parts, offset = [], 0
        while offset < len(raw):
            end = min(len(raw), offset + TEXT_PART_BYTES)
            while end < len(raw) and raw[end] & 0xC0 == 0x80:
                end -= 1
            parts.append(raw[offset:end].decode("utf-8"))
            offset = end
        if not parts:
            parts = [""]
        sha = hashlib.sha256(raw).hexdigest()
        async with self._store(conversation_store_name(meta.conversation_id)) as store:
            checkpoint = await save_checkpoint(
                store, self._scope(meta), outcome.state, cache_root=self.cache_root,
                turn=self._checkpoint_turn(original.value.receipt, meta),
                compaction=outcome.compaction, sdk_stop=outcome.sdk_stop)
            hashes = tuple(hashlib.sha256(part.encode("utf-8")).hexdigest() for part in parts)
            # ``_immutable`` reads each part back and compares it byte for byte; the parts are the UTF-8 split of
            # ``raw`` above, so their hashes, joined length and ``sha`` already match the manifest below. The second
            # full read (``_verify_result``) is kept only for re-entry, where nothing was just written.
            await bounded([
                lambda index=index, text=text: self._immutable(
                    store, f"text:{sha}:{index}", {"text": text, "sha256": hashes[index]})
                for index, text in enumerate(parts)])
            delegation = outcome.delegation or original.value.receipt.delegation
            if (outcome.delegation is not None and original.value.receipt.delegation is not None
                    and outcome.delegation != original.value.receipt.delegation):
                raise Problem(409, "NATIVE_STATE_INVALID", "the published delegation differs from its original receipt")
            turn = _Turn(
                request_id=original.value.receipt.request_id, conversation_id=meta.conversation_id, n=meta.n,
                submit_digest=original.value.receipt.submit_digest, text_sha256=sha, text_bytes=len(raw),
                text_parts=hashes, checkpoints=(checkpoint,), usage=outcome.usage, delegation=delegation,
                assembly=outcome.assembly,
            )
            required_turn = _changed(turn, assembly=None)
            required_rid = _changed(original.value, receipt=_changed(
                original.value.receipt, status="completed", result=required_turn.reference(),
                usage=outcome.usage, delegation=turn.delegation, assembly=None))
            turn = _changed(turn, assembly=_fit_assembly(turn.assembly, required_turn, required_rid))
            await self._immutable(store, f"turn:{meta.n}", turn)
        receipt = _changed(original.value.receipt, status="completed", result=turn.reference(),
                           usage=outcome.usage, delegation=turn.delegation, assembly=turn.assembly)
        current, _ = await self._cas(original, _changed(original.value, receipt=receipt))
        if current.value.receipt.status == "started" and current.value.receipt.cancel_requested:
            return await self._unknown(current, "NATIVE_TURN_CANCELLED",
                                       usage=outcome.usage, delegation=turn.delegation, assembly=turn.assembly)
        if current.value.receipt.status == "started":
            return await self._reenter(current)
        return current.value.receipt

    async def handle_turn(self, ctx: TaskContext[NativeTaskInput]):
        result = await self._handle_turn(ctx)
        if isinstance(result, NativeReceipt):
            if ctx.entry_mode == "recovered" and not ctx.cancel_requested and not ctx.timeout_exceeded:
                from .native_continuations import schedule_continuation
                try:
                    await schedule_continuation(self, result)
                except NativePostTurnPending:
                    await self._notify_terminal(result)
                    raise
            return await self._notify_terminal(result)
        return result

    async def _handle_turn(self, ctx: TaskContext[NativeTaskInput]):
        dropped = None
        if ctx.input is None and ctx.entry_mode == "recovered" and self.task_observer is not None:
            # Core 2.2.0 re-enters a drained input with payload.input=None (it reads steering.active_input only while
            # drain_in_progress); that input is read from the task record. Started, it re-enters as usual; accepted,
            # core can never run it again, so it is settled resendable (below).
            try:
                dropped = await self._dropped_input(ctx.task_id)
            except (Problem, *_TASK_TRANSPORT_ERRORS) as error:
                if isinstance(error, Problem) and error.status < 500:
                    raise
                raise NativePostTurnPending() from None  # core retries within its finite budget
        if dropped is not None:
            supplied = dropped
        else:
            supplied = ctx.input if isinstance(ctx.input, NativeTaskInput) else _decode(ctx.input, NativeTaskInput)
        binding, _ = await self._agent_record()
        if ctx.shutdown.is_set():
            # A started RID is never marked here: an earlier lifetime that crashed stays unmarked, so it still counts.
            if ctx.is_steered_turn:
                # A drained input: core's re-entry gets payload.input=None and can never run it, so it ends resendable.
                await self._settle_unstarted(supplied, best_effort=True)
            return await self._exit_for_recovery(ctx)
        original = await self._rid(supplied.request_id)
        if ctx.task_id != binding.task_id or binding.hosted_session_id != self.identity.session_id:
            source = original.value.receipt
            if (ctx.entry_mode != "recovered" or source.status == "accepted"
                    or ctx.task_id not in {source.agent_id, binding_task_id(source.agent_id, source.hosted_session_id)}):
                raise Problem(403, "NATIVE_STATE_SCOPE", "the task does not belong to this Agent session binding")
        # core 2.1.0 steering reports last_input_id on ctx; the persisted input owns this RID.
        if supplied.call_id != original.value.call_id:
            raise Problem(403, "NATIVE_STATE_SCOPE", "the persisted caller context differs from the accepted request")
        if original.value.receipt.status != "accepted":
            recovered = ctx.entry_mode == "recovered" and not ctx.cancel_requested and not ctx.timeout_exceeded
            if recovered:
                await self._count_unmarked_reentry(original)
            try:
                return await self._reenter(original, recovered=recovered)
            except TaskDeferred:
                if not ctx.shutdown.is_set():
                    raise
                return await self._exit_for_recovery(ctx, original)
        if dropped is not None:
            try:
                return await self._settle_unstarted(supplied)
            except (Problem, *_TASK_TRANSPORT_ERRORS) as error:
                if isinstance(error, Problem) and error.status < 500:
                    raise
                raise NativePostTurnPending() from None
        drifted = supplied.execution_version is not None and supplied.execution_version != self.identity.agent_version
        # A RID refused once never runs: its owner was told to send it again as a new message, so a resend of the same
        # RID (relay, API retry, a wait task re-run after a recycle) at the current version would do the work twice.
        # Strict: an unconfirmed read retries later rather than runs it. Fresh for an adopted RID: another session wrote
        # its marker, so a miss this process cached does not count (``adopt`` reads leniently on a store error).
        if drifted or await self._refused(supplied.request_id, fresh=original.value.adopted_from is not None,
                                          strict=True):
            marked = (drifted and original.value.receipt.hosted_session_id == self.identity.session_id  # as at enqueue
                      and await self._mark_refused(supplied))
            from .native_left_receipts import dequeue

            # It never runs here, so it must not stay first in its conversation's left queue (best effort).
            await dequeue(self, original.value.receipt)
            if marked:
                await self._notify_terminal(_refused_final(original.value.receipt))
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the original input cannot execute in another Hosted version")
        if channel(original.value.receipt.state_profile) != "invocations":
            raise Problem(409, "NATIVE_STATE_SCOPE", "Responses turns never run through the Invocations chain")
        await self._require_original_session(receipt=original.value.receipt)
        from .native_left_receipts import defer, dequeue, turn_order

        order = await turn_order(self, original)
        if not order.go:
            # ADR-0009 §7a "(r)(s) 的競爭": it stays ``accepted`` (an ordinary queued message, never refused) and waits
            # in its conversation's left queue with a wait task of its own; this chain turn ends now, so the Agent's
            # other conversations go on. The wait task puts it back on the chain when its turn comes.
            try:
                await defer(self, original, order.behind)
            except (Problem, *_TASK_TRANSPORT_ERRORS) as error:
                if isinstance(error, Problem) and error.status < 500:
                    raise
                raise NativePostTurnPending() from None
            return original.value.receipt
        control = NativeTurnControl()

        def signals(value):
            value.shutdown = ctx.shutdown.is_set()
            value.timeout = ctx.timeout_exceeded
            value.cancelled = value.cancelled or ctx.cancel_requested

        async with self.execution_slot(original, control, signals) as acquired:
            # Before "started": while an earlier turn's CLI still runs this one stays accepted (no second SDK) until
            # that CLI exits, or the recovery shutdown the executor asked for closes it (below), or the bound does.
            exited = not acquired or await self._await_runtime_exit(
                lambda: ctx.shutdown.is_set() or ctx.cancel_requested or ctx.timeout_exceeded,
                original.value.receipt.conversation_id)
            signals(control)
            starting = perf_counter()
            if ctx.shutdown.is_set():
                if ctx.is_steered_turn:
                    # A drained input: core's re-entry gets payload.input=None and can never run it.
                    await self._settle_unstarted(supplied, best_effort=True)
                return await self._exit_for_recovery(ctx, original)
            original = await self._rid(supplied.request_id)
            if original.value.receipt.hosted_session_id != self.identity.session_id:
                # ADR-0009 M9: the session the Agent switched to adopted this never-started receipt; it runs there.
                return original.value.receipt
            if original.value.receipt.status != "accepted":
                return await self._reenter(original)
            if control.timeout:
                return await self._unknown(original, "NATIVE_TURN_TIMEOUT")
            if control.cancelled or original.value.receipt.cancel_requested:
                return await self._unknown(original, "NATIVE_TURN_CANCELLED")
            if not acquired:
                return original.value.receipt
            if not exited:
                return await self._close_unstarted(original)
            started = _changed(original.value, run_token=uuid4().hex,
                               receipt=_changed(original.value.receipt, status="started"))
            # Use the ETag persisted in the accepted task input, not a refreshed read.
            try:
                original, ours = await self._cas(_Versioned(original.value, supplied.accepted_etag), started)
            except _UnchangedRidWrite:
                return await self._unknown(original, "NATIVE_STATE_STORE_UNKNOWN")
            elapsed_timing("native_start", starting, job_id=timing_id(supplied.request_id))
            if not ours:
                if original.value.receipt.hosted_session_id != self.identity.session_id:
                    return original.value.receipt
                return await self._reenter(original)
            if order.queued:
                await dequeue(self, original.value.receipt)
            result = await self._execute_started(original, control, signals)
            if ctx.shutdown.is_set():
                return await self._exit_for_recovery(ctx, original)
            return result

    async def _execute_started(self, original, control, signals):
        done = asyncio.get_running_loop().create_future()
        self.executing[done] = asyncio.current_task()
        rid = original.value.receipt.request_id
        self.running_controls[rid] = control
        try:
            return await self._run_started(original, control, signals)
        finally:
            if self.running_controls.get(rid) is control:
                del self.running_controls[rid]
            del self.executing[done]
            done.set_result(None)

    async def _run_started(self, original, control, signals):
        watcher = None
        appends_settled = False
        saving = False
        outcome = None
        rid = original.value.receipt.request_id
        try:
            if original.value.receipt.continuation is not None:
                from .native_continuations import stop_requested
                if await stop_requested(self, original.value.receipt):
                    return await self._unknown(original, "NATIVE_TURN_CANCELLED")
            original, meta, previous = await self._allocate_timed(original)
            control.adopted = original.value.adopted_from is not None
            control.checkpoint_turn = self._checkpoint_turn(original.value.receipt, meta)
            control.retain_delegation = lambda accepted, bound=original: self._retain_delegation(bound, accepted)
            watcher = asyncio.create_task(self._observe_control(signals, original, control))
            # Phases recorded inside the SDK turn (memory recall, sdk_process_start) inherit this turn's timing ID.
            with work_timing(timing_id(rid)), phase_timing("native_executor"):
                outcome = await self.executor(original.value.receipt, self._scope(meta), previous, control)
            if watcher.done():
                watcher.result()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if control.steering is not None:
                from .native_appends import settle_appends
                appends_settled = True
                await settle_appends(self, original.value.receipt, control, finished=True)
            signals(control)
            current = await self._rid(rid)
            if control.shutdown:
                return current.value.receipt
            if current.value.run_token != original.value.run_token or current.value.receipt.status != "started":
                return current.value.receipt
            if control.fenced:
                return await self._unknown(current, "NATIVE_TURN_UNKNOWN",
                                           usage=outcome.usage, delegation=outcome.delegation, assembly=outcome.assembly)
            if control.timeout:
                return await self._unknown(current, "NATIVE_TURN_TIMEOUT",
                                           usage=outcome.usage, delegation=outcome.delegation, assembly=outcome.assembly)
            if control.cancelled or current.value.receipt.cancel_requested:
                return await self._unknown(current, "NATIVE_TURN_CANCELLED",
                                           usage=outcome.usage, delegation=outcome.delegation, assembly=outcome.assembly)
            saving = True
            with phase_timing("native_save_turn", job_id=timing_id(rid)):
                # Every save step is idempotent (content-addressed creates, the RID CAS on its original ETag), so an
                # unconfirmed (503) step is retried with the same outcome; the SDK never runs again.
                for delay in (*SAVE_RETRY_SECONDS, None):
                    try:
                        return await self._save_turn(current, meta, outcome)
                    except _UnknownRidWrite:
                        raise
                    except Problem as error:
                        if error.status != 503 or delay is None:
                            raise
                        log.warning("FA_NATIVE_SAVE_RETRY code=%s", error.body["error"]["code"])
                    await asyncio.sleep(delay)
        except _UnknownRidWrite:
            raise
        except Exception as exc:
            # Never repeat the SDK after started, including provider and persistence failures.
            signals(control)
            if control.shutdown:
                return (await self._rid(rid)).value.receipt
            code = ("NATIVE_TURN_TIMEOUT" if control.timeout else
                    "NATIVE_TURN_CANCELLED" if control.cancelled else
                    "NATIVE_TURN_UNKNOWN" if control.fenced else
                    exc.body["error"]["code"] if isinstance(exc, Problem) else "NATIVE_TURN_UNKNOWN")
            usage = exc.usage if isinstance(exc, NativeSdkFailure) else None if outcome is None else outcome.usage
            delegation = (exc.delegation if isinstance(exc, NativeSdkFailure)
                          else None if outcome is None else outcome.delegation)
            assembly = (exc.assembly if isinstance(exc, NativeSdkFailure)
                        else None if outcome is None else outcome.assembly)
            log.warning("FA_NATIVE_TURN_UNKNOWN code=%s exception_type=%s", code, type(exc).__name__)
            current = await self._rid(rid)
            if current.value.run_token != original.value.run_token:
                return current.value.receipt
            if saving:
                return await self._reenter(current, failure_code=code, usage=usage,
                                           delegation=delegation, assembly=assembly)
            return await self._unknown(current, code, usage=usage, delegation=delegation, assembly=assembly)
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if control.steering is not None and not appends_settled:
                from .native_appends import settle_appends
                control.steering.close()
                await settle_appends(self, original.value.receipt, control, finished=True)

    async def _continuation_blocker(self, meta, execution_sid):
        if meta.state_profile == NATIVE_RESPONSES_PROFILE and meta.hosted_session_id != execution_sid:
            from .native_responses_wire import responses_environment_blocker

            blocker = responses_environment_blocker()
            if blocker is not None:
                return blocker
        if meta.hosted_session_id == execution_sid or meta.n == 0 or meta.base_ckpt is not None:
            return None
        if meta.rid is not None:
            previous = await self.read(meta.rid)
            if previous.status == "completed" and previous.result is not None:
                return None
            # ADR-0009 §7a (r): no checkpoint is owed. Checkpoints are saved only when a turn completes and
            # ``_allocate`` carries the last completed one in ``base_ckpt``, so an empty ``base_ckpt`` with a last
            # receipt that is not ``completed`` (accepted, started or unknown) means no turn of this conversation ever
            # completed: the new session starts it without a checkpoint, in a fresh SDK session (``_allocate``), after
            # the left receipt can no longer complete (``_behind_left``). CTX-03's refusal stays for a checkpoint that
            # is owed but can not be read, at restore.
            return None
        return "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE"

    async def conversation(self, cid):
        meta, _ = await self._meta(cid)
        current, _ = await self._agent_record()
        previous = (None if meta.state_profile != NATIVE_RESPONSES_PROFILE or meta.rid is None else
                    (await self._rid(meta.rid)).value.receipt.response_id)
        return NativeConversation(
            state_profile=meta.state_profile,
            agent_id=meta.agent_id, hosted_session_id=meta.hosted_session_id,
            execution_hosted_session_id=(current.hosted_session_id if current.environment is not None
                                        and current.environment.state == "ready" else None),
            continuation_blocker=await self._continuation_blocker(meta, current.hosted_session_id),
            conversation_id=cid, last_turn=meta.n, last_request_id=meta.rid,
            capture=meta.capture, capture_digest=meta.capture.sha256,
            inflight_request_id=meta.inflight_rid, last_response_id=previous,
        )

    async def history(self, command: NativeHistory):
        from .native_checkpoint import conversation_store_name

        conversation = await self.conversation(command.conversation_id)
        stop = min(conversation.last_turn, command.after_turn + command.limit)
        turns = []
        async with self._store(conversation_store_name(command.conversation_id)) as store:
            for n in range(command.after_turn + 1, stop + 1):
                item = await store.get_item(f"turn:{n}")
                if item is None:
                    continue
                stored = _decode(item.value, _Turn)
                receipt = await self.read(stored.request_id)
                if receipt.status != "completed":
                    continue
                turn = await self._turn(store, receipt, n=n, item=item)
                turns.append(NativePublicTurn(
                    conversation_id=command.conversation_id, request_id=turn.request_id, turn_number=n,
                    user_text=receipt.command.content if receipt.human_origin and receipt.continuation is None else None,
                    agent_text=receipt.command.content if receipt.source is not None and receipt.continuation is None else None,
                    routine=receipt.routine,
                    routine_text=receipt.command.content if receipt.routine is not None and receipt.continuation is None else None,
                    continuation=receipt.continuation,
                    continuation_text=receipt.command.content if receipt.continuation is not None else None,
                    source=receipt.source, result=turn.reference(), usage=turn.usage,
                    delegation=receipt.delegation, queued_relay=receipt.queued_relay,
                    notification_error=receipt.notification_error, finished_at=_created_at(item),
                ))
        return NativeHistoryPage(conversation=conversation, turns=tuple(turns),
                                 next_after_turn=stop if stop < conversation.last_turn else None)

    async def result(self, command: NativeResult):
        from .native_checkpoint import conversation_store_name

        receipt = await self.read(command.request_id)
        self._address(receipt, command.conversation_id)
        if receipt.status != "completed":
            raise Problem(409, "NATIVE_RESULT_MISSING", "this RID has no confirmed complete result")
        async with self._store(conversation_store_name(receipt.conversation_id)) as store:
            turn = await self._turn(store, receipt)
            text = await self._text(store, turn, command.part)
        return NativeResultPage(
            conversation_id=receipt.conversation_id, request_id=receipt.request_id, result=turn.reference(),
            part=command.part, text=text, next_part=command.part + 1 if command.part + 1 < len(turn.text_parts) else None,
            complete=len(turn.text_parts) == 1,
        )

    @staticmethod
    def _address(receipt, cid):
        if receipt.conversation_id != cid:
            raise Problem(403, "NATIVE_STATE_SCOPE", "the RID does not belong to this conversation")

    async def turn_status(self, command):
        """One invocation for a poller: the receipt, its public progress and a completed single-page reply."""
        from .behavior_native_turn_wire import NativeTurnStatus
        from .native_checkpoint import conversation_store_name
        from .native_progress import read_preview, read_progress

        receipt = await self._observed_read(command.request_id, cid=command.conversation_id)
        progress = await read_progress(self, NativeProgressRead(
            conversation_id=command.conversation_id, request_id=command.request_id))
        text = next_part = finished_at = None
        if receipt.status == "completed" and receipt.result is not None:
            async with self._store(conversation_store_name(receipt.conversation_id)) as store:
                item = await store.get_item(f"turn:{receipt.turn_number}")
                turn = await self._turn(store, receipt, item=item)
                if len(turn.text_parts) == 1:
                    text = await self._text(store, turn, 0)
                else:
                    next_part = 0
            finished_at = _created_at(item)
        return NativeTurnStatus(receipt=receipt, progress=progress, text=text, next_part=next_part,
                                preview=await read_preview(self, receipt), finished_at=finished_at)

    async def response_not_started(self, command: NativeResponseNotStarted):
        original = await self._rid(command.request_id)
        receipt = original.value.receipt
        self._address(receipt, command.conversation_id)
        if receipt.state_profile != NATIVE_RESPONSES_PROFILE:
            raise Problem(409, "NATIVE_STATE_SCOPE", "this rejection can only close the Responses lane")
        if command.submit_digest != receipt.submit_digest:
            raise Problem(409, "NATIVE_RID_CONFLICT", "the rejection differs from the original submission")
        if receipt.status != "accepted":
            return receipt
        await self._require_original_session(receipt=receipt)
        if original.value.run_token is not None or receipt.turn_number is not None or receipt.response_id is not None:
            raise Problem(409, "NATIVE_STATE_INVALID", "this original receipt already has execution material")
        if receipt.request_id in self.responses_queued:
            raise Problem(409, "NATIVE_TURN_QUEUED", "the original response is already in the Agent queue")
        log.warning("FA_NATIVE_RESPONSE_NOT_STARTED reason=%s", command.reason)
        if receipt.cancel_requested:
            return await self._notify_terminal(await self._unknown(original, "NATIVE_TURN_CANCELLED"))
        return await self._close_unstarted(original)

    async def cancel(self, command: NativeCancel):
        original = await self._rid(command.request_id)
        for _ in range(3):
            receipt = original.value.receipt
            self._address(receipt, command.conversation_id)
            unstarted_response = receipt.state_profile == NATIVE_RESPONSES_PROFILE and receipt.status == "accepted"
            if receipt.status not in {"accepted", "started"} or receipt.cancel_requested and not unstarted_response:
                if receipt.status == "unknown":
                    from .native_continuations import stop_handoff
                    await stop_handoff(self, receipt)
                return receipt
            cancelled = _changed(receipt, cancel_requested=True)
            if unstarted_response:
                cancelled = _changed(cancelled, status="unknown", failure_code="NATIVE_TURN_CANCELLED")
            replacement = _changed(original.value, receipt=cancelled)
            current, saved = await self._cas(original, replacement)
            if saved:
                self._wake_control(command.request_id)
                return await self._notify_terminal(current.value.receipt) if unstarted_response else current.value.receipt
            original = current
        # The durable flag targets this RID; never cancel or reclaim the whole Agent chain.
        raise Problem(409, "NATIVE_TURN_UNKNOWN", "cancellation was not confirmed; read the original RID")

    async def delivered(self, command: NativeDelivered):
        original = await self._rid(command.request_id)
        receipt = original.value.receipt
        self._address(receipt, command.conversation_id)
        if receipt.result is None or receipt.result.result_digest != command.result_digest:
            raise Problem(409, "NATIVE_DELIVERY_CONFLICT", "delivery must acknowledge the original complete result")
        if receipt.delivered:
            return receipt
        current, _ = await self._cas(original, _changed(original.value, receipt=_changed(receipt, delivered=True)))
        return current.value.receipt

    async def dispatch(self, command: NativeRequest):
        from .behavior_native_turn_wire import NativeSessionFilesOmission
        if isinstance(command, NativeSessionFilesOmission):
            from .native_files_omission import confirm_omission
            return await confirm_omission(self, command)
        from .native_files_wire import NativeSessionFiles
        if isinstance(command, NativeSessionFiles):
            from .native_environment_files import session_files
            return await session_files(self, command)
        from .behavior_native_turn_wire import NativeContinuationRead, NativeContinuationReady, NativeContinuationSubmit
        if isinstance(command, NativeContinuationReady):
            from .native_continuations import acknowledge_ready
            return await acknowledge_ready(self, command)
        if isinstance(command, NativeContinuationRead):
            from .native_continuations import read_continuation
            return await read_continuation(self, command.request_id)
        if isinstance(command, NativeContinuationSubmit):
            return await self.continue_turn(command)
        if isinstance(command, NativeHitlHistoryRead):
            from .native_effects import read_hitl_history
            return await read_hitl_history(self, command)
        from .behavior_native_turn_wire import NativeReceivedRead
        if isinstance(command, NativeReceivedRead):
            from .native_received import read_received
            return await read_received(self, command)
        if isinstance(command, NativeProgressRead):
            from .native_progress import read_progress
            return await read_progress(self, command)
        from .native_progress_wire import NativeTurnStatusRead
        if isinstance(command, NativeTurnStatusRead):
            return await self.turn_status(command)
        if isinstance(command, (NativeAppend, NativeAppendRead)):
            from .native_appends import append, read_append
            return await (append(self, command) if isinstance(command, NativeAppend) else read_append(self, command))
        if isinstance(command, NativeSessionRetarget):
            from .native_environment import retarget_session
            return await retarget_session(self, command)
        from .native_skill_draft_wire import NativeSkillDraftIntentRead
        from .native_image_wire import NativeImageRead, NativeImagesRead

        if isinstance(command, (NativeImagesRead, NativeImageRead)):
            from .native_images import read_image, read_images
            return await (read_image(self, command) if isinstance(command, NativeImageRead) else read_images(self, command))
        if isinstance(command, NativeSkillDraftIntentRead):
            from .native_skill_draft import read_intent
            return await read_intent(self, command)
        from .behavior_user_images import NativeUserImageUpload
        if isinstance(command, NativeUserImageUpload):
            from .native_user_images import upload
            return await upload(self, command)
        if isinstance(command, NativeSessionFollow):
            from .native_environment import follow_session
            return await follow_session(self, command)
        from .behavior_native_turn_wire import NativeSessionAbandon
        if isinstance(command, NativeSessionAbandon):
            from .native_environment_abandon import abandon_session
            return await abandon_session(self, command)
        from .behavior_native_turn_wire import NativeSessionCut
        if isinstance(command, NativeSessionCut):
            from .native_environment_cut import cut_session
            return await cut_session(self, command)
        from .behavior_native_turn_wire import NativeSessionWithdraw
        if isinstance(command, NativeSessionWithdraw):
            from .native_environment_cut import withdraw_session
            return await withdraw_session(self, command)
        from .behavior_native_turn_wire import NativeUnfinishedRead
        if isinstance(command, NativeUnfinishedRead):
            from .native_environment_abandon import unfinished_sessions
            return await unfinished_sessions(self)
        if isinstance(command, NativeSessionRead):
            return await self.session_state(environment_request_id=command.environment_request_id,
                                            workspace=command.workspace)
        if isinstance(command, NativeSessionPrepare):
            return await self.prepare_session(command)
        if isinstance(command, NativeSubmit):
            return await self.submit(command)
        if isinstance(command, NativeAgentReceive):
            return await self.receive(command)
        if isinstance(command, NativeRoutineSubmit):
            return await self.receive_routine(command)
        if isinstance(command, NativeRead):
            return await self._observed_read(command.request_id)
        if isinstance(command, NativeConversationRead):
            return await self.conversation(command.conversation_id)
        if isinstance(command, NativeHistory):
            return await self.history(command)
        if isinstance(command, NativeResult):
            return await self.result(command)
        if isinstance(command, NativeCancel):
            return await self.cancel(command)
        if isinstance(command, NativeResponseNotStarted):
            return await self.response_not_started(command)
        if isinstance(command, NativeDelivered):
            return await self.delivered(command)
        if isinstance(command, NativeCandidateRead):
            from .native_memory_candidate import read_candidate
            return await read_candidate(self, command)
        from .behavior_native_turn_wire import NativePrewarm
        if isinstance(command, NativePrewarm):
            return await self.prewarm(command)
        from .behavior_native_turn_wire import NativeSessionCheckpointCheck, NativeSessionPrerestore
        if isinstance(command, (NativeSessionCheckpointCheck, NativeSessionPrerestore)):
            from .native_checkpoint_check import read_back
            return await read_back(self, command)
        raise Problem(422, "NATIVE_REQUEST_INVALID", "unknown native operation")

    async def prewarm(self, command):
        """The owner is typing (README#typing-prewarm). Reaching this session already woke its compute (the platform
        provisions compute when a session is referenced), and ``native_response`` refused a replaced session as for
        every read on the API's reader. Here only the named toolbox versions start listing in the background. No task,
        RID, record, admission or execution lock is touched, and nothing is written."""
        from .behavior_native_turn_wire import NativePrewarmView
        start = getattr(self.executor, "prewarm", None)
        count = 0 if start is None else start(command.toolboxes)
        return NativePrewarmView(agent_id=self.identity.agent_id, hosted_session_id=self.identity.session_id,
                                 toolboxes=count)


async def native_response(request, runtime: NativeTurnRuntime | None, *, action="invoke"):
    from starlette.responses import JSONResponse
    from .behavior_http_diagnostics import _exception_type, native_correlation_id
    from .behavior_native_turn_wire import NATIVE_TURN_ERRORS, NativeContinuationSubmit

    entered = monotonic()
    details = {
        "action": action if action in {"invoke", "observe", "cancel"} else "unknown",
        "agent_id": None if runtime is None else runtime.identity.agent_id,
        "agent_version": None if runtime is None else runtime.identity.agent_version,
        "session_id": None if runtime is None else runtime.identity.session_id,
        "invocation_id": native_correlation_id(getattr(request.state, "invocation_id", None)),
        "operation": None, "request_id_sha256": None,
    }

    def diagnostic(phase, *, status=None, code=None, exception=None):
        log.warning("FA_NATIVE_HTTP %s", json.dumps({
            **details, "phase": phase, "status": status, "code": code,
            "exception_type": None if exception is None else _exception_type(exception),
            "elapsed_ms": round((monotonic() - entered) * 1000),
        }, sort_keys=True))

    def refused(problem, reason):
        from .behavior_native_turn_wire import before_dispatch
        return before_dispatch(problem, reason, None if runtime is None else runtime.identity.agent_version)

    diagnostic("entered")
    try:
        # ADR-0009 M1 (i): the v1 alias is the fixed v1 string, not the send constant, until the first image after 2d-ii.
        if runtime is None or request.headers.getlist(NATIVE_TURN_HEADER) not in (
                [UNLABELED_STATE_PROFILE], [runtime.state_profile]):
            raise refused(Problem(403, "NATIVE_TURNS_DISABLED", "the Hosted native path is not enabled"),
                          "runtime" if runtime is None else "profile")
        if request.state.session_id != runtime.identity.session_id:
            raise Problem(403, "NATIVE_STATE_SCOPE", "the platform session differs from this Hosted process")
        from .behavior_native_turn_wire import NATIVE_EXPECTED_VERSION_HEADER, require_expected_version
        expected = request.headers.getlist(NATIVE_EXPECTED_VERSION_HEADER)
        if len(expected) > 1:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one expected Hosted version is required")
        try:
            require_expected_version(expected[0] if expected else None, runtime.identity.agent_version)
        except Problem as refusal:
            raise (refused(refusal, "version") if refusal.status == 409 else refusal) from None
        if action == "observe":
            raise Problem(422, "NATIVE_REQUEST_INVALID", "use a read-RID invocation; GET does not infer a RID")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_ITEM_BYTES:
                raise Problem(413, "NATIVE_REQUEST_TOO_LARGE", "the complete native request exceeds its bound")
            raw.extend(chunk)
        try:
            def unique(pairs):
                value = dict(pairs)
                if len(value) != len(pairs):
                    raise ValueError("duplicate fields")
                return value

            json.loads(raw, object_pairs_hook=unique)
            command = NATIVE_REQUEST.validate_json(raw)
        except (ValueError, TypeError):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "only the typed native request is accepted") from None
        profile = getattr(command, "state_profile", None)
        # ADR-0009 boundary 8: the body's Invocations profile is bounded like the header, so a v1 runtime stores no v2.
        if profile is not None and channel(profile) == "invocations" and profile not in (
                UNLABELED_STATE_PROFILE, runtime.state_profile):
            raise refused(Problem(403, "NATIVE_TURNS_DISABLED", "the Hosted native path is not enabled"), "profile")
        details["operation"] = command.operation
        original = command.command if isinstance(command, (
            NativeSubmit, NativeAgentReceive, NativeRoutineSubmit, NativeContinuationSubmit)) else command
        rid = getattr(original, "request_id", None)
        if rid is not None:
            details["request_id_sha256"] = hashlib.sha256(rid.encode("utf-8")).hexdigest()
        if action == "cancel" and not isinstance(command, NativeCancel):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "cancellation must name its original CID and RID")
        from .behavior_native_turn_wire import NATIVE_LOCATOR_BINDING_ROW, NATIVE_LOCATOR_HEADER
        located = request.headers.getlist(NATIVE_LOCATOR_HEADER) == [NATIVE_LOCATOR_BINDING_ROW]
        # The API invokes its known session directly; one State store read here refuses a stale binding.
        record = (await runtime._agent_record())[0] if command.operation in _RECORD_READS else None
        if record is not None and runtime.replaced(record, located=located):
            # ADR-0009 4a: the error body (never the snapshot) names the Agent's current SID for the API to go to.
            raise refused(Problem(
                409, "NATIVE_SESSION_CHANGED", "another environment replaced this Hosted session; nothing was read",
                current_hosted_session_id=record.hosted_session_id), "replaced")
        result = await runtime.dispatch(command)
        status = 202 if isinstance(command, (
            NativeSubmit, NativeAgentReceive, NativeRoutineSubmit, NativeContinuationSubmit, NativeAppend)) else 200
        response = JSONResponse(result.model_dump(mode="json"), status_code=status)
        diagnostic("returned", status=status)
        return response
    except Problem as exc:
        response = JSONResponse(exc.body, status_code=exc.status)
        code = exc.body["error"]["code"]
        diagnostic("returned", status=exc.status, code=code if code in NATIVE_TURN_ERRORS else "UNCLASSIFIED")
        return response
    except (Exception, asyncio.CancelledError) as exc:
        diagnostic("raised", exception=exc)
        raise
