"""ADR-0007 §6: the Hosted agent's own private Foundry memory (search before, update after a human turn).

Backend-agnostic recall and write behavior runs per backend in test_memory_backend_contract.py (ADR-0011 §8); this
file keeps the Foundry configuration, its request shapes and the old (AssistantSession) lane.

Offline: the credential, memory service and API are local doubles; no network, runtime process or model call.
"""

import asyncio
import json
import logging
import socket

import httpx
import pytest

from shared_context import behavior_private_memory as memory

ENDPOINT = "https://fa-acct.services.ai.azure.com/api/projects/fa-project"
STORE = "fa-memory-store"
AGENT = "fa-agent-cc96"
TOKEN = "memory-secret-token"
ENV = {memory.STORE_ENV: STORE, memory.ENDPOINT_ENV: ENDPOINT, memory.AGENT_ENV: AGENT}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import _socket

    def forbidden(sock, address):
        # asyncio's own event-loop self-pipe on Windows uses loopback; any other connection fails.
        if address[0] not in {"127.0.0.1", "::1"}:
            pytest.fail("private memory tests must not reach the network")
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def handler(sent, *, search=None, status=None, error=None):
    def handle(request):
        sent.append(request)
        if error is not None:
            raise error
        if request.url.path.endswith(":update_memories"):
            return httpx.Response(status or 202, json={"update_id": "u1", "status": "queued"})
        if status is not None:
            return httpx.Response(status, json={"error": {"code": "denied", "message": TOKEN}})
        return httpx.Response(200, json=search if search is not None else {
            "search_id": "s1", "memories": [
                {"memory_item": {"memory_id": "m1", "content": "Prefers  short\nanswers", "kind": "user_profile"}},
                {"memory_item": {"memory_id": "m2", "content": "Works on FA", "kind": "user_profile"}}],
            "usage": {"embedding_tokens": 7, "input_tokens": 0, "output_tokens": 0, "total_tokens": 7}})
    return handle


def service(sent, **kwargs):
    return httpx.MockTransport(handler(sent, **kwargs))


def private(sent, **kwargs):
    return memory.PrivateMemory.from_environment(lambda: TOKEN, environ=ENV, transport=service(sent, **kwargs))


def test_off_unless_a_store_is_configured_and_only_to_the_project_endpoint(caplog):
    assert memory.PrivateMemory.from_environment(lambda: TOKEN, environ={}) is None
    with caplog.at_level(logging.WARNING):
        for bad in ({memory.ENDPOINT_ENV: "https://evil.example/api/projects/p"},
                    {memory.STORE_ENV: "bad/store"}, {memory.AGENT_ENV: ""}):
            assert memory.PrivateMemory.from_environment(lambda: TOKEN, environ={**ENV, **bad}) is None
    assert caplog.text.count("reason=config_invalid") == 3
    configured = memory.PrivateMemory.from_environment(lambda: TOKEN, environ=ENV)
    # One scope per Agent, in the characters the asynchronous extractor accepts.
    assert configured.scope == memory.scope_for(AGENT) != memory.scope_for("fa-agent-other")
    assert configured.scope.startswith("fap1-") and configured.scope.replace("-", "").isalnum()


