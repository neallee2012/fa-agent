"""Internal assistant exception diagnostics never become response data or new dispatch."""

import json
import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant_api import create_assistant_app, TICKET_HEADER
from shared_context.source_materials import digest
from test_conversation_transfer_startup import claimed_turn, synthetic_standing_v1
from test_instruction_observer_resume import observer_case
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token


@pytest.mark.parametrize("phase,missing", [
    ("model_consume", "binding"),
    ("reserve-model", "binding"),
    ("restore", "binding"),
    ("model_consume", "private"),
    ("model_consume", "ticket"),
])
def test_assistant_exception_reports_only_safe_key_phase_and_work(observer_case, signing_key, monkeypatch,
                                                                  caplog, phase, missing):
    c, loop = observer_case
    opened = claimed_turn(c, "diagnostic-original-work")
    private = "PRIVATE_EXAMPLE_BODY_AND_CREDENTIAL"
    missing_key = (opened.binding.binding_id if missing == "binding" else
                   opened.ticket.get_secret_value() if missing == "ticket" else private)

    def broken(*args, **kwargs):
        raise KeyError(missing_key)

    provider = c.h.composition.assistant.provider
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": private}], "stream": True}
    identifier = digest(body)
    if phase == "model_consume":
        c.authority.reserve_model(opened.session, aw.Reservation(
            request_id=identifier, request_sha256=identifier), opened.agent)
        suffix, command = "model", aw.ModelRequest(request_id=identifier, body=body)
        monkeypatch.setattr(c.authority, "consume_model", broken)
    elif phase == "reserve-model":
        monkeypatch.setattr(c.authority, "reserve_model", broken)
        suffix, command = phase, aw.Reservation(request_id=identifier, request_sha256=identifier)
    else:
        from shared_context.behavior_wire import Command
        monkeypatch.setattr(c.authority, "restore", broken)
        suffix, command = "restore", Command(request_id="sdk-restore")
    app = create_assistant_app(c.authority, c.h.composition.verifier, provider=provider)
    headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": opened.agent.oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",)), TICKET_HEADER: opened.ticket.get_secret_value(), "Content-Type": "application/json"}
    root = f"/behavior/v1/bindings/{opened.binding.binding_id}/assistant/sessions/{opened.assignment.session_id}"
    with caplog.at_level(logging.WARNING, logger="shared_context.behavior_assistant_api"), TestClient(
            app, backend_options={"loop_factory": lambda: loop}, raise_server_exceptions=False) as client:
        response = client.post(root + "/" + suffix, content=command.model_dump_json(), headers=headers)
    assert response.status_code == 503 and private not in response.text
    rows = [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
            if record.getMessage().startswith("ASSISTANT_EXCEPTION ")]
    assert rows == [{"exception_type": "KeyError", "phase": phase,
                     "work_id": opened.session.execution.work_id,
                     "key": opened.binding.binding_id if missing == "binding" else "REDACTED"}]
    assert private not in caplog.text and opened.ticket.get_secret_value() not in caplog.text
    assert headers["Authorization"] not in caplog.text and c.sessions.calls == []


@pytest.mark.parametrize("missing", ["binding", "text", "private", "ticket"])
def test_real_provider_failure_logs_only_safe_key_without_sending_or_returning_it(
    observer_case, signing_key, monkeypatch, caplog, missing,
):
    c, loop = observer_case
    opened = claimed_turn(c, "provider-diagnostic-original")
    private = "PRIVATE_PROVIDER_LOOKUP_AND_BODY"
    value = {"binding": opened.binding.binding_id, "text": "text", "private": private,
             "ticket": opened.ticket.get_secret_value()}[missing]
    expected = value if missing in {"binding", "text"} else "REDACTED"
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": private}], "stream": True}
    identifier = digest(body)
    c.authority.reserve_model(opened.session, aw.Reservation(
        request_id=identifier, request_sha256=identifier), opened.agent)
    sent = []

    def credential(_scope):
        raise KeyError(value)

    provider = c.h.composition.assistant.provider
    monkeypatch.setattr(provider, "credential", SimpleNamespace(get_token=credential))
    monkeypatch.setattr(provider, "transport", httpx.MockTransport(
        lambda request: sent.append(request) or httpx.Response(500)))
    app = create_assistant_app(c.authority, c.h.composition.verifier, provider=provider)
    headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": opened.agent.oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",)), TICKET_HEADER: opened.ticket.get_secret_value()}
    root = f"/behavior/v1/bindings/{opened.binding.binding_id}/assistant/sessions/{opened.assignment.session_id}/model"
    with caplog.at_level(logging.WARNING), TestClient(
        app, backend_options={"loop_factory": lambda: loop}, raise_server_exceptions=False,
    ) as client:
        response = client.post(root, json=aw.ModelRequest(request_id=identifier, body=body).model_dump(mode="json"),
                               headers=headers)
    rows = [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("ASSISTANT_PROVIDER_UNRESOLVED ")]
    assert len(rows) == 1 and "phase=provider_token " in rows[0] and "exception_type=KeyError " in rows[0]
    assert f"key={expected}" in rows[0]
    assert response.status_code == 503 and sent == [] and c.sessions.calls == []
    assert private not in caplog.text + response.text
    assert opened.ticket.get_secret_value() not in caplog.text + response.text
    assert "OFFLINE-provider-credential" not in caplog.text + response.text
    assert headers["Authorization"] not in caplog.text + response.text
    assert '"key"' not in response.text
