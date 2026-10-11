from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from threading import Barrier, Lock
from unittest.mock import Mock

import httpx
import anyio
import pytest
from azure.core.credentials import AccessToken
from fastapi.testclient import TestClient

from conftest import A, B, C, AUDIENCE, TENANT, FakeMemory, InMemoryRepository
from shared_context.api import create_app
from shared_context.auth import JwtVerifier
from shared_context.errors import Problem
from shared_context.m2_gateway import M2Gateway, M2Settings
from shared_context.service import ContextService
from shared_context.storage import MAX_PAYLOAD_BYTES, Record, serialize

CHAT = "/model/v1/chat/completions"
BUDGET = "/model/v1/budget"
PROJECT = "https://synthetic.services.ai.azure.com/api/projects/poc"
MODEL = "gpt-4.1-mini"
BODY = {"model": MODEL, "messages": [{"role": "user", "content": "Say amber."}]}
NATIVE = {
    "id": "chatcmpl-native", "object": "chat.completion", "created": 1,
    "model": "gpt-4.1-mini-2025-04-14",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "amber"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
}


def png_fixture(width=1280, height=800, *, total_bytes=None):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    pixels = (b"\x00" + b"\x00" * (width * 4)) * height
    image = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels))
    end = chunk(b"IEND", b"")
    if total_bytes is not None:
        padding = total_bytes - len(image) - len(end) - 20
        assert padding >= 0
        image += chunk(b"tEXt", b"padding\x00" + b"x" * padding)
    return image + end


class Credential:
    def __init__(self):
        self.scopes = []

    def get_token(self, *scopes, **kwargs):
        self.scopes.append(scopes)
        return AccessToken("synthetic-native-token-" + str(len(self.scopes)), 9999999999)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


class CounterRepository(InMemoryRepository):
    def __init__(self):
        super().__init__()
        self.writes = []

    def create(self, team, row, payload):
        self.writes.append((team, row, deepcopy(payload), None))
        return super().create(team, row, payload)

    def replace(self, team, row, payload, etag):
        self.writes.append((team, row, deepcopy(payload), etag))
        return super().replace(team, row, payload, etag)

    def seed_legacy_entries(self, entries):
        team, row, payload, _ = self.writes[0]
        record = self.get(team, row)
        legacy = [{key: value for key, value in entry.items() if value is not None} for entry in entries]
        self.replace(team, row, {**payload, "entries": legacy}, record.etag)


@pytest.fixture
def relays(system):
    with ExitStack() as clients:
        def make(handler=None, *, limit=3, actor=A, service=None, credential=None, run_id="m2-local",
                 additional_requests=None, amendment_id=None):
            observed = []
            credential = credential or Credential()
            service = service or system[0]

            async def transport(request):
                observed.append(request)
                if handler is not None:
                    return handler(request)
                return httpx.Response(200, json=NATIVE, headers={"x-request-id": "native-request-1"})

            http = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
            settings = M2Settings(run_id, MODEL, limit, additional_requests, amendment_id)
            gateway = M2Gateway(service, settings, PROJECT, credential, http)
            client = clients.enter_context(TestClient(create_app(service, lambda: actor, model_gateway=gateway)))
            return client, observed, credential

        yield make


def legacy_counter(relays, auth, *, count=109, unknowns=True, run_id="m2-20260914-1638", repository=None):
    repository = repository if repository is not None else CounterRepository()
    service = ContextService(repository, FakeMemory(), auth)
    original, _, _ = relays(service=service, limit=120, run_id=run_id)
    assert original.post(CHAT, json=BODY).status_code == 200
    entries = []
    for index in range(count):
        unknown = unknowns and index % 4 == 0
        entries.append({
            "attempt_id": f"{index + 1:032x}", "owner_oid": A.oid if index % 2 == 0 else B.oid,
            "state": "unknown" if unknown else "upstream_response",
            "upstream_status": None if unknown else 200,
            "request_id": None if unknown else "r" * 128,
            "usage": None if unknown else {key: 10 ** 9 for key in NATIVE["usage"]},
        })
    repository.seed_legacy_entries(entries)
    assert original.get(BUDGET).json()["records"] == entries
    return service, repository, entries


def test_json_relay_preserves_native_result_and_uses_only_server_model_credentials(relays):
    client, observed, credential = relays()
    response = client.post(CHAT, json=BODY, headers={
        "Authorization": "Bearer caller-only-token", "api-key": "do-not-forward",
        "Cookie": "private=do-not-forward", "X-Upstream-URL": "https://example.invalid",
    })
    assert response.status_code == 200 and response.json() == NATIVE
    assert response.headers["x-request-id"] == "native-request-1"
    assert response.headers["cache-control"] == "no-store"
    assert len(observed) == 1
    request = observed[0]
    assert str(request.url) == PROJECT + "/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer synthetic-native-token-1"
    assert "api-key" not in request.headers and "cookie" not in request.headers
    assert "x-upstream-url" not in request.headers
    assert json.loads(request.content) == {
        **BODY, "max_tokens": 1024, "n": 1, "store": False, "parallel_tool_calls": False,
    }
    assert credential.scopes == [("https://ai.azure.com/.default",)]


def test_cold_sdk_tool_source_marker_is_bounded_and_not_forwarded(relays):
    client, observed, _ = relays()
    message = {
        "role": "tool", "tool_call_id": "call-read", "content": "saved draft",
        "__copilotMessageSource": {"kind": "tool", "name": "fa_read_file"},
    }
    assert client.post(CHAT, json={**BODY, "messages": [message]}).status_code == 200
    assert json.loads(observed[0].content)["messages"] == [
        {"role": "tool", "tool_call_id": "call-read", "content": "saved draft"},
    ]
    for marker in (
        {"kind": "system", "name": "fa_read_file"},
        {"kind": "tool", "name": "x" * 65},
        {"kind": "tool", "name": "fa_read_file", "trusted": True},
    ):
        assert client.post(CHAT, json={**BODY, "messages": [
            {**message, "__copilotMessageSource": marker},
        ]}).status_code == 422
    assert len(observed) == 1


