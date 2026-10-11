"""Original native RID effects for the SDK-owned synchronous child; no Job or agent loop."""

import json
from typing import Literal

from pydantic import Field, model_validator

from .behavior_capabilities import BUILTIN_CAPABILITIES, DELEGATE_READER_ENTRY_ID
from .behavior_delegation_runtime import MAX_PARALLEL_CHILDREN, DelegationOutcome, SdkDelegationRuntime
from .behavior_delegation_wire import (
    CAPABILITY_ID, DelegationObservation, DelegationPrepare, DelegationPublicText, DelegationStatus,
    pack_delegation_text, unpack_delegation_text, validate_delegation_outcome,
)
from .behavior_native_turn_wire import material_digest
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .native_capabilities import NativeCapabilityBinding, NativeCapabilityContext, NativeCapabilityProvider
from .native_effects import NativeEffectClaim, NativeEffectRecord


class NativeDelegationReply(WireModel):
    schema_version: Literal["fa-native-delegation-reply-v1"] = "fa-native-delegation-reply-v1"
    agent_id: Identifier
    conversation_id: Identifier
    parent_request_id: Identifier
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


class _Progress(WireModel):
    status: DelegationStatus = "unknown"
    child_agent_id: Identifier | None = None
    text: DelegationPublicText | None = None
    failure_code: Identifier | None = None
    cancel_requested: bool = False
    child_cancelled: bool = False
    parent_idle: bool = False
    observations: dict[str, Digest] = Field(default_factory=dict)

    @model_validator(mode="after")
    def original_outcome(self):
        if (self.status in {"running", "completed", "failed", "incomplete", "cancelled"}
                or self.child_cancelled or self.parent_idle or self.text is not None) and self.child_agent_id is None:
            raise ValueError("the original child identity is required")
        if self.status == "completed" and self.text is None:
            raise ValueError("a complete original child result is required")
        if self.status == "failed" and self.failure_code is None:
            raise ValueError("a failed child retains its explicit public reason")
        if self.status == "incomplete" and self.failure_code != "DELEGATION_RESULT_INCOMPLETE":
            raise ValueError("incomplete is not a successful result")
        if (self.child_cancelled and self.parent_idle) != (self.status == "cancelled"):
            raise ValueError("cancelled requires the original child and parent stop observations")
        return self


def _json(value):
    return json.loads(value.model_dump_json())


def delegation_material(receipt, sdk_session_id, arguments):
    return {
        "kind": "native-sdk-delegation-v1", "sdk_session_id": sdk_session_id,
        "submit_digest": receipt.submit_digest, "capture_digest": receipt.capture_digest, "arguments": arguments,
    }


