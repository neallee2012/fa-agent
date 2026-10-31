from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A, TENANT
from legacy_m2_m4.api import create_app
from shared_context.errors import Problem
from legacy_m2_m4.m2_gateway import M2Gateway, M2Settings, is_model_upload
from shared_context.m4_fixtures import PROMPT_TOKENS_LIMIT, SUITE
from test_m2_gateway import BODY, BUDGET, CHAT, Credential, NATIVE, PROJECT
from test_m4_api import BASE, SETTINGS, m3, m4


def gateway(service, handler, *, milestone="m4", settings=None):
    return M2Gateway(
        service, settings or SETTINGS.model_settings, PROJECT, Credential(),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), milestone=milestone,
    )


def app(service, selected, **kwargs):
    return create_app(
        service.context, lambda: A, artifact_service=service.artifacts,
        verified_artifact_identity_dependency=lambda: A, m4_service=service, m4_model_gateway=selected, **kwargs,
    )


def test_m4_counter_is_independent_of_exhausted_m2_and_immutable_m3(m4):
    service, repository, _, _, _ = m4
    forwarded = []

    def send(request):
        forwarded.append(request)
        return httpx.Response(200, json=NATIVE, headers={"x-request-id": "native-m4"})

    old_base = gateway(service.context, send, milestone="m2",
                       settings=M2Settings("m2-prior", "gpt-4.1-mini", 120))
    for _ in range(120):
        old_base.reserve(A)
    asyncio.run(old_base.aclose())
    m2 = gateway(service.context, send, milestone="m2",
                 settings=M2Settings("m2-prior", "gpt-4.1-mini", 120, 40, "prior-amendment"))
    for _ in range(40):
        m2.reserve(A)
    m3_gateway = gateway(service.context, send, milestone="m3",
                         settings=M2Settings("m3-prior", "gpt-4.1-mini", 80))
    for _ in range(59):
        m3_gateway.reserve(A)
    preserved = copy.deepcopy(repository.data)
    m4_gateway = gateway(service.context, send)
    with TestClient(app(service, m4_gateway, model_gateway=m2, m3_model_gateway=m3_gateway)) as client:
        assert client.post(CHAT, json=BODY).status_code == 429
        assert client.get(BUDGET).json()["attempts"] == 160
        assert client.get("/m3" + BUDGET).json()["attempts"] == 59
        assert client.get("/m3" + BUDGET).json()["remaining"] == 21
        for _ in range(2):
            response = client.post("/m4" + CHAT, json=BODY)
            assert response.status_code == 200 and response.json() == NATIVE
            assert response.headers["x-m4-attempt-id"]
            assert "x-m2-attempt-id" not in response.headers and "x-m3-attempt-id" not in response.headers
            assert response.headers["x-should-retry"] == "false"
            assert response.headers["cache-control"] == "no-store"
        budget = client.get("/m4" + BUDGET).json()
        assert budget["run_id"] == SUITE and budget["attempts"] == 2 and budget["remaining"] == 58
        assert budget["delivery_state"] == "unknown"
        assert budget["amendment"] is None and budget["original_max_requests"] == budget["max_requests"] == 60
        assert all(record["usage"] == NATIVE["usage"] for record in budget["records"])
        for _ in range(58):
            m4_gateway.reserve(A)
        assert client.post("/m4" + CHAT, json=BODY).status_code == 429
        assert client.get("/m4" + BUDGET).json()["remaining"] == 0
    assert len(forwarded) == 2
    assert m4_gateway.partition == "m4-" + TENANT
    for key, value in preserved.items():
        assert repository.data[key] == value
    reopened = gateway(service.context, send)
    assert reopened.budget(A)["attempts"] == 60
    with pytest.raises(Problem) as exc:
        reopened.reserve(A)
    assert exc.value.status == 429
    asyncio.run(reopened.aclose())