@pytest.mark.parametrize("controls", [{}, {"parallel_tool_calls": True}, {"parallel_tool_calls": False}])
def test_outbound_model_requests_always_disable_parallel_tool_calls(relays, controls):
    client, observed, _ = relays()
    assert client.post(CHAT, json={**BODY, **controls}).status_code == 200
    assert json.loads(observed[0].content)["parallel_tool_calls"] is False


@pytest.mark.parametrize("target", ["message", "tool-schema", "unicode", "implicit-control", "true-control"])
def test_cumulative_serialized_text_and_tool_schema_budget_is_exactly_128_kib(relays, target):
    body = {
        "model": MODEL, "stream": False, "max_tokens": 1024, "n": 1, "store": False, "parallel_tool_calls": False,
        "messages": [
            {"role": "system", "content": "Follow the current screenshot."},
            {"role": "user", "content": "Inspect the desktop. " * 512},
            {"role": "assistant", "content": "Inspecting.", "tool_calls": [
                {"id": "call_inspect", "type": "function", "function": {"name": "inspect", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_inspect", "content": "Previous inspection result. " * 512},
        ],
        "tools": [{"type": "function", "function": {
            "name": "inspect", "parameters": {"type": "object", "properties": {}, "description": "Schema text. " * 512},
        }}],
    }
    container, key = (
        (body["tools"][0]["function"]["parameters"], "description") if target == "tool-schema"
        else (body["messages"][1], "content")
    )
    container[key] = ""
    remaining = 128 * 1024 - len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    container[key] = "\u754c" * (remaining // 3) + "x" * (remaining % 3) if target == "unicode" else "x" * remaining
    assert len(json.dumps(body, ensure_ascii=False).encode("utf-8")) == 128 * 1024
    incoming = dict(body)
    if target == "implicit-control":
        incoming.pop("parallel_tool_calls")
    elif target == "true-control":
        incoming["parallel_tool_calls"] = True
    client, observed, credential = relays()
    assert client.post(CHAT, json=incoming).status_code == 200
    assert json.loads(observed[0].content) == body
    container[key] += "x"
    assert client.post(CHAT, json=incoming).status_code == 422
    assert client.get(BUDGET).json()["attempts"] == 1
    assert len(observed) == len(credential.scopes) == 1


def test_attempt_budget_survives_gateway_restart_and_repeated_identical_requests(relays):
    first, first_requests, _ = relays(limit=2)
    assert first.post(CHAT, json=BODY).status_code == 200
    before_restart = first.get(BUDGET)
    assert before_restart.status_code == 200
    assert before_restart.json()["attempts"] == 1
    second, second_requests, _ = relays(limit=2)
    assert second.post(CHAT, json=BODY).status_code == 200
    exhausted = second.post(CHAT, json=BODY)
    assert exhausted.status_code == 429
    state = second.get(BUDGET).json()
    assert state["run_id"] == "m2-local"
    assert state["attempts"] == 2 and state["remaining"] == 0 and state["max_requests"] == 2
    assert len(first_requests) == len(second_requests) == 1


@pytest.mark.parametrize("changes", [
    {"model": "gpt-4.1"}, {"n": 2}, {"n": True}, {"store": True}, {"store": "false"},
    {"max_tokens": 1025}, {"max_tokens": 0}, {"max_tokens": True},
    {"max_completion_tokens": 2048}, {"max_tokens": 10, "max_completion_tokens": 10},
    {"base_url": "https://example.invalid"}, {"metadata": {"owner": "agent-b"}},
    {"messages": []}, {"messages": [{"role": "function", "content": "legacy"}]},
    {"messages": [{"role": "user", "content": 123}]},
    {"messages": [{"role": "user", "content": "hello", "owner": "agent-b"}]},
    {"messages": [{"role": "tool", "content": "result"}]},
    {"messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}]},
    {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}},
    ]}]},
    {"tools": [{"type": "web_search"}]}, {"stream": "true"},
    {"stream_options": {"include_usage": True}},
])
def test_invalid_or_unbounded_requests_do_not_consume_attempts_or_forward(relays, changes):
    client, observed, credential = relays()
    response = client.post(CHAT, json={**BODY, **changes})
    assert response.status_code == 422
    assert client.get(BUDGET).json()["attempts"] == 0
    assert observed == [] and credential.scopes == []


def test_native_sse_is_buffered_without_rewriting_chunks_and_exposes_native_usage(relays):
    native = (
        b': native heartbeat\r\n\r\n'
        b'data: {"id":"chatcmpl-native","object":"chat.completion.chunk",'
        b'"model":"gpt-4.1-mini-2025-04-14","choices":[{"index":0,'
        b'"delta":{"role":"assistant","content":"amber"},"finish_reason":null}]}\r\n\r\n'
        b'data: {"id":"chatcmpl-native","object":"chat.completion.chunk",'
        b'"model":"gpt-4.1-mini-2025-04-14","choices":[{"index":0,'
        b'"delta":{},"finish_reason":"stop"}]}\r\n\r\n'
        b'data: {"id":"chatcmpl-native","object":"chat.completion.chunk",'
        b'"model":"gpt-4.1-mini-2025-04-14","choices":[],'
        b'"usage":{"prompt_tokens":12,"completion_tokens":2,"total_tokens":14}}\r\n\r\n'
        b'data: [DONE]\r\n\r\n'
    )
    stream = Chunks(native[:43], native[43:173], native[173:])
    client, observed, _ = relays(lambda _: httpx.Response(200, stream=stream, headers={
        "Content-Type": "text/event-stream; charset=utf-8", "x-request-id": "native-sse-1",
        "x-ratelimit-remaining-requests": "9", "Set-Cookie": "do-not-reflect=secret",
        "Authorization": "do-not-reflect",
    }))
    response = client.post(CHAT, json={**BODY, "stream": True, "stream_options": {"include_usage": True}})
    assert response.status_code == 200 and response.content == native
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["x-request-id"] == "native-sse-1"
    assert response.headers["x-ratelimit-remaining-requests"] == "9"
    assert "set-cookie" not in response.headers and "authorization" not in response.headers
    assert observed[0].headers["accept"] == "text/event-stream"
    assert json.loads(observed[0].content)["stream_options"] == {"include_usage": True}
    record = client.get(BUDGET).json()["records"][0]
    assert record["state"] == "upstream_response" and record["upstream_status"] == 200
    assert record["request_id"] == "native-sse-1" and record["usage"] == NATIVE["usage"]
    assert stream.closed


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_native_http_errors_preserve_status_body_and_charge_attempts(relays, status):
    raw = b'{ "error": {"code":"NativeError","message":"synthetic native error"} }'
    client, observed, _ = relays(lambda _: httpx.Response(status, content=raw, headers={
        "content-type": "application/json", "retry-after": "7", "x-ms-request-id": "native-error-1",
    }), limit=1)
    response = client.post(CHAT, json=BODY)
    assert response.status_code == status and response.content == raw
    assert response.headers["retry-after"] == "7"
    state = client.get(BUDGET).json()
    assert state["attempts"] == 1 and state["remaining"] == 0
    assert state["records"][0]["upstream_status"] == status
    assert client.post(CHAT, json=BODY).status_code == 429
    assert len(observed) == 1


@pytest.mark.parametrize("failure,status", [
    (httpx.ReadTimeout("synthetic timeout"), 504),
    (httpx.ConnectError("synthetic connection error"), 502),
    (httpx.RemoteProtocolError("synthetic incomplete response"), 502),
])
def test_native_transport_failure_remains_unknown_and_is_never_retried(relays, failure, status):
    def fail(_):
        raise failure
    client, observed, _ = relays(fail, limit=1)
    assert client.post(CHAT, json=BODY).status_code == status
    state = client.get(BUDGET).json()
    assert state["attempts"] == 1 and state["records"][0]["state"] == "unknown"
    assert client.post(CHAT, json=BODY).status_code == 429
    assert len(observed) == 1


def test_uncertain_counter_commit_never_forwards_and_still_consumes_capacity(relays, auth):
    class LostAcknowledgement(InMemoryRepository):
        def create(self, team, row, payload):
            super().create(team, row, payload)
            raise Problem(502, "UPSTREAM_ERROR", "synthetic committed write lost its response")

    service = ContextService(LostAcknowledgement(), FakeMemory(), auth)
    client, observed, credential = relays(service=service, limit=1)
    assert client.post(CHAT, json=BODY).status_code == 502
    assert client.get(BUDGET).json()["attempts"] == 1
    restarted, after_restart, _ = relays(service=service, limit=1)
    assert restarted.post(CHAT, json=BODY).status_code == 429
    assert observed == after_restart == [] and credential.scopes == []


@pytest.mark.parametrize("fault", [
    Problem(502, "UPSTREAM_ERROR", "synthetic storage outage"),
    ValueError("synthetic malformed Table JSON"),
    Record({"unexpected": "counter data"}, 'W/"valid"'),
])
def test_counter_read_uncertainty_never_reaches_credentials_or_model(relays, auth, fault):
    class UncertainRepository(InMemoryRepository):
        def get(self, team, row):
            if isinstance(fault, Exception):
                raise fault
            return fault

    service = ContextService(UncertainRepository(), FakeMemory(), auth)
    client, observed, credential = relays(service=service)
    response = client.post(CHAT, json=BODY)
    assert response.status_code == 502
    assert observed == [] and credential.scopes == []


def test_run_configuration_cannot_raise_the_cap_after_restart(relays):
    first, _, _ = relays(limit=1)
    assert first.post(CHAT, json=BODY).status_code == 200
    changed, observed, credential = relays(limit=120)
    assert changed.post(CHAT, json=BODY).status_code == 409
    assert changed.get(BUDGET).status_code == 409
    assert observed == [] and credential.scopes == []


@pytest.mark.parametrize("actor,status", [(A, 200), (B, 200), (C, 403)])
def test_gateway_allows_only_configured_ab_actors_and_their_team(relays, actor, status):
    client, observed, _ = relays(actor=actor)
    assert client.post(CHAT, json=BODY).status_code == status
    assert client.get(BUDGET).status_code == status
    assert len(observed) == (1 if status == 200 else 0)


def test_gateway_configuration_is_all_or_nothing_and_strictly_capped():
    assert M2Settings.from_env({}) is None
    values = {"CONTEXT_M2_RUN_ID": "m2-run-1", "CONTEXT_M2_MODEL": MODEL, "CONTEXT_M2_MAX_REQUESTS": "120"}
    assert M2Settings.from_env(values) == M2Settings("m2-run-1", MODEL, 120)
    for key in values:
        with pytest.raises(ValueError, match="CONTEXT_M2"):
            M2Settings.from_env({k: v for k, v in values.items() if k != key})
    for changes in (
        {"CONTEXT_M2_RUN_ID": "../bad"}, {"CONTEXT_M2_MODEL": "gpt-4.1"},
        {"CONTEXT_M2_MAX_REQUESTS": "0"}, {"CONTEXT_M2_MAX_REQUESTS": "121"},
        {"CONTEXT_M2_MAX_REQUESTS": "1.0"}, {"CONTEXT_M2_MAX_REQUESTS": "01"},
    ):
        with pytest.raises(ValueError, match="CONTEXT_M2"):
            M2Settings.from_env({**values, **changes})


def test_operator_amendment_configuration_is_explicit_paired_and_bounded():
    base = {"CONTEXT_M2_RUN_ID": "m2-20260914-1638", "CONTEXT_M2_MODEL": MODEL, "CONTEXT_M2_MAX_REQUESTS": "120"}
    amendment = {"CONTEXT_M2_ADDITIONAL_REQUESTS": "40", "CONTEXT_M2_AMENDMENT_ID": "m2-ui-allowance"}
    configured = M2Settings.from_env({**base, **amendment})
    assert configured.max_requests == 120
    assert configured.additional_requests == 40 and configured.amendment_id == "m2-ui-allowance"
    assert M2Settings.from_env(base) == M2Settings("m2-20260914-1638", MODEL, 120)
    assert M2Settings.from_env({**base, **amendment, "CONTEXT_M2_ADDITIONAL_REQUESTS": "1"}).additional_requests == 1
    for values in (
        amendment,
        {**base, "CONTEXT_M2_ADDITIONAL_REQUESTS": "40"},
        {**base, "CONTEXT_M2_AMENDMENT_ID": "m2-ui-allowance"},
        *({**base, **amendment, "CONTEXT_M2_ADDITIONAL_REQUESTS": value}
          for value in ("", "0", "41", "01", "40.0", "-1", "+1", " 1", "1 ", "true", "1e1")),
        *({**base, **amendment, "CONTEXT_M2_AMENDMENT_ID": value}
          for value in ("", "../invalid", "with space", "id\n", "a" * 65)),
        {**base, **amendment, "CONTEXT_M2_MAX_REQUESTS": "160"},
    ):
        with pytest.raises(ValueError, match="CONTEXT_M2"):
            M2Settings.from_env(values)
    for additional, amendment_id in ((0, "id"), (41, "id"), (True, "id"), (1.0, "id"), (1, None), (None, "id")):
        with pytest.raises(ValueError, match="CONTEXT_M2"):
            M2Settings("m2-local", MODEL, 120, additional_requests=additional, amendment_id=amendment_id)


def test_one_amendment_preserves_all_109_legacy_receipts_and_survives_restart(relays, auth):
    service, repository, entries = legacy_counter(relays, auth)
    writes_before = len(repository.writes)
    options = dict(service=service, limit=120, run_id="m2-20260914-1638",
                   additional_requests=40, amendment_id="m2-ui-allowance")
    amended, observed, credential = relays(**options)
    response = amended.get(BUDGET)
    assert response.status_code == 200
    budget = response.json()
    assert budget["run_id"] == "m2-20260914-1638"
    assert budget["original_max_requests"] == 120 and budget["max_requests"] == 160
    assert budget["amendment"] == {"id": "m2-ui-allowance", "additional_requests": 40}
    assert budget["attempts"] == 109 and budget["remaining"] == 51
    assert budget["records"] == entries
    assert len(repository.writes) == writes_before + 1 and repository.writes[-1][3] is not None
    stored = repository.writes[-1][2]
    assert stored["max_requests"] == 120
    assert set(stored["entries"][1]) == {"i", "o", "s", "h", "r", "u"}
    assert set(stored["entries"][1]["u"]) == {"p", "c", "t"}
    restarted, restarted_requests, restarted_credential = relays(**options)
    assert restarted.get(BUDGET).json() == budget
    assert len(repository.writes) == writes_before + 1
    assert observed == restarted_requests == credential.scopes == restarted_credential.scopes == []
    assert "synthetic-native-token" not in response.text


def test_amended_cap_160_uses_fresh_cas_etags_and_fits_worst_case_storage(relays, auth):
    service, repository, entries = legacy_counter(relays, auth, unknowns=False, run_id="r" * 64)
    largest_usage = {key: 10 ** 9 for key in NATIVE["usage"]}
    client, observed, credential = relays(
        lambda _: httpx.Response(200, json={**NATIVE, "usage": largest_usage}, headers={"x-request-id": "r" * 128}),
        service=service, limit=120, run_id="r" * 64, additional_requests=40, amendment_id="a" * 64,
    )
    for _ in range(51):
        assert client.post(CHAT, json=BODY, headers={
            "Authorization": "Bearer synthetic-private-caller",
            "X-M2-Additional-Requests": "400", "X-M2-Amendment-Id": "ignored-header",
        }).status_code == 200
    assert client.post(CHAT, json=BODY).status_code == 429
    budget = client.get(BUDGET).json()
    assert budget["max_requests"] == budget["attempts"] == len(budget["records"]) == 160
    assert budget["original_max_requests"] == 120 and budget["remaining"] == 0
    assert budget["records"][:109] == entries
    assert len(observed) == len(credential.scopes) == 51
    stored = repository.writes[-1][2]
    size = len(serialize(stored))
    assert size < MAX_PAYLOAD_BYTES == 48 * 1024
    assert all(len(entry["r"]) == 128 and entry["u"] == {"p": 10 ** 9, "c": 10 ** 9, "t": 10 ** 9}
               for entry in stored["entries"])
    assert "synthetic-private-caller" not in json.dumps(stored)
    assert "synthetic-native-token" not in json.dumps(stored)
    assert "x-m2-additional-requests" not in observed[0].headers
    assert "x-m2-amendment-id" not in observed[0].headers
    assert "amendment" not in json.loads(observed[0].content)
    print(f"160 worst-size compact receipts: {size} bytes; storage limit: {MAX_PAYLOAD_BYTES} bytes")


@pytest.mark.parametrize("additional,amendment_id,limit", [
    (39, "m2-ui-allowance", 120), (40, "another-amendment", 120),
    (None, None, 120), (40, "m2-ui-allowance", 119),
])
def test_persisted_amendment_cannot_be_changed_removed_or_rebased(relays, auth, additional, amendment_id, limit):
    service, repository, entries = legacy_counter(relays, auth)
    approved, _, _ = relays(service=service, limit=120, run_id="m2-20260914-1638",
                           additional_requests=40, amendment_id="m2-ui-allowance")
    approved_budget = approved.get(BUDGET).json()
    writes_before = len(repository.writes)
    changed, observed, credential = relays(service=service, limit=limit, run_id="m2-20260914-1638",
                                          additional_requests=additional, amendment_id=amendment_id)
    assert changed.get(BUDGET).status_code == 409
    assert changed.post(CHAT, json=BODY).status_code == 409
    assert approved.get(BUDGET).json() == approved_budget
    assert approved_budget["records"] == entries
    assert len(repository.writes) == writes_before
    assert observed == credential.scopes == []


@pytest.mark.parametrize("fault,status,committed", [
    ("conflict", 412, False), ("uncommitted", 502, False), ("lost-response", 502, True),
    ("bad-etag", 502, True), ("bad-payload", 502, True),
])
def test_uncertain_amendment_cas_never_retries_or_replays_model(relays, auth, fault, status, committed):
    class FaultedRepository(CounterRepository):
        fault = None
        amendment_writes = 0

        def replace(self, team, row, payload, etag):
            if self.fault is None:
                return super().replace(team, row, payload, etag)
            self.amendment_writes += 1
            if self.fault == "conflict":
                raise Problem(412, "ETAG_CONFLICT", "synthetic amendment conflict")
            if self.fault == "uncommitted":
                raise Problem(502, "UPSTREAM_ERROR", "synthetic failed amendment write")
            saved = super().replace(team, row, payload, etag)
            if self.fault == "bad-etag":
                return Record(saved.payload, "*")
            if self.fault == "bad-payload":
                return Record({**saved.payload, "max_requests": 119}, saved.etag)
            raise Problem(502, "UPSTREAM_ERROR", "synthetic committed amendment lost its response")

    repository = FaultedRepository()
    service, _, entries = legacy_counter(relays, auth, repository=repository)
    repository.fault = fault
    client, observed, credential = relays(service=service, limit=120, run_id="m2-20260914-1638",
                                         additional_requests=40, amendment_id="m2-ui-allowance")
    response = client.post(CHAT, json=BODY)
    assert response.status_code == status and response.headers["x-should-retry"] == "false"
    assert repository.amendment_writes == 1
    assert observed == credential.scopes == []
    reconciled, after_restart, _ = relays(
        service=service, limit=120, run_id="m2-20260914-1638",
        additional_requests=40 if committed else None, amendment_id="m2-ui-allowance" if committed else None,
    )
    budget = reconciled.get(BUDGET).json()
    assert budget["max_requests"] == (160 if committed else 120)
    assert budget["attempts"] == 109 and budget["records"] == entries
    assert budget["amendment"] == ({"id": "m2-ui-allowance", "additional_requests": 40} if committed else None)
    assert repository.amendment_writes == 1 and after_restart == []


def test_amendment_requires_an_existing_counter_in_the_same_run(relays, auth):
    repository = CounterRepository()
    client, observed, credential = relays(
        service=ContextService(repository, FakeMemory(), auth), limit=120,
        run_id="not-an-existing-run", additional_requests=40, amendment_id="m2-ui-allowance",
    )
    assert client.get(BUDGET).status_code == 502
    assert client.post(CHAT, json=BODY).status_code == 502
    assert repository.writes == observed == credential.scopes == []


@pytest.mark.parametrize("changes", [
    {"amendment": {"id": "caller-amendment", "additional_requests": 40}},
    {"additional_requests": 40}, {"amendment_id": "caller-amendment"}, {"max_requests": 160},
    {"CONTEXT_M2_ADDITIONAL_REQUESTS": 40, "CONTEXT_M2_AMENDMENT_ID": "caller-amendment"},
])
def test_callers_cannot_amend_allowances_in_request_bodies(relays, changes):
    client, observed, credential = relays()
    assert client.post(CHAT, json={**BODY, **changes}).status_code == 422
    budget = client.get(BUDGET).json()
    assert budget["amendment"] is None
    assert budget["max_requests"] == budget["original_max_requests"] == 3
    assert budget["attempts"] == 0 and observed == credential.scopes == []


@pytest.mark.parametrize("name,alias", [
    ("attempt_id", "i"), ("owner_oid", "o"), ("state", "s"),
    ("upstream_status", "h"), ("request_id", "r"), ("usage", "u"),
    ("prompt_tokens", "p"), ("completion_tokens", "c"), ("total_tokens", "t"),
])
def test_duplicate_legacy_and_compact_aliases_are_rejected_before_amending(relays, auth, name, alias):
    service, repository, entries = legacy_counter(relays, auth, count=1, unknowns=False)
    ambiguous = deepcopy(entries)
    value = ambiguous[0]["usage"] if alias in {"p", "c", "t"} else ambiguous[0]
    value[alias] = deepcopy(value[name])
    repository.seed_legacy_entries(ambiguous)
    writes_before = len(repository.writes)
    client, observed, credential = relays(service=service, limit=120, run_id="m2-20260914-1638",
                                         additional_requests=40, amendment_id="m2-ui-allowance")
    response = client.post(CHAT, json=BODY)
    assert response.status_code == 502 and response.json()["error"]["code"] == "M2_COUNTER_INVALID"
    assert len(repository.writes) == writes_before and observed == credential.scopes == []


@pytest.mark.parametrize("changes", [
    {"i": "not-an-id"}, {"o": "not-an-oid"}, {"s": "success"}, {"h": "200"},
    {"r": "x" * 129}, {"u": {"p": True, "c": 1, "t": 2}}, {"unexpected": "field"},
])
def test_malformed_compact_receipts_fail_closed(relays, auth, changes):
    service, repository, _ = legacy_counter(relays, auth, count=1, unknowns=False)
    invalid = {
        "i": "1" * 32, "o": A.oid, "s": "upstream_response", "h": 200, "r": "native-request",
        "u": {"p": 12, "c": 2, "t": 14}, **changes,
    }
    repository.seed_legacy_entries([invalid])
    writes_before = len(repository.writes)
    client, observed, credential = relays(service=service, limit=120, run_id="m2-20260914-1638")
    assert client.get(BUDGET).status_code == 502
    assert client.post(CHAT, json=BODY).status_code == 502
    assert len(repository.writes) == writes_before and observed == credential.scopes == []


def test_production_opt_in_reuses_api_identity_and_closes_model_transport_without_startup_calls(monkeypatch):
    import shared_context.production as production

    values = {
        "CONTEXT_TENANT_ID": TENANT, "CONTEXT_API_AUDIENCE": AUDIENCE,
        "CONTEXT_ACTORS_JSON": json.dumps({A.oid: {"actor": A.actor, "team": A.team}}),
        "CONTEXT_ISSUERS_JSON": json.dumps([f"https://sts.windows.net/{TENANT}/"]),
        "CONTEXT_TABLE_ENDPOINT": "https://synthetic.table.core.windows.net", "CONTEXT_TABLE_NAME": "workstate",
        "CONTEXT_MEMORY_ENDPOINT": PROJECT, "CONTEXT_MEMORY_STORE": "poc-team-memory",
        "CONTEXT_M2_RUN_ID": "m2-production", "CONTEXT_M2_MODEL": MODEL, "CONTEXT_M2_MAX_REQUESTS": "120",
        "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000027",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in ("CONTEXT_BACKUP_BLOB_ENDPOINT", "CONTEXT_BACKUP_CONTAINER"):
        monkeypatch.delenv(key, raising=False)
    credential, table, project = Mock(), Mock(), Mock()
    identity_factory = Mock(return_value=credential)
    monkeypatch.setattr(production, "ManagedIdentityCredential", identity_factory)
    monkeypatch.setattr(production, "TableClient", Mock(return_value=table))
    monkeypatch.setattr(production, "AIProjectClient", Mock(return_value=project))

    class Transport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True

    transport = Transport(lambda _: pytest.fail("no native model call at startup or without auth"))
    factory = Mock(return_value=transport)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    with TestClient(production.create_production_app()) as client:
        assert client.get(BUDGET).status_code == 401
        assert client.post(CHAT, content=b"x" * (4 * 1024 * 1024 + 1)).status_code == 401
        assert table.method_calls == project.method_calls == credential.method_calls == []
    identity_factory.assert_called_once_with(client_id=values["AZURE_CLIENT_ID"])
    factory.assert_called_once_with(retries=0)
    assert transport.closed
    for resource in (credential, table, project):
        resource.close.assert_called_once()


async def asgi_post(app, receive, headers=None):
    messages = []

    async def send(message):
        messages.append(message)

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "https", "path": CHAT, "root_path": "", "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
        "server": ("testserver", 443), "client": ("127.0.0.1", 10000),
    }, receive, send)
    return messages


