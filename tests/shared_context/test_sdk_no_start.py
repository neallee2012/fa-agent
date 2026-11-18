"""Versioned no-SDK disposition through original controller and signed routes."""

import json
import base64
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_foundry import native_key
from shared_context.errors import Problem
from approved_skill_api_helpers import approve_skill_version
from test_behavior_assistant import assistant
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from sdk_no_start_helpers import report_packet


def close_native_entry(c, monkeypatch):
    issue = c.h.assistant.issue

    def changed(start):
        result = issue(start)
        approve_skill_version(c.h, "receipt-guide", "Approved after assignment before SDK entry.")
        return result

    monkeypatch.setattr(c.h.assistant, "issue", changed)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    return c.supervisor.observe(c.starts[0].launch_id)


def post_native_report(c, probe, body):
    launch_id = c.starts[0].launch_id
    return c.h.client.post(
        f"/behavior/v1/bindings/{c.binding.binding_id}/hosted/launches/{launch_id}/probes/{probe.probe_id}/report",
        json=body, headers={**c.h.headers("agent"),
            "X-Behavior-Report-Token": c.host.report_tokens[launch_id].get_secret_value()})


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


def no_sdk_reported(c, monkeypatch):
    observed = close_native_entry(c, monkeypatch)
    probe, body = report_packet(c, observed)
    response = post_native_report(c, probe, body)
    assert response.status_code == 200
    return SimpleNamespace(launch_id=c.starts[0].launch_id, evidence_id=response.json()["evidence_id"],
                           execution=c.h.workers.inspect(c.binding.job_id).binding)


def saved_row(c, row_key, change):
    """Rewrite one saved row as storage damage would, keeping its inline material matching."""
    from shared_context.behavior_delivery import _material
    entity = c.h.transport.backing.rows[(c.h.store._scope.table_name, c.h.store._scope.partition_key,
                                         "behavior:v1:row:" + row_key)]
    payload = json.loads(entity["Payload"])
    change(payload["data"])
    if payload["material"] is not None:
        payload["material"] = _material(payload["data"]).model_dump(mode="json")
    entity["Payload"] = json.dumps(payload).encode()
    return payload["data"]


def saved_gate(c, saved, change):
    from shared_context.behavior_delivery import _digest
    report = saved_row(c, native_key("report", saved.evidence_id), lambda data: change(data["sdk_no_start"]["gate"]))
    saved_row(c, native_key("evidence", saved.evidence_id), lambda data: data.update(report_digest=_digest(report)))


def assigned_task(c, saved, change):
    """Rewrite the assigned input (never re-read by a running turn) and return the new task's digest."""
    from shared_context.behavior_assistant_wire import Task
    from shared_context.behavior_delivery import _digest
    launch = c.native.launch(saved.launch_id)
    task = Task.model_validate_json(launch.start.input)
    task = task.model_copy(update=change(task))
    start = launch.start.model_copy(update={"input": task.model_dump_json().encode()}).model_dump(mode="json")
    saved_row(c, native_key("launch", saved.launch_id), lambda data: data.update(start=start))
    return _digest(task.model_dump(mode="json"))


def refused_reconcile(c, saved):
    with pytest.raises(Problem) as caught:
        c.native.reconcile_sealed(saved.execution)
    assert (caught.value.status, caught.value.body["error"]["code"]) == (403, "NATIVE_TERMINAL_INVALID")
    assert caught.value.body["error"]["message"] == "retained native exit/claim fence is inconsistent"
