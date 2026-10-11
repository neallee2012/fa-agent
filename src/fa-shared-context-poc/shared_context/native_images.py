"""Original RID screenshots: immutable PNG items and one small, append-only image list in its own store."""

import asyncio
import base64
import hashlib
import logging
import subprocess
from typing import Literal

from .assistant_image_capability import _fit
from .assistant_image_wire import (
    ENTRY_ID, IMAGE_DELIVERY_GUIDANCE, MAX_TURN_IMAGES, TOOL_NAME, DeliverImageArguments,
    DeliverImageCommand, ImageEntry,
)
from .behavior_capabilities import BUILTIN_CAPABILITIES
from .behavior_native_turn_wire import material_digest
from .errors import Problem
from .m2_copilot import ToolFailure, ToolOutput, ToolSpec
from .native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
from .native_checkpoint import conversation_store_name
from .native_image_wire import (
    NativeImageContent, NativeImageRead, NativeImageReceipt, NativeImageScope, NativeImagesRead, NativeTurnImages,
)
from .native_turns import _CONFLICT, _UNCERTAIN, _decode, _json

log = logging.getLogger(__name__)


class _ImageRecord(NativeImageScope):
    kind: Literal["fa-native-image-bytes-v1"] = "fa-native-image-bytes-v1"
    image: DeliverImageCommand


def _scope(receipt):
    return dict(agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
                request_id=receipt.request_id, submit_sha256=receipt.submit_digest)


def image_key(rid, sha256):
    return "img:" + material_digest({"kind": "native-image", "rid": rid, "sha256": sha256})


def images_key(rid):
    return "images:" + hashlib.sha256(rid.encode("utf-8")).hexdigest()


def _unknown():
    return Problem(503, "IMAGE_DELIVERY_UNKNOWN", "the original screenshot is unconfirmed; do not claim it was delivered")


def _checked(item, model, receipt):
    if item is None:
        raise _unknown()
    try:
        value = _decode(item.value, model)
    except (Problem, ValueError, TypeError):
        raise Problem(409, "IMAGE_DELIVERY_CONFLICT", "the original screenshot record is invalid") from None
    if any(getattr(value, key) != expected for key, expected in _scope(receipt).items()):
        raise Problem(409, "IMAGE_DELIVERY_CONFLICT", "the screenshot belongs to another original native turn")
    return value


def _listing(item, receipt):
    return NativeTurnImages(**_scope(receipt)) if item is None else _checked(item, NativeTurnImages, receipt)


def _entry(listing, sha256):
    entry = next((item for item in listing.entries if item.version_id == sha256[:32]), None)
    if entry is not None and entry.sha256 != sha256:
        raise Problem(409, "IMAGE_DELIVERY_CONFLICT", "the screenshot version has different complete bytes")
    return entry


async def _content(store, receipt, entry):
    item = await store.get_item(image_key(receipt.request_id, entry.sha256))
    record = _checked(item, _ImageRecord, receipt)
    try:
        return NativeImageContent(**_scope(receipt), entry=entry, png_base64=record.image.png_base64)
    except ValueError:
        raise Problem(409, "IMAGE_DELIVERY_CONFLICT", "the PNG differs from its original image list") from None


async def deliver(context, image):
    runtime, receipt = context.runtime, context.receipt
    if runtime is None or receipt.agent_id != runtime.identity.agent_id or not await context.current():
        raise Problem(403, "IMAGE_DELIVERY_DISABLED", "the original native turn no longer owns this screenshot")
    record = _ImageRecord(**_scope(receipt), image=image)
    key, list_key = image_key(receipt.request_id, image.sha256), images_key(receipt.request_id)
    async with runtime._store(conversation_store_name(receipt.conversation_id)) as store:
        item = await store.get_item(list_key)
        listing = _listing(item, receipt)
        entry = _entry(listing, image.sha256)
        if entry is not None:
            await _content(store, receipt, entry)
            return NativeImageReceipt(**_scope(receipt), **entry.model_dump())
        if len(listing.entries) == MAX_TURN_IMAGES:
            raise Problem(409, "IMAGE_DELIVERY_LIMIT", "the original reply already contains four screenshots")
        saved = await store.get_item(key)
        if saved is None:
            try:
                await store.create_item(key, _json(record))
            except _UNCERTAIN as error:
                log.warning("NATIVE_IMAGE_WRITE_UNCONFIRMED exception_type=%s", type(error).__name__)
            saved = await store.get_item(key)
        if _checked(saved, _ImageRecord, receipt) != record:
            raise Problem(409, "IMAGE_DELIVERY_CONFLICT", "the original screenshot bytes cannot be replaced")
        for _ in range(MAX_TURN_IMAGES + 1):
            listing = _listing(item, receipt)
            entry = _entry(listing, image.sha256)
            if entry is not None:
                return NativeImageReceipt(**_scope(receipt), **entry.model_dump())
            if len(listing.entries) == MAX_TURN_IMAGES:
                raise Problem(409, "IMAGE_DELIVERY_LIMIT", "the original reply already contains four screenshots")
            if not await context.current():
                raise Problem(403, "IMAGE_DELIVERY_DISABLED", "the original turn stopped before publishing its screenshot")
            entry = ImageEntry(version_id=image.sha256[:32], number=len(listing.entries) + 1,
                               sha256=image.sha256, byte_length=len(image.data))
            updated = NativeTurnImages(**_scope(receipt), entries=(*listing.entries, entry))
            conflict = False
            try:
                if item is None:
                    await store.create_item(list_key, _json(updated))
                else:
                    if not item.etag:
                        raise Problem(409, "IMAGE_DELIVERY_CONFLICT", "the original screenshot list has no ETag")
                    await store.set_item(list_key, _json(updated), if_match=item.etag)
            except _CONFLICT:
                conflict = True
            except _UNCERTAIN as error:
                log.warning("NATIVE_IMAGE_LIST_UNCONFIRMED exception_type=%s", type(error).__name__)
            item = await store.get_item(list_key)
            confirmed = _listing(item, receipt)
            entry = _entry(confirmed, image.sha256)
            if entry is not None:
                return NativeImageReceipt(**_scope(receipt), **entry.model_dump())
            if not conflict:
                raise _unknown()
        raise Problem(409, "IMAGE_DELIVERY_CONFLICT", "the original screenshot list changed concurrently")