def test_real_jwt_authentication_runs_before_receiving_a_large_body(system, auth):
    gateway = M2Gateway(system[0], M2Settings("auth-before-body", MODEL, 1), PROJECT, Credential(),
                        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("no model call"))))
    with TestClient(create_app(system[0], JwtVerifier(auth), model_gateway=gateway)) as client:
        async def receive():
            pytest.fail("unauthenticated request body must not be consumed")

        messages = asyncio.run(asgi_post(client.app, receive, {"Content-Length": str(4 * 1024 * 1024 + 1)}))
        assert messages[0]["status"] == 401


@pytest.mark.parametrize("when", ["before_forward", "during_forward"])
def test_client_disconnect_leaves_a_durable_unknown_attempt(relays, when):
    disconnected = when == "before_forward"

    def handler(_):
        nonlocal disconnected
        disconnected = True
        return httpx.Response(200, json=NATIVE)

    client, observed, _ = relays(handler, limit=1)
    delivered_body = False

    async def receive():
        nonlocal delivered_body
        if not delivered_body:
            delivered_body = True
            return {"type": "http.request", "body": json.dumps(BODY).encode(), "more_body": False}
        if disconnected:
            return {"type": "http.disconnect"}
        await anyio.sleep_forever()

    messages = asyncio.run(asgi_post(client.app, receive, {"Content-Type": "application/json"}))
    assert messages[0]["status"] == 499
    state = client.get(BUDGET).json()
    assert state["attempts"] == 1 and state["records"][0]["state"] == "unknown"
    assert len(observed) == (0 if when == "before_forward" else 1)


