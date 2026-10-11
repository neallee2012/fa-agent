"""Complete large HITL commands in the existing Table EGT, not in a generic Team writer."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field

from .behavior_operations import _parse, _write
from .behavior_store import ControlWrite, InlineMaterial, MAX_INLINE_BYTES
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .native_hitl_wire import NativeAttentionItem, NativeHitlApprovalReference, NativeHitlPending, hitl_key
from .source_materials import canonical


class HitlMaterialPart(WireModel):
    kind: Literal["native_hitl_material_part"] = "native_hitl_material_part"
    tenant_id: Identifier
    team_id: Identifier
    parent_id: Digest
    index: Annotated[int, Field(ge=0, le=15)]


class HitlPackedRecord(WireModel):
    kind: Literal["native_hitl_packed_record"] = "native_hitl_packed_record"
    tenant_id: Identifier
    team_id: Identifier
    record_id: Digest
    record_kind: Literal["native_hitl_intent", "native_hitl_pending"]
    sha256: Digest
    byte_length: Annotated[int, Field(ge=1, le=512 * 1024)]
    parts: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=16)]
    pending: NativeAttentionItem | None = None
    agent_oid: Identifier | None = None


def pending_summary(value: NativeHitlPending) -> NativeAttentionItem:
    return NativeAttentionItem(
        item_id=value.pending_id,
        approval=NativeHitlApprovalReference(
            pending_id=value.pending_id, pending_digest=value.sha256, call=value.command.action.call),
        created_at=value.created_at, expires_at=value.expires_at)


def material_writes(key, value):
    raw = canonical(value.model_dump(mode="json"))
    if len(raw) <= MAX_INLINE_BYTES:
        return (_write(key, value, "create"),)
    chunks = [raw[start:start + MAX_INLINE_BYTES] for start in range(0, len(raw), MAX_INLINE_BYTES)]
    scope = dict(tenant_id=value.tenant_id, team_id=value.team_id)
    parts = tuple(hitl_key("material", value.tenant_id, value.team_id, key, str(index)) for index in range(len(chunks)))
    packed = HitlPackedRecord(
        **scope, record_id=key, record_kind=value.kind, sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw), parts=parts, pending=pending_summary(value) if isinstance(value, NativeHitlPending) else None,
        agent_oid=value.agent_oid if isinstance(value, NativeHitlPending) else None)
    writes = tuple(ControlWrite(
        row_id=part, mode="create",
        data=HitlMaterialPart(**scope, parent_id=key, index=index).model_dump(mode="json"),
        material=InlineMaterial(data=chunk, sha256=hashlib.sha256(chunk).hexdigest()))
        for index, (part, chunk) in enumerate(zip(parts, chunks)))
    return (_write(key, packed, "create"), *writes)


def unpack(record, key, reader):
    data = record.payload["data"]
    if data.get("kind") != "native_hitl_packed_record":
        return data
    packed = _parse(data, HitlPackedRecord)
    if packed.record_id != key or len(packed.parts) != (packed.byte_length + MAX_INLINE_BYTES - 1) // MAX_INLINE_BYTES:
        raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original material reference differs")
    chunks = []
    for index, part_id in enumerate(packed.parts):
        if part_id != hitl_key("material", packed.tenant_id, packed.team_id, key, str(index)):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the material part belongs to another original record")
        row = reader(part_id)
        if row is None:
            raise Problem(503, "NATIVE_HITL_MATERIAL_UNAVAILABLE", "the complete original approval material is unavailable")
        part = _parse(row.payload["data"], HitlMaterialPart)
        material = _parse(row.payload["material"], InlineMaterial)
        if (part.tenant_id, part.team_id, part.parent_id, part.index) != (
                packed.tenant_id, packed.team_id, key, index):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the material part identity differs")
        if len(material.data) != min(MAX_INLINE_BYTES, packed.byte_length - index * MAX_INLINE_BYTES):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the material part length differs")
        chunks.append(material.data)
    raw = b"".join(chunks)
    if len(raw) != packed.byte_length or hashlib.sha256(raw).hexdigest() != packed.sha256:
        raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the complete original approval material differs")
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError):
        raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original approval material is not valid JSON") from None
    if (value.get("kind"), value.get("tenant_id"), value.get("team_id")) != (
            packed.record_kind, packed.tenant_id, packed.team_id):
        raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original material scope differs")
    if packed.record_kind == "native_hitl_pending":
        pending = _parse(value, NativeHitlPending)
        if packed.pending != pending_summary(pending) or packed.agent_oid != pending.agent_oid:
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the approval projection differs from the original material")
    elif packed.pending is not None or packed.agent_oid is not None:
        raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "an intent cannot be projected as an approval")
    return value
