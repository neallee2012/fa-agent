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
