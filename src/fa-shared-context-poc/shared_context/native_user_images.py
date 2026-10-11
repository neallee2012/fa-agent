"""Hosted side of user-attached images (B1 PA-08): one immutable item per PNG in this Agent's State store.

Upload is content-addressed and create-only, so a repeated or concurrent upload of the same bytes is the same
item and a lost reply is resolved by uploading again. A new turn is accepted only when every referenced image is
present and the captured model reads images; the run loads the same bytes for the SDK. Items are never deleted
here, so an image that later leaves the model window can still be read back in its original form.
"""

from typing import Literal

from .backup_models import MAX_BACKUP_BYTES
from .behavior_assistant_wire import InstructionSubmit
from .behavior_sdk_files import LARGE_RESULT_RESERVE_BYTES
from .behavior_user_images import NativeUserImageStored, NativeUserImageUpload, UserImage, canonical_png, stored
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .native_turns import _decode

USER_IMAGES_STORE = "fa/v1/user-images"


def user_image_key(sha256: str) -> str:
    return "uimg:" + sha256


class _UserImageRecord(WireModel):
    kind: Literal["fa-native-user-image-bytes-v1"] = "fa-native-user-image-bytes-v1"
    agent_id: Identifier
    sha256: Digest
    png_base64: str


async def upload(runtime, command: NativeUserImageUpload) -> NativeUserImageStored:
    command = NativeUserImageUpload.model_validate_json(command.model_dump_json())
    receipt = stored(runtime.identity.agent_id, command.png_base64)
    record = _UserImageRecord(agent_id=receipt.agent_id, sha256=receipt.sha256, png_base64=command.png_base64)
    # The first upload creates this Agent's image store (official get_or_create); reads never create it.
    async with runtime._store(USER_IMAGES_STORE, create=True) as store:
        await runtime._immutable(store, user_image_key(receipt.sha256), record)
    return receipt


def _images(command) -> tuple[UserImage, ...]:
    return command.images if isinstance(command, InstructionSubmit) else ()


async def load_user_images(runtime, agent_id: str, images) -> tuple[tuple[UserImage, str], ...]:
    """The exact PNGs the original instruction names; a missing or different item refuses the turn."""
    if not images:
        return ()
    loaded = []
    try:
        async with runtime._store(USER_IMAGES_STORE) as store:
            for image in images:
                item = await store.get_item(user_image_key(image.sha256))
                if item is None:
                    raise Problem(409, "NATIVE_IMAGE_MISSING", "an attached image was not uploaded to this Agent")
                try:
                    record = _decode(item.value, _UserImageRecord)
                    data = canonical_png(record.png_base64)
                except (Problem, ValueError, TypeError):
                    raise Problem(409, "NATIVE_IMAGE_INVALID", "an attached image item is invalid") from None
                if (record.agent_id, record.sha256, len(data)) != (agent_id, image.sha256, image.byte_length):
                    raise Problem(409, "NATIVE_IMAGE_INVALID", "an attached image differs from its original reference")
                loaded.append((image, record.png_base64))
    except Problem as error:
        # No image was ever uploaded to this Agent, so its store does not exist yet.
        if error.body["error"]["code"] == "NATIVE_STATE_STORE_MISSING":
            raise Problem(409, "NATIVE_IMAGE_MISSING", "an attached image was not uploaded to this Agent") from None
        raise
    return tuple(loaded)


def require_state_room(state_bytes: int, encoded_lengths) -> None:
    """The SDK keeps each attached image's base64 in the saved conversation (bounded like screenshots); refuse a
    turn whose images and one reply reserve would not fit, instead of losing a paid reply when the state is saved."""
    if state_bytes + sum(encoded_lengths) + LARGE_RESULT_RESERVE_BYTES > MAX_BACKUP_BYTES:
        raise Problem(409, "NATIVE_IMAGE_STATE_FULL",
                      "this conversation has no room for these images; start a new conversation")


async def _conversation_state_bytes(runtime, conversation_id) -> int:
    """The latest checkpoint's encoded length (never below the SDK file bytes it restores); read-only."""
    if conversation_id is None:
        return 0
    from .native_checkpoint import conversation_store_name
    meta, _ = await runtime._meta(conversation_id)
    checkpoint = meta.base_ckpt
    if meta.rid is not None:
        previous = (await runtime._rid(meta.rid)).value.receipt
        if previous.status == "completed":
            async with runtime._store(conversation_store_name(conversation_id)) as store:
                checkpoint = (await runtime._turn(store, previous)).checkpoints[0]
    return 0 if checkpoint is None else checkpoint.byte_length


async def check_user_images(runtime, request) -> None:
    """Before a new RID is accepted: a model that reads images, their uploaded bytes and room in the conversation."""
    images = _images(request.command)
    if not images:
        return
    if request.capture.model.supports_image_input is not True:
        raise Problem(422, "NATIVE_IMAGE_UNSUPPORTED", "the selected model does not read attached images")
    loaded = await load_user_images(runtime, runtime.identity.agent_id, images)
    require_state_room(await _conversation_state_bytes(runtime, request.command.conversation_id),
                       (len(data) for _, data in loaded))


async def turn_user_images(runtime, receipt) -> tuple[tuple[UserImage, str], ...]:
    return await load_user_images(runtime, receipt.agent_id, _images(receipt.command))
