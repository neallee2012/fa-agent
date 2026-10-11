"""Signed preflight and console consumers; no SDK, model, or external network."""

import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_work_policy import WorkPreflightQuery, derive_work_binding
from shared_context.memory_bridge_settings import memory_slot_ids
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_conversation_note_memory import SETTINGS, enable_memory, memory_conversations
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_frozen_interface import open_ui
from test_web_test_console_standing import configuration
from web_test_console.adapter import ConsoleError, parse_config
from web_test_console.instructions import InstructionConnection


@pytest.fixture
def memory_console(memory_conversations, tmp_path):
    h, control, _ = memory_conversations
    local = _LocalHttp(h.client)
    c = SimpleNamespace(h=h, control=control, journal=tmp_path / "memory.sqlite3",
                        calls=[], preflights=[], mutate=lambda request, response: response)
    c.config = parse_config(json.dumps(configuration(h, control)).encode())
    c.connection = InstructionConnection(schema_version="fa-console-instructions-connection-v1",
        configuration_sha256=c.config.sha256, backend_core_sha256="c" * 64)

    def send(request):
        before = copy.deepcopy(h.transport.backing.rows)
        response = local.handle_request(request)
        c.calls.append((request.method, request.url.path))
        if request.url.path.endswith("/preflight"):
            assert before == h.transport.backing.rows
            c.preflights.append(response.json())
        return c.mutate(request, response)

    c.backend = httpx.Client(transport=httpx.MockTransport(send), follow_redirects=False)
    c.browser = open_ui(c)
    try:
        yield c
    finally:
        c.browser.ui.close()
        c.backend.close()
        assert h.endpoint.requests == []


def query(command, read_id="inspect-memory"):
    return WorkPreflightQuery(read_id=read_id, request_id=command.request_id,
        submit_body_sha256=hashlib.sha256(command.model_dump_json().encode()).hexdigest(),
        content=command.content, conversation_id=command.conversation_id, model=command.model)


def test_memory_preflight_distinguishes_candidate_from_original_capture(memory_console):
    c = memory_console
    settings = enable_memory(c.h)
    draft = c.browser.ui.get("/api/state").json()["composer"]
    command = InstructionSubmit(request_id=draft["request_id"], content="Keep this original memory scope.",
                                model="gpt-4.1-mini")
    before = copy.deepcopy(c.h.transport.backing.rows)
    response = c.h.client.post("/work-policy/v1/preflight",
        content=query(command).model_dump_json(), headers=c.h.headers())
    assert response.status_code == 200, response.text
    assert c.h.transport.backing.rows == before
    candidate = response.json()["memory_setting_ref"]
    assert candidate["state"] == "candidate" and candidate["settings_id"] == settings
    assert candidate["admission_id"] == derive_work_binding(c.control.policy, command.request_id)[0]

    submitted = c.browser.ui.post(f"/api/instructions/{command.request_id}/submit",
        json={"content": command.content, "model": command.model}, headers=c.browser.headers)
    assert submitted.status_code == 200, submitted.text
    record = c.control.records[submitted.json()["task"]["binding_id"]]
    assert candidate["capture_sha256"] == digest(record.profile.memory_scope.model_dump(mode="json"))
    disabled = c.h.client.post(SETTINGS, headers=c.h.headers(), json={
        "schema_version": "fa-memory-bridge-settings-command-v1", "request_id": "disable-future-memory",
        "expected_settings_id": settings, "enabled": False,
    })
    assert disabled.status_code == 200
    c.calls.clear()
    observed = c.browser.ui.post(f"/api/tasks/{command.request_id}/status", json={}, headers=c.browser.headers)
    assert observed.status_code == 200, observed.text
    retained = c.preflights[-1]["memory_setting_ref"]
    assert retained == {**candidate, "state": "captured"}
    console = c.browser.app.state.console
    assert console.instruction_observations[command.request_id].preflight.memory_setting_ref.model_dump(mode="json") == retained
    assert c.preflights[-1]["authorization"] is False
    assert not any(path.endswith("/submit") for _, path in c.calls)


def test_instruction_status_keeps_reading_job_when_preflight_is_temporarily_unavailable(memory_console):
    c = memory_console
    draft = c.browser.ui.get("/api/state").json()["composer"]
    response = c.browser.ui.post(f"/api/instructions/{draft['request_id']}/submit",
        json={"content": "Keep status readable.", "model": "gpt-4.1-mini"}, headers=c.browser.headers)
    assert response.status_code == 200, response.text
    primed = c.browser.ui.post(f"/api/tasks/{draft['request_id']}/status", json={}, headers=c.browser.headers)
    assert primed.status_code == 200, primed.text
    task = primed.json()["task"]
    assert task["execution"]["observation"] is not None

    def unavailable(request, response):
        if request.url.path == "/work-policy/v1/preflight":
            return httpx.Response(503, json={"error": {"code": "OFFLINE_PRECHECK_DOWN"}})
        return response

    c.mutate = unavailable
    c.calls.clear()
    observed = c.browser.ui.post(f"/api/tasks/{draft['request_id']}/status", json={}, headers=c.browser.headers)
    assert observed.status_code == 200, observed.text
    task = observed.json()["task"]
    assert task["status"]["data"]["job_id"]
    assert task["execution"]["observation"] is None
    assert task["execution"]["blocking_code"] == "BACKEND_UNAVAILABLE"
    assert c.browser.app.state.console.instruction_observations.get(draft["request_id"]) is None


