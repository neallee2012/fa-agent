"""Original RID/tool-call effects in ADR-0007's existing State store, with no replay policy."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from typing import Annotated, Literal
from uuid import uuid4

from azure.ai.agentserver.core.storage import FoundryStorageError, FoundryStoragePreconditionError
from azure.core.exceptions import AzureError
from pydantic import Field, JsonValue, model_validator

from .behavior_native_turn_wire import NativeReceipt, material_digest
from .behavior_wire import Digest, Identifier, WireModel
from .behavior_sdk_state import NATIVE_SEGMENT_BYTES
from .native_hitl_wire import (
    NativeHitlEffectState, NativeHitlReviewEntry, NativeHitlReviewHistory, native_hitl_effect_material,
)
from .native_continuation_wire import NativeOperation
from .errors import Problem
from .native_turns import EFFECTS_STORE, NativeTurnControl, NativeTurnRuntime, _decode, _json

_UNCERTAIN = (FoundryStorageError, AzureError, OSError)


class NativeEffectImage(WireModel):
    kind: Literal["fa-native-effect-image-v1"] = "fa-native-effect-image-v1"
    sha256: Digest
    byte_length: Annotated[int, Field(ge=1, le=1024 * 1024)]
    segments: Annotated[int, Field(ge=1, le=2)]


class NativeEffectRecord(WireModel):
    kind: Literal["fa-native-tool-effect-v1"] = "fa-native-tool-effect-v1"
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    tool_call_id: Annotated[str, Field(min_length=1, max_length=256)]
    input_digest: Digest
    token: Identifier
    status: Literal["pending", "done"] = "pending"
    result: JsonValue = None
    progress: JsonValue = Field(default=None, exclude_if=lambda value: value is None)
    hitl: NativeHitlEffectState | None = Field(default=None, exclude_if=lambda value: value is None)
    operation: NativeOperation | None = Field(default=None, exclude_if=lambda value: value is None)
    dispatch_started: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    refusal_code: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def pending_has_no_result(self):
        if self.status == "pending" and self.result is not None:
            raise ValueError("an unresolved effect cannot claim a result")
        if (self.operation is None) != (self.dispatch_started is None):
            raise ValueError("only a recorded operation may carry a dispatch watermark")
        if self.operation is not None and self.status == "done" and self.dispatch_started is not True:
            raise ValueError("a completed operation must retain its original dispatch watermark")
        if self.refusal_code is not None and (
                self.operation is None or self.dispatch_started is not False or self.status != "pending"):
            raise ValueError("a refused operation cannot claim execution")
        return self


@dataclass(frozen=True)
class NativeEffectClaim:
    record: NativeEffectRecord
    etag: str
    fresh: bool


class _OperationIndex(WireModel):
    kind: Literal["fa-native-operation-index-v1"] = "fa-native-operation-index-v1"
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    submit_digest: Digest
    calls: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] = ()

    @model_validator(mode="after")
    def unique_calls(self):
        if len(set(self.calls)) != len(self.calls):
            raise ValueError("operation indexes retain distinct SDK call IDs")
        return self


def _memory_claim(key, value):
    if not isinstance(value, dict) or not isinstance(value.get("request_id"), str):
        return False
    if value.get("kind") == "fa-private-memory-update-claim-v1":
        from .native_private_memory import claim_key
    elif value.get("kind") == "fa-team-note-claim-v1":
        from .native_team_memory import claim_key
    else:
        return False
    return key == claim_key(value["request_id"])


async def read_hitl_history(runtime, command):
    receipt = await runtime.read(command.request_id)
    if receipt.agent_id != runtime.identity.agent_id or receipt.conversation_id != command.conversation_id:
        raise Problem(403, "NATIVE_STATE_SCOPE", "reviews belong to the original Agent and conversation")
    entries = []
    try:
        async with runtime._store(EFFECTS_STORE) as store:
            page = await store.list_keys(limit=command.limit, after=command.after)
            if len(page.keys) > command.limit or page.has_more and (
                    not page.last_id or page.last_id == command.after):
                raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original review page did not advance")
            for key in page.keys:
                if not key.key.startswith("fx:"):
                    continue
                item = await store.get_item(key.key)
                if item is None:
                    raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "a listed original effect could not be read")
                if _memory_claim(key.key, item.value):
                    continue
                record = _decode(item.value, NativeEffectRecord)
                if key.key != NativeEffects._key(record):
                    raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the saved effect key differs from its original call")
                if record.agent_id != receipt.agent_id:
                    raise Problem(403, "NATIVE_STATE_SCOPE", "the saved effect belongs to another Agent")
                if (record.conversation_id, record.request_id) != (receipt.conversation_id, receipt.request_id):
                    continue
                state = record.hitl
                if state is not None and (
                        state.action.call.agent_id, state.action.call.conversation_id,
                        state.action.call.request_id, state.action.call.tool_call_id) != (
                            record.agent_id, record.conversation_id, record.request_id, record.tool_call_id):
                    raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the saved review belongs to another original call")
                if state is not None:
                    material = native_hitl_effect_material(receipt, state.action)
                    if state.action.category == "delegation":
                        from .native_delegation import delegation_material
                        material = delegation_material(receipt, state.action.call.sdk_session_id, state.action.arguments)
                    if record.input_digest != material_digest(material):
                        raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the action differs from the original effect input")
                reviews = () if state is None else state.reviews
                entries.append(NativeHitlReviewEntry(
                    tool_call_id=record.tool_call_id, action=None if state is None else state.action,
                    action_digest=None if state is None else state.action.sha256, reviews=reviews,
                    status="recorded" if reviews else "not_recorded",
                    time_status="recorded" if reviews and all(review.reviewed_at is not None for review in reviews)
                    else "unknown",
                ))
    except _UNCERTAIN:
        raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original review history read is unconfirmed") from None
    return NativeHitlReviewHistory(
        agent_id=receipt.agent_id, conversation_id=receipt.conversation_id, request_id=receipt.request_id,
        entries=tuple(entries), next_after=page.last_id if page.has_more else None,
    )


class NativeEffects:
    def __init__(self, runtime: NativeTurnRuntime, receipt: NativeReceipt, control: NativeTurnControl | None = None):
        self.runtime, self.receipt, self.control = runtime, receipt, control

    def _record(self, call_id, material, *, operation=None):
        receipt = self.receipt
        return NativeEffectRecord(
            agent_id=receipt.agent_id, conversation_id=receipt.conversation_id, request_id=receipt.request_id,
            tool_call_id=call_id, input_digest=material_digest(material), token=uuid4().hex,
            operation=operation, dispatch_started=None if operation is None else False,
        )

    @staticmethod
    def _key(record):
        # Keep arbitrary SDK call IDs out of key syntax and below core's 128-character bound.
        return ("opfx:" if record.operation is not None else "fx:") + material_digest({
            "kind": record.kind, "rid": record.request_id, "tool_call_id": record.tool_call_id,
        })

    async def _active(self):
        if self.receipt.continuation is not None:
            from .native_continuations import stop_requested
            if await stop_requested(self.runtime, self.receipt):
                raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the original continuation root was stopped")
        original = await self.runtime.read(self.receipt.request_id)
        if (original.agent_id != self.runtime.identity.agent_id
                or original.conversation_id != self.receipt.conversation_id
                or original.submit_digest != self.receipt.submit_digest
                or original.status not in {"started", "completed"}
                or original.status != self.receipt.status or original.cancel_requested
                or original.result != self.receipt.result
                or self.control is not None and self.control.stop_requested()):
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the original native turn no longer permits this effect")

    @staticmethod
    def _checked(item, expected):
        if item is None:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original effect record cannot be confirmed")
        record = _decode(item.value, NativeEffectRecord)
        if (record.agent_id, record.conversation_id, record.request_id, record.tool_call_id, record.input_digest) != (
                expected.agent_id, expected.conversation_id, expected.request_id,
                expected.tool_call_id, expected.input_digest):
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "this original effect key has different input or scope")
        if expected.operation is not None and record.operation != expected.operation:
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the original operation identity differs")
        return record

    async def read(self, call_id, material, *, operation=None) -> NativeEffectRecord | None:
        expected = self._record(call_id, material, operation=operation)
        try:
            async with self.runtime._store(EFFECTS_STORE) as store:
                item = await store.get_item(self._key(expected))
        except _UNCERTAIN:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original effect read is unconfirmed") from None
        return None if item is None else self._checked(item, expected)

    async def reserve(self, call_id, material, *, operation=None) -> NativeEffectClaim:
        await self._active()
        expected = self._record(call_id, material, operation=operation)
        try:
            async with self.runtime._store(EFFECTS_STORE) as store:
                try:
                    await store.create_item(self._key(expected), _json(expected))
                except _UNCERTAIN:
                    # Our unique token can reconcile only this pending write, before any effect has run.
                    pass
                item = await store.get_item(self._key(expected))
        except _UNCERTAIN:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "effect reservation is unconfirmed; do not execute") from None
        found = self._checked(item, expected)
        return NativeEffectClaim(found, item.etag, found.token == expected.token and found.status == "pending")

    def _index(self):
        receipt = self.receipt
        return _OperationIndex(
            agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
            request_id=receipt.request_id, submit_digest=receipt.submit_digest)

    def _index_key(self):
        return "op-index:" + material_digest({"rid": self.receipt.request_id})

    def _checked_index(self, item):
        if item is None:
            raise Problem(409, "NATIVE_CONTINUATION_WATERMARK_UNAVAILABLE",
                          "this original turn has no complete operation index")
        value = _decode(item.value, _OperationIndex)
        if value.model_dump(exclude={"calls"}) != self._index().model_dump(exclude={"calls"}):
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the operation index belongs to another original turn")
        return value

    async def start_operations(self):
        await self._active()
        try:
            async with self.runtime._store(EFFECTS_STORE) as store:
                item = await store.get_item(self._index_key())
                if item is None:
                    try:
                        await store.create_item(self._index_key(), _json(self._index()))
                    except _UNCERTAIN:
                        pass
                    item = await store.get_item(self._index_key())
                self._checked_index(item)
        except _UNCERTAIN:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original operation index is unconfirmed") from None

    def _operation_material(self, operation):
        return {"kind": "native-sdk-operation-v1", "submit_digest": self.receipt.submit_digest,
                "operation": operation.model_dump(mode="json")}

    async def prepare_operation(self, call_id, operation: NativeOperation):
        claim = await self.reserve(call_id, self._operation_material(operation), operation=operation)
        try:
            async with self.runtime._store(EFFECTS_STORE) as store:
                for _ in range(3):
                    item = await store.get_item(self._index_key())
                    index = self._checked_index(item)
                    if call_id in index.calls:
                        return claim
                    value = _OperationIndex.model_validate_json(
                        index.model_copy(update={"calls": (*index.calls, call_id)}).model_dump_json())
                    try:
                        await store.set_item(self._index_key(), _json(value), if_match=item.etag)
                        return claim
                    except _UNCERTAIN as error:
                        found = self._checked_index(await store.get_item(self._index_key()))
                        if call_id in found.calls:
                            return claim
                        if not isinstance(error, FoundryStoragePreconditionError):
                            raise Problem(503, "NATIVE_EFFECT_UNKNOWN",
                                          "the original operation index write is unconfirmed") from None
        except _UNCERTAIN:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original operation index is unconfirmed") from None
        raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original operation index remains conflicted")

    async def operation_records(self) -> tuple[NativeEffectRecord, ...]:
        try:
            async with self.runtime._store(EFFECTS_STORE) as store:
                index = self._checked_index(await store.get_item(self._index_key()))
                records = []
                for call_id in index.calls:
                    key = "opfx:" + material_digest({
                        "kind": "fa-native-tool-effect-v1", "rid": self.receipt.request_id, "tool_call_id": call_id})
                    item = await store.get_item(key)
                    if item is None:
                        raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "an indexed original operation is missing")
                    record = _decode(item.value, NativeEffectRecord)
                    if record.operation is None or self._key(record) != key:
                        raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the indexed operation identity differs")
                    expected = self._record(call_id, self._operation_material(record.operation),
                                            operation=record.operation)
                    records.append(self._checked(item, expected))
                return tuple(records)
        except _UNCERTAIN:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original operations could not be read") from None

    def _original_claim(self, claim: NativeEffectClaim):
        if not claim.fresh or claim.record.status != "pending":
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "only the original in-memory claim may record its effect")
        if (claim.record.agent_id, claim.record.conversation_id, claim.record.request_id) != (
                self.runtime.identity.agent_id, self.receipt.conversation_id, self.receipt.request_id):
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the effect reservation belongs to another turn")
        if self.receipt.agent_id != self.runtime.identity.agent_id or not claim.etag:
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the original effect owner or ETag differs")

    async def _write_claim(self, claim: NativeEffectClaim, replacement: NativeEffectRecord) -> NativeEffectClaim:
        self._original_claim(claim)
        try:
            async with self.runtime._store(EFFECTS_STORE) as store:
                item = await store.get_item(self._key(claim.record))
                original = self._checked(item, claim.record)
                if original.token != claim.record.token:
                    raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the original reservation token differs")
                if original == replacement:
                    return NativeEffectClaim(original, item.etag, True)
                if original != claim.record or item.etag != claim.etag:
                    raise Problem(409, "NATIVE_EFFECT_CONFLICT", "read the original effect; this claim is stale")
                try:
                    await store.set_item(self._key(replacement), _json(replacement), if_match=claim.etag)
                except _UNCERTAIN:
                    pass
                item = await store.get_item(self._key(replacement))
        except _UNCERTAIN:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original effect observation is unconfirmed") from None
        found = self._checked(item, replacement)
        if found != replacement:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original effect observation does not match")
        return NativeEffectClaim(found, item.etag, True)

    async def observe(self, claim: NativeEffectClaim, progress: JsonValue) -> NativeEffectClaim:
        observed = NativeEffectRecord.model_validate_json(
            claim.record.model_copy(update={"progress": progress}).model_dump_json())
        return await self._write_claim(claim, observed)

    async def mark_dispatched(self, claim: NativeEffectClaim) -> NativeEffectClaim:
        if (claim.record.operation is None or claim.record.dispatch_started is not False
                or claim.record.refusal_code is not None):
            raise Problem(409, "NATIVE_EFFECT_UNKNOWN", "only a proven unstarted operation may dispatch")
        await self._active()
        value = NativeEffectRecord.model_validate_json(
            claim.record.model_copy(update={"dispatch_started": True}).model_dump_json())
        observed = await self._write_claim(claim, value)
        await self._active()
        return observed

    async def refuse_operation(self, claim: NativeEffectClaim, code: str) -> NativeEffectClaim:
        if claim.record.operation is None or claim.record.dispatch_started is not False:
            raise Problem(409, "NATIVE_EFFECT_UNKNOWN", "a dispatched operation cannot become an unstarted refusal")
        value = NativeEffectRecord.model_validate_json(
            claim.record.model_copy(update={"refusal_code": code}).model_dump_json())
        return await self._write_claim(claim, value)

    async def annotate_hitl(self, claim: NativeEffectClaim, value: NativeHitlEffectState) -> NativeEffectClaim:
        call = value.action.call
        if (call.agent_id, call.conversation_id, call.request_id, call.tool_call_id) != (
                claim.record.agent_id, claim.record.conversation_id, claim.record.request_id, claim.record.tool_call_id):
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "approval metadata must belong to this original effect")
        old = claim.record.hitl
        if old is not None and (old.action != value.action or value.reviews[:len(old.reviews)] != old.reviews
                or old.pending is not None and old.pending != value.pending
                or old.authorization is not None and old.authorization != value.authorization):
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "original approval material cannot be rebound or rewritten")
        if value.authorization is not None and value.authorization.effect_token != claim.record.token:
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "authorization belongs to another reservation")
        observed = NativeEffectRecord.model_validate_json(
            claim.record.model_copy(update={"hitl": value}).model_dump_json())
        return await self._write_claim(claim, observed)

    def _image_key(self, record, image, index):
        return "fx-img:" + material_digest({"effect": self._key(record), "image": image.sha256}) + ":" + str(index)

    async def save_image(self, claim: NativeEffectClaim, encoded: str) -> NativeEffectImage:
        """Keep original PNG bytes in the same State store, using N3's 512 KiB segment size."""
        self._original_claim(claim)
        data = base64.b64decode(encoded, validate=True)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the original tool image must be a PNG")
        chunks = [data[start:start + NATIVE_SEGMENT_BYTES] for start in range(0, len(data), NATIVE_SEGMENT_BYTES)]
        image = NativeEffectImage(sha256=hashlib.sha256(data).hexdigest(), byte_length=len(data), segments=len(chunks))
        try:
            async with self.runtime._store(EFFECTS_STORE) as store:
                for index, chunk in enumerate(chunks):
                    value = {"kind": "fa-native-effect-image-segment-v1", "effect": self._key(claim.record),
                             "token": claim.record.token, "sha256": image.sha256, "index": index,
                             "count": len(chunks), "segment_sha256": hashlib.sha256(chunk).hexdigest(),
                             "data": base64.b64encode(chunk).decode("ascii")}
                    key = self._image_key(claim.record, image, index)
                    try:
                        await store.create_item(key, value)
                    except _UNCERTAIN:
                        pass
                    item = await store.get_item(key)
                    if item is None or item.value != value:
                        raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original image segment is unconfirmed")
                await self._load_image(store, claim.record, image)
        except _UNCERTAIN:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original effect image is unconfirmed") from None
        return image

    async def _load_image(self, store, record, image):
        if image.segments != (image.byte_length + NATIVE_SEGMENT_BYTES - 1) // NATIVE_SEGMENT_BYTES:
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the original image segment count differs")
        chunks = []
        for index in range(image.segments):
            item = await store.get_item(self._image_key(record, image, index))
            try:
                value = item.value
                raw = base64.b64decode(value["data"], validate=True)
                if (set(value) != {"kind", "effect", "token", "sha256", "index", "count", "segment_sha256", "data"}
                        or (value["kind"], value["effect"], value["token"], value["sha256"], value["index"], value["count"]) !=
                        ("fa-native-effect-image-segment-v1", self._key(record), record.token, image.sha256, index, image.segments)
                        or hashlib.sha256(raw).hexdigest() != value["segment_sha256"]
                        or len(raw) != min(NATIVE_SEGMENT_BYTES, image.byte_length - index * NATIVE_SEGMENT_BYTES)):
                    raise ValueError("different image segment")
            except (AttributeError, KeyError, TypeError, ValueError):
                raise Problem(409, "NATIVE_EFFECT_CONFLICT", "an original image segment is missing or differs") from None
            chunks.append(raw)
        raw = b"".join(chunks)
        if len(raw) != image.byte_length or hashlib.sha256(raw).hexdigest() != image.sha256:
            raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the complete original image differs")
        return base64.b64encode(raw).decode("ascii")

    async def read_image(self, call_id, material):
        record = await self.read(call_id, material)
        if record is None or record.status != "done" or not isinstance(record.result, dict):
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original image result is not confirmed")
        reference = record.result.get("image")
        if reference is None:
            return record.result.get("png_base64")
        image = _decode(reference, NativeEffectImage)
        try:
            async with self.runtime._store(EFFECTS_STORE) as store:
                return await self._load_image(store, record, image)
        except _UNCERTAIN:
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original effect image could not be read") from None

    async def finish(self, claim: NativeEffectClaim, result: JsonValue) -> NativeEffectRecord:
        done = NativeEffectRecord.model_validate_json(
            claim.record.model_copy(update={"status": "done", "result": result}).model_dump_json())
        return (await self._write_claim(claim, done)).record

    async def once(self, call_id, material, operation) -> JsonValue:
        claim = await self.reserve(call_id, material)
        if not claim.fresh:
            if claim.record.status == "done":
                return claim.record.result
            raise Problem(503, "NATIVE_EFFECT_UNKNOWN", "the original effect is pending; never invoke it again")
        await self._active()
        result = await operation()
        return (await self.finish(claim, result)).result