@pytest.mark.parametrize("settings", [
    M2Settings("m4-context-02", "gpt-4.1-mini", 60),
    M2Settings("m3-prior", "gpt-4.1-mini", 60),
    M2Settings(SUITE, "gpt-4.1-mini", 61),
    M2Settings(SUITE, "gpt-4.1-mini", 59),
    M2Settings(SUITE, "gpt-4.1-mini", 60, 1, "amendment"),
])
def test_m4_rejects_new_run_changed_budget_or_allowance_amendment(m4, settings):
    with pytest.raises(ValueError, match="M4 requires"):
        M2Gateway(m4[0].context, settings, PROJECT, Credential(), milestone="m4")


def test_gateway_mapping_never_relabels_unknown_milestones_or_legacy_routes(m4):
    for milestone, prefix in (("m2", ""), ("m3", "/m3"), ("m4", "/m4")):
        assert is_model_upload({"method": "POST", "path": prefix + CHAT}, milestone=milestone)
        for other in ("", "/m3", "/m4"):
            if other != prefix:
                assert not is_model_upload({"method": "POST", "path": other + CHAT}, milestone=milestone)
    with pytest.raises(ValueError):
        is_model_upload({"method": "POST", "path": CHAT}, milestone="unknown")
    with pytest.raises(ValueError):
        M2Gateway(m4[0].context, SETTINGS.model_settings, PROJECT, Credential(), milestone="unknown")
    m4_gateway = gateway(m4[0].context, lambda _: httpx.Response(200, json=NATIVE))
    try:
        with pytest.raises(ValueError, match="reserved for M2"):
            create_app(m4[0].context, lambda: A, model_gateway=m4_gateway)
        with pytest.raises(ValueError, match="configured together"):
            create_app(m4[0].context, lambda: A, m4_model_gateway=m4_gateway)
        with pytest.raises(ValueError, match="configured together"):
            create_app(m4[0].context, lambda: A, m4_service=m4[0])
    finally:
        asyncio.run(m4_gateway.aclose())


def test_m4_disabled_by_default_preserves_existing_routes_and_synthetic_memory(system):
    _, repository, memory, clients = system
    client = clients[A.actor]
    assert client.get("/m4" + BUDGET).status_code == 404
    assert client.post("/m4" + CHAT, json=BODY).status_code == 404
    assert client.post(BASE + "/memory/seed", json={}).status_code == 404
    assert client.get(BASE + f"/works/{SUITE}-short/context").status_code == 404
    assert client.post("/teams/team-one/memory/team", json={"marker": "legacy", "value": "amber"}).status_code == 202
    assert memory.calls[0][0:2] == ("start", "team:team-one")
    assert not any(partition.startswith("m4-") for partition, _ in repository.data)


