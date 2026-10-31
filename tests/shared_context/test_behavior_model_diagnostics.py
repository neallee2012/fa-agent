"""Local sockets enforce HTTPX timeouts; no cloud credentials or model services."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from datetime import timedelta
from dataclasses import replace
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_hosted import AssistantClient, AssistantSupervisor, AssistantSession
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.m2_copilot import _ModelGateway
from shared_context.errors import Problem
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import key, _Session, _row
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import worker_key
from shared_context.source_materials import digest
from test_behavior_assistant_sse import stream
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_behavior_assistant_claim_boundaries import reviewed
from test_m3_auth import jwk_client, signing_key


BODY = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "synthetic"}],
        "max_tokens": 1024, "stream": True}
CANARY = "PRIVATE_SYNTHETIC_NEVER_RETAIN"


@contextmanager
def socket_server(handle):
    seen, errors = [], []
    release = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            self.body = self.rfile.read(int(self.headers.get("content-length", "0")))
            seen.append(self.path)
            try:
                handle(self, release)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as error:
                errors.append(type(error))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield SimpleNamespace(port=server.server_port, seen=seen, release=release)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive() and not errors


class LocalHTTP(httpx.BaseTransport):
    def __init__(self, port):
        self.port, self.http = port, httpx.HTTPTransport(retries=0)
    def handle_request(self, request):
        local = httpx.Request(request.method, request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port),
                              headers=request.headers, content=request.content, extensions=request.extensions)
        return self.http.handle_request(local)
    def close(self):
        self.http.close()


class LocalAsyncHTTP(httpx.AsyncBaseTransport):
    def __init__(self, port, *, read_timeout=None):
        self.port, self.http = port, httpx.AsyncHTTPTransport(retries=0)
        self.read_timeout, self.timeouts = read_timeout, []
    async def handle_async_request(self, request):
        self.timeouts.append(dict(request.extensions["timeout"]))
        if self.read_timeout is not None:
            request.extensions["timeout"] = {**request.extensions["timeout"], "read": self.read_timeout}
        local = httpx.Request(request.method, request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port),
                              headers=request.headers, content=request.content, extensions=request.extensions)
        return await self.http.handle_async_request(local)
    async def aclose(self):
        await self.http.aclose()


def send_bytes(handler, content, status=200, content_type="text/event-stream"):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)
    handler.wfile.flush()


@pytest.mark.parametrize("where", ["headers", "body"])
def test_real_socket_model_timeout_keeps_first_safe_phase(where, monkeypatch):
    from shared_context import behavior_assistant_hosted as hosted
    monkeypatch.setattr(hosted, "CLIENT_MODEL_SECONDS", 0.1)
    def handle(handler, release):
        if where == "body":
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b"data: ")
            handler.wfile.flush()
        assert release.wait(3)
    with socket_server(handle) as server:
        client = AssistantClient("https://synthetic.invalid",
            SimpleNamespace(binding_id="binding", session_id="a" * 64, task=SimpleNamespace(instruction=None)),
            SecretStr("b" * 64),
            token_provider=lambda: CANARY, transport=LocalHTTP(server.port))
        client.http.timeout = httpx.Timeout(0.1)
        started = time.monotonic()
        try:
            with pytest.raises(Problem) as unknown:
                client.model(httpx.Request("POST", "https://synthetic.invalid/chat/completions", json=BODY))
        finally:
            client.close()
        assert 0.07 <= time.monotonic() - started < 1.5
        assert len(server.seen) == 1
        error = unknown.value.body["error"]
        assert error.get("upstream_phase") == ("request" if where == "headers" else "response_body")
        assert error.get("upstream_status") == (None if where == "headers" else 200)
        assert error.get("exception_type") == "ReadTimeout"
        assert CANARY not in str(unknown.value.body)


def test_provider_http_rejection_survives_real_socket_and_gateway():
    def handle(handler, release):
        send_bytes(handler, CANARY.encode(), status=429, content_type="application/json")
    with socket_server(handle) as server:
        provider = AssistantProvider("https://synthetic.invalid/openai/v1/chat/completions",
            SimpleNamespace(get_token=lambda _: SimpleNamespace(token=CANARY)), transport=LocalAsyncHTTP(server.port))
        async def reserve(_):
            pass
        async def send(request):
            return await provider.send(json.loads(request.content))
        async def run():
            async with httpx.AsyncClient() as http:
                endpoint = httpx.URL("https://synthetic.invalid/chat/completions")
                boundary = AssistantModelBoundary(reserve, sender=send)
                gateway = _ModelGateway(endpoint, http, lambda _: None, boundary=boundary,
                                        private_token=lambda _: asyncio.sleep(0, result=CANARY))
                with pytest.raises(RuntimeError):
                    await gateway.send_request(httpx.Request("POST", endpoint, json=BODY),
                                               SimpleNamespace(request_id="original", session_id="original"))
                assert len(server.seen) == 1
                failure = getattr(boundary, "failure", None)
                assert failure is not None
                assert failure["provider_status"] == 429
                assert failure["upstream_error_phase"] == "provider_response"
                assert CANARY not in json.dumps(failure)
                with pytest.raises(RuntimeError):
                    await gateway.send_request(httpx.Request("POST", endpoint, json=BODY),
                                               SimpleNamespace(request_id="second", session_id="original"))
                assert len(server.seen) == 1
        asyncio.run(run())


@contextmanager
def native_model_http(c, provider_http):
    def api_http(handler, release):
        result = c.client.post(handler.path, content=handler.body, headers=dict(handler.headers))
        handler.send_response(result.status_code)
        for key, value in result.headers.items():
            if key.lower() not in {"transfer-encoding", "connection"}:
                handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(result.content)
    with socket_server(provider_http) as provider, socket_server(api_http) as api:
        c.composition.assistant.provider.transport = LocalAsyncHTTP(provider.port)
        c.composition.assistant.provider.credential = SimpleNamespace(
            get_token=lambda _: SimpleNamespace(token=CANARY))
        c.hosted.http.close()
        c.hosted.http = httpx.Client(transport=LocalHTTP(api.port), trust_env=False, timeout=httpx.Timeout(20))
        yield provider, api


@pytest.mark.parametrize("failure", ["429", "unsafe-sse", "stop-response-lost"])
def test_provider_failure_survives_actual_sdk_api_and_supervisor(native_acquire, tmp_path, monkeypatch, failure):
    c = native_acquire
    reviewed(c)
    def provider_http(handler, release):
        send_bytes(handler, ("data: " + CANARY + "\n\n").encode() if failure == "unsafe-sse" else CANARY.encode(),
                   status=200 if failure == "unsafe-sse" else 429,
                   content_type="text/event-stream" if failure == "unsafe-sse" else "application/json")
    if failure == "stop-response-lost":
        monkeypatch.setattr(c.hosted, "stopped", lambda _: (_ for _ in ()).throw(
            Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", CANARY)))
    budget_key = control_key("budget", c.binding.owner.root_budget_id)
    before = c.store.read_snapshot((budget_key,))[budget_key].payload["data"]
    with native_model_http(c, provider_http) as (provider, api):
        supervisor = AssistantSupervisor(tmp_path / "host", runtime_path=Path(os.environ["M2_TEST_RUNTIME"]))
        supervisor._execute(SimpleNamespace(launch_id=c.launch_id), c.hosted)
        with supervisor.journal.connect() as db:
            diagnostic = json.loads(db.execute("SELECT body FROM assistant_failures WHERE launch_id=?",
                                              (c.launch_id,)).fetchone()[0])
        assert len(provider.seen) == 1
        assert sum(path.endswith("/model") for path in api.seen) == 1
        assert diagnostic["phase"] == "sdk_run"
        assert diagnostic.get("provider_status") == (200 if failure == "unsafe-sse" else 429)
        assert diagnostic.get("upstream_error_phase") == "provider_response"
        if failure == "unsafe-sse":
            assert diagnostic["boundary_phase"] == "sse_frame"
        assert CANARY not in json.dumps(diagnostic)
        saved = c.store.read_inventory()
        session = _row(saved.rows, key("session", c.assignment.session_id), _Session)
        assert session.model_requests == 1 and session.context_read is False
        assert key("result", c.binding.work.work_id) not in saved.rows
        assert saved.rows[budget_key].payload["data"]["remaining_units"] == before["remaining_units"] - 1
        intents = [row.payload["data"] for row_id, row in saved.rows.items()
                   if row.payload["data"].get("request_sha256") is not None
                   and row_id == key("model-intent", c.assignment.session_id, row.payload["data"]["request_sha256"])]
        assert len(intents) == 1 and intents[0]["consumed"] is True
        assert not supervisor.record_failure(c.launch_id, "sdk_run", Problem(503, "ASSISTANT_SDK_UNKNOWN", CANARY))
        with supervisor.journal.connect() as db:
            assert json.loads(db.execute("SELECT body FROM assistant_failures WHERE launch_id=?",
                                         (c.launch_id,)).fetchone()[0]) == diagnostic


def test_synchronous_credential_does_not_starve_provider_event_loop():
    entered, released, posts = threading.Event(), threading.Event(), []
    def token(_):
        entered.set()
        assert released.wait(1)
        return SimpleNamespace(token=CANARY)
    def handle(handler, release):
        posts.append(1)
        send_bytes(handler, stream([{}], finish_reason="stop").content)
    with socket_server(handle) as server:
        provider = AssistantProvider("https://synthetic.invalid/openai/v1/chat/completions",
                                    SimpleNamespace(get_token=token), transport=LocalAsyncHTTP(server.port))
        async def run():
            start = time.monotonic()
            pending = asyncio.create_task(provider.send(BODY))
            await asyncio.sleep(0.02)
            elapsed = time.monotonic() - start
            released.set()
            try:
                await pending
            except Problem:
                pass
            assert entered.is_set()
            assert elapsed < 0.3, "synchronous credential blocked the API event loop"
        asyncio.run(run())


def test_provider_current_evidence_does_not_block_event_loop(monkeypatch):
    from shared_context.behavior_assistant_server import _CurrentAssistantProvider, AssistantServer
    entered, release = threading.Event(), threading.Event()
    def evidence(_):
        entered.set()
        assert release.wait(1)
    monkeypatch.setattr(AssistantServer, "acquire_evidence", evidence)
    def handle(handler, _):
        send_bytes(handler, stream([{}], finish_reason="stop").content)
    with socket_server(handle) as server:
        composition = SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
            provider_endpoint="https://synthetic.invalid/openai/v1/chat/completions")))
        provider = _CurrentAssistantProvider(composition, SimpleNamespace(
            get_token=lambda _: SimpleNamespace(token=CANARY)), transport=LocalAsyncHTTP(server.port))
        async def run():
            started = time.monotonic()
            task = asyncio.create_task(provider.send(BODY))
            await asyncio.sleep(0.02)
            elapsed = time.monotonic() - started
            release.set()
            try:
                await task
            except Exception:
                pass
            assert entered.is_set() and elapsed < 0.3
        asyncio.run(run())


def test_cancelled_model_sender_cannot_dispatch_after_late_token(monkeypatch, tmp_path):
    from shared_context import behavior_assistant_hosted as hosted
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def token():
        entered.set()
        assert release.wait(2)
        return CANARY
    def handle(handler, _):
        send_bytes(handler, stream([{}], finish_reason="stop").content)
    with socket_server(handle) as server:
        client = AssistantClient("https://synthetic.invalid", SimpleNamespace(
            binding_id="binding", session_id="a" * 64, sdk_session_id="a" * 64, resume=False,
            task=SimpleNamespace(instruction=None)),
            SecretStr("b" * 64), token_provider=token, transport=LocalHTTP(server.port))
        original = client.model
        def model(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()
        monkeypatch.setattr(client, "model", model)
        monkeypatch.setattr(client, "stopped", lambda _: None)
        async def sdk(**kwargs):
            async with asyncio.timeout(0.1):
                await kwargs["behavior_boundary"].sender(httpx.Request(
                    "POST", "https://synthetic.invalid/chat/completions", json=BODY))
        monkeypatch.setattr(hosted, "run_copilot", sdk)
        async def run():
            with pytest.raises(Problem):
                await AssistantSession(client, root=tmp_path, runtime_path=tmp_path / "unused")._run(tmp_path)
            assert entered.is_set()
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
        try:
            asyncio.run(run())
        finally:
            release.set()
            client.close()
        assert server.seen == [], "cancelled turn sent its first model HTTP request after token returned"


@pytest.mark.parametrize("wait_at", ["provider_response", "current_evidence"])
def test_explicit_legacy_twenty_second_budget_is_not_provider_success(native_acquire, monkeypatch, wait_at):
    from shared_context import behavior_assistant_hosted as hosted
    monkeypatch.setattr(hosted, "CLIENT_MODEL_SECONDS", 20)
    from shared_context.behavior_assistant_server import AssistantServer
    c = native_acquire
    reviewed(c)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    def provider_http(handler, _):
        if wait_at == "provider_response":
            entered.set()
            assert release.wait(25)
        send_bytes(handler, stream([{}], finish_reason="stop").content)
        returned.set()
    with native_model_http(c, provider_http) as (provider, api):
        c.hosted.acquire()
        c.hosted.reserve_model(digest(BODY))
        if wait_at == "current_evidence":
            original = AssistantServer.acquire_evidence
            intent_key = key("model-intent", c.assignment.session_id, digest(BODY))
            def delayed(composition):
                intent = c.store.read_snapshot((intent_key,))[intent_key]
                if intent.payload["data"]["consumed"]:
                    entered.set()
                    assert release.wait(25)
                return original(composition)
            monkeypatch.setattr(AssistantServer, "acquire_evidence", staticmethod(delayed))
        assert c.hosted.http.timeout.read == 20
        started = time.monotonic()
        try:
            with pytest.raises(Problem) as unknown:
                c.hosted.model(httpx.Request("POST", "https://synthetic.invalid/chat/completions", json=BODY))
        finally:
            release.set()
        elapsed = time.monotonic() - started
        assert entered.is_set() and returned.wait(3)
        assert 19 <= elapsed < 24
        assert unknown.value.body["error"]["exception_type"] == "ReadTimeout"
        assert unknown.value.body["error"]["upstream_phase"] == "request"
        assert len(provider.seen) == sum(path.endswith("/model") for path in api.seen) == 1
    assert _row(c.store.read_inventory().rows, key("session", c.assignment.session_id), _Session).model_requests == 1
    print(f"wait_at={wait_at}; explicit-legacy-client-read-timeout={elapsed:.3f}s; "
          "provider-returned-after-client=True; model-dispatches=1")


def test_provider_stream_timeout_uses_real_socket_and_preserves_body_phase():
    def handle(handler, release):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b"data: ")
        handler.wfile.flush()
        assert release.wait(3)
    with socket_server(handle) as server:
        transport = LocalAsyncHTTP(server.port, read_timeout=0.1)
        provider = AssistantProvider("https://synthetic.invalid/openai/v1/chat/completions",
                                    SimpleNamespace(get_token=lambda _: SimpleNamespace(token=CANARY)),
                                    transport=transport)
        with pytest.raises(Problem) as unknown:
            asyncio.run(provider.send(BODY))
        detail = unknown.value.body["error"]
        assert detail["provider_status"] == 200 and detail["boundary_phase"] == "response_body"
        assert detail["exception_type"] == "ReadTimeout"
        assert transport.timeouts == [{"connect": 5, "read": 60, "write": 60, "pool": 60}]
        assert len(server.seen) == 1


def test_httpx_inactivity_timeout_is_not_a_total_response_deadline():
    data = stream([{}], finish_reason="stop").content
    def handle(handler, _):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        for offset in range(0, len(data), 32):
            time.sleep(0.04)
            handler.wfile.write(data[offset:offset + 32])
            handler.wfile.flush()
    with socket_server(handle) as server:
        with httpx.Client(transport=LocalHTTP(server.port), timeout=0.1, trust_env=False) as client:
            started = time.monotonic()
            response = client.post("https://synthetic.invalid/model")
            assert response.content == data and time.monotonic() - started > 0.15


@pytest.mark.parametrize("pending_at", ["provider_token", "provider_evidence"])
def test_cancelled_provider_wait_never_posts_after_sync_dependency_returns(monkeypatch, pending_at):
    from shared_context.behavior_assistant_server import _CurrentAssistantProvider, AssistantServer
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def wait():
        entered.set()
        assert release.wait(3)
        finished.set()
    def token(_):
        if pending_at == "provider_token":
            wait()
        return SimpleNamespace(token=CANARY)
    def evidence(_):
        if pending_at == "provider_evidence":
            wait()
    monkeypatch.setattr(AssistantServer, "acquire_evidence", evidence)
    with socket_server(lambda h, _: send_bytes(h, stream([{}], finish_reason="stop").content)) as server:
        provider = _CurrentAssistantProvider(SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
            provider_endpoint="https://synthetic.invalid/openai/v1/chat/completions"))),
            SimpleNamespace(get_token=token), transport=LocalAsyncHTTP(server.port))
        async def run():
            pending = asyncio.create_task(provider.send(BODY))
            assert await asyncio.to_thread(entered.wait, 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
        asyncio.run(run())
        assert server.seen == []


def test_capacity_uncertain_and_elapsed_lease_do_not_invent_execution_revocation(native_acquire):
    c = native_acquire
    reviewed(c)
    c.hosted.acquire()
    binding = c.starts[0].binding
    def observe_unknown(original_binding):
        probe = c.native.probe(original_binding)
        launch = c.native.launch(c.launch_id)
        command = wire.HostedReport(
            request_id=probe.probe_id, invocation_id=launch.native_invocation_id,
            identity=c.identity, process_id=launch.process_id, claim_id=launch.claim_id,
            report_token=c.native_claim.report_token, probe_id=probe.probe_id, supervisor_id=launch.process_id,
            process_generation=launch.process_generation, binding_digest=digest(binding.model_dump(mode="json")),
            phase="spawn_unknown", sequence=1)
        return c.native.report(c.binding.binding_id, c.launch_id, probe.probe_id, command, c.agent).evidence_id
    workers = c.native.workers
    workers.adapter = replace(workers.adapter, query=observe_unknown)
    for _ in range(workers.contract.queries_per_generation):
        workers.reconcile(binding.job_id, binding.owner)
    job = workers.inspect(binding.job_id)
    assert job.state == "capacity_uncertain" and job.queries_reserved == 8
    pool_key = worker_key("pool", c.config.workers.workspace_id)
    pool = c.store.read_snapshot((pool_key,))[pool_key].payload["data"]
    from datetime import datetime
    c.clock[0] = datetime.fromisoformat(pool["slots"][0]["lease_until"].replace("Z", "+00:00")) + timedelta(seconds=1)
    c.hosted.reserve_model(digest(BODY))
    assert c.hosted.context().item_count == 3
    assert workers.inspect(binding.job_id).state == "capacity_uncertain"
    assert workers.inspect(binding.job_id).queries_reserved == 8
    assert c.store.read_snapshot((pool_key,))[pool_key].payload["data"] == pool


@pytest.mark.parametrize("phase,status,error_type", [
    ("token", None, httpx.ConnectError), ("request", None, httpx.WriteTimeout),
    ("response_body", 200, httpx.RemoteProtocolError),
])
def test_model_failure_filters_dynamic_exception_names_and_private_details(phase, status, error_type):
    from shared_context.behavior_http_diagnostics import model_failure, model_response_diagnostics
    class PrivateException(error_type):
        pass
    PrivateException.__name__ = CANARY
    detail = model_failure(PrivateException(CANARY), phase="transport")
    assert detail["exception_type"] == "UNCLASSIFIED"
    forged = httpx.Headers([
        ("x-behavior-model-upstream-error-code", CANARY),
        ("x-behavior-model-upstream-error-phase", CANARY),
        ("x-behavior-model-exception-type", CANARY),
        ("x-behavior-model-provider-status", "200"),
        ("x-behavior-model-provider-status", "429"),
        ("authorization", CANARY),
    ])
    assert model_response_diagnostics(forged) == {}
    error = Problem(503, "ASSISTANT_MODEL_OUTCOME_UNKNOWN", CANARY,
                    upstream_phase=phase, upstream_status=status,
                    upstream_error_code="ASSISTANT_MODEL_UNKNOWN", raw_body=CANARY,
                    exception_type=error_type.__name__, retry=True)
    safe = model_failure(error, phase="transport")
    assert safe["exception_type"] == error_type.__name__
    assert CANARY not in json.dumps(safe) and "retry" not in safe


def test_boundary_close_failure_does_not_overwrite_first_stream_failure():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: "
            raise httpx.ReadTimeout(CANARY)
        async def aclose(self):
            raise RuntimeError(CANARY)
    response = httpx.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"},
                              request=httpx.Request("POST", "https://synthetic.invalid/model"))
    boundary = AssistantModelBoundary(None)
    with pytest.raises(RuntimeError):
        asyncio.run(boundary.response(response))
    assert boundary.failure["boundary_phase"] == "response_body"
    assert boundary.failure["exception_type"] == "ReadTimeout"
    assert CANARY not in json.dumps(boundary.failure)


def test_custom_sender_never_fetches_or_copies_an_unused_runtime_bearer():
    reservations, sends = [], []
    async def reserve(value):
        reservations.append(value)
    async def token(_):
        pytest.fail("the sender owns credentials; runtime must not request a redundant token")
    async def sender(request):
        sends.append(request)
        assert "authorization" not in request.headers
        return stream([{}], finish_reason="stop")
    async def run():
        endpoint = httpx.URL("https://synthetic.invalid/chat/completions")
        boundary = AssistantModelBoundary(reserve, sender=sender)
        async with httpx.AsyncClient() as http:
            gateway = _ModelGateway(endpoint, http, lambda _: None, boundary=boundary, private_token=token)
            await gateway.send_request(httpx.Request("POST", endpoint, json=BODY),
                                       SimpleNamespace(request_id="one", session_id="one"))
        assert len(reservations) == len(sends) == 1
    asyncio.run(run())


@pytest.mark.parametrize("change", ["ticket", "expired", "cancelled", "changed-request", "replayed-consume"])
def test_model_http_current_authority_fences_still_prevent_provider_send(native_acquire, change):
    c = native_acquire
    reviewed(c)
    c.hosted.acquire()
    c.hosted.reserve_model(digest(BODY))
    body = dict(BODY)
    if change == "ticket":
        c.hosted.ticket = SecretStr("0" * 64)
    elif change == "expired":
        c.clock[0] = c.config.policies[0].policy.valid_until + timedelta(seconds=1)
    elif change == "cancelled":
        c.native.workers.cancel(c.binding.job_id, c.binding.owner, expected_binding=c.starts[0].binding)
    elif change == "changed-request":
        body["temperature"] = 0
    else:
        from shared_context.behavior_assistant_wire import ModelRequest
        session = c.authority.current_session(c.binding.binding_id, c.assignment.session_id, c.ticket, c.agent)
        c.authority.consume_model(session, ModelRequest(request_id=digest(BODY), body=BODY), c.agent)
    with native_model_http(c, lambda h, _: send_bytes(h, stream([{}], finish_reason="stop").content)) as (provider, api):
        with pytest.raises(Problem) as unknown:
            c.hosted.model(httpx.Request("POST", "https://synthetic.invalid/chat/completions", json=body))
        assert provider.seen == [] and len([p for p in api.seen if p.endswith("/model")]) == 1
        assert unknown.value.body["error"]["upstream_status"] in ({503} if change == "expired" else {403, 409, 412})
    assert _row(c.store.read_inventory().rows, key("session", c.assignment.session_id), _Session).model_requests == 1


def test_post_consume_evidence_conflict_is_distinct_and_never_refunds_or_sends(native_acquire, monkeypatch):
    c = native_acquire
    reviewed(c)
    c.hosted.acquire()
    c.hosted.reserve_model(digest(BODY))
    intent_key = key("model-intent", c.assignment.session_id, digest(BODY))
    original = c.composition.require_current_native_evidence
    conflicts = []
    def evidence():
        original()
        if c.store.read_snapshot((intent_key,))[intent_key].payload["data"]["consumed"]:
            conflicts.append(True)
            raise Problem(412, "BEHAVIOR_CONFLICT", CANARY)
    monkeypatch.setattr(c.composition, "require_current_native_evidence", evidence)
    with native_model_http(c, lambda h, _: send_bytes(h, stream([{}], finish_reason="stop").content)) as (provider, api):
        with pytest.raises(Problem) as unknown:
            c.hosted.model(httpx.Request("POST", "https://synthetic.invalid/chat/completions", json=BODY))
        detail = unknown.value.body["error"]
        assert detail["upstream_status"] == 412 and detail["upstream_error_code"] == "BEHAVIOR_CONFLICT"
        assert detail["upstream_error_phase"] == "provider_evidence"
        assert len(conflicts) == 3 and provider.seen == []
        assert len([path for path in api.seen if path.endswith("/model")]) == 1
        assert c.store.read_snapshot((intent_key,))[intent_key].payload["data"]["consumed"] is True
        assert CANARY not in json.dumps(detail)


def test_actual_sdk_sixty_second_turn_cancels_a_live_socket_stream(tmp_path):
    from shared_context.m2_copilot import run_copilot
    entered = threading.Event()
    events, reservations = [], []
    def handle(handler, release):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", "65536")
        handler.end_headers()
        entered.set()
        # Keep socket reads active below HTTPX's inactivity timeout. The SDK
        # turn's asyncio deadline, not a scripted exception, must end the wait.
        for _ in range(90):
            if release.wait(1):
                break
            handler.wfile.write(b"data: ")
            handler.wfile.flush()
    async def reserve(value):
        reservations.append(value)
    work = tmp_path / "work"
    work.mkdir()
    with socket_server(handle) as server:
        async def run():
            async with httpx.AsyncClient(transport=LocalAsyncHTTP(server.port),
                                         timeout=httpx.Timeout(60), trust_env=False) as http:
                async def sender(request):
                    return await http.send(request, stream=True)
                boundary = AssistantModelBoundary(reserve, sender=sender)
                started = time.monotonic()
                with pytest.raises((TimeoutError, RuntimeError)):
                    await run_copilot(
                        state_dir=tmp_path / "sdk", working_directory=work,
                        sdk_session_id="offline-real-sixty-second-deadline", resume=False,
                        prompt="Use a permitted tool only.", tools=[], api_url="https://synthetic.invalid",
                        token_provider=lambda: CANARY, on_event=events.append, timeout_seconds=60,
                        runtime_path=Path(os.environ["M2_TEST_RUNTIME"]), behavior_boundary=boundary)
                elapsed = time.monotonic() - started
                assert entered.is_set() and len(reservations) == len(server.seen) == 1
                assert 59 <= elapsed < 85
                assert boundary.failure is not None and CANARY not in json.dumps(boundary.failure)
                assert boundary.failure["exception_type"] in {"TimeoutError", "CancelledError"}
                assert any(event["type"] in {"m2.aborted", "m2.abort_failed"} for event in events)
                assert all(set(event) <= {"type", "diagnostic"} for event in events)
                print(f"real-sdk-turn-deadline={elapsed:.3f}s; socket-model-dispatches=1; "
                      f"failure={json.dumps(boundary.failure, sort_keys=True)}")
        asyncio.run(run())
