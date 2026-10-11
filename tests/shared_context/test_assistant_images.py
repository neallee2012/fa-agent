"""Screenshots delivered with a reply: immutable PNG versions read only through the original work's authority."""

import base64
import hashlib
import struct
import zlib
from datetime import datetime, timezone

import pytest

from shared_context.behavior_capabilities import CapabilityGrant
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_profile_selection import template
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import configured, open_turn


def png(color=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes([color, 0, 0]) * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def command(data):
    from shared_context.assistant_image_wire import DeliverImageCommand
    return DeliverImageCommand(request_id="deliver-image", sha256=hashlib.sha256(data).hexdigest(),
                               png_base64=base64.b64encode(data).decode("ascii"))


def test_image_capability_adds_only_its_tool_and_guidance():
    from shared_context.behavior_capabilities import CapabilityCapture, available_grants
    from shared_context.behavior_capability_prompt import assemble_capabilities, session_needs
    from shared_context.behavior_skill_prompt import assemble_prompt

    grant = available_grants(None, ("fa-image-delivery-v1",))["fa-image-delivery-v1"]
    assert grant.tools == ("deliver_image",) and grant.vision is False
    capture = CapabilityCapture(head_selection_id="selected-images", grants=(grant,))
    prompt = assemble_capabilities(HISTORY_PROFILE, capture)
    assert prompt.system_message.startswith(assemble_prompt(HISTORY_PROFILE).system_message)
    assert "call deliver_image" in prompt.system_message and "Never paste image data or links" in prompt.system_message
    assert session_needs(capture).custom_tools == ("deliver_image",)


def test_screenshots_the_model_gateway_refuses_are_replaced_not_sent():
    from shared_context.behavior_assistant_gateway import MAX_IMAGE_DATA_URL_CHARS
    from shared_context.m2_copilot import MODEL_IMAGE_URL_CHARS, _omit_unsendable_images

    assert MODEL_IMAGE_URL_CHARS == MAX_IMAGE_DATA_URL_CHARS
    small = "data:image/png;base64," + base64.b64encode(png()).decode()
    large = "data:image/png;base64," + "A" * MODEL_IMAGE_URL_CHARS
    body = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": small}},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}},
        {"type": "image_url", "image_url": {"url": large}},
    ]}]}
    assert _omit_unsendable_images(body) == 2
    parts = body["messages"][0]["content"]
    assert parts[0]["image_url"]["url"] == small
    assert [part["type"] for part in parts] == ["image_url", "text", "text"]


def test_only_a_complete_png_within_the_bound_is_a_delivery_command():
    from shared_context.assistant_image_wire import DeliverImageCommand, MAX_IMAGE_BYTES

    data = png()
    assert command(data).data == data
    for bad in (b"GIF89a" + data[6:], data[:8] + b"\x00" * 20):
        with pytest.raises(ValueError):
            command(bad)
    with pytest.raises(ValueError):
        DeliverImageCommand(request_id="deliver-image", sha256="0" * 64, png_base64=base64.b64encode(data).decode())
    with pytest.raises(ValueError):
        command(data + b"\x00" * (MAX_IMAGE_BYTES - len(data) + 1))


@pytest.fixture
def image_turn(assistant):
    from shared_context.assistant_images import IMAGE_CAPABILITY

    h, control, human = configured(assistant)
    builtin = IMAGE_CAPABILITY
    grant = CapabilityGrant(entry_id=builtin.entry_id, entry_sha256=builtin.entry_sha256,
                            kind="fa-builtin", tools=builtin.tools, vision=False)
    h.runtime.capability_grants = {builtin.entry_id: grant}
    select_profile(control, h.assistant, human, SelectConversationProfile(
        request_id="select-image-delivery", expected_selection_id=None,
        template=template(HISTORY_PROFILE, capabilities=(grant.ref,),
                          user_text_file_profile="fa-user-text-files-v1",
                          checkpoint_profile=control.policy.profile.checkpoint_profile,
                          sdk_start_profile=control.policy.profile.sdk_start_profile)))
    turn = open_turn(h, control, human, "image-delivery-turn")
    try:
        yield turn
    finally:
        turn.client.close()


def agent(turn):
    return turn.h.verifier(turn.h.client.build_request("GET", "/", headers=turn.h.headers("agent")))