@pytest.mark.parametrize("usage,code,status", [
    (None, "M4_NATIVE_USAGE_REQUIRED", 502),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "M4_NATIVE_USAGE_REQUIRED", 502),
    ({"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 99}, "M4_NATIVE_USAGE_REQUIRED", 502),
    ({"prompt_tokens": PROMPT_TOKENS_LIMIT + 1, "completion_tokens": 1, "total_tokens": PROMPT_TOKENS_LIMIT + 2},
     "M4_INPUT_TOKEN_LIMIT", 413),
])
def test_m4_requires_native_usage_and_counts_response_before_rejecting_limit(m4, usage, code, status):
    forwarded = []

    def send(request):
        forwarded.append(request)
        result = copy.deepcopy(NATIVE)
        if usage is None:
            del result["usage"]
        else:
            result["usage"] = usage
        return httpx.Response(200, json=result, headers={"x-request-id": "received-not-delivered"})

    selected = gateway(m4[0].context, send)
    with TestClient(app(m4[0], selected)) as client:
        response = client.post("/m4" + CHAT, json=BODY)
        assert response.status_code == status and response.json()["error"]["code"] == code
        budget = client.get("/m4" + BUDGET).json()
        assert budget["attempts"] == 1 and budget["remaining"] == 59
        record = budget["records"][0]
        assert record["state"] == "upstream_response" and record["upstream_status"] == 200
        assert record["usage"] == usage and record["request_id"] == "received-not-delivered"
        assert record["attempt_id"] == response.json()["error"]["attempt_id"]
        assert budget["delivery_state"] == "unknown"
    assert len(forwarded) == 1


def test_m4_native_sse_usage_is_preserved_and_boundaries_are_inclusive(m4):
    native = (
        'data: {"id":"native-m4","object":"chat.completion.chunk","model":"gpt-4.1-mini",'
        '"choices":[{"index":0,"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":8192,"completion_tokens":1,"total_tokens":8193}}\n\n'
        'data: [DONE]\n\n'
    ).encode()
    observed = []

    def send(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, content=native, headers={"content-type": "text/event-stream"})

    selected = gateway(m4[0].context, send)
    with TestClient(app(m4[0], selected)) as client:
        response = client.post("/m4" + CHAT, json={**BODY, "stream": True})
        assert response.status_code == 200 and response.content == native
        assert client.get("/m4" + BUDGET).json()["records"][0]["usage"]["prompt_tokens"] == 8192
    assert observed[0]["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize("status", [400, 403, 429, 503])
def test_m4_native_errors_preserve_http_status_and_raw_diagnostic(m4, status):
    raw = b'{"error":{"code":"NativeSyntheticError","message":"unchanged native failure"}}'
    selected = gateway(m4[0].context, lambda _: httpx.Response(
        status, content=raw, headers={"content-type": "application/json", "x-ms-request-id": "failed-native"},
    ))
    with TestClient(app(m4[0], selected)) as client:
        response = client.post("/m4" + CHAT, json=BODY)
        assert response.status_code == status and response.content == raw
        assert response.headers["x-m4-attempt-id"]
        assert client.get("/m4" + BUDGET).json()["records"][0]["upstream_status"] == status


def test_m4_unknown_transport_is_reserved_once_and_never_automatically_retried(m4):
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout")

    selected = gateway(m4[0].context, fail)
    with TestClient(app(m4[0], selected)) as client:
        assert client.post("/m4" + CHAT, json=BODY).status_code == 504
        budget = client.get("/m4" + BUDGET).json()
        assert budget["attempts"] == 1 and budget["records"][0]["state"] == "unknown"
    assert len(calls) == 1


def test_m4_gateway_authenticates_before_consuming_body(m4):
    calls = []

    def denied():
        raise Problem(401, "TOKEN_REQUIRED", "verified identity required")

    selected = gateway(m4[0].context, lambda request: calls.append(request))
    target = create_app(
        m4[0].context, denied, artifact_service=m4[0].artifacts,
        verified_artifact_identity_dependency=lambda: A, m4_service=m4[0], m4_model_gateway=selected,
    )
    responses = []

    async def receive():
        raise AssertionError("body read before authentication")

    async def send(message):
        responses.append(message)

    try:
        asyncio.run(target({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "https", "path": "/m4" + CHAT,
            "raw_path": ("/m4" + CHAT).encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-length", b"99999999"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1), "server": ("test", 443),
        }, receive, send))
    finally:
        asyncio.run(selected.aclose())
    assert responses[0]["status"] == 401 and calls == [] and selected.credential.scopes == []
    assert (b"x-should-retry", b"false") in responses[0]["headers"]


def test_all_gateways_and_owned_resources_close_even_when_an_earlier_close_fails(m4):
    service = m4[0]
    selected = gateway(service.context, lambda _: httpx.Response(200, json=NATIVE))
    old = gateway(service.context, lambda _: httpx.Response(200, json=NATIVE), milestone="m3",
                  settings=M2Settings("m3-close", "gpt-4.1-mini", 80))
    asyncio.run(old.aclose())
    old.aclose = AsyncMock(side_effect=RuntimeError("synthetic close failure"))
    owned_close = Mock()
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        with TestClient(app(service, selected, m3_model_gateway=old, close=owned_close)):
            pass
    assert selected.http.is_closed
    old.aclose.assert_awaited_once()
    owned_close.assert_called_once()
