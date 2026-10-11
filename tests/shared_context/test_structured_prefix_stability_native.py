"""Observe the existing structured-work prefix across a resumed and a new job."""

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest
from copilot import CopilotClient
from fastapi.testclient import TestClient

from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import AssistantSubmit
from shared_context.behavior_work_policy_api import install_work_policy_routes
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant, call
from test_behavior_api import METRICS
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled


@pytest.mark.parametrize("legacy_resume", [True, False], ids=["legacy-resume", "steady-independent"])
def test_structured_prefix_remains_fixed_in_supported_work_lifecycles(
    assistant, monkeypatch, legacy_resume,
):
    h = assistant if legacy_resume else enabled(assistant)[0]
    prefix_sha = "68daaf7242fa325f8bfb107c4cfc7bb44e065b9eed93d91780b3f402fb3b6f71"
    source_canary = hashlib.sha256(METRICS).hexdigest()
    tool_canary = "fa_call_1"
    first_responses = [call("read_context", {}, 1), {}] if legacy_resume else [
        call("read_context", {}, 1),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 2),
        call("finish_task", {}, 3),
    ]
    # The fixed structured boundary canonicalizes provider tool IDs.
    first_responses[0]["tool_calls"][0]["id"] = "STRUCTURED_PROVIDER_TOOL_CANARY"
    responses = iter([*first_responses, call("read_context", {}, 4),
                      call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 5),
                      call("finish_task", {}, 6)])
    requests, sessions = [], []
    create = CopilotClient.create_session
    resume = CopilotClient.resume_session

    def observe(session, options, resumed):
        message = options["system_message"]
        assert message["mode"] == "replace"
        assert len(message["content"].encode("utf-8")) == 264
        assert hashlib.sha256(message["content"].encode("utf-8")).hexdigest() == prefix_sha
        sessions.append((session.session_id, resumed))

    async def create_observed(client, **options):
        result = await create(client, **options)
        observe(result, options, False)
        return result

    async def resume_observed(client, session_id, **options):
        result = await resume(client, session_id, **options)
        observe(result, options, True)
        return result

    monkeypatch.setattr(CopilotClient, "create_session", create_observed)
    monkeypatch.setattr(CopilotClient, "resume_session", resume_observed)

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        body = json.loads(request.content)
        system = [message["content"] for message in body["messages"] if message["role"] == "system"]
        assert len(system) == 1 and isinstance(system[0], str)
        assert hashlib.sha256(system[0].encode("utf-8")).hexdigest() == prefix_sha
        assert len(system[0].encode("utf-8")) == 264
        assert source_canary not in system[0] and tool_canary not in system[0]
        if len(requests) == 1:
            results = [message for message in body["messages"] if message["role"] == "tool"]
            assert results[-1]["tool_call_id"] == tool_canary
            assert source_canary in json.dumps(results[-1]["content"])
        assert {tool["function"]["name"] for tool in body["tools"]} == {
            "read_context", "save_result", "finish_task", "execute_operation",
        }
        tool_hash = hashlib.sha256(json.dumps(
            body["tools"], sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if requests:
            assert tool_hash == requests[0]["tool_catalog_sha256"]
        requests.append({
            "ordinal": len(requests) + 1, "prefix_sha256": prefix_sha,
            "prefix_utf8_bytes": 264, "tool_catalog_sha256": tool_hash,
        })
        return sse(request, next(responses), len(requests))

    gateway = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_STRUCTURED_PREFIX")),
        transport=httpx.MockTransport(provider),
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    if not legacy_resume:
        install_work_policy_routes(h.app, SimpleNamespace(
            runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
            require_current_native_evidence=lambda: None,
        ))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    h.executor.model_transport = None

    def submit(request_id):
        route = (f"/behavior/v1/bindings/{h.binding.binding_id}/assistant/submit"
                 if legacy_resume else "/work-policy/v1/submit")
        result = h.client.post(
            route,
            content=AssistantSubmit(request_id=request_id).model_dump_json(), headers=h.headers(),
        )
        assert result.status_code == 200, result.text
        return ({"binding_id": h.binding.binding_id} if legacy_resume else result.json())

    with TemporaryDirectory(prefix="fa-struct-") as directory:
        h.executor.root = Path(directory) / "first"
        first = submit("structured-prefix-first")
        first_result = h.runtime.controller.step(first["binding_id"])
        assert first_result.state == ("waiting_dependency" if legacy_resume else "succeeded")
        assert first_result.result_forwarded is not legacy_resume
        assert len(requests) == (2 if legacy_resume else 3)
        h.time += timedelta(seconds=1)
        h.executor.root = Path(directory) / "second"
        second = first if legacy_resume else submit("structured-prefix-second")
        if not legacy_resume:
            assert second["binding_id"] != first["binding_id"]
        finished = h.runtime.controller.step(second["binding_id"])
        assert finished.state == "succeeded" and finished.result_forwarded
        assert not finished.obligation_fulfilled

    assert len(requests) == (5 if legacy_resume else 6)
    assert [resumed for _, resumed in sessions] == [False, legacy_resume]
    assert (sessions[0][0] == sessions[1][0]) is legacy_resume
    if output := os.environ.get("FA_STRUCTURED_PREFIX_EVIDENCE_DIR"):
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        name = "legacy-resume" if legacy_resume else "steady-independent"
        (destination / (name + ".json")).write_text(json.dumps({
            "evidence": "Existing structured-work HTTP authority and actual SDK with offline provider.",
            "work_lifecycle": name,
            "prefix_sha256": prefix_sha, "prefix_utf8_bytes": 264,
            "requests": requests, "sdk_session_openings": 2,
            "paused_then_resumed_original_session": legacy_resume,
            "independent_work_used_new_session": not legacy_resume,
            "last_result_forwarded": True,
            "exactly_one_system_message": True,
            "source_tool_canaries_observed_only_as_data": True,
            "final_work_completion_is_not_client_ack": True,
            "paid_model_requests": 0, "cloud_requests": 0,
            "limitations": [
                "Structured default prefix only, not arbitrary custom policy content or live profile switching.",
                "Steady-work unfinished-job restart remains fenced; legacy resume is a different existing policy.",
                "No provider cache, model-quality, deployment or human-read claim.",
                "This probe does not collect OS process exit codes or stop-return counts.",
            ],
        }, indent=2) + "\n", encoding="utf-8")
