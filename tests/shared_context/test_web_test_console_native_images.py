"""Native 8765 image reads use the existing signed N2 owner route, never a fake legacy binding."""

import base64
import hashlib

import pytest

from shared_context.assistant_image_wire import ImageEntry
from shared_context.native_image_wire import NativeImageContent, NativeTurnImages
from test_assistant_images import png
from test_native_turn_api import instruction_models, jwk_client, native_api, signing_key, synthetic_standing_v1
from test_web_test_console_native import first, native_console, send


def images_for(c):
    draft = first(c)
    assert send(c, draft).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    data = png(7)
    digest = hashlib.sha256(data).hexdigest()
    entry = ImageEntry(version_id=digest[:32], number=1, sha256=digest, byte_length=len(data))
    scope = dict(agent_id=original.agent_id, conversation_id=original.conversation_id,
                 request_id=original.request_id, submit_sha256=original.submit_digest)
    content = NativeImageContent(**scope, entry=entry, png_base64=base64.b64encode(data).decode("ascii"))
    listing = NativeTurnImages(**scope, entries=(entry,))
    c.api.remote.reply_override = lambda command: listing if command.operation == "images" else content
    return original, content, data


def test_native_console_lists_and_downloads_only_original_png_bytes_without_post(native_console):
    c = native_console
    original, content, data = images_for(c)
    before = len(c.calls)
    base = "/api/instructions/" + original.request_id + "/images"
    listing = c.ui.client.get(base)
    assert listing.status_code == 200, listing.text
    assert listing.json()["entries"] == [content.entry.model_dump(mode="json")]
    image = c.ui.client.get(base + "/" + content.entry.version_id)
    assert image.status_code == 200 and image.content == data, image.text
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-sha256"] == content.entry.sha256
    assert image.headers["content-disposition"] == 'attachment; filename="fa-screenshot-1.png"'
    assert image.headers["cache-control"] == "no-store"
    assert all(method == "GET" and "/behavior/" not in path for method, path, _ in c.calls[before:])
    assert c.ui.client.get(base + "?source=another-agent").status_code == 403


@pytest.mark.parametrize("damage", ["source", "bytes"])
def test_native_console_never_serves_changed_image_provenance_or_bytes(native_console, damage):
    c = native_console
    original, content, _data = images_for(c)
    content = content.model_copy(update=(
        {"submit_sha256": "f" * 64} if damage == "source"
        else {"png_base64": base64.b64encode(png(6)).decode("ascii")}))
    c.api.remote.reply_override = lambda _command: content
    before = len(c.calls)
    response = c.ui.client.get(
        "/api/instructions/" + original.request_id + "/images/" + content.entry.version_id)
    assert response.status_code in {409, 502}
    assert response.headers["content-type"] != "image/png"
    assert all(method == "GET" for method, _path, _body in c.calls[before:])
