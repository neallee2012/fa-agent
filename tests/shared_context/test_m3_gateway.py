from __future__ import annotations

import copy
import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A
from legacy_m2_m4.api import create_app
from legacy_m2_m4.m2_gateway import M2Gateway, M2Settings
from shared_context.m2_copilot import ToolOutput, ToolSpec, run_copilot
from test_m2_gateway import BODY, CHAT, BUDGET, PROJECT, NATIVE, Credential
from test_m2_integration import RUNTIME, completion
from test_m3_artifacts import m3


def test_m3_inference_and_exhaustion_never_reset_or_charge_m2_receipts(m3):
    service, repository, _ = m3
    forwarded = []

    async def send(request):
        forwarded.append(request)
        return httpx.Response(200, json=NATIVE)

    m2 = M2Gateway(service.context, M2Settings("m2-prior", "gpt-4.1-mini", 1), PROJECT, Credential(),
                   httpx.AsyncClient(transport=httpx.MockTransport(send)))
    m3_gateway = M2Gateway(service.context, M2Settings("m3-local", "gpt-4.1-mini", 2), PROJECT, Credential(),
                           httpx.AsyncClient(transport=httpx.MockTransport(send)), milestone="m3")
    with TestClient(create_app(
        service.context, lambda: A, model_gateway=m2, artifact_service=service,
        verified_artifact_identity_dependency=lambda: A, m3_model_gateway=m3_gateway,
    )) as http:
        assert http.post(CHAT, json=BODY).status_code == 200
        prior = copy.deepcopy(repository.data[m2.partition, m2.row])
        assert http.post(CHAT, json=BODY).status_code == 429
        for _ in range(2):
            response = http.post("/m3" + CHAT, json=BODY)
            assert response.status_code == 200
            assert response.headers["x-m3-attempt-id"]
            assert "x-m2-attempt-id" not in response.headers
            assert response.headers["x-should-retry"] == "false"
        assert http.post("/m3" + CHAT, json=BODY).status_code == 429
        assert http.get(BUDGET).json()["attempts"] == 1
        assert http.get("/m3" + BUDGET).json()["attempts"] == 2
        assert http.get("/m3" + BUDGET).json()["remaining"] == 0
    assert repository.data[m2.partition, m2.row] == prior
    assert len(forwarded) == 3
    assert m2.partition != m3_gateway.partition


@pytest.mark.skipif(not RUNTIME.is_file(), reason="requires explicitly provisioned SDK runtime 1.0.83")
def test_actual_sdk_uses_only_the_separate_m3_gateway(m3, tmp_path):
    service, _, _ = m3
    observed, calls = [], []

    async def upstream(request):
        observed.append(request)
        if len(observed) == 1:
            content = completion(1, {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_m3", "type": "function",
                "function": {"name": "fa_artifact_probe", "arguments": "{}"},
            }]}, "tool_calls")
        else:
            assert len(observed) == 2
            content = completion(2, {"role": "assistant", "content": "M3 route verified"}, "stop")
        return httpx.Response(200, content=content.encode(), headers={"content-type": "text/event-stream"})

    async def artifact_probe(packet):
        calls.append(packet["tool_call_id"])
        return ToolOutput(text="Synthetic artifact route fixture; not an Azure execution.")

    async def run():
        working = tmp_path / "work"
        working.mkdir()
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_http:
            gateway = M2Gateway(service.context, M2Settings("m3-sdk-local", "gpt-4.1-mini", 3), PROJECT,
                                Credential(), upstream_http, milestone="m3")
            app = create_app(service.context, lambda: A, artifact_service=service,
                             verified_artifact_identity_dependency=lambda: A, m3_model_gateway=gateway)
            result = await run_copilot(
                state_dir=tmp_path / "sdk", working_directory=working, sdk_session_id=str(uuid4()),
                resume=False, prompt="Call the artifact probe once, then report completion.",
                tools=[ToolSpec("fa_artifact_probe", "Read a synthetic artifact.",
                                {"type": "object", "properties": {}, "additionalProperties": False}, artifact_probe)],
                api_url="https://context.example", token_provider=lambda: "synthetic-hosted-token",
                on_event=lambda _event: None, timeout_seconds=45, runtime_path=RUNTIME,
                http_transport=httpx.ASGITransport(app), milestone="m3",
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://context.example") as http:
                assert (await http.get(BUDGET)).status_code == 404
                budget = (await http.get("/m3" + BUDGET)).json()
            return result, budget

    outcome, budget = asyncio.run(run())
    assert outcome["final_message"] == "M3 route verified"
    assert calls == ["call_m3"] and budget["attempts"] == 2 and budget["remaining"] == 1