async def _original(runtime, command):
    receipt = await runtime.read(command.request_id)
    if receipt.agent_id != runtime.identity.agent_id or receipt.conversation_id != command.conversation_id:
        raise Problem(403, "NATIVE_STATE_SCOPE", "screenshots belong to the original Agent and conversation")
    return receipt


async def read_images(runtime, command: NativeImagesRead):
    receipt = await _original(runtime, command)
    async with runtime._store(conversation_store_name(receipt.conversation_id)) as store:
        return _listing(await store.get_item(images_key(receipt.request_id)), receipt)


async def read_image(runtime, command: NativeImageRead):
    receipt = await _original(runtime, command)
    async with runtime._store(conversation_store_name(receipt.conversation_id)) as store:
        listing = _listing(await store.get_item(images_key(receipt.request_id)), receipt)
        entry = next((item for item in listing.entries if item.version_id == command.version_id), None)
        if entry is None:
            raise Problem(404, "IMAGE_DELIVERY_NO_IMAGE", "this original reply has no confirmed screenshot with that version")
        return await _content(store, receipt, entry)


def image_delivery_provider():
    builtin = BUILTIN_CAPABILITIES[ENTRY_ID]

    def accepts(grant):
        return (grant.entry_id == ENTRY_ID and grant.kind == "fa-builtin" and grant.entry_sha256 == builtin.entry_sha256
                and grant.tools == builtin.tools and not grant.vision)

    async def bind(context, grants):
        async def handle(packet):
            try:
                DeliverImageArguments.model_validate(packet["arguments"])
            except (KeyError, ValueError, TypeError):
                raise ToolFailure("deliver_image takes no arguments.", public_code="CAPABILITY_ARGUMENTS") from None
            encoded = context.latest_image
            if encoded is None:
                raise ToolFailure(IMAGE_DELIVERY_GUIDANCE["IMAGE_DELIVERY_NO_IMAGE"], public_code="IMAGE_DELIVERY_NO_IMAGE")
            try:
                data = await asyncio.to_thread(_fit, base64.b64decode(encoded, validate=True), context.root / "image-tmp")
                image = DeliverImageCommand(request_id="deliver-image", sha256=hashlib.sha256(data).hexdigest(),
                                            png_base64=base64.b64encode(data).decode("ascii"))
            except (ValueError, OSError, subprocess.SubprocessError):
                raise ToolFailure(IMAGE_DELIVERY_GUIDANCE["IMAGE_DELIVERY_INVALID"], public_code="IMAGE_DELIVERY_INVALID") from None
            try:
                result = await deliver(context, image)
            except Problem as error:
                code = error.body["error"]["code"]
                code = code if code in IMAGE_DELIVERY_GUIDANCE else "IMAGE_DELIVERY_UNKNOWN"
                raise ToolFailure(IMAGE_DELIVERY_GUIDANCE[code], public_code=code) from None
            return ToolOutput(result.model_dump_json())

        tool = ToolSpec(
            TOOL_NAME,
            "Save this turn's latest browser or desktop screenshot for the original conversation's owner. "
            "No arguments or public link. The same PNG never adds another copy; only a confirmed receipt means it was saved.",
            DeliverImageArguments.model_json_schema(), handle)
        return NativeCapabilityBinding(tools=(tool,), result_schemas={TOOL_NAME: NativeImageReceipt})

    return NativeCapabilityProvider(accepts=accepts, bind=bind, name="image_delivery")
