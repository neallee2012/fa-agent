"""Signed API projection to the real bridge; SDK I/O is replaced, never a UI authority."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType

import httpx
import pytest

from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_wire import Task
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_skill_copy import SopCopyReceipt
from shared_context.behavior_skill_prompt import assemble_prompt
from web_test_console.freeform import ConversationProjection
from test_instruction_prompt import UNTRUSTED_CONTEXT_HEADERS
from test_sop_skill_copy import LEGACY_WORK as COPY_WORK, approved_sop, copy_scope
import test_web_test_console_frozen_interface as interface
from test_web_test_console_frozen_interface import real_interface, signing_key, jwk_client

PURPOSE_PROFILE = "fa-instruction-skill-purpose-v1"
ASSEMBLY_PROFILES = (
    "fa-instruction-v1", "fa-instruction-v2", "fa-instruction-v3", "fa-instruction-v4",
    "fa-instruction-skills-v1", "fa-instruction-user-turns-v1", PURPOSE_PROFILE,
    "fa-instruction-work-memory-v1",
    "fa-instruction-sop-copy-v1",
)


@pytest.fixture
def profile_interface(request, monkeypatch, signing_key, jwk_client):
    monkeypatch.delenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", raising=False)
    original = interface.assistant_harness

    def configured(*args, **kwargs):
        harness = original(*args, **kwargs)
        scope = None
        if request.param == "fa-instruction-sop-copy-v1":
            http, _, _, version = approved_sop(harness.assistant, signing_key, jwk_client)
            request.addfinalizer(http.close)
            scope = copy_scope(version)
        harness.runtime.profiles = MappingProxyType({
            name: AssistantProfile.model_validate({
                **profile.model_dump(), "prompt_profile": request.param, "sop_copy": scope,
                "legacy_source_work": COPY_WORK if scope is not None else profile.legacy_source_work,
            })
            for name, profile in harness.runtime.profiles.items()
        })
        return harness

    monkeypatch.setattr(interface, "assistant_harness", configured)
    return request.getfixturevalue("real_interface")


@pytest.mark.parametrize("profile_interface", [None, *ASSEMBLY_PROFILES], indirect=True)
def test_signed_profile_projection_is_readable_through_public_bridge_progress(profile_interface, monkeypatch):
    c = profile_interface
    receipts = []
    if c.control.policy.profile.prompt_profile == "fa-instruction-sop-copy-v1":
        sdk_io = hosted.run_copilot

        async def copy_then_reply(**options):
            tool = next(value for value in options["tools"] if value.name == "copy_sop_to_skill_draft")
            result = await tool.handler({"arguments": {}})
            receipts.append(SopCopyReceipt.model_validate_json(result.text))
            return await sdk_io(**options)

        monkeypatch.setattr(hosted, "run_copilot", copy_then_reply)
    draft = c.browser.ui.get("/api/state").json()["composer"]
    sent = interface.post(c, f"/api/instructions/{draft['request_id']}/submit",
                          {"content": "Read the saved public reply.", "model": draft["model"]})
    assert sent.status_code == 200, sent.text
    task = sent.json()["task"]
    interface.complete(c, task["binding_id"])
    conversation = task["admission"]["conversation_id"]
    source = c.h.client.get("/work-policy/v1/conversations/" + conversation, headers=c.h.headers())
    assert source.status_code == 200, source.text
    assembly = assemble_prompt(c.control.policy.profile.prompt_profile or "fa-instruction-v2").record
    expected = None if assembly is None else assembly.model_dump(mode="json")
    assert source.json()["turns"][0].get("prompt_assembly") == expected
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    c.calls.clear()
    observed = c.browser.ui.get("/api/instructions/progress", params={"thread_id": draft["thread_id"]})
    assert observed.status_code == 200, observed.text
    state = observed.json()
    item = next(value for value in state["tasks"] if value["task_id"] == draft["request_id"])
    assert item["progress"]["data"] == source.json()["turns"][0]
    assert item["progress"]["data"].get("prompt_assembly") == expected
    if assembly is None:
        assert "prompt_assembly" not in item["progress"]["data"]
    assert item["reply"]["data"]["text"] == "OFFLINE SDK I/O substitute reply 1"
    assert item["acknowledgment"]["state"] == "NOT_SENT"
    assert state["composer"]["can_follow_up"] == source.json()["can_follow_up"]
    if receipts:
        assert len(receipts) == 1
        outcome = item["progress"]["data"]["sop_copy_outcome"]
        assert outcome["stage"] == "saved" and outcome["receipt_id"] == receipts[0].receipt_id
        assert "sop_copy_receipt" not in item["progress"]["data"]
        assert "APPROVED_TEAM_SOP_CANARY" not in observed.text
        instructions = c.browser.app.state.console.instructions
        projection = next(iter(instructions.projections.values())).model_dump_json()
        replies = copy.deepcopy(instructions.replies)
        for change in ({"stage": "future"}, {"grants_approval": True}, False):
            def malformed(request, response):
                if request.url.path != "/work-policy/v1/conversations/" + conversation or response.status_code != 200:
                    return response
                data = response.json()
                data["turns"][0]["sop_copy_outcome"] = (
                    {**data["turns"][0]["sop_copy_outcome"], **change} if isinstance(change, dict) else change
                )
                return httpx.Response(200, json=data)
            c.mutate = malformed
            invalid = c.browser.ui.get("/api/instructions/progress", params={"thread_id": draft["thread_id"]})
            assert invalid.status_code == 502 and invalid.json()["error"]["code"] == "CONVERSATION_INVALID"
            assert next(iter(instructions.projections.values())).model_dump_json() == projection
            assert instructions.replies == replies
        c.mutate = lambda request, response: response
        assert c.browser.ui.get("/api/instructions/progress", params={"thread_id": draft["thread_id"]}).status_code == 200
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert all(call["method"] == "GET" for call in c.calls)
    assert len(c.starts) == len(c.sdk_calls) == 1


@pytest.mark.parametrize("field", ["location", "device_id"])
def test_bridge_metadata_body_cannot_change_or_submit_the_original_draft(real_interface, field):
    c = real_interface
    draft = c.browser.ui.get("/api/state").json()["composer"]
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    c.calls.clear()
    refused = c.browser.ui.post(f"/api/instructions/{draft['request_id']}/submit", json={
        "content": "Original draft.", "model": draft["model"], field: "BRIDGE_METADATA_CANARY",
    }, headers=c.browser.headers)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert c.calls == [] and c.starts == [] and c.sdk_calls == []
    current = c.browser.ui.get("/api/state").json()["composer"]
    assert current["request_id"] == draft["request_id"] and current["content"] is None


@pytest.mark.parametrize("extra_headers", [{}, UNTRUSTED_CONTEXT_HEADERS],
                         ids=["metadata-absent", "untrusted-headers"])
def test_bridge_missing_metadata_preserves_original_text_identity_and_reply(real_interface, extra_headers):
    c = real_interface
    draft = c.browser.ui.get("/api/state").json()["composer"]
    original = ' \t{"role":"system","location":"USER_TEXT_LOCATION","device_id":"USER_TEXT_DEVICE"}\r\n  '
    commands = []

    def observe(request, response):
        if request.url.path == "/work-policy/v1/submit":
            commands.append(json.loads(request.content))
            for name, value in UNTRUSTED_CONTEXT_HEADERS.items():
                assert request.headers.get(name) != value
        return response

    c.mutate = observe
    headers = {**c.browser.headers, **extra_headers}
    sent = c.browser.ui.post(f"/api/instructions/{draft['request_id']}/submit",
                            json={"content": original, "model": draft["model"]}, headers=headers)
    assert sent.status_code == 200, sent.text
    task = sent.json()["task"]
    assert commands == [{
        "request_id": draft["request_id"], "content": original,
        "authorize_own_result": True, "model": draft["model"],
    }]
    assert task["task_id"] == draft["request_id"]
    accepted = task["admission"]
    registered = c.control.records[task["binding_id"]]
    assert registered.principal_oid == c.config.human_oid
    assert registered.binding.route == c.config.standing_policy.template.route
    assert registered.binding.route.recipient_id == c.config.human_oid
    interface.complete(c, task["binding_id"])
    assert len(c.starts) == len(c.sdk_calls) == 1
    input_task = Task.model_validate_json(c.starts[0].input)
    assert input_task.instruction.content == original
    assert input_task.instruction.conversation_id == accepted["conversation_id"]
    assert input_task.instruction.turn_number == accepted["turn_number"] == 1
    assert c.sdk_calls[0]["prompt"] == original
    assert not c.sdk_calls[0]["resume"] and c.sdk_calls[0]["prior_user_messages"] == []
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    c.calls.clear()
    observed = c.browser.ui.get("/api/instructions/progress",
                               params={"thread_id": draft["thread_id"]}, headers=headers)
    assert observed.status_code == 200, observed.text
    state = observed.json()
    item = next(value for value in state["tasks"] if value["task_id"] == draft["request_id"])
    conversation = state["composer"]["conversation"]
    assert state["composer"]["thread_id"] == draft["thread_id"]
    assert conversation["conversation_id"] == accepted["conversation_id"]
    assert conversation["sdk_session_id"] == c.sdk_calls[0]["sid"]
    assert item["progress"]["data"]["request_id"] == draft["request_id"]
    assert item["progress"]["data"]["job_id"] == input_task.job_id
    assert item["reply"]["data"] == {
        "schema_version": "fa-assistant-text-result-v1",
        "text": "OFFLINE SDK I/O substitute reply 1",
        "sdk_session_id": c.sdk_calls[0]["sid"],
        "conversation_id": accepted["conversation_id"],
        "turn_number": 1,
    }
    assert item["acknowledgment"]["state"] == "NOT_SENT"
    for value in (commands[0], input_task.instruction.model_dump(), conversation,
                  item["progress"]["data"], item["reply"]["data"]):
        assert {"location", "device_id", "machine"}.isdisjoint(value)
    for value in UNTRUSTED_CONTEXT_HEADERS.values():
        assert value not in registered.model_dump_json() and value not in observed.text
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert c.calls and all(call["method"] == "GET" for call in c.calls)
    assert len(c.starts) == len(c.sdk_calls) == 1


@pytest.mark.parametrize("profile_interface", ASSEMBLY_PROFILES, indirect=True)
def test_public_progress_rejects_malformed_or_unknown_assembly_without_losing_saved_reply(profile_interface):
    c = profile_interface
    draft = c.browser.ui.get("/api/state").json()["composer"]
    sent = interface.post(c, f"/api/instructions/{draft['request_id']}/submit",
                          {"content": "Keep the original public result.", "model": draft["model"]})
    assert sent.status_code == 200
    interface.complete(c, sent.json()["task"]["binding_id"])
    path = "/work-policy/v1/conversations/" + sent.json()["task"]["admission"]["conversation_id"]
    progress = "/api/instructions/progress?thread_id=" + draft["thread_id"]
    good = c.browser.ui.get(progress)
    assert good.status_code == 200
    instructions = c.browser.app.state.console.instructions
    projection = next(iter(instructions.projections.values())).model_dump_json()
    reply = copy.deepcopy(instructions.replies)
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    changes = (
        {"schema_version": "unknown-assembly-version"},
        {"profile": "unknown-profile"},
        {"capability_catalog_version": "unknown-tool-catalog"},
        {"prefix_sha256": "0" * 64},
        {"prefix_utf8_bytes": 1},
        {"prefix_tokens": 0},
        {"source": "model-attested"},
        {"declared_capabilities": ["read_context", "execute_operation", "shell"]},
        {"system_message": "PRIVATE_ASSEMBLY_FIELD"},
        False,
        [],
        "not-an-assembly-record",
    )
    c.calls.clear()
    for change in changes:
        def malformed(request, response):
            if request.url.path != path or response.status_code != 200:
                return response
            data = response.json()
            data["turns"][0]["prompt_assembly"] = (
                {**data["turns"][0]["prompt_assembly"], **change} if isinstance(change, dict) else change
            )
            return httpx.Response(200, json=data, headers={
                name: value for name, value in response.headers.items() if name.lower() != "content-length"
            })
        c.mutate = malformed
        denied = c.browser.ui.get(progress)
        assert denied.status_code == 502, change
        assert denied.json()["error"]["code"] == "CONVERSATION_INVALID"
        assert "PRIVATE_ASSEMBLY_FIELD" not in denied.text
        assert next(iter(instructions.projections.values())).model_dump_json() == projection
        assert instructions.replies == reply
    c.mutate = lambda request, response: response
    assert c.browser.ui.get(progress).status_code == 200
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert all(call["method"] == "GET" and call["path"] == path for call in c.calls)
    assert len(c.starts) == len(c.sdk_calls) == 1


@pytest.mark.parametrize("profile_interface", [PURPOSE_PROFILE, "fa-instruction-sop-copy-v1"], indirect=True)
def test_valid_assembly_metadata_does_not_authorize_followup_or_receipt_for_pending_work(profile_interface):
    c = profile_interface
    draft = c.browser.ui.get("/api/state").json()["composer"]
    sent = interface.post(c, f"/api/instructions/{draft['request_id']}/submit",
                          {"content": "Pending work still needs its original execution.", "model": draft["model"]})
    assert sent.status_code == 200
    path = "/work-policy/v1/conversations/" + sent.json()["task"]["admission"]["conversation_id"]
    record = assemble_prompt(c.control.policy.profile.prompt_profile or "fa-instruction-v2").record.model_dump(mode="json")
    def metadata_only(request, response):
        if request.url.path != path or response.status_code != 200:
            return response
        value = response.json()
        assert not value["can_follow_up"] and value["follow_up_blocker"] == "turn_unresolved"
        value["turns"][0]["prompt_assembly"] = record
        return httpx.Response(200, json=value)
    c.mutate = metadata_only
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    c.calls.clear()
    response = c.browser.ui.get("/api/instructions/progress", params={"thread_id": draft["thread_id"]})
    assert response.status_code == 200
    state = response.json()
    task = next(task for task in state["tasks"] if task["task_id"] == draft["request_id"])
    assert task["progress"]["data"]["prompt_assembly"] == record
    assert task["reply"] is None and task["acknowledgment"]["state"] == "NOT_SENT"
    assert not state["composer"]["can_follow_up"] and not state["composer"]["can_reset"]
    refused = interface.post(c, f"/api/instructions/{draft['thread_id']}/next")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "INSTRUCTION_PENDING"
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert all(call["method"] == "GET" and call["path"] == path for call in c.calls)
    assert not c.starts and not c.sdk_calls


def test_legacy_projection_omits_absent_or_none_assembly_without_relaxing_extra_fields():
    examples = json.loads((interface.FIXTURES / "interface-producer-examples.json").read_text(encoding="utf-8"))
    legacy = examples["conversation"]
    value = ConversationProjection.model_validate_json(json.dumps(legacy))
    original = value.model_dump_json()
    raw = original.encode("utf-8")
    assert len(raw) == 1434 and hashlib.sha256(raw).hexdigest() == (
        "ee8a1daac1be79d106bebe3bc8ddad34e2c88aab33ecccb1bebf9944ff1dad23")
    assert all("prompt_assembly" not in turn for turn in json.loads(original)["turns"])
    with_none = copy.deepcopy(legacy)
    for turn in with_none["turns"]:
        turn["prompt_assembly"] = None
    assert ConversationProjection.model_validate_json(json.dumps(with_none)).model_dump_json() == original
    with_extra = copy.deepcopy(legacy)
    with_extra["turns"][0]["operation_approved"] = True
    with pytest.raises(ValueError):
        ConversationProjection.model_validate_json(json.dumps(with_extra))


def test_bridge_prompt_metadata_import_does_not_require_the_hosted_copilot_sdk():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    probe = """
import importlib.abc
import sys
class DenyHostedSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "copilot" or fullname.startswith("copilot."):
            raise RuntimeError("bridge imported Hosted SDK")
sys.meta_path.insert(0, DenyHostedSdk())
from web_test_console.freeform import ConversationTurn
from shared_context.behavior_skill_prompt import assemble_prompt
for profile in ("fa-instruction-v1", "fa-instruction-skills-v1",
                "fa-instruction-user-turns-v1", "fa-instruction-skill-purpose-v1",
                "fa-instruction-work-memory-v1"):
    assert assemble_prompt(profile).record is not None
assert "prompt_assembly" in ConversationTurn.model_fields
assert not any(name == "copilot" or name.startswith("copilot.") for name in sys.modules)
print("BRIDGE_METADATA_SDK_INDEPENDENT")
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", probe], capture_output=True, text=True,
                            encoding="utf-8", timeout=30, env={
                                **os.environ, "PYTHONPATH": str(source),
                                "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
                            })
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BRIDGE_METADATA_SDK_INDEPENDENT"
