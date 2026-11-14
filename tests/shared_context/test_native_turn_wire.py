"""SDK-independent N1/N2 contracts; synthetic captures, no cloud or model calls."""

import hashlib
import json

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


_PENDING = {
    "agent_id": "agent", "request_id": "original-follow", "previous_hosted_session_id": "source",
    "previous_agent_version": "41", "hosted_session_id": "destination", "hosted_agent_version": "43",
    "state": "pending", "reason": "task_active",
}
_SCAN = {"reason": "scan_pending", "files_check": {"category": "scan_pending"}}
_OMITTED = {"files_migrated": False, "files_skip_request_id": "original-confirmation"}
_MIGRATED = {"files_migrated": True, "files_manifest_sha256": "a" * 64}
_READY = {"state": "ready", "reason": None}
_DISTINCT = "version following requires a distinct execution session"
_REQUEST = "a persisted transition retains its original request"
_READY_REASON = "a ready environment has no unresolved transition reason"
_CHECK = "an environment check detail is only progress, not a ready or confirmed result"
_OMISSION_CONFIRMATION = "omitted files require the original explicit confirmation, not a migration manifest"
_SKIP_ONLY_OMISSION = "only an explicit file omission names its confirmation"
_POLICY = "an automatic omission retains its policy basis, reason and original decision"
_MANIFEST = "migrated files require their original manifest"
_SAVED = "a saved but inactive confirmation cannot claim migration or an active omission"
_SAVED_REASON = "an original omission reason requires its saved confirmation reference"
_ABSENT = "an absent confirmation observation cannot grant or claim file migration"
_PENDING_CONFIRMATION = "a pending confirmation is unactivated progress, not an omission or ready state"


@pytest.mark.parametrize("change", [
    {"state": "current", "hosted_session_id": "source", "request_id": None, "reason": None},
    {"state": "available", "request_id": None, "reason": "source_active"},
    _READY, _SCAN, _OMITTED, _MIGRATED,
    {**_OMITTED, "files_omission_basis": "system_policy", "files_omission_reason": "source_changed"},
    {"files_confirmation_saved": "original-confirmation", "files_confirmation_reason": "destination"},
    {"files_confirmation_absent": "original-confirmation"},
    *({"reason": reason, "files_confirmation_pending": "original-confirmation"}
      for reason in ("accepted_or_started", "admission_pending", "task_active", "task_unconfirmed")),
    {**_SCAN, "files_confirmation_pending": "original-confirmation"},
])
def test_environment_view_accepts_each_consistent_transition(change):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    raw = {**_PENDING, **change}
    view = NativeEnvironmentView.model_validate_json(json.dumps(raw))
    assert view.model_dump(mode="json", exclude_defaults=True) == {
        key: value for key, value in raw.items() if value is not None}


@pytest.mark.parametrize("message,change", [
    (_DISTINCT, {"hosted_session_id": "source"}),
    (_DISTINCT, {**_READY, "hosted_session_id": "source", "request_id": None}),
    (_REQUEST, {"request_id": None}),
    (_REQUEST, {**_READY, "request_id": None}),
    (_REQUEST, {"state": "ready", "reason": "task_active", "request_id": None}),
    (_READY_REASON, {"state": "ready", "reason": "task_active"}),
    (_READY_REASON, {"state": "ready", "reason": "task_active", "files_check": {"category": "scan_pending"}}),
    (_CHECK, {"files_check": {"category": "scan_pending"}}),
    (_CHECK, {**_READY, "files_check": {"category": "scan_pending"}}),
    (_CHECK, {"reason": "scan_pending", "files_check": {"category": "active_waiting"}}),
    (_CHECK, {**_SCAN, "state": "available"}),
    (_CHECK, {"files_check": {"category": "scan_pending"}, "files_migrated": False}),
    (_OMISSION_CONFIRMATION, {"files_migrated": False}),
    (_OMISSION_CONFIRMATION, {**_OMITTED, "files_manifest_sha256": "a" * 64}),
    (_OMISSION_CONFIRMATION, {"files_migrated": False, "files_omission_basis": "system_policy"}),
    (_SKIP_ONLY_OMISSION, {"files_skip_request_id": "original-confirmation"}),
    (_SKIP_ONLY_OMISSION, {**_MIGRATED, "files_skip_request_id": "original-confirmation"}),
    (_SKIP_ONLY_OMISSION, {"files_skip_request_id": "original-confirmation", "files_omission_basis": "system_policy"}),
    (_POLICY, {"files_omission_basis": "system_policy", "files_omission_reason": "source"}),
    (_POLICY, {**_MIGRATED, "files_omission_basis": "system_policy", "files_omission_reason": "source"}),
    (_POLICY, {**_OMITTED, "files_omission_basis": "system_policy"}),
    (_POLICY, {**_OMITTED, "files_omission_reason": "source"}),
    (_POLICY, {"files_migrated": True, "files_omission_reason": "source"}),
    (_MANIFEST, {"files_migrated": True}),
    (_MANIFEST, {"files_migrated": True, "files_confirmation_saved": "original-confirmation"}),
    (_SAVED, {**_READY, "files_confirmation_saved": "original-confirmation"}),
    (_SAVED, {**_MIGRATED, "files_confirmation_saved": "original-confirmation"}),
    (_SAVED, {**_OMITTED, "files_confirmation_saved": "original-confirmation"}),
    (_SAVED, {"state": "available", "files_confirmation_saved": "c", "files_confirmation_reason": "source"}),
    (_SAVED_REASON, {"files_confirmation_reason": "source"}),
    (_SAVED_REASON, {**_READY, "files_confirmation_reason": "source", "files_confirmation_absent": "c"}),
    (_ABSENT, {**_READY, "files_confirmation_absent": "c"}),
    (_ABSENT, {**_MIGRATED, "files_confirmation_absent": "c"}),
    (_ABSENT, {**_OMITTED, "files_confirmation_absent": "c"}),
    (_ABSENT, {"files_confirmation_saved": "c", "files_confirmation_absent": "c"}),
    (_ABSENT, {"files_confirmation_absent": "c", "files_confirmation_pending": "c"}),
    (_ABSENT, {**_READY, "files_confirmation_absent": "c", "files_confirmation_pending": "c"}),
    (_PENDING_CONFIRMATION, {**_READY, "files_confirmation_pending": "c"}),
    (_PENDING_CONFIRMATION, {"state": "available", "files_confirmation_pending": "c"}),
    (_PENDING_CONFIRMATION, {"reason": "task_access_denied", "files_confirmation_pending": "c"}),
    (_PENDING_CONFIRMATION, {"reason": None, "files_confirmation_pending": "c"}),
    (_PENDING_CONFIRMATION, {"reason": "scan_pending", "files_confirmation_pending": "c"}),
    (_PENDING_CONFIRMATION, {**_MIGRATED, "files_confirmation_pending": "c"}),
    (_PENDING_CONFIRMATION, {**_OMITTED, "files_confirmation_pending": "c"}),
    (_PENDING_CONFIRMATION, {"files_confirmation_saved": "c", "files_confirmation_pending": "c"}),
])
def test_environment_view_names_the_first_broken_transition_rule(message, change):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    with pytest.raises(ValidationError) as caught:
        NativeEnvironmentView.model_validate_json(json.dumps({**_PENDING, **change}))
    assert [error["msg"] for error in caught.value.errors()] == [f"Value error, {message}"]
