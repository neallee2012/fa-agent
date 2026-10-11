"""Real screenshot delivery route behind the Hosted factory, with no SDK process or live service."""

import asyncio
import base64
import hashlib
from types import SimpleNamespace

import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context.assistant_image_capability import ENTRY_ID, image_delivery_capability
from shared_context.assistant_image_wire import ImageReceipt
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure
from test_assistant_images import image_turn, png
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


@pytest.mark.parametrize("lose_response", [False, True])
def test_factory_delivers_the_latest_screenshot_once_and_types_the_receipt(image_turn, monkeypatch, lose_response):
    from shared_context.assistant_images import turn_images

    c = image_turn
    calls = []
    original = c.client._post

    def post(action, command, model, **options):
        calls.append(action)
        result = original(action, command, model, **options)
        if lose_response and len(calls) == 1:
            raise Problem(503, "ASSISTANT_API_UNKNOWN", "synthetic response loss after the original commit")
        return result

    monkeypatch.setattr(c.client, "_post", post)
    session = SimpleNamespace(client=c.client, _authority_lock=asyncio.Lock(), latest_image=None)
    grant = c.assignment.task.instruction.capabilities.grants[0]
    assert hosted.CAPABILITY_HANDLERS[ENTRY_ID] is image_delivery_capability
    specs, models = image_delivery_capability(session, grant)
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results=models)
    assert tuple(spec.name for spec in specs) == ("deliver_image",)
    data = png(3)

    async def run():
        with pytest.raises(ToolFailure) as missing:
            await specs[0].handler({"arguments": {}})
        assert missing.value.public_code == "IMAGE_DELIVERY_NO_IMAGE"
        assert boundary.public_tool_failure(specs[0].name, missing.value.public_code)
        session.latest_image = base64.b64encode(data).decode()
        with pytest.raises(ToolFailure) as extra:
            await specs[0].handler({"arguments": {"url": "https://example.com"}})
        assert extra.value.public_code == "CAPABILITY_ARGUMENTS"
        if lose_response:
            with pytest.raises(ToolFailure) as unknown:
                await specs[0].handler({"arguments": {}})
            assert unknown.value.public_code == "CAPABILITY_UNAVAILABLE"
        first = await specs[0].handler({"arguments": {}})
        second = await specs[0].handler({"arguments": {}})
        assert second == first
        receipt = ImageReceipt.model_validate_json(boundary.tool_result(specs[0].name, first.text))
        assert receipt.sha256 == hashlib.sha256(data).hexdigest() and receipt.number == 1
        session.latest_image = base64.b64encode(b"not a png at all, just text").decode()
        with pytest.raises(ToolFailure) as invalid:
            await specs[0].handler({"arguments": {}})
        assert invalid.value.public_code == "IMAGE_DELIVERY_INVALID"

    asyncio.run(run())
    assert set(calls) == {"deliver-image"}
    assert [entry.sha256 for entry in turn_images(c.h.assistant, c.binding.binding_id, c.human).entries] == [
        hashlib.sha256(data).hexdigest()]


def test_imagemagick_gets_no_hosted_secret_from_the_environment(monkeypatch, tmp_path):
    """A resized screenshot's convert is dumpable after exec; a same-UID shell command could read its environ."""
    from shared_context import assistant_image_capability as capability
    monkeypatch.setenv("IDENTITY_HEADER", "fa-fit-canary")
    seen = []

    def run(args, **options):
        seen.append(options["env"])
        return SimpleNamespace(stdout=png(1))
    monkeypatch.setattr(capability, "os", SimpleNamespace(name="posix", environ=capability.os.environ))
    monkeypatch.setattr(capability.shutil, "which", lambda name: "/usr/bin/convert")
    monkeypatch.setattr(capability.subprocess, "run", run)
    capability._fit(png(3) + b"\0" * capability.MAX_IMAGE_BYTES, tmp_path / "image-tmp")
    assert seen and all("fa-fit-canary" not in env.values() and "IDENTITY_HEADER" not in env for env in seen)
    assert set(seen[0]) == {"PATH", "LANG", "HOME", "TMPDIR", "MAGICK_TEMPORARY_PATH"}
