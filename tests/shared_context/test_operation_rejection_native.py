"""One bounded fixed-SDK witness of the signed human rejection producer."""

from pathlib import Path
import tempfile

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_operations import ToolBrokerCore, control_key
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
import test_skill_guidance_native as guidance


@pytest.fixture
def native_parent():
    with tempfile.TemporaryDirectory(prefix="fa-rejection-sdk-") as directory:
        parent = Path(directory)
        assert not str(parent).startswith("\\\\?\\")
        yield parent


def test_fixed_sdk_honors_the_original_signed_human_rejection(
        assistant, signing_key, jwk_client, monkeypatch, native_parent):
    prepare, authorize = guidance.prepare_operation, ToolBrokerCore.authorize
    mkdtemp = guidance.tempfile.mkdtemp
    rejected, denials, roots = [], [], []
    owned = native_parent

    def reject_prepared(turn):
        prepared, preview = prepare(turn)
        command = wire.OperationRejection(
            schema_version="fa-operation-rejection-v1",
            request_id="native-human-rejection", decision_id="native-human-rejection-decision",
            description_digest=prepared["description_digest"],
            adapter_contract_digest=prepared["adapter_contract_digest"],
        )
        path = "/behavior/v1/bindings/" + turn.binding.binding_id + "/operation/reject"
        response = turn.h.client.post(path, content=command.model_dump_json(), headers=turn.h.headers())
        assert response.status_code == 200 and response.json()["result"]["rejected"] is True
        opkey, grantkey = control_key("operation", turn.binding.operation_id), control_key("grant", turn.binding.grant_id)
        rows = turn.h.store.read_snapshot((opkey, grantkey))
        assert rows[opkey].payload["data"]["human_rejection"]["decision_id"] == command.decision_id
        assert rows[grantkey] is None
        rejected.append(turn.binding.operation_id)
        guidance.record("signed-human-rejection", {
            "rejected": True, "original_operation": turn.binding.operation_id,
            "decision_id": command.decision_id, "description_digest": command.description_digest,
            "grant_present": False,
        })
        return prepared, preview

    def observe_authorization(core, command, context):
        try:
            return authorize(core, command, context)
        except Problem as error:
            if command.operation_id in rejected:
                denials.append(error.body["error"]["code"])
            raise

    def owned_directory(*args, **kwargs):
        if kwargs.get("prefix") != "s6-native-":
            return mkdtemp(*args, **kwargs)
        assert not roots, "The witness may create only one owned runtime root"
        result = mkdtemp(*args, **{**kwargs, "dir": str(owned)})
        assert Path(result).resolve().is_relative_to(owned.resolve())
        roots.append(result)
        return result

    monkeypatch.setattr(guidance, "prepare_operation", reject_prepared)
    monkeypatch.setattr(ToolBrokerCore, "authorize", observe_authorization)
    monkeypatch.setattr(guidance.tempfile, "mkdtemp", owned_directory)
    guidance.test_loaded_malicious_guidance_is_data_for_the_fixed_sdk_and_cannot_execute_an_unapproved_operation(
        assistant, signing_key, jwk_client, monkeypatch,
    )
    assert len(rejected) == len(roots) == 1
    assert denials == ["OPERATION_HUMAN_REJECTED"]
    guidance.record("fixed-sdk-human-rejection", {
        "original_human_rejections": len(rejected), "observed_core_denials": denials,
        "driver": "existing-fixed-sdk-guidance-load-deny-read-finish",
        "scope": "one runtime, four scripted provider requests, no new capability or native loader",
        "original_operation_and_accounting_preserved": True,
        "limitation": "Not natural model quality, Hosted execution, arbitrary adapters or full SP-20 acceptance.",
    })