def test_exact_four_mib_with_inline_png_is_accepted_without_raising_old_json_limits(relays):
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jG5kAAAAASUVORK5CYII="
    body = {**BODY, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Describe this local synthetic PNG."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png, "detail": "low"}},
    ]}]}
    encoded = json.dumps(body).encode()
    padded = encoded + b" " * (4 * 1024 * 1024 - len(encoded))
    client, observed, _ = relays()
    assert client.post(CHAT, content=padded, headers={"Content-Type": "application/json"}).status_code == 200
    assert json.loads(observed[0].content)["messages"] == body["messages"]
    assert client.post(CHAT, content=padded + b" ", headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post("/teams/team-one/works/oversized", content=b"x" * 65537,
                       headers={"Content-Type": "application/json"}).status_code == 413
    assert client.get(BUDGET).json()["attempts"] == 1


def test_one_256_kib_1280_by_800_png_is_preserved_at_the_128_kib_text_boundary(relays):
    png = png_fixture(total_bytes=256 * 1024)
    assert len(png) == 256 * 1024
    text = {"type": "text", "text": ""}
    image = {"type": "image_url", "image_url": {"url": "", "detail": "low"}}
    body = {
        **BODY, "max_tokens": 1024, "n": 1, "store": False, "parallel_tool_calls": False,
        "messages": [{"role": "user", "content": [text, image]}],
    }
    text["text"] = "x" * (128 * 1024 - len(json.dumps(body, ensure_ascii=False).encode("utf-8")))
    image["image_url"]["url"] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    client, observed, credential = relays()
    assert client.post(CHAT, json=body).status_code == 200
    assert json.loads(observed[0].content) == body
    text["text"] += "x"
    assert client.post(CHAT, json=body).status_code == 422
    assert client.get(BUDGET).json()["attempts"] == 1
    assert len(observed) == len(credential.scopes) == 1


@pytest.mark.parametrize("case", ["two-parts", "two-messages", "large-png", "wide", "tall"])
def test_extra_or_oversized_pngs_are_rejected_before_reserving_or_forwarding(relays, case):
    png = png_fixture(
        width=1281 if case == "wide" else 1280, height=801 if case == "tall" else 800,
        total_bytes=256 * 1024 + 1 if case == "large-png" else None,
    )
    image = {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
    }}
    messages = [{"role": "user", "content": [image]}]
    if case == "two-parts":
        messages[0]["content"].append(image)
    elif case == "two-messages":
        messages.append({"role": "user", "content": [image]})
    client, observed, credential = relays()
    assert client.post(CHAT, json={**BODY, "messages": messages}).status_code == 422
    assert client.get(BUDGET).json()["attempts"] == 0
    assert observed == [] and credential.scopes == []


