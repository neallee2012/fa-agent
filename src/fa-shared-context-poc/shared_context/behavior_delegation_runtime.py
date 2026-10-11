"""Narrow SDK event/hook adapter for the captured synchronous children of one parent turn.

The SDK owns dispatch and runs the ``task`` calls of one turn in parallel; each call keeps its own
reservation, child and result. The released pre-tool hook lacks a tool-call ID, so only an exact,
unique preceding task-start event may supply it; missing or ambiguous events deny dispatch rather
than inventing an ID.
"""

import asyncio
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Protocol

from .behavior_delegation_wire import (
    CHILD_AGENT_TYPE, DelegationModelRef, DelegationObservation, DelegationPrepare,
    DelegationReply, DelegationStatus, DelegationTask, DELEGATION_PREPARE_BYTES, DELEGATION_SIZE_REFUSAL,
)
from .behavior_wire import Command
from .behavior_capabilities import CAPABILITY_CLIENT_SECONDS
from .errors import Problem

# A small fixed bound on task calls in one native parent turn (the SDK itself runs them in parallel).
MAX_PARALLEL_CHILDREN = 4
_ARGUMENTS = (" Use agent_type=fa-delegate-v1 and mode=sync, with name, description and prompt only; "
              "overrides are unavailable.")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _field(value, snake, camel):
    return value.get(snake, value.get(camel))


def not_started(reason, extra=""):
    """A call FA denied before any reservation or child: nothing ran, so the model may retry or do it itself."""
    return (f"Delegation not started [{reason}]: this subtask did not run and no child was created; "
            f"call task again for it or do it yourself.{extra}")


class DelegationOutcome(Protocol):
    sdk_session_id: str
    delegation_id: str
    tool_call_id: str
    input_sha256: str
    child_agent_id: str | None
    status: DelegationStatus
    result: str | None
    fresh: bool

    def model_dump_json(self) -> str: ...


class _Child:
    """One original task call: its reservation reply, SDK child and observations."""

    def __init__(self, tool_call_id: str, input_sha256: str):
        self.tool_call_id, self.input_sha256 = tool_call_id, input_sha256
        self.reply = None
        self.child_id = None
        self.last_text = None
        self.last_has_tools = False
        self.finished = False
        self.idle_event_id = None
        self.completion_observed = asyncio.Event()


