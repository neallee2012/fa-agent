"""The shared HTTP route preserves legacy bytes without restricting new instructions."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant_wire import AssistantSubmit, InstructionSubmit, Task
from shared_context.behavior_work_policy import WorkPreflightQuery, derive_work
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled


@pytest.fixture
def runtime_path():
    return Path(__file__)  # Admission only: no runtime, provider or model is started.


def route(assistant):
    h, control, human, _ = enabled(assistant)
    starts = []
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
    ), start_work=starts.append)
    return h, control, human, starts


def post(h, action, command):
    return h.client.post("/work-policy/v1/" + action, content=command.model_dump_json(), headers=h.headers())


@pytest.mark.parametrize("with_defaults", [False, True])
def test_legacy_http_submission_keeps_original_registration_wrapper_and_once(assistant, monkeypatch, with_defaults):
    for name in ("FA_INSTRUCTION_MODEL_CATALOG_FILE", "FA_INSTRUCTION_MODEL_FACTS_FILE",
                 "FA_INSTRUCTION_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    h, control, human, starts = route(assistant)
    command = AssistantSubmit(request_id="legacy-http-original")
    command_sha = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
    _, expected, profile = derive_work(control.policy, command.request_id, h.runtime.mode)
    preview = post(h, "preflight", WorkPreflightQuery(
        read_id="legacy-preflight", request_id=command.request_id, submit_body_sha256=command_sha))
    assert preview.status_code == 200, preview.text
    assert "conversation_id" not in preview.json()
    raw = command.model_dump_json() if with_defaults else json.dumps({"request_id": command.request_id})
    accepted = h.client.post("/work-policy/v1/submit", content=raw, headers=h.headers())
    assert accepted.status_code == 200, accepted.text
    assert set(accepted.json()) == {"binding_id", "status"}
    assert accepted.json()["binding_id"] == expected.binding_id
    record = control.records[expected.binding_id]
    assert record.binding == expected and record.profile == profile
    assert record.submit_sha256 == command_sha and record.profile.instruction is None
    guard = h.store._guard()
    replay = post(h, "submit", command)
    assert replay.status_code == 200 and replay.content == accepted.content
    assert h.store._guard() == guard and control._models is None
    assert starts == [] and h.endpoint.requests == []


@pytest.mark.parametrize("explicit_model", [False, True])
def test_free_http_submission_stays_free_including_legacy_text_with_model(assistant, explicit_model):
    h, control, human, starts = route(assistant)
    text = (AssistantSubmit(request_id="unused").content if explicit_model
            else "  Explain why a rainbow is curved.\n")
    command = InstructionSubmit(request_id="free-http-original", content=text,
                                model="gpt-4.1-mini" if explicit_model else None)
    command_sha = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
    preview = post(h, "preflight", WorkPreflightQuery(
        read_id="free-preflight", request_id=command.request_id, submit_body_sha256=command_sha,
        content=command.content, model=command.model))
    assert preview.status_code == 200, preview.text
    accepted = post(h, "submit", command)
    assert accepted.status_code == 200, accepted.text
    assert set(accepted.json()) == {"binding_id", "status", "conversation_id", "turn_number", "model"}
    record = control.records[accepted.json()["binding_id"]]
    instruction = Task.model_validate_json(h.runtime.job_input(record.binding)).instruction
    assert instruction is not None and instruction.content == text
    assert instruction.model == accepted.json()["model"] == "gpt-4.1-mini"
    assert instruction.conversation_id == preview.json()["conversation_id"] == accepted.json()["conversation_id"]
    assert record.submit_sha256 == command_sha
    assert starts == [record.binding.binding_id] and h.endpoint.requests == []


def test_compatibility_parser_keeps_strict_body_validation(assistant):
    h, control, _, starts = route(assistant)
    guard = h.store._guard()
    original = AssistantSubmit(request_id="invalid-original").model_dump(mode="json")
    bodies = [
        {**original, "authorize_own_result": False},
        {**original, "extra_field": "not-supported"},
        {**original, "content": " \n\t"},
    ]
    for body in bodies:
        response = h.client.post("/work-policy/v1/submit", json=body, headers=h.headers())
        assert response.status_code == 422, response.text
    duplicate = ('{"request_id":"duplicate","content":"first","content":"second",'
                 '"authorize_own_result":true}')
    assert h.client.post("/work-policy/v1/submit", content=duplicate, headers=h.headers()).status_code == 422
    assert h.store._guard() == guard and starts == [] and h.endpoint.requests == []