@pytest.mark.parametrize("initial", [True, False])
def test_concurrent_counter_creation_or_cas_conflict_does_not_double_forward(relays, auth, initial):
    class RacingRepository(InMemoryRepository):
        race = False

        def __init__(self):
            super().__init__()
            self.barrier, self.guard, self.racing_reads = Barrier(2), Lock(), 0

        def get(self, team, row):
            with self.guard:
                synchronize = self.race and self.racing_reads < 2
                if synchronize:
                    self.racing_reads += 1
            try:
                record = super().get(team, row)
            except Problem:
                if synchronize:
                    self.barrier.wait(timeout=5)
                raise
            if synchronize:
                self.barrier.wait(timeout=5)
            return record

    repository = RacingRepository()
    service = ContextService(repository, FakeMemory(), auth)
    one, one_requests, _ = relays(service=service)
    two, two_requests, _ = relays(service=service)
    if not initial:
        assert one.post(CHAT, json=BODY).status_code == 200
    repository.race = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda client: client.post(CHAT, json=BODY).status_code, [one, two]))
    assert sorted(results) == ([200, 409] if initial else [200, 412])
    expected = 1 if initial else 2
    assert len(one_requests) + len(two_requests) == expected
    assert one.get(BUDGET).json()["attempts"] == expected


