"""The manifest resource tool is opt-in and stays behind the original signed authority."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from sdk_checkpoint_local import sse
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_server import _CurrentAssistantProvider
from shared_context.errors import Problem
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


def test_model_gateway_does_not_accept_resource_tool_without_explicit_profile_capability():
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions", object())
    body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Read data."}],
            "tools": [{"type": "function", "function": {"name": "read_skill_resource", "parameters": {}}}]}
    with pytest.raises(Problem) as denied:
        gateway.validate(body, text_reply=True, model="gpt-4.1-mini", skill_access=True)
    assert denied.value.body["error"]["code"] == "ASSISTANT_TOOLS_DENIED"
    gateway.validate(body, text_reply=True, model="gpt-4.1-mini", skill_access=True, skill_resource_access=True)


def test_production_provider_forwards_existing_work_memory_capability_without_weakening_last_gate():
    sent, checks = [], []
    def provider(request):
        sent.append(json.loads(request.content))
        return sse(request, {"content": "Existing work memory capability."}, 1)
    composition = SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
        provider_endpoint="https://provider.invalid/openai/v1/chat/completions")))
    gateway = _CurrentAssistantProvider(
        composition, SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_MEMORY_PROVIDER")),
        transport=httpx.MockTransport(provider),
    )
    body = {"model": "gpt-4.1-mini", "stream": True,
            "messages": [{"role": "user", "content": "Read reviewed memory."}],
            "tools": [{"type": "function", "function": {"name": "read_work_memory", "parameters": {}}}]}
    async def current():
        checks.append(True)
    async def run():
        try:
            response = await gateway.send(body, before_send=current, text_reply=True, model="gpt-4.1-mini",
                                          work_memory_access=True)
            await response.aread()
        finally:
            await gateway.aclose()
    asyncio.run(run())
    assert len(sent) == 1 and checks
