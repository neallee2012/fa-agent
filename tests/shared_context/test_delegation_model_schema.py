"""The model sees exactly the task arguments the original delegation gate accepts."""

import asyncio
from types import SimpleNamespace

import httpx
import jsonschema
import pytest

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_delegation_runtime import DelegationRuntime
from shared_context.behavior_delegation_wire import DelegationTask
from shared_context.m2_copilot import _ModelGateway
from test_m2_copilot import ScriptedEndpoint


SDK_TASK_SCHEMA = {
    "type": "object", "properties": {
        "agent_type": {"type": "string", "enum": ["task", "general-purpose", "fa-delegate-v1"]},
        "mode": {"type": "string", "enum": ["sync", "background"]},
        "name": {"type": "string"}, "description": {"type": "string"}, "prompt": {"type": "string"},
        "model": {"type": "string"}, "reasoning_effort": {"type": "string"}, "context_tier": {"type": "string"},
    }, "required": ["name", "prompt", "agent_type", "description"],
}


@pytest.mark.parametrize("enabled", [False, True])
def test_model_transport_narrows_only_captured_delegate_task_schema(enabled):
    async def run():
        endpoint = ScriptedEndpoint([{"content": "Synthetic result."}])
        endpoint.unbounded = True
        async def reserve(_):
            pass
        async def call(*_):
            pytest.fail("Model tool declarations cannot spawn or reserve a child")
        async def current():
            return True
        bridge = DelegationRuntime("job", "sdk", call, current) if enabled else None
        async with httpx.AsyncClient(transport=endpoint.transport) as client:
            boundary = AssistantModelBoundary(reserve, sender=client.send, text_reply=True, streaming=True)
            gateway = _ModelGateway(httpx.URL(endpoint.expected_url), client, lambda _: None, boundary=boundary,
                                    instruction=True, delegation_runtime=bridge)
            original = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Delegate."}],
                "tools": [{"type": "function", "function": {"name": "task", "parameters": SDK_TASK_SCHEMA}},
                          {"type": "function", "function": {"name": "read_context", "parameters": {"type": "object"}}}]}
            request = httpx.Request("POST", endpoint.expected_url, json=original,
                                   headers={"authorization": "Bearer " + endpoint.token()})
            response = await gateway.send_request(request, SimpleNamespace(
                request_id="scripted-request", session_id="sdk", agent_id="sdk", parent_agent_id=None))
            await response.aread()
        sent = endpoint.requests[0]
        schema = sent["tools"][0]["function"]["parameters"]
        assert schema == (DelegationTask.model_json_schema() if enabled else SDK_TASK_SCHEMA)
        assert sent["tools"][1] == original["tools"][1]
        if enabled:
            valid = {"agent_type": "fa-delegate-v1", "mode": "sync", "name": "child",
                     "description": "Synthetic comparison", "prompt": "Only supplied synthetic material."}
            jsonschema.validate(valid, schema)
            for changes in ({"agent_type": "task"}, {"mode": "background"}, {"model": "gpt-4.1-mini"}):
                with pytest.raises(jsonschema.ValidationError):
                    jsonschema.validate({**valid, **changes}, schema)
    asyncio.run(run())