def test_the_backend_is_foundry_unless_set_and_cosmos_needs_the_api_and_its_extraction_model(caplog):
    """ADR-0011: ``cosmos`` reaches memory only through the native lane's API connection with an extraction model."""
    from shared_context.cosmos_private_memory import CosmosPrivateMemory

    api = ("https://api.offline.invalid", lambda: "OFFLINE")
    explicit = memory.PrivateMemory.from_environment(lambda: TOKEN, environ={**ENV, memory.BACKEND_ENV: "foundry"},
                                                     api=api)
    assert explicit.scope == memory.scope_for(AGENT) and explicit.store == STORE and explicit.chains_updates
    model = {memory.EXTRACTION_MODEL_ENV: "gpt-4.1-mini"}
    cosmos = memory.PrivateMemory.from_environment(
        lambda: TOKEN, environ={**ENV, memory.BACKEND_ENV: "cosmos", **model}, api=api)
    assert type(cosmos) is CosmosPrivateMemory and not cosmos.chains_updates
    assert (cosmos.scope, cosmos.store, cosmos.model, cosmos.api_url) == (
        memory.scope_for(AGENT), None, "gpt-4.1-mini", "https://api.offline.invalid")
    with caplog.at_level(logging.WARNING):
        for backend, api_value, extra, reason in (
                ("cosmos", None, model, "api_unavailable"), ("azure-search", None, model, "config_invalid"),
                ("cosmos", api, {memory.EXTRACTION_MODEL_ENV: "bad model"}, "config_invalid"),
                ("cosmos", api, {**model, memory.ENDPOINT_ENV: "https://example.com"}, "config_invalid"),
                ("cosmos", api, {}, None)):
            caplog.clear()
            assert memory.PrivateMemory.from_environment(
                lambda: TOKEN, environ={**ENV, memory.BACKEND_ENV: backend, **extra}, api=api_value) is None
            assert caplog.text.count("FA_PRIVATE_MEMORY_INACTIVE reason=") == (reason is not None)
            assert reason is None or f"FA_PRIVATE_MEMORY_INACTIVE reason={reason}" in caplog.text
    assert [memory.unavailable_reason(environ) for environ in (
        {}, {memory.STORE_ENV: STORE}, {memory.BACKEND_ENV: "foundry"},
        {memory.BACKEND_ENV: "cosmos", memory.STORE_ENV: STORE}, {memory.BACKEND_ENV: "cosmos", **model},
        {memory.BACKEND_ENV: "azure-search", **model})] == [
        "not_configured", "configuration_invalid", "not_configured",
        "not_configured", "configuration_invalid", "configuration_invalid"]


def test_the_source_request_id_never_reaches_a_foundry_request():
    sent = []
    configured = private(sent)
    assert asyncio.run(configured.update_chained("I am vegetarian", None, request_id="rid-1")) == ("202", "u1")
    asyncio.run(configured.create("fat1-scope", "note", "chat_summary", event="E", request_id="rid-1"))
    assert [json.loads(request.content) for request in sent] == [
        {"scope": memory.scope_for(AGENT), "update_delay": 0, "items": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "I am vegetarian"}]}]},
        {"scope": "fat1-scope", "content": "note", "kind": "chat_summary"}]
    assert "rid-1" not in str(sent[0].url) + str(sent[1].url)


def test_a_recall_is_the_official_search_request_and_logs_its_usage(caplog):
    sent = []
    with caplog.at_level(logging.WARNING):
        found = asyncio.run(private(sent).recall("What do I prefer?"))
    assert found == ("Prefers short answers", "Works on FA")
    (request,) = sent
    assert str(request.url) == f"{ENDPOINT}/memory_stores/{STORE}:search_memories?api-version=v1"
    assert request.headers["authorization"] == "Bearer " + TOKEN
    assert request.headers["foundry-features"] == "MemoryStores=V1Preview"
    assert json.loads(request.content) == {
        "scope": memory.scope_for(AGENT), "options": {"max_memories": 5},
        "items": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What do I prefer?"}]}]}
    assert '"embedding_tokens": 7' in caplog.text and TOKEN not in caplog.text
    # Hosted keeps only WARNING and above: the success line and its usage must survive that filter.
    assert "FA_PRIVATE_MEMORY_SEARCH status=200 memories=2" in caplog.text


def test_without_a_token_nothing_is_sent(caplog):
    sent = []

    def no_token():
        raise RuntimeError("no identity")
    configured = memory.PrivateMemory.from_environment(no_token, environ=ENV, transport=service(sent))
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(configured.recall("q")) == ()
        assert asyncio.run(configured.remember("q")) == "unsent_or_unknown"
    assert sent == [] and caplog.text.count("status=token_unavailable") == 2


@pytest.mark.parametrize("kwargs, outcome", [
    ({}, "202"), ({"status": 401}, "401"), ({"error": httpx.ReadTimeout("lost")}, "unsent_or_unknown"),
])
def test_remember_is_sent_once_and_never_retried(kwargs, outcome, caplog):
    sent = []
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(private(sent, **kwargs).remember("I am vegetarian")) == outcome
    if outcome != "unsent_or_unknown":
        assert f"FA_PRIVATE_MEMORY_UPDATE status={outcome}" in caplog.text
    (request,) = sent
    assert str(request.url) == f"{ENDPOINT}/memory_stores/{STORE}:update_memories?api-version=v1"
    assert json.loads(request.content) == {
        "scope": memory.scope_for(AGENT), "update_delay": 0,
        "items": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "I am vegetarian"}]}]}