def test_all_120_attempts_fit_one_counter_row_and_121st_cannot_forward(relays):
    largest_usage = {key: 10 ** 9 for key in NATIVE["usage"]}
    client, observed, credential = relays(
        lambda _: httpx.Response(200, json={**NATIVE, "usage": largest_usage}, headers={"x-request-id": "a" * 128}),
        limit=120, run_id="r" * 64,
    )
    for _ in range(120):
        assert client.post(CHAT, json=BODY).status_code == 200
    assert client.post(CHAT, json=BODY).status_code == 429
    state = client.get(BUDGET).json()
    assert state["attempts"] == len(state["records"]) == 120 and state["remaining"] == 0
    assert len(observed) == len(credential.scopes) == 120
    assert state["records"][-1]["usage"] == largest_usage


def test_native_error_advises_sdk_not_to_automatically_retry(relays):
    client, observed, _ = relays(lambda _: httpx.Response(503, json={"error": {"message": "native unavailable"}}))
    response = client.post(CHAT, json=BODY)
    assert response.status_code == 503
    assert response.headers["x-should-retry"] == "false"
    assert len(observed) == 1


@pytest.mark.parametrize("raw", [
    b'{"model":"gpt-4.1-mini","model":"gpt-4.1-mini","messages":[]}',
    b'{"messages":[{"role":"user","content":"x"}],"temperature":NaN}',
    b'{"messages":[{"role":"user","content":"x"}],"tools":[{"type":"function",'
    b'"function":{"name":"f","parameters":{"type":"object","maximum":1e309}}}]}',
])
def test_invalid_json_and_nonfinite_schema_numbers_are_rejected_before_reserving(relays, raw):
    client, observed, credential = relays()
    assert client.post(CHAT, content=raw, headers={"Content-Type": "application/json"}).status_code == 422
    assert client.get(BUDGET).json()["attempts"] == 0
    assert observed == [] and credential.scopes == []


