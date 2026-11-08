"""Pins install_assistant_routes' registered table: order, paths, methods, names, route class and query params.

The authority is a bare AssistantAuthority instance; real flows live in the assistant API and native tests.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from shared_context import behavior_agent_message_api as agent_messages
from shared_context import behavior_assistant_api as assistant_api
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_assistant import AssistantAuthority
from shared_context.behavior_assistant_api import PREFIX, TICKET_HEADER, install_assistant_routes
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_attachments import ATTACHMENT_RESPONSE_BYTES, AttachedTexts
from shared_context.behavior_history_prompt import REPLY_READ_PROFILES
from shared_context.behavior_reviewed_note_prompt import REVIEWED_NOTE_PROFILE
from shared_context.behavior_skill_copy import SOP_COPY_PROFILE
from shared_context.behavior_skill_draft import SKILL_DRAFT_PROFILES
from shared_context.behavior_skill_prompt import SKILL_PROFILES
from shared_context.behavior_skill_resources_prompt import SKILL_RESOURCES_PROFILE
from shared_context.behavior_user_file_prompt import FILE_READ_PROFILES, USER_INPUT_READ_PROFILES
from shared_context.behavior_work_memory_prompt import WORK_MEMORY_PROFILE
from shared_context.errors import Problem

SESSION_ROUTES = [
    "context", "context-origin", "attached-text", "skill-catalog", "skill-catalog-presentation", "read-skill",
    "read-skill-resource", "read-user-turn", "read-user-file", "read-assistant-reply", "work-memory-catalog",
    "read-work-memory", "reviewed-note-catalog", "read-reviewed-note", "copy-sop-draft", "save-skill-draft",
    "capability-catalog", "capability-call", "capability-current", "stage-memory-candidate",
    "memory-candidate-reconcile", "deliver-image", "delegation-prepare", "delegation-observe", "delegation-read",
    "agent-message-send", "agent-message-read", "reserve-model", "save", "reconcile", "finish", "save-text-reply",
    "progress", "operation", "stopped", "status", "checkpoint", "restore", "resident-restore", "sdk-state",
]
ENTRY = [("/submit", "submit"), ("/launches/{launch_id}/claim", "native_claim"),
         ("/sessions/{session_id}/acquire", "acquire")]
MODEL = [("/sessions/{session_id}/model", "model", "_ModelRoute")]


def authority(mode="legacy"):
    bare = object.__new__(AssistantAuthority)
    bare.runtime = SimpleNamespace(mode=mode)
    return bare


def provider():
    return AssistantProvider("https://example.invalid/openai/v1/chat/completions", object())


def table(*, mode="legacy", model_provider=None, on_result_ready=None):
    app = FastAPI()
    install_assistant_routes(app, authority(mode), lambda request: object(),
                             provider=model_provider, on_result_ready=on_result_ready)
    return [(route.path, sorted(route.methods), route.name, type(route).__name__,
             [param.name for param in route.dependant.query_params])
            for route in app.routes if route.path.startswith(PREFIX)]


def expected(*, model=False, ready=False):
    rows = [(PREFIX + path, ["POST"], name, "APIRoute", []) for path, name in ENTRY]
    rows += [(PREFIX + path, ["POST"], name, kind, []) for path, name, kind in MODEL if model]
    suffixes = SESSION_ROUTES + (["execution-ready"] if ready else [])
    rows += [(PREFIX + "/sessions/{session_id}/" + suffix, ["POST"], "invoke", "APIRoute", []) for suffix in suffixes]
    return rows


@pytest.mark.parametrize("model_provider,ready", [(True, True), (True, False), (False, True), (False, False)])
def test_assistant_route_table_keeps_order_paths_methods_and_route_class(model_provider, ready):
    actual = table(model_provider=provider() if model_provider else None,
                   on_result_ready=(lambda binding_id: None) if ready else None)

    assert actual == expected(model=model_provider, ready=ready)


def test_native_mode_with_a_provider_registers_the_model_route():
    assert table(mode="native", model_provider=provider()) == expected(model=True)


@pytest.mark.parametrize("case,code", [
    ("not-authority", "ASSISTANT_CONFIGURATION"),
    ("native-without-provider", "ASSISTANT_MODEL_DISABLED"),
    ("not-provider", "ASSISTANT_MODEL_DISABLED"),
])
def test_install_refuses_incomplete_composition(case, code):
    app = FastAPI()
    target = SimpleNamespace(runtime=SimpleNamespace(mode="legacy")) if case == "not-authority" else authority(
        "native" if case == "native-without-provider" else "legacy")
    with pytest.raises(Problem) as raised:
        install_assistant_routes(app, target, lambda request: object(),
                                 provider=object() if case == "not-provider" else None)

    assert (raised.value.status, raised.value.body["error"]["code"]) == (503, code)
    assert [route.path for route in app.routes if route.path.startswith(PREFIX)] == (
        [PREFIX + path for path, _ in ENTRY] if case == "not-provider" else [])


TICKET = "a" * 64
SESSION = "b" * 64
ROOT = "/behavior/v1/bindings/b-1/assistant"


class Kind(BaseModel):
    kind: str


class Message(BaseModel):
    message: Kind


def stub_app(monkeypatch, *, on_result_ready=None, session_binding="b-1", **handlers):
    """A bare authority whose session lookups and handlers record calls instead of touching storage."""
    calls = []
    bare = authority()
    session = SimpleNamespace(binding_id=session_binding, assignment=SimpleNamespace(sdk_start_binding=None))
    bare.current_session = lambda *args, **kwargs: calls.append(("current_session", kwargs)) or session
    bare.model_session = lambda *args: calls.append(("model_session",)) or session
    bare.native_claim = lambda *args: calls.append(("native_claim",))
    bare.acquire_current = lambda *args: calls.append(("acquire_current",))
    bare.finish = lambda *args: calls.append(("finish",)) or Kind(kind="done")
    bare.record_instruction_activity = lambda s: calls.append(("activity",))
    monkeypatch.setattr(agent_messages, "read", lambda *args: calls.append(("read",)) or Message(
        message=Kind(kind=reads.pop(0))))
    reads = []
    for name, handler in handlers.items():
        setattr(bare, name, handler)
    app = FastAPI()
    _install_error_handlers(app)
    install_assistant_routes(app, bare, lambda request: object(), provider=provider(),
                             on_result_ready=on_result_ready)
    client = TestClient(app, raise_server_exceptions=False, headers={"authorization": "Bearer stand-in"})
    return client, calls, reads, bare


def post(client, path, body, **headers):
    return client.post(ROOT + path, content=json.dumps(body), headers={"content-type": "application/json", **headers})


@pytest.mark.parametrize("tokens", [[], ["a" * 64, "a" * 64], ["A" * 64]])
def test_native_claim_refuses_anything_but_one_report_token_before_the_claim(monkeypatch, tokens):
    client, calls, *_ = stub_app(monkeypatch)
    response = client.post(ROOT + "/launches/l-1/claim", content="{}", headers=[
        ("content-type", "application/json"), *[("x-behavior-report-token", value) for value in tokens]])

    assert (response.status_code, response.json()["error"]["code"], calls) == (403, "BEHAVIOR_ACCESS_DENIED", [])


@pytest.mark.parametrize("ticket,session_id", [(None, SESSION), ("short", SESSION), (TICKET, "not-hex")])
def test_acquire_refuses_a_bad_ticket_or_session_as_an_acquire_error(monkeypatch, ticket, session_id):
    client, calls, *_ = stub_app(monkeypatch)
    headers = {} if ticket is None else {TICKET_HEADER: ticket}
    response = post(client, f"/sessions/{session_id}/acquire", {"request_id": "r-1"}, **headers)

    assert response.status_code == 403
    assert (response.headers["x-behavior-assistant-error-code"],
            response.headers["x-behavior-assistant-error-phase"]) == ("ASSISTANT_TICKET_DENIED", "acquire_authority")
    assert calls == []


@pytest.mark.parametrize("ticket,session_id", [(None, SESSION), ("short", SESSION), (TICKET, "not-hex")])
def test_session_route_refuses_a_bad_ticket_or_session_before_any_lookup(monkeypatch, ticket, session_id):
    client, calls, *_ = stub_app(monkeypatch)
    headers = {} if ticket is None else {TICKET_HEADER: ticket}
    response = post(client, f"/sessions/{session_id}/finish", {"request_id": "finish-task"}, **headers)

    assert (response.status_code, response.json()["error"]["code"], calls) == (403, "BEHAVIOR_ACCESS_DENIED", [])


def test_session_route_refuses_a_session_bound_to_another_assignment(monkeypatch):
    client, calls, *_ = stub_app(monkeypatch, session_binding="other")
    response = post(client, f"/sessions/{SESSION}/finish", {"request_id": "finish-task"}, **{TICKET_HEADER: TICKET})

    assert response.status_code == 403
    assert calls == [("current_session", {"active": True})]


def test_session_route_requires_its_fixed_command_id_before_the_handler(monkeypatch):
    client, calls, *_ = stub_app(monkeypatch)
    refused = post(client, f"/sessions/{SESSION}/finish", {"request_id": "other"}, **{TICKET_HEADER: TICKET})
    accepted = post(client, f"/sessions/{SESSION}/finish", {"request_id": "finish-task"}, **{TICKET_HEADER: TICKET})

    assert (refused.status_code, refused.json()["error"]["code"]) == (422, "BEHAVIOR_INVALID_REQUEST")
    assert (accepted.status_code, accepted.json()) == (200, {"kind": "done"})
    assert calls == [("current_session", {"active": True})] * 2 + [("finish",)]


@pytest.mark.parametrize("ready", [True, False])
def test_agent_message_read_signals_only_a_request_or_handoff(monkeypatch, ready):
    signals = []
    client, calls, reads, _ = stub_app(monkeypatch, on_result_ready=signals.append if ready else None)
    reads.extend(["request", "reply", "handoff"])
    for _ in range(3):
        response = post(client, f"/sessions/{SESSION}/agent-message-read",
                        {"request_id": "agent-message-read", "message_key": "k-1"}, **{TICKET_HEADER: TICKET})
        assert response.status_code == 200

    assert signals == (["b-1", "b-1"] if ready else [])
    assert calls == [("current_session", {"active": False}), ("read",), ("activity",)] * 3


@pytest.mark.parametrize("ticket", [None, "short"])
def test_model_route_refuses_a_bad_ticket_in_the_authority_phase(monkeypatch, ticket):
    client, calls, *_ = stub_app(monkeypatch)
    headers = {} if ticket is None else {TICKET_HEADER: ticket}
    response = post(client, f"/sessions/{SESSION}/model", {}, **headers)

    assert response.status_code == 403
    assert response.headers["x-behavior-model-upstream-error-phase"] == "model_authority"
    assert calls == []


def test_model_route_refuses_a_session_bound_to_another_assignment(monkeypatch):
    client, calls, *_ = stub_app(monkeypatch, session_binding="other")
    response = post(client, f"/sessions/{SESSION}/model", {}, **{TICKET_HEADER: TICKET})

    assert response.status_code == 403
    assert response.headers["x-behavior-model-upstream-error-phase"] == "model_authority"
    assert calls == [("model_session",)]


@pytest.mark.parametrize("suffix,refusal", [("delegation-prepare", True), ("finish", False)])
def test_an_oversized_delegation_prepare_is_refused_before_any_reservation(monkeypatch, suffix, refusal):
    client, calls, *_ = stub_app(monkeypatch)
    body = {"request_id": suffix, "padding": "x" * (300 * 1024)}
    response = post(client, f"/sessions/{SESSION}/{suffix}", body, **{TICKET_HEADER: TICKET})

    assert response.status_code == 413
    if refusal:
        assert (response.headers["x-behavior-assistant-error-code"],
                response.headers["x-behavior-delegation-stage"]) == ("DELEGATION_PREPARE_TOO_LARGE", "before-reservation")
    else:
        assert response.json()["error"]["code"] == "BEHAVIOR_BODY_LIMIT"
    assert calls == [("current_session", {"active": True})]


@pytest.mark.parametrize("suffix,logged", [("restore", True), ("finish", False)])
def test_a_session_lookup_failure_is_logged_only_for_restore_and_reserve(monkeypatch, caplog, suffix, logged):
    client, _, _, bare = stub_app(monkeypatch)

    def broken(*args, **kwargs):
        raise KeyError("x")

    bare.current_session = broken
    with caplog.at_level("WARNING", logger="shared_context.behavior_assistant_api"):
        response = post(client, f"/sessions/{SESSION}/{suffix}", {"request_id": "x"}, **{TICKET_HEADER: TICKET})

    assert response.status_code == 503
    assert ("session_authority" in caplog.text) is logged


def model_options(monkeypatch, profile, model_provider):
    """The keyword options the model route hands provider.validate for one free-instruction profile."""
    seen = []
    client, _, _, bare = stub_app(monkeypatch)
    instruction = SimpleNamespace(prompt_profile=profile, capabilities=None, model="m-1", model_provider=model_provider)
    bare.model_session = lambda *args: SimpleNamespace(
        binding_id="b-1", execution=SimpleNamespace(job_id="j-1", work_id="w-1"),
        assignment=SimpleNamespace(task=SimpleNamespace(instruction=instruction), session_id=SESSION))

    def validate(self, body, **options):
        seen.append(options)
        raise Problem(403, "TEST_STOP", "stop after validate")

    monkeypatch.setattr(AssistantProvider, "validate", validate)
    response = post(client, f"/sessions/{SESSION}/model", {"request_id": "r-1", "body": {}}, **{TICKET_HEADER: TICKET})
    assert response.status_code == 403
    return list(seen[0].items())


PROFILES = sorted({
    "fa-instruction-v1", "fa-instruction-v4", "fa-instruction-agent-role-v4", *SKILL_PROFILES, *SKILL_DRAFT_PROFILES,
    *FILE_READ_PROFILES, *USER_INPUT_READ_PROFILES, *REPLY_READ_PROFILES, SKILL_RESOURCES_PROFILE, WORK_MEMORY_PROFILE,
    REVIEWED_NOTE_PROFILE, SOP_COPY_PROFILE,
})

ACCESS = {
    'fa-instruction-agent-role-v1': ['user_turn_access', 'user_file_access', 'assistant_reply_access'],
    'fa-instruction-agent-role-v2': ['user_turn_access', 'user_file_access', 'assistant_reply_access'],
    'fa-instruction-agent-role-v3': ['user_turn_access', 'user_file_access', 'assistant_reply_access'],
    'fa-instruction-agent-role-v4': ['user_turn_access', 'user_file_access', 'assistant_reply_access'],
    'fa-instruction-assistant-replies-v1': ['user_turn_access', 'assistant_reply_access'],
    'fa-instruction-history-skill-draft-memory-v1': ['user_turn_access', 'user_file_access', 'assistant_reply_access', 'reviewed_note_access', 'skill_draft_access'],
    'fa-instruction-history-skill-draft-v1': ['user_turn_access', 'user_file_access', 'assistant_reply_access', 'skill_draft_access'],
    'fa-instruction-history-v1': ['user_turn_access', 'user_file_access', 'assistant_reply_access'],
    'fa-instruction-skill-draft-v1': ['skill_draft_access'],
    'fa-instruction-skill-purpose-v1': ['skill_access'],
    'fa-instruction-skill-resources-v1': ['skill_access', 'skill_resource_access'],
    'fa-instruction-skills-v1': ['skill_access'],
    'fa-instruction-sop-copy-v1': ['sop_copy_access'],
    'fa-instruction-user-files-v1': ['user_turn_access', 'user_file_access'],
    'fa-instruction-user-turns-v1': ['user_turn_access'],
    'fa-instruction-v1': [],
    'fa-instruction-v4': [],
    'fa-instruction-work-memory-v1': ['work_memory_access'],
}


@pytest.mark.parametrize("profile", PROFILES)
def test_model_route_hands_each_profile_its_fixed_options_in_order(monkeypatch, profile):
    assert model_options(monkeypatch, profile, None) == (
        [("text_reply", True), ("model", "m-1")] + [(key, True) for key in ACCESS[profile]])


def test_model_route_names_the_instruction_model_provider_before_the_access_options(monkeypatch):
    assert model_options(monkeypatch, SKILL_RESOURCES_PROFILE, "openai-responses") == [
        ("text_reply", True), ("model", "m-1"), ("model_provider", "openai-responses"),
        ("skill_access", True), ("skill_resource_access", True)]


def test_progress_and_reserve_use_the_model_session_and_progress_records_activity(monkeypatch):
    client, calls, *_ = stub_app(monkeypatch, reserve_model=lambda *args: calls.append(("reserve_model",)) or Kind(
        kind="reserved"))
    monkeypatch.setattr(assistant_api, "save_progress", lambda authority, session, command: calls.append(
        ("save_progress",)) or Kind(kind="saved"))
    progress = post(client, f"/sessions/{SESSION}/progress", {"request_id": "progress-1", "progress": {}},
                    **{TICKET_HEADER: TICKET})
    reserve = post(client, f"/sessions/{SESSION}/reserve-model", {"request_id": "r-1", "request_sha256": "c" * 64},
                   **{TICKET_HEADER: TICKET})

    assert (progress.status_code, reserve.status_code) == (200, 200)
    assert calls == [("model_session",), ("save_progress",), ("activity",), ("model_session",), ("reserve_model",)]


@pytest.mark.parametrize("suffix,logged", [("reserve-model", True), ("finish", False)])
def test_a_refused_reservation_logs_its_code(monkeypatch, caplog, suffix, logged):
    def refused(*args):
        raise Problem(409, "TEST_REFUSED", "refused")

    client, *_ = stub_app(monkeypatch, reserve_model=refused, finish=refused)
    body = {"request_id": "r-1", "request_sha256": "c" * 64} if suffix == "reserve-model" else {"request_id": "finish-task"}
    with caplog.at_level("WARNING"):
        response = post(client, f"/sessions/{SESSION}/{suffix}", body, **{TICKET_HEADER: TICKET})

    assert response.status_code == 409
    assert ('"code":"TEST_REFUSED"' in caplog.text) is logged


@pytest.mark.parametrize("suffix,mapped", [("sdk-state", True), ("finish", False)])
def test_an_sdk_state_body_refusal_becomes_an_assistant_error(monkeypatch, suffix, mapped):
    client, calls, *_ = stub_app(monkeypatch)

    async def refused(request, model, **limits):
        raise Problem(409, "SDK_STATE_INCOMPLETE", "incomplete")

    monkeypatch.setattr(assistant_api, "_body", refused)
    response = post(client, f"/sessions/{SESSION}/{suffix}", {}, **{TICKET_HEADER: TICKET})

    assert response.status_code == 409
    assert response.headers.get("x-behavior-assistant-error-code") == ("SDK_STATE_INCOMPLETE" if mapped else None)


class LargeAttachments(AttachedTexts):
    padding: str


def test_attachment_and_capability_answers_keep_their_own_response_bounds(monkeypatch):
    client, *_ = stub_app(monkeypatch, attached_text=lambda *args: LargeAttachments.model_construct(
        padding="x" * (ATTACHMENT_RESPONSE_BYTES + 1)))
    monkeypatch.setattr(assistant_api, "capability_catalog", lambda *args: Kind(kind="x" * (160 * 1024)))
    attached = post(client, f"/sessions/{SESSION}/attached-text", {"request_id": "current-attachments"},
                    **{TICKET_HEADER: TICKET})
    catalog = post(client, f"/sessions/{SESSION}/capability-catalog", {"request_id": "capability-catalog"},
                   **{TICKET_HEADER: TICKET})

    assert (attached.status_code, catalog.status_code) == (413, 200)


@pytest.mark.parametrize("tools", [True, False])
def test_a_delegated_model_request_with_tools_is_refused_before_its_validation(monkeypatch, tools):
    client, calls, _, bare = stub_app(monkeypatch)
    bare.model_session = lambda *args: SimpleNamespace(
        binding_id="b-1", execution=SimpleNamespace(job_id="j-1", work_id="w-1"),
        assignment=SimpleNamespace(task=SimpleNamespace(instruction=None), session_id=SESSION))
    monkeypatch.setattr(AssistantProvider, "validate", lambda self, body, **options: None)

    def validate_model(*args):
        calls.append(("validate_model",))
        raise Problem(403, "TEST_STOP", "stop after delegation validation")

    monkeypatch.setattr(assistant_api.delegation, "validate_model", validate_model)
    body = {"request_id": "r-1", "body": {"tools": [{"type": "function"}]} if tools else {},
            "delegation": {"tool_call_id": "call-1", "child_agent_id": "child-1"}}
    response = post(client, f"/sessions/{SESSION}/model", body, **{TICKET_HEADER: TICKET})

    assert response.status_code == 403
    assert response.headers["x-behavior-model-upstream-error-phase"] == "model_request"
    assert calls == ([("activity",)] if tools else [("activity",), ("validate_model",)])
