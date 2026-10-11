"""Owner-signed N2 screenshot reads; Foundry replies are explicit transport doubles."""

import base64
import hashlib

import pytest

from shared_context.assistant_image_wire import ImageEntry
from shared_context.behavior_native_turn_api import PREFIX
from shared_context.behavior_native_turn_wire import NativeReceipt
from shared_context.native_image_wire import NativeImageContent, NativeTurnImages
from test_assistant_images import png
from test_native_turn_api import instruction_models, jwk_client, native_api, signing_key, submit, synthetic_standing_v1


def image_case(case):
    response = submit(case)
    assert response.status_code == 202
    receipt = NativeReceipt.model_validate_json(response.content)
    scope = dict(agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
                 request_id=receipt.request_id, submit_sha256=receipt.submit_digest)
    data = png(5)
    digest = hashlib.sha256(data).hexdigest()
    entry = ImageEntry(version_id=digest[:32], number=1, sha256=digest, byte_length=len(data))
    content = NativeImageContent(**scope, entry=entry, png_base64=base64.b64encode(data).decode("ascii"))
    listing = NativeTurnImages(**scope, entries=(entry,))
    case.remote.reply_override = lambda command: listing if command.operation == "images" else content
    return receipt, data, content


def test_owner_reads_complete_native_png_without_job_or_new_submission(native_api):
    case = native_api
    original, data, content = image_case(case)
    base = PREFIX + f"/conversations/{original.conversation_id}/requests/{original.request_id}/images"
    before = len(case.remote.calls)
    listing = case.client.get(base, headers=case.headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["entries"] == [content.entry.model_dump(mode="json")]
    response = case.client.get(base + "/" + content.entry.version_id, headers=case.headers)
    assert response.status_code == 200, response.text
    assert NativeImageContent.model_validate_json(response.content).data == data
    assert response.headers["cache-control"] == "no-store"
    assert {command.operation for _, command in case.remote.calls[before:]} <= {"images", "image", "session_read"}
    assert case.client.get(base).status_code == 401
    assert case.client.get(base + "?agent_id=somebody-else", headers=case.headers).status_code == 422
    assert case.client.get(base + "/not-an-image-version", headers=case.headers).status_code == 422


@pytest.mark.parametrize("damage", ["request", "conversation", "agent", "version", "bytes"])
def test_native_image_read_rejects_a_different_or_corrupted_original(native_api, damage):
    case = native_api
    original, _data, content = image_case(case)
    if damage == "bytes":
        replacement = content.model_copy(update={"png_base64": base64.b64encode(png(6)).decode("ascii")})
    elif damage == "version":
        data = png(6)
        digest = hashlib.sha256(data).hexdigest()
        replacement = NativeImageContent(**content.model_dump(exclude={"entry", "png_base64"}),
            entry=ImageEntry(version_id=digest[:32], number=1, sha256=digest, byte_length=len(data)),
            png_base64=base64.b64encode(data).decode("ascii"))
    else:
        replacement = content.model_copy(update={damage + "_id": "different-original"})
    case.remote.reply_override = lambda _command: replacement
    path = PREFIX + f"/conversations/{original.conversation_id}/requests/{original.request_id}/images/{content.entry.version_id}"
    response = case.client.get(path, headers=case.headers)
    assert response.status_code in {409, 502}
    assert "png_base64" not in response.json()