class NativeDelegationRuntime(SdkDelegationRuntime):
    max_children = MAX_PARALLEL_CHILDREN

    def __init__(self, context: NativeCapabilityContext):
        receipt, scope = context.receipt, context.scope
        grant = next((value for value in (() if receipt.capture.capabilities is None else receipt.capture.capabilities.grants)
                      if value.entry_id == CAPABILITY_ID), None)
        if grant is None or grant.entry_sha256 != BUILTIN_CAPABILITIES[CAPABILITY_ID].entry_sha256:
            raise Problem(403, "DELEGATION_DISABLED", "the original native turn did not select the fixed child")
        if (receipt.agent_id, receipt.conversation_id) != (scope.agent_id, scope.conversation_id):
            raise Problem(403, "DELEGATION_SCOPE_CHANGED", "the child must belong to the original native conversation")
        if receipt.status != "started" or context.identity.agent_id != receipt.agent_id:
            raise Problem(403, "DELEGATION_PARENT_INACTIVE", "only the original started native turn may bind a child")
        self.context = context
        self.effects = context.effects
        # One original effect claim and material per SDK task call of this turn.
        self.claims: dict[str, NativeEffectClaim] = {}
        self.materials: dict[str, dict] = {}
        reader = next((value for value in receipt.capture.capabilities.grants
                       if value.entry_id == DELEGATE_READER_ENTRY_ID), None)
        if reader is not None:
            from .behavior_skill_prompt import assemble_prompt
            if (reader.entry_sha256 != BUILTIN_CAPABILITIES[DELEGATE_READER_ENTRY_ID].entry_sha256
                    or "read_user_file" not in assemble_prompt(receipt.capture.template.role.profile).record.declared_capabilities):
                raise Problem(403, "DELEGATION_DISABLED", "the child reader must already belong to the parent")
        super().__init__(scope.sdk_session_id, self._operation, context.current,
                         diagnostic_scope=("request_id", receipt.request_id),
                         before_prepare=None if context.hitl is None else self._hitl_prepare,
                         read_user_files=reader is not None)

    async def _hitl_prepare(self, command):
        call = command.tool_call_id
        self.materials[call] = delegation_material(
            self.context.receipt, self.sdk_session_id, command.arguments.model_dump(mode="json"))
        self.claims[call] = await self.effects.reserve(call, self.materials[call])
        self.claims[call] = await self.context.hitl.delegation(command, self.claims[call], self.materials[call])

    def original_reply(self, value: DelegationOutcome) -> NativeDelegationReply:
        original = NativeDelegationReply.model_validate_json(value.model_dump_json())
        receipt = self.context.receipt
        if (original.agent_id, original.conversation_id, original.parent_request_id) != (
                receipt.agent_id, receipt.conversation_id, receipt.request_id):
            raise Problem(409, "DELEGATION_SCOPE_CHANGED", "the original native parent differs")
        return original

    @staticmethod
    def _progress(record: NativeEffectRecord) -> _Progress:
        value = record.result if record.status == "done" else record.progress
        return _Progress() if value is None else _Progress.model_validate_json(json.dumps(value))

    def _reply(self, record: NativeEffectRecord, *, fresh=False) -> NativeDelegationReply:
        progress = self._progress(record)
        receipt = self.context.receipt
        return NativeDelegationReply(
            agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
            parent_request_id=receipt.request_id, sdk_session_id=self.sdk_session_id,
            delegation_id=material_digest({
                "agent": receipt.agent_id, "cid": receipt.conversation_id,
                "rid": receipt.request_id, "call": record.tool_call_id,
            }),
            tool_call_id=record.tool_call_id, input_sha256=self.children[record.tool_call_id].input_sha256,
            child_agent_id=progress.child_agent_id, status=progress.status,
            result=None if progress.text is None else unpack_delegation_text(progress.text),
            failure_code=progress.failure_code, fresh=fresh, cancel_requested=progress.cancel_requested,
            child_cancelled=progress.child_cancelled, parent_idle=progress.parent_idle,
            stop_confirmed=progress.status == "cancelled",
        )

    async def read(self, tool_call_id=None) -> NativeDelegationReply:
        if tool_call_id is None and len(self.claims) == 1:
            tool_call_id = next(iter(self.claims))
        claim, material = self.claims.get(tool_call_id), self.materials.get(tool_call_id)
        if claim is None or material is None:
            raise Problem(409, "DELEGATION_ORIGINAL_REQUIRED", "the original native child reservation is required")
        record = await self.effects.read(claim.record.tool_call_id, material)
        if record is None:
            raise Problem(503, "DELEGATION_UNKNOWN", "the original native child effect is unavailable")
        return self._reply(record)

    async def require_complete(self):
        try:
            await super().require_complete()
        except Problem as error:
            self.failure = error
            raise

    async def _operation(self, action, command):
        if action == "prepare":
            command = DelegationPrepare.model_validate_json(command.model_dump_json())
            call = command.tool_call_id
            material = delegation_material(
                self.context.receipt, self.sdk_session_id, command.arguments.model_dump(mode="json"))
            if self.context.hitl is not None:
                claim = self.claims.get(call)
                if claim is None or self.materials.get(call) != material or claim.record.tool_call_id != call:
                    raise Problem(403, "NATIVE_HITL_PERMISSION_REQUIRED", "the original approved child claim is required")
            else:
                self.materials[call] = material
                self.claims[call] = await self.effects.reserve(call, material)
            return self._reply(self.claims[call].record, fresh=self.claims[call].fresh)
        claim = self.claims.get(getattr(command, "tool_call_id", None))
        if action != "observe" or claim is None or not claim.fresh:
            raise Problem(409, "DELEGATION_ORIGINAL_REQUIRED", "only the original claim may observe its SDK child")
        command = DelegationObservation.model_validate_json(command.model_dump_json())
        if command.tool_call_id != claim.record.tool_call_id:
            raise Problem(409, "DELEGATION_SCOPE_CHANGED", "the SDK event names a different original call")
        current = self._progress(claim.record)
        event_digest = material_digest(command.model_dump(mode="json"))
        prior = current.observations.get(command.event_id)
        if prior is not None:
            if prior != event_digest:
                raise Problem(409, "DELEGATION_OBSERVATION_INVALID", "the original event has different material")
            return self._reply(claim.record)
        if claim.record.status == "done":
            raise Problem(409, "DELEGATION_TERMINAL", "a later event cannot replace the original child outcome")
        if (current.child_agent_id is not None and command.child_agent_id is not None
                and current.child_agent_id != command.child_agent_id):
            raise Problem(409, "DELEGATION_CHILD_CHANGED", "a later child cannot replace the original child")
        changes = {"observations": {**current.observations, command.event_id: event_digest}}
        if command.kind == "started":
            changes.update(child_agent_id=command.child_agent_id,
                           status="unknown" if current.cancel_requested else "running")
        elif command.kind == "result":
            incomplete = command.outcome == "completed" and (
                command.ended_in_tool_call or not command.text or not command.text.strip())
            changes.update(
                child_agent_id=command.child_agent_id, text=pack_delegation_text(command.text),
                status="incomplete" if incomplete else command.outcome,
                failure_code="DELEGATION_RESULT_INCOMPLETE" if incomplete else command.failure_code,
            )
            if command.outcome == "cancelled":
                changes.update(child_cancelled=True, status="cancelled" if current.parent_idle else "unknown",
                               failure_code=None if current.parent_idle else "DELEGATION_STOP_PENDING")
        elif command.kind == "cancel":
            changes.update(cancel_requested=True, status="unknown", failure_code="DELEGATION_STOP_PENDING")
        elif command.kind == "parent_idle":
            if current.child_agent_id is None or not (current.cancel_requested or current.child_cancelled):
                raise Problem(409, "DELEGATION_STOP_SCOPE", "idle requires this original child's cancellation")
            changes.update(parent_idle=True, status="cancelled" if current.child_cancelled else "unknown",
                           failure_code=None if current.child_cancelled else "DELEGATION_STOP_PENDING")
        else:
            changes.update(status="unknown", failure_code=command.failure_code,
                           child_agent_id=command.child_agent_id or current.child_agent_id)
        updated = _Progress.model_validate_json(current.model_copy(update=changes).model_dump_json())
        if updated.status in {"completed", "failed", "incomplete", "cancelled"} or command.kind == "unknown":
            done = await self.effects.finish(claim, _json(updated))
            self.claims[command.tool_call_id] = NativeEffectClaim(done, claim.etag, False)
            return self._reply(done)
        self.claims[command.tool_call_id] = await self.effects.observe(claim, _json(updated))
        return self._reply(self.claims[command.tool_call_id].record)


def delegation_provider() -> NativeCapabilityProvider:
    def accepts(grant):
        return (grant.entry_id in {CAPABILITY_ID, DELEGATE_READER_ENTRY_ID}
                and grant.entry_sha256 == BUILTIN_CAPABILITIES[grant.entry_id].entry_sha256)

    async def bind(context, grants):
        if not grants or any(not accepts(grant) for grant in grants):
            raise Problem(403, "DELEGATION_DISABLED", "one captured fixed delegation grant is required")
        if not any(grant.entry_id == CAPABILITY_ID for grant in grants):
            return NativeCapabilityBinding()
        return NativeCapabilityBinding(delegation_runtime=NativeDelegationRuntime(context))

    return NativeCapabilityProvider(accepts=accepts, bind=bind, name="delegation")
