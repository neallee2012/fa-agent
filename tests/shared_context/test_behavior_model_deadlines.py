"""Real ASGI/HTTP sockets and producer receipts; never a cloud model service."""
from contextlib import contextmanager
import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from datetime import timedelta

from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_model_diagnostics import BODY, CANARY, LocalHTTP, socket_server, send_bytes
from test_behavior_assistant_sse import stream
from behavior_assistant_acquire_conflicts_helpers import installed_assistant
from test_m3_auth import jwk_client, signing_key
from types import SimpleNamespace
from pydantic import SecretStr


@contextmanager
def asgi_server(app):
    import logging
    # uvicorn.Config applies its logging config (uvicorn propagate=False, levels, handlers); restore it so a
    # later test's caplog still receives uvicorn.error records.
    saved = [(logger, logger.level, logger.propagate, logger.disabled, list(logger.handlers))
             for logger in map(logging.getLogger, ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"))]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", access_log=False,
                            log_level="critical", loop="asyncio", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    try:
        end = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < end:
            time.sleep(0.01)
        assert server.started and thread.is_alive()
        yield port
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        for logger, level, propagate, disabled, handlers in saved:
            logger.setLevel(level)
            logger.propagate, logger.disabled, logger.handlers[:] = propagate, disabled, handlers
        assert not thread.is_alive(), "owned ASGI server must finish after test waiters are released"


def request():
    return httpx.Request("POST", "https://synthetic.invalid/model", json=BODY)


@pytest.mark.parametrize("stage", ["model-token", "reserve-token"])
def test_sdk_turn_returns_without_joining_late_sync_credential(tmp_path, monkeypatch, stage):
    from shared_context import behavior_assistant_hosted as hosted
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def token():
        entered.set()
        assert release.wait(2)
        finished.set()
        return CANARY
    async def sdk(**kwargs):
        async with asyncio.timeout(0.1):
            if stage == "model-token":
                await kwargs["behavior_boundary"].sender(request())
            else:
                await kwargs["behavior_boundary"].before_request(digest(BODY))
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    monkeypatch.setattr(hosted, "SDK_TURN_SECONDS", 0.1)
    with socket_server(lambda h, _: send_bytes(h, stream([{}], finish_reason="stop").content)) as server:
        client = hosted.AssistantClient("https://synthetic.invalid", SimpleNamespace(
            binding_id="binding", session_id="a" * 64, sdk_session_id="a" * 64, resume=False,
            task=SimpleNamespace(instruction=None)),
            SecretStr("b" * 64), token_provider=token, transport=LocalHTTP(server.port))
        monkeypatch.setattr(client, "stopped", lambda _: None)
        timer = threading.Timer(0.7, release.set)
        timer.start()
        try:
            start = time.monotonic()
            with pytest.raises(Problem):
                asyncio.run(hosted.AssistantSession(client, root=tmp_path,
                    runtime_path=tmp_path / "not-used")._run(tmp_path))
            elapsed = time.monotonic() - start
            assert entered.is_set() and elapsed < 0.4
        finally:
            release.set()
            timer.cancel()
            timer.join(2)
            assert finished.wait(2)
            client.close()
        assert server.seen == [], "cancelled turn cannot grant late first dispatch"


def test_client_absolute_wait_expires_despite_regular_socket_chunks(monkeypatch):
    from shared_context import behavior_assistant_hosted as hosted
    monkeypatch.setattr(hosted, "CLIENT_MODEL_SECONDS", 0.2)
    def api_http(handler, release):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", "65536")
        handler.end_headers()
        for _ in range(100):
            if release.wait(0.02):
                break
            handler.wfile.write(b"data: ")
            handler.wfile.flush()
    with socket_server(api_http) as server:
        client = hosted.AssistantClient("https://synthetic.invalid", SimpleNamespace(
            binding_id="b", session_id="a" * 64, task=SimpleNamespace(instruction=None)), SecretStr("b" * 64),
            token_provider=lambda: CANARY, transport=LocalHTTP(server.port))
        try:
            started = time.monotonic()
            with pytest.raises(Problem) as unknown:
                asyncio.run(client.send_model(request()))
            assert 0.15 < time.monotonic() - started < 0.8
            assert unknown.value.body["error"]["upstream_error_phase"] == "client_model_timeout"
            assert len(server.seen) == 1
        finally:
            server.release.set()
            client.close()


def test_deadline_defaults_are_assistant_only_and_not_http_flags():
    import inspect
    from shared_context.behavior_assistant_deadlines import SERVER_MODEL_SECONDS, CLIENT_MODEL_SECONDS, SDK_TURN_SECONDS
    from shared_context.m2_copilot import run_copilot
    assert (SERVER_MODEL_SECONDS, CLIENT_MODEL_SECONDS, SDK_TURN_SECONDS) == (75, 90, 240)
    assert inspect.signature(run_copilot).parameters["timeout_seconds"].default == 240
    from shared_context.behavior_assistant_wire import ModelRequest
    # Later opt-in fields (checkpoint_compaction 230c7ca1, delegation) are not deadlines and stay off the default wire.
    assert set(ModelRequest.model_fields) == {"request_id", "body", "checkpoint_compaction", "delegation"}
    assert not any(word in name for name in ModelRequest.model_fields for word in ("deadline", "timeout", "seconds"))
    assert set(ModelRequest(request_id="a" * 64, body={}).model_dump(mode="json")) == {"request_id", "body"}
