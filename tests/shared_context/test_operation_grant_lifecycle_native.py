"""Bounded SDK witnesses for signed revocation and actual grant expiry."""

from datetime import timedelta
from pathlib import Path

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_operations import ToolBrokerCore, control_key
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_operation_rejection_native import native_parent
from test_skill_guidance_authority import approval_command, operation_path
import test_skill_guidance_native as guidance


@pytest.mark.parametrize("disposition", ["revoked", "expired"])
def test_fixed_sdk_checks_the_current_grant_after_later_claimed_approval(
        assistant, signing_key, jwk_client, monkeypatch, native_parent, disposition):
    prepare, authorize, mkdtemp = guidance.prepare_operation, ToolBrokerCore.authorize, guidance.tempfile.mkdtemp
    operations, denials, roots = [], [], []
    record = guidance.record

    def scoped_record(name, data):
        record(disposition + "-" + name, data)

    def change_current_grant(turn):
        prepared, preview = prepare(turn)
        approval = approval_command(turn, prepared)
        if disposition == "expired":
            approval = approval.model_copy(update={"expires_at": turn.h.time + timedelta(seconds=1)})
        approved = turn.h.client.post(operation_path(turn) + "/approve",
            content=approval.model_dump_json(), headers=turn.h.headers())
        assert approved.status_code == 200, approved.text
        grant_key = control_key("grant", turn.binding.grant_id)
        before = turn.h.store.read_snapshot((grant_key,))[grant_key].payload["data"]
        assert before["revoked"] is False and before["remaining_uses"] == 1
        if disposition == "revoked":
            command = wire.OperationRevocation(request_id="native-current-revoke", decision_id="native-revoke-decision",
                description_digest=approval.description_digest, expected_revocation_revision=0)
            response = turn.h.client.post(operation_path(turn) + "/revoke",
                content=command.model_dump_json(), headers=turn.h.headers())
            assert response.status_code == 200 and response.json()["result"]["revoked"] is True
        else:
            turn.h.time += timedelta(seconds=2)
        after = turn.h.store.read_snapshot((grant_key,))[grant_key].payload["data"]
        if disposition == "revoked":
            assert after["revoked"] is True and after["revocation_revision"] == 1
        else:
            assert after == before and turn.h.time > approval.expires_at
        assert after["remaining_uses"] == before["remaining_uses"]
        assert after["remaining_units"] == before["remaining_units"]
        operations.append(turn.binding.operation_id)
        scoped_record("original-grant", {
            "approved_by_signed_human": True, "current_disposition": disposition,
            "grant_existed_before_change": True, "grant_bytes_unchanged": disposition == "expired",
            "signed_revoke": disposition == "revoked",
            "remaining_uses_preserved": True, "remaining_units_preserved": True,
        })
        return prepared, preview

    def observe_authorization(core, command, context):
        try:
            return authorize(core, command, context)
        except Problem as error:
            if command.operation_id in operations:
                denials.append(error.body["error"]["code"])
            raise

    def owned_directory(*args, **kwargs):
        if kwargs.get("prefix") != "s6-native-":
            return mkdtemp(*args, **kwargs)
        assert not roots, "Each lifecycle witness may create only one owned runtime root"
        result = mkdtemp(*args, **{**kwargs, "dir": str(native_parent)})
        assert Path(result).resolve().is_relative_to(native_parent.resolve())
        roots.append(result)
        return result

    monkeypatch.setattr(guidance, "record", scoped_record)
    monkeypatch.setattr(guidance, "prepare_operation", change_current_grant)
    monkeypatch.setattr(ToolBrokerCore, "authorize", observe_authorization)
    monkeypatch.setattr(guidance.tempfile, "mkdtemp", owned_directory)
    guidance.test_loaded_malicious_guidance_is_data_for_the_fixed_sdk_and_cannot_execute_an_unapproved_operation(
        assistant, signing_key, jwk_client, monkeypatch, expected_denial_status=403,
    )
    assert len(operations) == len(roots) == 1
    assert denials == ["OPERATION_GRANT_DENIED"]
    scoped_record("current-grant-denial", {
        "current_disposition": disposition, "observed_core_denials": denials,
        "driver": "existing-fixed-sdk-guidance-load-deny-read-finish",
        "scope": "one runtime and four scripted provider requests per disposition",
        "limitation": "Grant denial is not evidence of rollback, refund, arbitrary-adapter or natural-model behavior.",
    })