@pytest.mark.parametrize("body,content_type,streaming", [
    (b'{"not":"a completion"}', "application/json", False),
    (b'{"choices":[]}', "application/json", False),
    (b"<html>unexpected</html>", "text/html", False),
    (b'data: {"choices":[]}\n\n', "text/event-stream", True),
    (b'data: [DONE]\n\n', "text/event-stream", True),
    (b"data: not-json\n\ndata: [DONE]\n\n", "text/event-stream", True),
    (b"x" * (2 * 1024 * 1024 + 1), "application/json", False),
], ids=["not-completion", "no-choice", "html", "incomplete-sse", "empty-sse", "bad-sse", "oversized"])
def test_incomplete_or_oversized_native_success_is_never_fabricated(relays, body, content_type, streaming):
    stream = Chunks(body)
    client, observed, _ = relays(lambda _: httpx.Response(200, stream=stream, headers={"Content-Type": content_type}))
    response = client.post(CHAT, json={**BODY, "stream": streaming})
    assert response.status_code == 502
    assert client.get(BUDGET).json()["records"][0]["state"] == "unknown"
    assert len(observed) == 1 and stream.closed


def test_large_native_error_preserves_status_with_explicit_bounded_truncation(relays):
    raw = b"x" * (64 * 1024 + 1)
    client, _, _ = relays(lambda _: httpx.Response(429, stream=Chunks(raw), headers={"Content-Type": "text/plain"}))
    response = client.post(CHAT, json=BODY)
    assert response.status_code == 429 and response.content == raw[:64 * 1024]
    assert response.headers["x-m2-response-truncated"] == "true"


