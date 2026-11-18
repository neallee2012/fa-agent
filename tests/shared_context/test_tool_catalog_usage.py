"""TOOL-01 declared tools match handlers and disabled capabilities are refused; TOOL-07 per-request usage facts."""

from dataclasses import replace
import json
import logging
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_instruction_cost import usage_facts
from assistant_api_helpers import assistant_api
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