def test_delivered_screenshots_are_immutable_versions_and_a_repeat_adds_no_copy(image_turn):
    from shared_context.assistant_images import deliver_image, image_bytes, turn_images

    c = image_turn
    first, second = png(1), png(2)
    receipt = deliver_image(c.h.assistant, c.session, command(first), agent(c))
    assert (receipt.number, receipt.sha256, receipt.byte_length) == (1, hashlib.sha256(first).hexdigest(), len(first))
    assert receipt.version_id == receipt.sha256[:32] and receipt.visible_to == "the-original-conversation-human"
    guard = c.h.store._guard()
    assert deliver_image(c.h.assistant, c.session, command(first), agent(c)) == receipt
    assert c.h.store._guard() == guard
    assert deliver_image(c.h.assistant, c.session, command(second), agent(c)).number == 2
    listing = turn_images(c.h.assistant, c.binding.binding_id, c.human)
    assert [(entry.number, entry.sha256) for entry in listing.entries] == [
        (1, hashlib.sha256(first).hexdigest()), (2, hashlib.sha256(second).hexdigest())]
    entry, data = image_bytes(c.h.assistant, c.binding.binding_id, receipt.version_id, c.human)
    assert data == first and entry.number == 1
    for color in (3, 4):
        deliver_image(c.h.assistant, c.session, command(png(color)), agent(c))
    with pytest.raises(Problem) as limited:
        deliver_image(c.h.assistant, c.session, command(png(5)), agent(c))
    assert limited.value.body["error"]["code"] == "IMAGE_DELIVERY_LIMIT"
    assert len(turn_images(c.h.assistant, c.binding.binding_id, c.human).entries) == 4


def test_delivery_needs_the_current_captured_capability_and_the_agent(image_turn):
    from shared_context.assistant_images import deliver_image, turn_images

    c = image_turn
    with pytest.raises(Problem) as human:
        deliver_image(c.h.assistant, c.session, command(png()), c.human)
    assert human.value.status == 403
    c.h.runtime.capability_grants = {}
    with pytest.raises(Problem) as removed:
        deliver_image(c.h.assistant, c.session, command(png()), agent(c))
    assert removed.value.body["error"]["code"] == "IMAGE_DELIVERY_DISABLED"
    assert turn_images(c.h.assistant, c.binding.binding_id, c.human).entries == ()


def test_unknown_upload_is_finished_from_its_stored_blob_without_resending(image_turn, monkeypatch):
    from shared_context.assistant_images import deliver_image

    c = image_turn
    data = png(7)
    original = c.h.assistant.artifacts._finish_upload
    monkeypatch.setattr(c.h.assistant.artifacts, "_finish_upload",
                        lambda *_: (_ for _ in ()).throw(TimeoutError("lost acknowledgement")))
    with pytest.raises(Problem) as unknown:
        deliver_image(c.h.assistant, c.session, command(data), agent(c))
    assert unknown.value.body["error"]["code"] == "IMAGE_DELIVERY_UNKNOWN"
    monkeypatch.setattr(c.h.assistant.artifacts, "_finish_upload", original)
    puts = []
    store_put = c.h.assistant.artifacts.store.put
    monkeypatch.setattr(c.h.assistant.artifacts.store, "put", lambda *a: puts.append(a) or store_put(*a))
    receipt = deliver_image(c.h.assistant, c.session, command(data), agent(c))
    assert receipt.number == 1 and receipt.sha256 == hashlib.sha256(data).hexdigest() and puts == []


def test_human_routes_read_png_through_current_read_only(image_turn):
    from shared_context.assistant_image_api import install_image_routes
    from shared_context.assistant_images import deliver_image

    c = image_turn
    install_image_routes(c.h.app, c.h.assistant, c.h.verifier)
    data = png(9)
    receipt = deliver_image(c.h.assistant, c.session, command(data), agent(c))
    path = "/behavior/v1/bindings/" + c.binding.binding_id + "/images"
    before = c.h.store._guard()
    listing = c.h.client.get(path, headers=c.h.headers("read-only"))
    assert listing.status_code == 200 and listing.json()["entries"][0]["sha256"] == receipt.sha256
    image = c.h.client.get(path + "/" + receipt.version_id, headers=c.h.headers("read-only"))
    assert image.status_code == 200 and image.content == data
    assert image.headers["content-type"] == "image/png" and image.headers["cache-control"] == "no-store"
    assert image.headers["x-content-type-options"] == "nosniff"
    for identity in ("agent", "old-m3"):
        assert c.h.client.get(path, headers=c.h.headers(identity)).status_code == 403
    assert c.h.client.get(path + "?url=true", headers=c.h.headers()).status_code == 422
    assert c.h.client.get(path + "/" + "0" * 32, headers=c.h.headers()).status_code == 404
    assert c.h.store._guard() == before


