"""Versioned no-SDK start profile and legacy row bytes."""

import json

import pytest


def test_server_selection_is_versioned_and_pinned_without_new_identity_scope():
    from shared_context.behavior_server_config import ServerConfiguration
    from test_behavior_assistant_server import assistant_configuration
    raw = assistant_configuration()
    before = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert "sdk_start_profile" not in before.assistant.profiles[0].model_dump(mode="json")
    raw["assistant"]["profiles"][0]["sdk_start_profile"] = "fa-sdk-start-gate-v1"
    after = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert before.sha256 != after.sha256
    assert before.resource_bindings()["assistant_sha256"] != after.resource_bindings()["assistant_sha256"]
    assert before.identity == after.identity and before.table == after.table
    raw["assistant"]["profiles"][0]["sdk_start_profile"] = "fa-sdk-start-gate-future"
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(raw))


def test_legacy_execution_and_report_bytes_do_not_gain_no_sdk_fields():
    from shared_context.behavior_hosted import LocalExecution
    from shared_context.behavior_wire import HostedReportBody
    execution = {
        "launch_id": "launch", "assignment_digest": "1" * 64, "supervisor_id": "supervisor",
        "child_nonce": "nonce", "phase": "spawn_unknown", "child_pid": None, "control": "continue",
        "control_request_id": None, "outcome": None, "checkpoint": None, "result": None,
        "exit_code": None, "child_exited": False, "sequence": 1,
    }
    report = {
        "request_id": "report", "invocation_id": "invocation",
        "identity": {"agent_id": "11111111-1111-1111-1111-111111111111", "agent_name": "agent",
                     "agent_version": "8", "session_id": "hosted"},
        "process_id": "supervisor", "claim_id": "claim", "probe_id": "probe", "process_generation": 1,
        "binding_digest": "2" * 64, "supervisor_id": "supervisor", "sequence": 1, "phase": "spawn_unknown",
        "child_pid": None, "outcome": None, "checkpoint": None, "result": None, "exit_code": None,
        "child_exited": False,
    }
    for model, document in ((LocalExecution, execution), (HostedReportBody, report)):
        original = json.dumps(document, separators=(",", ":"))
        assert model.model_validate_json(original).model_dump_json() == original
