"""Observe the unchanged model fence without revealing execution or credential values."""

import json
import logging

import httpx
from fastapi.testclient import TestClient
import pytest

from shared_context import behavior_assistant_api as api
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant import key
from shared_context.source_materials import digest
from test_conversation_transfer_startup import claimed_turn, synthetic_standing_v1
from test_instruction_observer_resume import observer_case
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token


@pytest.mark.parametrize("condition", [
    "state_not_running", "execution_changed", "authority_gate", "after_consume", "unchanged",
])
def test_model_fence_records_only_safe_condition_and_elapsed_consume(observer_case, signing_key, monkeypatch,
                                                                    caplog, condition):
    """consume's CAS gate is the last fence; a change after it is inside the seconds-level send window."""
    c, loop = observer_case
    opened = claimed_turn(c, "fence-diagnostic-original")
    clock = [100.0]
    monkeypatch.setattr(api, "perf_counter", lambda: clock[0])
    sent = []
    private = "PRIVATE_MODEL_BODY_AND_ASSIGNMENT"

    def cancel():
        c.h.runtime.dep.workers.cancel(opened.binding.job_id, opened.binding.owner,
                                       expected_binding=opened.session.execution)

    async def send(body, **options):
        clock[0] += 0.25
        if condition == "after_consume":
            cancel()
        sent.append(body)
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"}, request=httpx.Request("POST", "https://synthetic.invalid/model"))

    provider = c.h.composition.assistant.provider
    monkeypatch.setattr(provider, "send", send)
    app = api.create_assistant_app(c.authority, c.h.composition.verifier, provider=provider)
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": private}], "stream": True}
    identifier = digest(body)
    c.authority.reserve_model(opened.session, aw.Reservation(
        request_id=identifier, request_sha256=identifier), opened.agent)
    if condition == "state_not_running":
        c.authority.stopped(opened.session, aw.TurnStopped(request_id="turn-stopped", reason="sdk_failed"))
    elif condition == "authority_gate":
        cancel()
    elif condition == "execution_changed":
        entry = c.authority.model_session

        def changed(*args, **kwargs):
            current = entry(*args, **kwargs)
            return current.model_copy(update={"execution": current.execution.model_copy(
                update={"generation": current.execution.generation + 1})})

        monkeypatch.setattr(c.authority, "model_session", changed)
    headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": opened.agent.oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",)), api.TICKET_HEADER: opened.ticket.get_secret_value()}
    path = f"/behavior/v1/bindings/{opened.binding.binding_id}/assistant/sessions/{opened.assignment.session_id}/model"
    with caplog.at_level(logging.WARNING, logger="shared_context.behavior_assistant_api"), TestClient(
            app, backend_options={"loop_factory": lambda: loop}) as client:
        response = client.post(path, json={"request_id": identifier, "body": body}, headers=headers)
    diagnostics = [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
                   if record.getMessage().startswith("ASSISTANT_MODEL_FENCE ")]
    intent = key("model-intent", opened.assignment.session_id, identifier)
    consumed = c.h.runtime.store.read_snapshot((intent,))[intent].payload["data"]["consumed"]
    if condition in {"unchanged", "after_consume"}:
        assert response.status_code == 200 and sent == [body] and diagnostics == [] and consumed is True
    else:
        assert response.status_code == 403 and sent == [] and consumed is False
        assert response.json()["error"]["code"] == "BEHAVIOR_ACCESS_DENIED"
        assert diagnostics == [{"condition": "authority_gate", "phase": "model_consume",
                                "work_id": opened.session.execution.work_id}]
        raw = json.dumps(diagnostics)
        assert private not in raw and opened.assignment.session_id not in raw
        assert opened.ticket.get_secret_value() not in raw and headers["Authorization"] not in raw
        assert "condition" not in response.text
    assert c.sessions.calls == []