def _read_bytes(monkeypatch, call):
    from shared_context import behavior_store

    total = [0]
    original = behavior_store.BehaviorStore._read

    def counted(self, key, model, fetched=behavior_store._NOT_FETCHED):
        view = self._read_view()
        fresh = view is not None and not view.verifying and key not in view.values
        value = original(self, key, model, fetched)
        if fresh:
            total[0] += len(key.encode()) + (0 if value is None else len(behavior_store._json(value[0])))
        return value

    with monkeypatch.context() as patch:
        patch.setattr(behavior_store.BehaviorStore, "_read", counted)
        response = call()
    return response, total[0]


def test_follow_up_progress_after_a_screenshot_stays_readable_as_other_work_grows(image_turn, monkeypatch):
    from shared_context.assistant_images import deliver_image
    from shared_context.behavior_assistant_wire import PublicProgress, PublicProgressEvent
    from test_sdk_compaction_product import open_turn, ordinary, settle

    c = image_turn
    deliver_image(c.h.assistant, c.session, command(png(1)), agent(c))
    settle(c, ordinary(c.assignment.sdk_session_id))
    cid = c.assignment.task.instruction.conversation_id
    follow = open_turn(c.h, c.control, c.human, "image-follow-up", cid)
    others = []
    try:
        follow.client.progress(PublicProgress(events=(PublicProgressEvent(
            seq=1, at=datetime(2026, 1, 1, tzinfo=timezone.utc), kind="stage", status="started",
            summary="Preparing reply"),)))
        root = "/work-policy/v1/"
        conversation = root + "conversations/" + cid

        def http(path):
            def read():
                response = c.h.client.get(path, headers=c.h.headers())
                return response.status_code, response.json().get("error", {}).get("code")
            return read

        def environment():
            # The live route reads the preview inside this same pure read with the wired checkpoint reader.
            from shared_context.behavior_conversation_transfer import preview
            assert c.h.assistant.runtime is c.control.runtime
            try:
                c.h.store.consistent_read(lambda: preview(c.control, c.h.assistant, c.human, cid),
                                          max_keys=None, row_scoped=True)
            except Problem as error:
                return error.status, error.body["error"]["code"]
            return 200, None

        reads = {"conversation": http(conversation), "models": http(root + "models"),
                 "selection": http(root + "agent-template/selection"), "environment": environment}
        used = {}
        for name, read in reads.items():
            outcome, used[name] = _read_bytes(monkeypatch, read)
            assert outcome[0] != 413, (name, outcome)
        assert reads["environment"]() == (409, "CONVERSATION_TRANSFER_NOT_READY")
        # Every turn the agent ever ran elsewhere must not count against these pure-read bounds.
        others = [open_turn(c.h, c.control, c.human, f"other-work-{n}") for n in range(3)]
        full = c.h.store._scope
        limited = []
        for name, read in reads.items():
            c.h.store._scope = full.model_copy(update={"max_batch_bytes": used[name] + 16384})
            if read()[0] == 413:
                limited.append(name)
        assert limited == []
        c.h.store._scope = full.model_copy(update={"max_batch_bytes": used["conversation"] + 16384})
        response = c.h.client.get(conversation, headers=c.h.headers())
        assert response.status_code == 200, response.text
        turns = response.json()["turns"]
        assert turns[0]["state"] == "succeeded" and turns[1]["state"] in {"starting", "running"}
        assert turns[1]["progress"]["events"][0]["summary"] == "Preparing reply"
        assert base64.b64encode(png(1)).decode() not in response.text
    finally:
        for turn in (follow, *others):
            turn.client.close()