@pytest.mark.parametrize("secret", ["synthetic-native-token-1", "caller-only-token"])
def test_credential_reflection_is_blocked_and_never_stored_in_budget(relays, secret):
    client, _, _ = relays(lambda _: httpx.Response(403, json={"error": {"message": "Bearer " + secret}}))
    response = client.post(CHAT, json=BODY, headers={"Authorization": "Bearer caller-only-token"})
    assert response.status_code == 502 and secret not in response.text
    budget = client.get(BUDGET)
    assert secret not in budget.text and budget.json()["records"][0]["state"] == "unknown"


def test_known_missing_counter_is_not_recreated_after_data_loss(relays, auth):
    class DisappearingRepository(InMemoryRepository):
        missing = False

        def get(self, team, row):
            if self.missing:
                raise Problem(404, "NOT_FOUND", "synthetic counter disappeared")
            return super().get(team, row)

        def create(self, team, row, payload):
            if self.missing:
                pytest.fail("a known lost counter must not be recreated")
            return super().create(team, row, payload)

    repository = DisappearingRepository()
    client, observed, _ = relays(service=ContextService(repository, FakeMemory(), auth), limit=1)
    assert client.post(CHAT, json=BODY).status_code == 200
    repository.missing = True
    assert client.post(CHAT, json=BODY).status_code == 502
    assert client.get(BUDGET).status_code == 502
    assert len(observed) == 1


def test_tool_call_history_and_max_completion_tokens_alias_are_supported(relays):
    body = {**BODY, "max_completion_tokens": 512, "parallel_tool_calls": True, "messages": [
        {"role": "developer", "content": "Use the supplied local tool."},
        {"role": "user", "content": "Inspect the synthetic work item."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_one", "type": "function", "function": {"name": "inspect", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_one", "content": "synthetic tool result"},
    ], "tools": [
        {"type": "function", "function": {"name": "inspect", "description": "Inspect a local fixture",
                                         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    ], "tool_choice": "auto"}
    client, observed, _ = relays()
    assert client.post(CHAT, json=body).status_code == 200
    forwarded = json.loads(observed[0].content)
    assert forwarded["max_tokens"] == 512 and "max_completion_tokens" not in forwarded
    assert forwarded["parallel_tool_calls"] is False
    assert forwarded["tools"] == body["tools"]
    assert forwarded["messages"][2]["tool_calls"] == body["messages"][2]["tool_calls"]


@pytest.mark.parametrize("changes", [
    {"model": []},
    {"choices": [{"index": 0, "message": {"role": "assistant", "content": "invalid"}, "finish_reason": []}]},
])
def test_malformed_native_field_types_return_explicit_protocol_errors(relays, changes):
    client, observed, _ = relays(lambda _: httpx.Response(200, json={**NATIVE, **changes}))
    response = client.post(CHAT, json=BODY)
    assert response.status_code == 502 and response.json()["error"]["code"] == "M2_UPSTREAM_PROTOCOL_ERROR"
    assert client.get(BUDGET).json()["records"][0]["state"] == "unknown"
    assert len(observed) == 1


def test_outcome_counter_failure_does_not_retry_an_already_executed_model(relays, auth):
    class UncertainOutcomeRepository(InMemoryRepository):
        def replace(self, team, row, payload, etag):
            raise Problem(502, "UPSTREAM_ERROR", "synthetic outcome write failure")

    client, observed, _ = relays(service=ContextService(UncertainOutcomeRepository(), FakeMemory(), auth), limit=1)
    response = client.post(CHAT, json=BODY)
    assert response.status_code == 502 and response.headers["x-should-retry"] == "false"
    assert client.get(BUDGET).json()["records"][0]["state"] == "unknown"
    assert client.post(CHAT, json=BODY).status_code == 429
    assert len(observed) == 1


def test_interrupted_native_stream_is_closed_and_not_retried(relays):
    stream = Chunks(b"data: ", httpx.ReadError("synthetic interrupted SSE"))
    client, observed, _ = relays(lambda _: httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"}))
    assert client.post(CHAT, json={**BODY, "stream": True}).status_code == 502
    assert client.get(BUDGET).json()["records"][0]["state"] == "unknown"
    assert len(observed) == 1 and stream.closed
