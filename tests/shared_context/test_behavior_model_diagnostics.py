"""Local sockets enforce HTTPX timeouts; no cloud credentials or model services."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.m2_copilot import _ModelGateway
from shared_context.errors import Problem
from test_behavior_assistant_sse import stream
from behavior_assistant_acquire_conflicts_helpers import installed_assistant
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
        self.port, self.http, self.timeouts = port, httpx.HTTPTransport(retries=0), []
    def handle_request(self, request):
        self.timeouts.append((request.url.path, dict(request.extensions["timeout"])))
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
            assert (await pending).status_code == 200
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
            assert (await task).status_code == 200
            assert entered.is_set() and elapsed < 0.3
        asyncio.run(run())


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
