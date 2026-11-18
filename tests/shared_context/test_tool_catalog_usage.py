"""TOOL-01 declared tools match handlers and disabled capabilities are refused; TOOL-07 per-request usage facts."""

from dataclasses import replace
import json
import logging
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_hosted import TICKET_HEADER
from shared_context.behavior_instruction_cost import usage_facts
from test_behavior_assistant import assistant
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


URL = "https://provider.invalid/openai/v1/chat/completions"


def stream(*chunks):
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


def test_usage_facts_report_tokens_cache_and_tool_calls_without_raising():
    raw = stream(
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "x"}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}},
                                                            {"index": 1, "id": "b"}]}}]},
        {"choices": [], "usage": {"prompt_tokens": 1500, "completion_tokens": 100,
                                  "prompt_tokens_details": {"cached_tokens": 1024}}},
    )
    assert usage_facts(raw) == {"prompt_tokens": 1500, "completion_tokens": 100, "cached_tokens": 1024,
                                "tool_calls": 2}
    assert usage_facts(stream({"choices": [{"index": 0, "delta": {"content": "hi"}}]})) == {
        "prompt_tokens": None, "completion_tokens": None, "cached_tokens": None, "tool_calls": 0}
    assert usage_facts(b"data: {not json\n\ndata: [\"x\"]\n\n") == {
        "prompt_tokens": None, "completion_tokens": None, "cached_tokens": None, "tool_calls": 0}


def test_model_route_logs_usage_facts_once_per_stream_without_content(assistant, caplog):
    from shared_context.behavior_assistant_wire import InstructionSubmit, ModelRequest, Reservation
    from shared_context.behavior_wire import Command
    from shared_context.source_materials import digest
    from work_policy_lifecycle_helpers import enabled as policy_enabled
    h, control, human, _ = policy_enabled(assistant)
    upstream = stream({"choices": [{"index": 0, "delta": {"content": "PRIVATE-VISIBLE-TEXT"}}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 2,
                                                "prompt_tokens_details": {"cached_tokens": 32}}})
    app = create_assistant_app(h.assistant, h.verifier, provider=AssistantProvider(
        URL, SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE")),
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, request=request, headers={"content-type": "text/event-stream"}, content=upstream))))
    binding, _ = control.submit(human, InstructionSubmit(request_id="usage-stream", content="OFFLINE", model="gpt-4.1-mini"))
    assigned = []
    h.workers.adapter = replace(h.workers.adapter, start=lambda start: assigned.append(h.assistant.issue(start)))
    h.workers.start(binding.job_id, binding.owner)
    assignment, ticket = assigned[0]
    path = f"/behavior/v1/bindings/{binding.binding_id}/assistant/sessions/{assignment.session_id}/"
    headers = {**h.headers("agent"), TICKET_HEADER: ticket.get_secret_value()}
    body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "OFFLINE"}]}
    command = ModelRequest(request_id=digest(body), body=body)
    with TestClient(app) as client, caplog.at_level(logging.WARNING):
        assert client.post(path + "acquire", content=Command(request_id="sdk-start").model_dump_json(),
                           headers=headers).status_code == 200
        assert client.post(path + "reserve-model", content=Reservation(
            request_id=command.request_id, request_sha256=command.request_id).model_dump_json(),
            headers=headers).status_code == 200
        response = client.post(path + "model", content=command.model_dump_json(), headers=headers)
        assert response.status_code == 200 and b"[DONE]" in response.content
    logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("FA_MODEL_USAGE ")]
    assert len(logged) == 1 and "PRIVATE-VISIBLE-TEXT" not in logged[0]
    assert json.loads(logged[0].split(" ", 1)[1]) == {
        "job_id": binding.job_id, "session_id": assignment.session_id, "request_sha256": command.request_id,
        "prompt_tokens": 40, "completion_tokens": 2, "cached_tokens": 32, "tool_calls": 0}
