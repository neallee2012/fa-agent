"""Signed model egress with image history and concurrent registry reads; no model or SDK process."""

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, get_ident
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant_api import create_assistant_app, TICKET_HEADER
from shared_context.behavior_assistant_server import _CurrentAssistantProvider
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_instruction import conversation_id
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_work_policy import WorkRegistration
from shared_context.errors import Problem
from shared_context.source_materials import digest
from shared_context.tool_catalog import build_catalog
from test_assistant_images import png
from test_conversation_profile_selection import template
from test_instruction_flow import instruction_models
from test_instruction_responses import responses_usage
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn
from assistant_sdk_fixture import sdk_assistant
from test_work_policy_lifecycle import enabled


@pytest.mark.parametrize("writer,images,model_provider", [
    ("true", True, None), ("true", False, None), ("false", True, None), ("false", False, None),
    ("true", True, "openai-responses"),
], ids=["paged-image", "paged-text", "legacy-image", "legacy-text", "responses-paged-image"])
def test_third_image_model_keeps_its_binding_during_conversation_registry_refresh(
    sdk_assistant, monkeypatch, images, writer, model_provider,
):
    h, control, human, _ = enabled(sdk_assistant)
    monkeypatch.setenv("FA_WORK_REGISTRY_WRITER", writer)
    h.runtime.capability_catalog = build_catalog([])
    grants = available_grants(h.runtime.capability_catalog, ("fa-desktop-v1",))
    h.runtime.capability_grants = grants
    selected = control.models.models["gpt-4.1-mini"]
    control.models.models[selected.id] = selected.model_copy(update={"supports_image_input": True})
    select_profile(control, h.assistant, human, SelectConversationProfile(
        request_id="select-desktop", expected_selection_id=None,
        template=template(HISTORY_PROFILE, user_text_file_profile="fa-user-text-files-v1",
                          capabilities=(grants["fa-desktop-v1"].ref,), model_provider=model_provider)))
    sent, token_reads, overlap, source_reads = [], [], [], []
    entered, release = Event(), Event()
    reader_thread = [None]
    original_refresh, original_sources = h.runtime.refresh_bindings, h.assistant.model_sources

    def credential(_scope):
        token_reads.append(True)
        return SimpleNamespace(token="OFFLINE-provider-token")

    def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=responses_usage() if model_provider else (
            b'data: {"choices":[{"delta":{"content":"step"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
            b'data: [DONE]\n\n'), headers={"content-type": "text/event-stream"})

    provider = _CurrentAssistantProvider(h.parent, SimpleNamespace(get_token=credential),
                                         transport=httpx.MockTransport(transport))
    app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    install_work_policy_routes(app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    old_client = h.client
    h.client = TestClient(app)
    turn = open_turn(h, control, human, "desktop-original-third-model")
    cid = turn.assignment.task.instruction.conversation_id

    def refresh(binding_id=None):
        original_refresh(binding_id)
        if get_ident() == reader_thread[0] and not entered.is_set():
            entered.set()
            assert release.wait(10)

    def read_conversation():
        reader_thread[0] = get_ident()
        return control.conversation(human, cid)

    def sources(session, principal):
        # The third model step's source authority read now happens at its reserve, not after consume.
        source_reads.append(True)
        if len(source_reads) != 3 or overlap:
            return original_sources(session, principal)
        with ThreadPoolExecutor(max_workers=1) as readers:
            reading = readers.submit(read_conversation)
            try:
                assert entered.wait(10)
                overlap.append({
                    "binding_present": session.binding_id in h.runtime.bindings,
                    "profile_present": session.binding_id in h.runtime.profiles,
                })
                try:
                    return original_sources(session, principal)
                except KeyError as error:
                    overlap[-1]["missing_original_binding"] = error.args == (session.binding_id,)
                    raise
            finally:
                release.set()
                reading.result(timeout=10)

    monkeypatch.setattr(h.runtime, "refresh_bindings", refresh)
    monkeypatch.setattr(h.assistant, "model_sources", sources)
    messages = [{"role": "user", "content": "Observe the original desktop, then continue."}]
    headers = {**h.headers("agent"), TICKET_HEADER: turn.client.ticket.get_secret_value()}
    root = f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/model"
    try:
        for number in range(1, 4):
            if number == 2:
                image = "data:image/png;base64," + base64.b64encode(png()).decode("ascii")
                if model_provider:
                    messages.extend([
                        {"type": "function_call", "call_id": "original-desktop-observe",
                         "name": "desktop_observe", "arguments": "{}"},
                        {"type": "function_call_output", "call_id": "original-desktop-observe", "output": [
                            {"type": "input_text", "text": "Original desktop observation."},
                            {"type": "input_image", "image_url": image},
                        ]},
                    ])
                else:
                    messages.extend([
                        {"role": "assistant", "tool_calls": [{"id": "original-desktop-observe", "type": "function",
                            "function": {"name": "desktop_observe", "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": "original-desktop-observe", "content": [
                            {"type": "text", "text": "Original desktop observation."},
                            *([{"type": "image_url", "image_url": {"url": image}}] if images else []),
                        ]},
                    ])
            elif number == 3:
                messages.append({"role": "assistant", "content": "Continuing the same original work."})
            body = {"model": "gpt-4.1-mini", "stream": True,
                    **({"input": messages, "store": False} if model_provider else {"messages": messages})}
            identifier = digest(body)
            turn.client.reserve_model(identifier)
            response = h.client.post(root, json=aw.ModelRequest(request_id=identifier, body=body).model_dump(mode="json"),
                                     headers=headers)
            assert response.status_code == 200, (number, response.text, overlap)
        assert overlap == [{"binding_present": True, "profile_present": True}]
        assert len(sent) == 3 and len(token_reads) == 3
        control.revoke(human, "revoke-original-after-refresh")
        refused = h.client.post(root, json=aw.ModelRequest(request_id=identifier, body=body).model_dump(mode="json"),
                                headers=headers)
        assert refused.status_code == 403 and len(sent) == 3
        assert h.sdk.starts == []
    finally:
        release.set()
        turn.client.close()
        import asyncio
        asyncio.run(provider.aclose())
        h.client.close()
        h.client = old_client


def test_channel_refresh_does_not_restore_work_removed_from_the_standing_registry(sdk_assistant, monkeypatch):
    # Admission contract since 7c66d6a9 (#264): removing a registry row keeps the turn it already admitted until the
    # next admission, which reads the registry again and refuses a follow-up whose prior turn is no longer listed.
    monkeypatch.setenv("FA_WORK_REGISTRY_WRITER", "false")
    h, control, human, _ = enabled(sdk_assistant)
    first = aw.InstructionSubmit(request_id="removed-original-binding", content="Keep only current registered work.")
    binding, _ = control.submit(human, first)
    assert binding.binding_id in control.records
    h.mutate(control.registry_key, entries=[])
    control.refresh()
    h.runtime.refresh_bindings()
    assert control.records[binding.binding_id].turn_authorization is not None
    assert binding.binding_id in h.runtime.bindings
    kept = set(control.records)
    with pytest.raises(Problem) as denied:
        control.submit(human, aw.InstructionSubmit(
            request_id="after-removal-follow-up", content="Continue.",
            conversation_id=conversation_id(control.policy, first)))
    assert (denied.value.status, denied.value.body["error"]["code"]) == (403, "BEHAVIOR_BINDING_DENIED")
    assert set(control.records) == kept
    assert h.sdk.starts == []


def test_registration_added_during_channel_publication_uses_one_work_snapshot(sdk_assistant, monkeypatch):
    from shared_context import behavior_work_registry

    h, control, human, _ = enabled(sdk_assistant)
    first, _ = control.submit(human, aw.InstructionSubmit(request_id="prior-to-publication", content="Original work."))
    command = aw.InstructionSubmit(request_id="during-publication", content="Another original work.")
    committed, continue_registration, added = Event(), Event(), Event()
    publishing_thread = get_ident()
    writer_thread, new_binding = [None], [None]
    interrupted = []
    original_transact = behavior_work_registry.transact
    original_refresh = control.refresh
    original_attribute = WorkRegistration.__getattribute__

    def transact(*args, **kwargs):
        receipt = original_transact(*args, **kwargs)
        if get_ident() == writer_thread[0]:
            new_binding[0] = receipt.payload["result"]["binding_id"]
            committed.set()
            assert continue_registration.wait(10)
        return receipt

    def refresh(binding_id=None):
        if get_ident() == writer_thread[0] and new_binding[0] in control.records:
            added.set()
        return original_refresh(binding_id)

    def attribute(record, name):
        if (name == "binding" and get_ident() == publishing_thread and committed.is_set() and not interrupted
                and original_attribute(record, "request_id") == "prior-to-publication"):
            interrupted.append(True)
            continue_registration.set()
            assert added.wait(10)
        return original_attribute(record, name)

    def register():
        writer_thread[0] = get_ident()
        return control.register(human, command.request_id,
            hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)

    monkeypatch.setattr(behavior_work_registry, "transact", transact)
    monkeypatch.setattr(control, "refresh", refresh)
    monkeypatch.setattr(WorkRegistration, "__getattribute__", attribute)
    with ThreadPoolExecutor(max_workers=1) as writers:
        writing = writers.submit(register)
        try:
            assert committed.wait(10)
            h.runtime.refresh_bindings()
        finally:
            continue_registration.set()
            second = writing.result(timeout=10)
    assert interrupted == [True] and added.is_set()
    for binding in (first, second.binding):
        assert h.runtime.bindings[binding.binding_id] == binding
        assert h.runtime.profiles[binding.binding_id] == control.records[binding.binding_id].profile
    assert h.sdk.starts == []
