"""SDK-independent N1/N2 contracts; synthetic captures, no cloud or model calls."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from shared_context.behavior_agent_role_prompt import AGENT_ROLE_PROFILE, RolePromptCapture, assemble_role_prompt
from shared_context.behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_capabilities import CapabilityCapture, CapabilityGrant
from shared_context.behavior_instruction_models import InstructionModel
from shared_context.behavior_native_turn_wire import (
    NATIVE_REQUEST, NATIVE_TURN_ERRORS, NativeReceipt, NativeResultPage, NativeResultReference,
    NativeAgentCommand, NativeAgentOrigin, NativeAgentReceive, NativeTurnAddress, NativeSubmit, NativeTurnCapture,
)


def capture():
    role = assemble_role_prompt().record
    text = "Reply using only the supplied synthetic material."
    return NativeTurnCapture(
        template=AgentTemplate(
            role=TemplateRole(profile=AGENT_ROLE_PROFILE, prefix_sha256=role.prefix_sha256,
                              capability_catalog_version=role.capability_catalog_version),
            settings=TemplateSettings(model_provider="openai-responses", user_text_file_profile="fa-user-text-files-v1"),
        ),
        model=InstructionModel(id="OFFLINE-model", display_name="OFFLINE model"),
        role_prompt=RolePromptCapture(version=1, sha256=hashlib.sha256(text.encode()).hexdigest(), text=text),
    )


def submit(rid="rid-1", cid=None, text="Original user text"):
    return NativeSubmit(command=InstructionSubmit(request_id=rid, conversation_id=cid, content=text),
                        capture=capture())


def agent_receive(rid="message-1", cid=None, recipient="11111111-1111-1111-1111-111111111111"):
    sender = NativeTurnAddress(agent_id="sender-agent", conversation_id="sender-cid", request_id="sender-rid")
    return NativeAgentReceive(
        command=NativeAgentCommand(request_id=rid, conversation_id=cid, content="Original Agent message"),
        capture=capture(), source=NativeAgentOrigin(
            kind="agent_request", message_kind="request", message_id=rid, sender=sender,
            recipient_agent_id=recipient, root_id="root-1", root=sender, hop=1,
        ),
    )


def receipt_for(request, status="started"):
    reference = None if status != "completed" else NativeResultReference(
        turn_number=1, turn_key="turn:1", result_digest="a" * 64,
        text_sha256="b" * 64, text_bytes=1, text_parts=1, checkpoint_sha256="c" * 64)
    return NativeReceipt(
        state_profile=request.state_profile,
        agent_id="11111111-1111-1111-1111-111111111111", hosted_session_id="OFFLINE-host-session",
        conversation_id=request.command.conversation_id or "native-cid", request_id=request.command.request_id,
        command=request.command, capture=request.capture, submit_digest=request.sha256,
        capture_digest=request.capture.sha256, source=getattr(request, "source", None),
        routine=getattr(request, "routine", None), status=status, turn_number=1, result=reference,
        response_id=("resp_offline_fixture" if request.state_profile == "foundry-responses-v1"
                     and status in {"started", "completed"} else None))


def test_exact_original_command_capture_and_rid_round_trip():
    original = submit()
    restored = NATIVE_REQUEST.validate_json(original.model_dump_json())
    assert restored == original
    assert restored.sha256 == original.sha256
    assert submit(text="Different").sha256 != original.sha256
    receipt = NativeReceipt(state_profile=original.state_profile,
        agent_id="OFFLINE-agent", hosted_session_id="OFFLINE-host-session",
        request_id=original.command.request_id, conversation_id="native-cid-1",
        submit_digest=original.sha256, capture_digest=original.capture.sha256,
        command=original.command, capture=original.capture, status="accepted",
    )
    assert NativeReceipt.model_validate_json(receipt.model_dump_json()) == receipt


@pytest.mark.parametrize("reason", [
    "scan_pending", "accepted_or_started", "admission_pending", "task_active", "task_unconfirmed",
])
def test_pending_omission_confirmation_cannot_claim_saved_or_active_files(reason):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    raw = {
        "agent_id": "agent", "request_id": "original-follow", "previous_hosted_session_id": "source",
        "previous_agent_version": "41", "hosted_session_id": "destination", "hosted_agent_version": "43",
        "state": "pending", "reason": reason, "files_confirmation_pending": "original-confirmation",
        **({"files_check": {"category": "scan_pending"}} if reason == "scan_pending" else {}),
    }
    value = NativeEnvironmentView.model_validate_json(json.dumps(raw))
    assert value.files_confirmation_pending == "original-confirmation"
    assert "files_migrated" not in value.model_dump(mode="json")
    for change in (
        {"files_migrated": False, "files_skip_request_id": "original-confirmation"},
        {"files_confirmation_saved": "original-confirmation"},
        {"state": "ready", "reason": None, "files_check": None},
        {"reason": "task_access_denied", "files_check": None},
    ):
        with pytest.raises(ValidationError):
            NativeEnvironmentView.model_validate_json(json.dumps({**raw, **change}))


@pytest.mark.parametrize("field,value", [
    ("call_id", "forged-platform-id"), ("team_id", "another-team"),
    ("binding_id", "invented-binding"), ("accepted_etag", "invented-etag"),
])
def test_user_and_api_dto_cannot_supply_execution_or_platform_context(field, value):
    original = submit().model_dump(mode="json")
    original[field] = value
    with pytest.raises(ValidationError):
        NATIVE_REQUEST.validate_json(json.dumps(original))


def test_capture_preserves_selected_model_and_role():
    original = submit().model_dump(mode="json")
    original["command"]["model"] = "another-model"
    with pytest.raises(ValidationError):
        NATIVE_REQUEST.validate_json(json.dumps(original))
    original = submit().model_dump(mode="json")
    original["capture"]["role_prompt"]["text"] = "Changed role"
    with pytest.raises(ValidationError):
        NATIVE_REQUEST.validate_json(json.dumps(original))
    assert {"NATIVE_STATE_STORE_MISSING", "NATIVE_STATE_STORE_TTL", "NATIVE_STATE_STORE_UNKNOWN"} <= NATIVE_TURN_ERRORS


def test_large_public_result_is_explicitly_paginated():
    reference = NativeResultReference(
        turn_number=1, turn_key="turn:1", result_digest="a" * 64, text_sha256="b" * 64,
        text_bytes=160_000, text_parts=2, checkpoint_sha256="c" * 64,
    )
    first = NativeResultPage(
        conversation_id="cid", request_id="rid", result=reference,
        part=0, text="x" * 80_000, next_part=1, complete=False,
    )
    assert len(first.text) > 32 * 1024
    with pytest.raises(ValidationError):
        NativeResultPage.model_validate_json(first.model_copy(update={"complete": True}).model_dump_json())


@pytest.mark.parametrize("selected", ["subset", "empty", "extra", "changed", "duplicate", "missing"])
def test_capabilities_are_the_retained_intersection_not_the_whole_template(selected):
    grants = tuple(CapabilityGrant(entry_id=name, entry_sha256=digit * 64, kind="fa-builtin",
                                  tools=(name + "_tool",), vision=False)
                   for name, digit in (("cap-a", "a"), ("cap-b", "b"), ("cap-c", "c")))
    original = capture().model_dump(mode="json")
    original["template"]["settings"]["capabilities"] = [grant.ref.model_dump(mode="json") for grant in grants[:2]]
    kept = {"subset": grants[:1], "empty": (), "extra": grants[2:], "changed": (
        grants[0].model_copy(update={"entry_sha256": "d" * 64}),),
        "duplicate": (grants[0], grants[0]), "missing": ()}[selected]
    original["capabilities"] = (None if selected == "missing" else {
        "schema_version": "fa-capability-capture-v1", "head_selection_id": "current",
        "grants": [grant.model_dump(mode="json") for grant in kept],
    })
    if selected in {"subset", "empty"}:
        value = NativeTurnCapture.model_validate_json(json.dumps(original))
        assert value.capabilities.grants == kept
        assert len(value.template.settings.capabilities) == 2
    else:
        with pytest.raises(ValidationError):
            NativeTurnCapture.model_validate_json(json.dumps(original))


def test_agent_source_is_per_turn_and_cannot_impersonate_human_authorization():
    request = agent_receive()
    assert NATIVE_REQUEST.validate_json(request.model_dump_json()) == request
    assert request.command.authorize_own_result is False
    values = request.model_dump(mode="json")
    for field, value in (("authorize_own_result", True),):
        changed = json.loads(json.dumps(values))
        changed["command"][field] = value
        with pytest.raises(ValidationError):
            NATIVE_REQUEST.validate_json(json.dumps(changed))
    values["operation"] = "submit"
    with pytest.raises(ValidationError):
        NATIVE_REQUEST.validate_json(json.dumps(values))
    changed = request.model_dump(mode="json")
    changed["source"]["root_id"] = "another-original-root"
    assert NATIVE_REQUEST.validate_json(json.dumps(changed)).sha256 != request.sha256
    assert "source" not in submit().model_dump(mode="json")


def test_result_origin_keeps_original_result_reference_and_return_depth():
    values = agent_receive().model_dump(mode="json")
    values["source"].update(kind="agent_result", message_kind="result", hop=0, in_reply_to="original-request",
                            result=NativeResultReference(
                                turn_number=2, turn_key="turn:2", result_digest="a" * 64, text_sha256="b" * 64,
                                text_bytes=1024, text_parts=1, checkpoint_sha256="c" * 64).model_dump(mode="json"))
    parsed = NATIVE_REQUEST.validate_json(json.dumps(values))
    assert parsed.source.hop == 0 and parsed.source.result.turn_number == 2
    values["source"]["in_reply_to"] = None
    with pytest.raises(ValidationError):
        NATIVE_REQUEST.validate_json(json.dumps(values))
    values["source"]["kind"] = "server_notice"
    with pytest.raises(ValidationError):
        NATIVE_REQUEST.validate_json(json.dumps(values))


@pytest.mark.parametrize("hop", [0, 12])
def test_native_hop_is_provenance_not_the_legacy_two_hop_limit(hop):
    values = agent_receive().model_dump(mode="json")
    values["source"]["hop"] = hop
    assert NATIVE_REQUEST.validate_json(json.dumps(values)).source.hop == hop
    values["source"]["hop"] = -1
    with pytest.raises(ValidationError):
        NATIVE_REQUEST.validate_json(json.dumps(values))


def test_api_import_needs_neither_copilot_nor_agentserver():
    code = """
import importlib.abc
import sys
class RejectHosted(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'copilot' or fullname.startswith(('copilot.', 'azure.ai.agentserver')):
            raise AssertionError('Hosted SDK import: ' + fullname)
sys.meta_path.insert(0, RejectHosted())
from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeReceipt
print('sdk-independent')
"""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc")}
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sdk-independent"
