"""Real authority/HTTP path after a tool result; provider replies are explicit offline fixtures."""
from dataclasses import replace
from types import SimpleNamespace
import json

from fastapi.testclient import TestClient

from shared_context.behavior_assistant_api import create_assistant_app, TICKET_HEADER
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit, Reservation, ModelRequest
from shared_context.behavior_wire import Command
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled


def test_instruction_can_continue_model_after_successful_read_context(assistant):
    h, control, human, _ = enabled(assistant)
    h.endpoint.expected_url = "https://provider.invalid/openai/v1/chat/completions"
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([
        {"tool_calls": [{"index": 0, "id": "read-call", "type": "function",
                         "function": {"name": "read_context", "arguments": "{}"}}]},
        {"content": "OFFLINE final answer after the tool"},
    ])
    provider = AssistantProvider(h.endpoint.expected_url, SimpleNamespace(
        get_token=lambda _: SimpleNamespace(token="OFFLINE")), transport=h.endpoint.transport)
    h.app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    h.client = TestClient(h.app)
    binding, _ = control.submit(human, InstructionSubmit(request_id="context-then-answer",
        content="Read the permitted source and then answer.", model="gpt-4.1-mini"))
    issued = []
    h.workers.adapter = replace(h.workers.adapter, start=lambda start: issued.append(h.assistant.issue(start)))
    h.workers.start(binding.job_id, binding.owner)
    assignment, ticket = issued[0]
    root = f"/behavior/v1/bindings/{binding.binding_id}/assistant/sessions/{assignment.session_id}/"
    headers = {**h.headers("agent"), TICKET_HEADER: ticket.get_secret_value()}
    assert h.client.post(root+"acquire", content=Command(request_id="sdk-start").model_dump_json(),
                         headers=headers).status_code == 200
    messages = [{"role": "user", "content": "Read the permitted source and then answer."}]
    def model():
        body = {"model": "gpt-4.1-mini", "messages": messages, "stream": True}
        sha = digest(body)
        reserve = h.client.post(root+"reserve-model",
            content=Reservation(request_id=sha, request_sha256=sha).model_dump_json(), headers=headers)
        assert reserve.status_code == 200, reserve.text
        response = h.client.post(root+"model",
            content=ModelRequest(request_id=sha, body=body).model_dump_json(), headers=headers)
        assert response.status_code == 200, response.text
        return response
    model()
    context = h.client.post(root+"context", content=Command(request_id="current-context").model_dump_json(),
                            headers=headers)
    assert context.status_code == 200, context.text
    messages.extend([
        {"role": "assistant", "tool_calls": [{"id": "read-call", "type": "function",
            "function": {"name": "read_context", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "read-call", "content": context.text},
    ])
    response = model()
    assert "OFFLINE final answer after the tool" in response.text
    assert len(h.endpoint.requests) == 2