class SdkDelegationRuntime(ABC):
    max_children = 1

    def __init__(self, sdk_session_id: str,
                 call: Callable[[str, Command], Awaitable[DelegationOutcome]],
                 current: Callable[[], Awaitable[bool]], *, diagnostic_scope: tuple[str, str],
                 before_prepare: Callable[[DelegationPrepare], Awaitable[None]] | None = None,
                 read_user_files: bool = False):
        self.sdk_session_id = sdk_session_id
        self.diagnostic_scope = diagnostic_scope
        self.call, self.current = call, current
        self.before_prepare = before_prepare
        self.read_user_files = read_user_files
        self.starts = {}
        self.children: dict[str, _Child] = {}
        self._pending = set()
        self.failure = None
        self._lock = asyncio.Lock()
        self.cancelled = False
        self._seen = set()
        self._tail = None

    def _only(self):
        return next(iter(self.children.values())) if len(self.children) == 1 else None

    # Single-child view (the job lane's only child); only tests and diagnostics read it.
    @property
    def tool_call_id(self):
        child = self._only()
        return None if child is None else child.tool_call_id

    @property
    def reply(self):
        child = self._only()
        return None if child is None else child.reply

    @property
    def child_id(self):
        child = self._only()
        return None if child is None else child.child_id

    def _by_agent(self, agent_id):
        if not isinstance(agent_id, str):
            return None
        return next((child for child in self.children.values() if child.child_id == agent_id), None)

    @abstractmethod
    def original_reply(self, value: DelegationOutcome) -> DelegationOutcome:
        """Validate the storage adapter's original parent identity before SDK use."""

    def observe(self, event):
        data = event.get("data", {})
        agent_id = _field(event, "agent_id", "agentId")
        if (event.get("type") == "tool.execution_start" and agent_id in {None, self.sdk_session_id}
                and _field(data, "tool_name", "toolName") == "task"):
            identity = _field(data, "tool_call_id", "toolCallId")
            if isinstance(identity, str):
                arguments = data.get("arguments")
                self.starts[identity] = (arguments if identity not in self.starts or self.starts[identity] == arguments
                                         else None)
            return
        if not self.children:
            return
        kind, event_id = event.get("type"), str(event.get("id", ""))
        if kind not in {"subagent.started", "subagent.completed", "subagent.failed", "assistant.message", "session.idle"}:
            return
        if not event_id or event_id in self._seen:
            return
        self._seen.add(event_id)
        try:
            if kind == "subagent.started":
                child = self.children.get(_field(data, "tool_call_id", "toolCallId"))
                if (child is None or _field(data, "agent_name", "agentName") != CHILD_AGENT_TYPE
                        or not isinstance(agent_id, str) or child.child_id not in {None, agent_id}
                        or self._by_agent(agent_id) not in {None, child}):
                    raise ValueError("original child identity differs")
                child.child_id = agent_id
                self._queue(child, DelegationObservation(event_id=event_id, tool_call_id=child.tool_call_id,
                                                         kind="started", child_agent_id=agent_id))
            elif kind == "assistant.message":
                child = self._by_agent(agent_id)
                if child is not None:
                    child.last_text = data.get("content")
                    child.last_has_tools = bool(_field(data, "tool_requests", "toolRequests"))
                    if child.last_text is not None and not isinstance(child.last_text, str):
                        raise ValueError("public child result must be text")
            elif kind in {"subagent.completed", "subagent.failed"}:
                child = self.children.get(_field(data, "tool_call_id", "toolCallId"))
                if child is None or child.child_id is None or agent_id != child.child_id:
                    raise ValueError("child completion identity differs")
                cancelled = data.get("cancelled") is True
                self.cancelled |= cancelled
                child.finished = not cancelled
                self._queue(child, DelegationObservation(event_id=event_id, tool_call_id=child.tool_call_id,
                    kind="result", child_agent_id=child.child_id, text=child.last_text,
                    ended_in_tool_call=child.last_has_tools,
                    outcome="cancelled" if cancelled else "failed" if kind == "subagent.failed" else "completed",
                    failure_code="SDK_CHILD_FAILED" if kind == "subagent.failed" and not cancelled else None))
                child.completion_observed.set()
                if cancelled and child.idle_event_id is not None:
                    self._queue_idle(child)
            elif (kind == "session.idle" and agent_id in {None, self.sdk_session_id}
                  and (self.cancelled or data.get("aborted") is True)):
                for child in self.children.values():
                    if child.child_id is not None and not child.finished:
                        child.idle_event_id = event_id
                        if self.cancelled:
                            self._queue_idle(child)
        except (TypeError, ValueError):
            self.failure = Problem(409, "DELEGATION_OBSERVATION_INVALID", "the original SDK child observation differs")
            logging.getLogger(__name__).warning("DELEGATION_OBSERVATION_INVALID kind=%s", kind)
            for child in self.children.values():
                child.completion_observed.set()

    def _queue_idle(self, child):
        self._queue(child, DelegationObservation(event_id=child.idle_event_id, tool_call_id=child.tool_call_id,
                                                 kind="parent_idle", child_agent_id=child.child_id))
        child.idle_event_id = None

    def _queue(self, child, command):
        previous = self._tail
        async def record():
            if previous is not None:
                await previous
            return await self._call(child, "observe", command)
        self._tail = asyncio.create_task(record())

    async def drain(self):
        if self._tail is not None:
            await self._tail
        if self.failure is not None:
            raise self.failure

    async def request_cancel(self):
        self.cancelled = True
        await self.drain()
        for child in list(self.children.values()):
            if child.reply is not None and child.reply.status not in {"unknown", "running"}:
                continue
            self._queue(child, DelegationObservation(kind="cancel",
                event_id=_hash(["cancel", self.sdk_session_id, child.tool_call_id]), tool_call_id=child.tool_call_id))
        await self.drain()

    async def _call(self, child, action, command):
        try:
            value = await self.call(action, command)
            value = self.original_reply(value)
            if (value.sdk_session_id != self.sdk_session_id
                    or value.tool_call_id != child.tool_call_id
                    or value.input_sha256 != child.input_sha256
                    or child.reply is not None and child.reply.delegation_id != value.delegation_id
                    or getattr(command, "child_agent_id", None) is not None
                    and command.child_agent_id != value.child_agent_id):
                raise Problem(409, "DELEGATION_SCOPE_CHANGED", "the original delegation response differs")
            child.reply = value
            return value
        except Exception as error:
            if (action == "prepare" and isinstance(error, Problem) and error.status == 413
                    and error.body["error"]["code"] == DELEGATION_SIZE_REFUSAL):
                raise
            self.failure = Problem(503, "DELEGATION_UNKNOWN", "original delegation persistence is unresolved; do not respawn")
            logging.getLogger(__name__).warning("DELEGATION_OBSERVATION_UNRESOLVED phase=%s exception_type=%s",
                                               action, type(error).__name__)
            raise self.failure from None

    async def pre_tool(self, data, context):
        name = data.get("toolName")
        def refused(reason, message=None):
            logging.getLogger(__name__).warning("FA_DELEGATION_REFUSED %s=%s reason=%s",
                                               *self.diagnostic_scope, reason)
            return {"permissionDecision": "deny", "permissionDecisionReason": message or (
                f"Delegation refused [{reason}]. The original reservation for this subtask is unresolved; "
                "do not resend it under another call or claim its result.")}
        if context.get("session_id") != self.sdk_session_id:
            return refused("session_mismatch")
        if data.get("sessionId") != self.sdk_session_id:
            child = self._by_agent(data.get("sessionId"))
            if not self.read_user_files or name != "read_user_file" or child is None or self.cancelled:
                return refused("child_tool_denied")
            try:
                await self.drain()
                if (child.reply is None or child.reply.status != "running"
                        or child.reply.child_agent_id != child.child_id or await self.current() is not True):
                    return refused("original_child_inactive")
            except Problem:
                return refused("original_child_unconfirmed")
            return {"permissionDecision": "allow"}
        if name != "task":
            return None
        try:
            task = DelegationTask.model_validate(data.get("toolArgs"))
        except ValueError:
            return refused("unsupported_arguments", not_started("unsupported_arguments", _ARGUMENTS))
        candidates = [identity for identity, arguments in self.starts.items()
                      if arguments == data.get("toolArgs")]
        if len(candidates) != 1:
            reason = "tool_call_unmatched" if not candidates else "tool_call_ambiguous"
            return refused(reason, not_started(reason, " Give each subtask its own distinct prompt."))
        command = DelegationPrepare(tool_call_id=candidates[0], arguments=task)

        def denied(reason, message):
            # Nothing ran for this call: forget its start so a retry with the same arguments stays unambiguous.
            self.starts.pop(command.tool_call_id, None)
            return refused(reason, message)

        size_refusal = "Delegation material size exceeds 192 KiB. No child or intent was started; reduce the supplied material."
        if len(command.model_dump_json().encode("utf-8")) > DELEGATION_PREPARE_BYTES:
            return denied("material_size_limit", size_refusal)
        async with self._lock:
            if self.cancelled:
                return denied("cancelled", "Delegation not started [cancelled]: this turn was cancelled; "
                              "start no further subtasks.")
            if (command.tool_call_id in self.children or command.tool_call_id in self._pending
                    or self.failure is not None):
                return refused("original_reserved_or_unknown")
            if len(self.children) + len(self._pending) >= self.max_children:
                return denied("child_limit", (
                    f"Delegation not started [child_limit]: at most {self.max_children} subtask call(s) can run "
                    "in this turn, so this one did not run; do it yourself."))
            # The slot is held while a human may still decide; the child exists only once its reservation starts.
            self._pending.add(command.tool_call_id)
        try:
            try:
                authorized = await self.current()
            except Exception:
                return denied("authority_unavailable", not_started("authority_unavailable"))
            if authorized is not True:
                return denied("current_authority_denied", not_started("current_authority_denied"))
            if self.before_prepare is not None:
                try:
                    await self.before_prepare(command)
                except Problem as error:
                    code = error.body["error"]["code"]
                    if not code.startswith("NATIVE_HITL_"):
                        raise
                    return denied(code, "This original child action was not approved; do not resend it.")
        except Exception as error:
            # The hook may already have written this call's reservation, so its outcome is unknown.
            self.failure = self.failure or Problem(
                503, "DELEGATION_UNKNOWN", "original delegation reservation is unresolved; do not respawn")
            logging.getLogger(__name__).warning("DELEGATION_OBSERVATION_UNRESOLVED phase=before_prepare exception_type=%s",
                                               type(error).__name__)
            return refused("authority_or_reservation_error")
        finally:
            self._pending.discard(command.tool_call_id)
        if self.failure is not None:
            # Another call's reservation became unresolved while this one waited.
            return refused("original_reserved_or_unknown")
        child = self.children[command.tool_call_id] = _Child(command.tool_call_id, _hash(task.model_dump(mode="json")))
        try:
            receipt = await self._call(child, "prepare", command)
            if self.cancelled:
                return refused("cancelled")
            if not receipt.fresh:
                self.failure = Problem(409, "DELEGATION_ORIGINAL_REQUIRED", "the original intent is already reserved; do not respawn")
                return refused("original_reserved_or_unknown")
            if receipt.status != "unknown" or receipt.child_agent_id is not None or receipt.result is not None:
                self.failure = Problem(409, "DELEGATION_RECORD_INVALID", "the first reservation cannot contain another child")
                return refused("initial_receipt_invalid")
        except Problem as error:
            if error.body["error"]["code"] == DELEGATION_SIZE_REFUSAL:
                self.children.pop(command.tool_call_id, None)
                return denied("material_size_limit", size_refusal)
            return refused("authority_or_reservation_error")
        except Exception:
            return refused("authority_or_reservation_error")
        return {"permissionDecision": "allow", "modifiedArgs": task.model_dump(mode="json")}

    async def post_tool(self, data, context):
        if data.get("toolName") != "task":
            return None
        refusal = {"modifiedResult": {"resultType": "failure", "textResultForLlm":
            "The original delegation outcome is unknown or refused; do not spawn another child or claim success."}}
        try:
            if data.get("sessionId") != self.sdk_session_id or context.get("session_id") != self.sdk_session_id:
                return refusal
            task = DelegationTask.model_validate(data.get("toolArgs"))
            matches = [child for child in self.children.values()
                       if child.input_sha256 == _hash(task.model_dump(mode="json"))]
            if len(matches) != 1:
                return refusal
            child = matches[0]
            await self.drain()
            if child.reply is None:
                return refusal
            actual = data.get("toolResult", {})
            if child.child_id is not None and isinstance(actual, dict) and actual.get("resultType") == "success":
                if not child.completion_observed.is_set():
                    await asyncio.wait_for(child.completion_observed.wait(), timeout=CAPABILITY_CLIENT_SECONDS)
                await self.drain()
            if (child.reply.status == "completed" and (not isinstance(actual, dict)
                    or actual.get("textResultForLlm") != child.reply.result)):
                raise Problem(409, "DELEGATION_RESULT_INVALID", "SDK output differs from the original complete saved result")
            return {"modifiedResult": {"resultType": "success" if child.reply.status == "completed" else "failure",
                                       "textResultForLlm": child.reply.model_dump_json()}}
        except Exception:
            self.failure = self.failure or Problem(503, "DELEGATION_UNKNOWN", "original child result could not be verified")
            logging.getLogger(__name__).warning("DELEGATION_RESULT_UNRESOLVED original_call=true")
            return refusal

    async def model_ref(self, context):
        await self.drain()
        if context.session_id != self.sdk_session_id:
            raise Problem(403, "DELEGATION_MODEL_DENIED", "model request belongs to another original SDK session")
        if context.agent_id in {None, self.sdk_session_id} and context.parent_agent_id is None:
            return None
        child = self._by_agent(context.agent_id)
        if (child is None or context.parent_agent_id != self.sdk_session_id
                or child.reply is None or child.reply.status != "running"):
            raise Problem(403, "DELEGATION_MODEL_DENIED", "only the original persisted child may use the parent model route")
        return DelegationModelRef(tool_call_id=child.tool_call_id, child_agent_id=child.child_id)

    async def require_complete(self):
        await self.drain()
        if any(child.reply is None or child.reply.status != "completed" for child in self.children.values()):
            raise Problem(409, "DELEGATION_INCOMPLETE", "the original child has no complete result; the parent cannot claim success")

    @property
    def attempted(self):
        """This turn called ``task`` (seen, held, reserved, failed or cancelled). A turn that never did has no SDK
        child, so its SDK may be kept (ADR-0005); any attempt stops it as before."""
        return bool(self.starts or self._pending or self.children) or self.failure is not None or self.cancelled

    async def finish(self):
        try:
            await self.drain()
        finally:
            # Record each still-running child even if another child's write fails.
            unresolved = None
            for child in list(self.children.values()):
                if child.reply is not None and child.reply.status == "running":
                    try:
                        await self._call(child, "observe", DelegationObservation(kind="unknown",
                            event_id=_hash(["ended_without_child_result", self.sdk_session_id, child.tool_call_id]),
                            tool_call_id=child.tool_call_id, child_agent_id=child.child_id,
                            failure_code="SDK_CHILD_OUTCOME_UNKNOWN"))
                    except Problem as error:
                        unresolved = unresolved or error
            if unresolved is not None:
                raise unresolved


class DelegationRuntime(SdkDelegationRuntime):
    """Keep the legacy Job/ticket wire at its existing adapter, not inside SDK event handling."""

    def __init__(self, job_id: str, sdk_session_id: str,
                 call: Callable[[str, Command], Awaitable[DelegationReply]],
                 current: Callable[[], Awaitable[bool]]):
        self.job_id = job_id
        super().__init__(sdk_session_id, call, current, diagnostic_scope=("job_id", job_id))

    def original_reply(self, value: DelegationOutcome) -> DelegationReply:
        from .behavior_assistant_boundary import AssistantModelBoundary

        original = AssistantModelBoundary.delegation_result(value)
        if original.parent_job_id != self.job_id:
            raise Problem(409, "DELEGATION_SCOPE_CHANGED", "the original parent job differs")
        return original