def test_instruction_preflight_does_not_treat_client_clock_skew_as_server_authority(memory_console):
    from web_test_console.work_policy import checked_observation
    c = memory_console
    draft = c.browser.ui.get("/api/state").json()["composer"]
    response = c.browser.ui.post(f"/api/instructions/{draft['request_id']}/submit",
        json={"content": "Check skew.", "model": "gpt-4.1-mini"}, headers=c.browser.headers)
    assert response.status_code == 200, response.text
    assert c.browser.ui.post(f"/api/tasks/{draft['request_id']}/status", json={}, headers=c.browser.headers).status_code == 200
    console = c.browser.app.state.console
    observed = console.instruction_observations[draft["request_id"]]
    reader = console.instructions.reader
    reader.clock = lambda: c.h.time - __import__("datetime").timedelta(milliseconds=1)
    checked = reader.checked(observed.query, console.instructions.task(draft["request_id"]),
                             observed.preflight.binding, observed.preflight.model_dump_json().encode(),
                             instruction=True)
    assert checked_observation(checked, observed.query, reader.clock(), instruction=True) == checked
    with pytest.raises(ConsoleError):
        checked_observation(checked, observed.query, reader.clock())


@pytest.mark.parametrize("damage", [
    "wrong-source", "wrong-claim", "extra", "missing", "no-ref", "wrong-admission", "candidate-for-admitted",
])
def test_console_rejects_incomplete_or_cross_work_memory_references(memory_console, damage):
    c = memory_console
    enable_memory(c.h)
    request_id = c.browser.ui.get("/api/state").json()["composer"]["request_id"]
    response = c.browser.ui.post(f"/api/instructions/{request_id}/submit",
        json={"content": "An original request.", "model": "gpt-4.1-mini"}, headers=c.browser.headers)
    assert response.status_code == 200

    def change(request, response):
        if request.url.path != "/work-policy/v1/preflight":
            return response
        value = response.json()
        binding, ref = value["binding"], value["memory_setting_ref"]
        if damage == "wrong-source":
            binding["source_ids"][-1] = "other-source"
        elif damage == "wrong-claim":
            binding["claim_ids"][-1] = "other-claim"
        elif damage == "extra":
            binding["source_ids"].append("extra-source")
        elif damage == "missing":
            binding["claim_ids"].pop()
        elif damage == "no-ref":
            value.pop("memory_setting_ref")
        elif damage == "wrong-admission":
            ref["admission_id"] = "b" * 64
        else:
            ref["state"] = "candidate"
        return httpx.Response(200, json=value)

    c.mutate = change
    c.calls.clear()
    before = copy.deepcopy(c.h.transport.backing.rows)
    console = c.browser.app.state.console
    task = console.instructions.task(request_id)
    with pytest.raises(ConsoleError) as refused:
        console.instructions.reader.read(console.instructions.query(task), SecretStr(console.vault.get()))
    assert refused.value.code == "WORK_POLICY_INVALID"
    assert c.h.transport.backing.rows == before
    assert not any(path.endswith("/submit") for _, path in c.calls)


def test_no_capture_omits_the_reference_and_does_not_allow_even_correctly_named_slots(memory_console):
    c = memory_console
    request_id = c.browser.ui.get("/api/state").json()["composer"]["request_id"]
    response = c.browser.ui.post(f"/api/instructions/{request_id}/submit",
        json={"content": "No memory for this conversation.", "model": "gpt-4.1-mini"}, headers=c.browser.headers)
    assert response.status_code == 200
    status = f"/api/tasks/{request_id}/status"
    assert c.browser.ui.post(status, json={}, headers=c.browser.headers).status_code == 200
    assert "memory_setting_ref" not in c.preflights[-1]
    enable_memory(c.h)
    assert c.browser.ui.post(status, json={}, headers=c.browser.headers).status_code == 200
    assert "memory_setting_ref" not in c.preflights[-1]

    def inject_slots(request, response):
        if request.url.path.endswith("/preflight"):
            value = response.json()
            admission = derive_work_binding(c.control.policy, request_id)[0]
            source, claim = memory_slot_ids(admission)
            value["binding"]["source_ids"].append(source)
            value["binding"]["claim_ids"].append(claim)
            return httpx.Response(200, json=value)
        return response

    c.mutate = inject_slots
    console = c.browser.app.state.console
    task = console.instructions.task(request_id)
    with pytest.raises(ConsoleError) as refused:
        console.instructions.reader.read(console.instructions.query(task), SecretStr(console.vault.get()))
    assert refused.value.code == "WORK_POLICY_INVALID"


def test_retained_capture_is_used_before_job_admission_even_after_global_setting_changes(memory_conversations):
    h, control, human = memory_conversations
    settings = enable_memory(h)
    command = InstructionSubmit(request_id="registered-before-job", content="Do not reinterpret my original settings.")
    control.register(human, command.request_id, hashlib.sha256(command.model_dump_json().encode()).hexdigest(),
                     instruction=command)
    assert h.client.post(SETTINGS, headers=h.headers(), json={
        "schema_version": "fa-memory-bridge-settings-command-v1", "request_id": "disable-after-register",
        "expected_settings_id": settings, "enabled": False,
    }).status_code == 200
    response = h.client.post("/work-policy/v1/preflight", content=query(command).model_dump_json(), headers=h.headers())
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["already_admitted"] is False
    assert value["memory_setting_ref"]["state"] == "captured"
    assert value["memory_setting_ref"]["settings_id"] == settings
