"""Real ASGI/HTTP sockets and producer receipts; never a cloud model service."""
from contextlib import contextmanager
import asyncio
import json
import os
from pathlib import Path
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from datetime import timedelta

from shared_context import behavior_assistant_api as api
from shared_context.behavior_server import application
from shared_context.errors import Problem
from shared_context.source_materials import digest
from shared_context.behavior_assistant import key, _Session, _row
from shared_context.behavior_operations import control_key
from test_behavior_model_diagnostics import BODY, CANARY, LocalHTTP, LocalAsyncHTTP, socket_server, send_bytes
from test_behavior_assistant_sse import stream
from test_behavior_assistant_acquire_conflicts import native_acquire, current_rotation
from test_behavior_rotation_cutoff import two_rotations
from test_behavior_rotation_settlement_history import current_cancelled
from test_behavior_runtime_rotation import rotated
from test_behavior_assistant_claim_boundaries import reviewed
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


@contextmanager
def model_stack(c, provider_handle):
    with socket_server(provider_handle) as provider:
        c.composition.assistant.provider.transport = LocalAsyncHTTP(provider.port)
        c.composition.assistant.provider.credential = SimpleNamespace(
            get_token=lambda _: SimpleNamespace(token=CANARY))
        with asgi_server(application(c.composition)) as port:
            c.hosted.http.close()
            c.hosted.http = httpx.Client(transport=LocalHTTP(port), timeout=httpx.Timeout(20, connect=5),
                                        trust_env=False, follow_redirects=False)
            yield provider


def request():
    return httpx.Request("POST", "https://synthetic.invalid/model", json=BODY)


@pytest.mark.parametrize("wait_at", ["provider", "last-gate"])
def test_legal_buffered_response_over_old_twenty_seconds_survives(native_acquire, monkeypatch, wait_at):
    c = native_acquire
    reviewed(c)
    def provider_http(handler, _):
        if wait_at == "provider":
            time.sleep(21)
        send_bytes(handler, stream([{}], finish_reason="stop").content)
    with model_stack(c, provider_http) as provider:
        c.hosted.acquire()
        c.hosted.reserve_model(digest(BODY))
        if wait_at == "last-gate":
            original = c.composition.require_current_native_evidence
            intent_key = key("model-intent", c.assignment.session_id, digest(BODY))
            def slow():
                if c.store.read_snapshot((intent_key,))[intent_key].payload["data"]["consumed"]:
                    time.sleep(21)
                return original()
            monkeypatch.setattr(c.composition, "require_current_native_evidence", slow)
        started = time.monotonic()
        response = c.hosted.model(request())
        elapsed = time.monotonic() - started
        assert response.status_code == 200 and b"[DONE]" in response.content
        assert 20 < elapsed < 35 and len(provider.seen) == 1
        assert c.hosted.http.timeout.read == 20, "non-model routes retain their original timeout"
        print(f"wait_at={wait_at}; buffered-response={elapsed:.3f}s; provider-posts=1; other-route-read-timeout=20")


def test_absolute_route_deadline_starts_before_first_native_evidence(native_acquire, monkeypatch):
    c = native_acquire
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    reviewed(c)
    wrapped = [route for route in c.client.app.routes if isinstance(route, api._ModelRoute)]
    assert len(wrapped) == 1 and wrapped[0].methods == {"POST"}
    assert wrapped[0].path == api.PREFIX + "/sessions/{session_id}/model"
    monkeypatch.setattr(api, "SERVER_MODEL_SECONDS", 0.2, raising=False)
    with model_stack(c, lambda h, _: send_bytes(h, stream([{}], finish_reason="stop").content)) as provider:
        c.hosted.acquire()
        c.hosted.reserve_model(digest(BODY))
        original = c.composition.require_current_native_evidence
        def blocked():
            entered.set()
            assert release.wait(2)
            try:
                return original()
            finally:
                completed.set()
        monkeypatch.setattr(c.composition, "require_current_native_evidence", blocked)
        c.hosted.http.headers["x-behavior-model-timeout"] = "999999"
        timer = threading.Timer(1, release.set)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(Problem) as deadline:
                c.hosted.model(request())
            elapsed = time.monotonic() - started
            assert entered.is_set() and elapsed < 0.8
            assert deadline.value.body["error"]["upstream_error_phase"] == "server_model_deadline"
        finally:
            release.set()
            timer.cancel()
            timer.join(2)
            assert completed.wait(2)
        assert provider.seen == [], "late evidence completion cannot resume provider dispatch"


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


@pytest.mark.parametrize("stage", ["token", "last-gate", "consume"])
def test_late_sync_server_stage_cannot_resume_provider_after_deadline(native_acquire, monkeypatch, stage):
    c = native_acquire
    reviewed(c)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(api, "SERVER_MODEL_SECONDS", 2)
    with model_stack(c, lambda h, _: send_bytes(h, stream([{}], finish_reason="stop").content)) as provider:
        c.hosted.acquire()
        c.hosted.reserve_model(digest(BODY))
        intent_key = key("model-intent", c.assignment.session_id, digest(BODY))
        budget_key = control_key("budget", c.binding.owner.root_budget_id)
        before = c.store.read_snapshot((budget_key,))[budget_key]
        def pause(function, *args):
            entered.set()
            assert release.wait(5)
            try:
                return function(*args)
            finally:
                finished.set()
        if stage == "token":
            c.composition.assistant.provider.credential = SimpleNamespace(
                get_token=lambda _: pause(lambda: SimpleNamespace(token=CANARY)))
        elif stage == "last-gate":
            original = c.composition.require_current_native_evidence
            def slow():
                if c.store.read_snapshot((intent_key,))[intent_key].payload["data"]["consumed"]:
                    return pause(original)
                return original()
            monkeypatch.setattr(c.composition, "require_current_native_evidence", slow)
        else:
            original = c.authority.consume_model
            monkeypatch.setattr(c.authority, "consume_model", lambda *args: pause(original, *args))
        try:
            started = time.monotonic()
            with pytest.raises(Problem) as unknown:
                asyncio.run(c.hosted.send_model(request()))
            elapsed = time.monotonic() - started
            assert entered.is_set() and 1.5 < elapsed < 3.5
            assert unknown.value.body["error"]["upstream_error_phase"] == "server_model_deadline"
        finally:
            release.set()
            assert finished.wait(3)
        assert provider.seen == []
        assert c.store.read_snapshot((intent_key,))[intent_key].payload["data"]["consumed"] is True
        assert c.store.read_snapshot((budget_key,))[budget_key] == before
        assert _row(c.store.read_inventory().rows, key("session", c.assignment.session_id), _Session).model_requests == 1
        with pytest.raises(Problem):
            asyncio.run(c.hosted.send_model(request()))
        assert provider.seen == [], "late committed receipt is not a new provider dispatch permit"


def test_drip_stream_exceeds_absolute_server_budget_not_inactivity(native_acquire, monkeypatch):
    c = native_acquire
    reviewed(c)
    monkeypatch.setattr(api, "SERVER_MODEL_SECONDS", 2)
    entered = threading.Event()
    def provider_http(handler, release):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", "65536")
        handler.end_headers()
        entered.set()
        for _ in range(200):
            if release.wait(0.02):
                break
            handler.wfile.write(b"data: ")
            handler.wfile.flush()
    with model_stack(c, provider_http) as provider:
        c.hosted.acquire()
        c.hosted.reserve_model(digest(BODY))
        started = time.monotonic()
        with pytest.raises(Problem) as unknown:
            asyncio.run(c.hosted.send_model(request()))
        elapsed = time.monotonic() - started
        assert entered.is_set() and 1.5 < elapsed < 3.5
        detail = unknown.value.body["error"]
        assert detail["upstream_status"] == 504 and detail["upstream_error_phase"] == "server_model_deadline"
        assert len(provider.seen) == 1
        print(f"drip-absolute-server={elapsed:.3f}s; injected-budget=2s; provider-io-limit=60s; provider-posts=1")


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


@pytest.mark.parametrize("revocation", ["cancel", "policy"])
def test_authority_change_after_consume_is_refused_at_the_next_reserve(native_acquire, revocation):
    """consume's CAS gate is the last check before send (seconds-level window); nothing is resent."""
    c = native_acquire
    reviewed(c)
    with model_stack(c, lambda h, _: send_bytes(h, stream([{}], finish_reason="stop").content)) as provider:
        c.hosted.acquire()
        c.hosted.reserve_model(digest(BODY))
        def token(_):
            if revocation == "cancel":
                c.native.workers.cancel(c.binding.job_id, c.binding.owner, expected_binding=c.starts[0].binding)
            else:
                c.clock[0] = c.config.policies[0].policy.valid_until + timedelta(seconds=1)
            return SimpleNamespace(token=CANARY)
        c.composition.assistant.provider.credential = SimpleNamespace(get_token=token)
        if revocation == "policy":
            # Legacy (non-instruction) egress keeps the provider's own evidence gate, which sees the expiry.
            with pytest.raises(Problem):
                asyncio.run(c.hosted.send_model(request()))
            assert provider.seen == []
            return
        asyncio.run(c.hosted.send_model(request()))
        assert len(provider.seen) == 1
        intent = c.store.read_snapshot((key("model-intent", c.assignment.session_id, digest(BODY)),))
        assert next(iter(intent.values())).payload["data"]["consumed"] is True
        with pytest.raises(Problem):
            c.hosted.reserve_model(digest({**BODY, "temperature": 0.5}))
        assert len(provider.seen) == 1


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


def test_six_model_reservations_remain_exactly_bounded_without_provider_or_refund(native_acquire):
    c = native_acquire
    reviewed(c)
    with model_stack(c, lambda h, _: send_bytes(h, stream([{}], finish_reason="stop").content)) as provider:
        c.hosted.acquire()
        budget_key = control_key("budget", c.binding.owner.root_budget_id)
        before = c.store.read_snapshot((budget_key,))[budget_key].payload["data"]
        for i in range(6):
            c.hosted.reserve_model(digest({**BODY, "temperature": i / 10}))
        with pytest.raises(Problem) as denied:
            c.hosted.reserve_model(digest({**BODY, "temperature": 0.7}))
        assert denied.value.body["error"]["upstream_status"] == 429
        after = c.store.read_snapshot((budget_key,))[budget_key].payload["data"]
        assert after["remaining_units"] == before["remaining_units"] - 6
        assert after["remaining_attempts"] == before["remaining_attempts"] - 6
        assert _row(c.store.read_inventory().rows, key("session", c.assignment.session_id), _Session).model_requests == 6
        assert provider.seen == []


@pytest.mark.parametrize("cutoff", ["server", "client", "sdk"])
def test_real_sdk_turn_cancel_keeps_inflight_unknown_and_never_starts_second_sdk(native_acquire, monkeypatch, tmp_path, cutoff):
    from shared_context import behavior_assistant_hosted as hosted
    from shared_context.behavior_hosted import LocalAssignment
    c = native_acquire
    reviewed(c)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(hosted, "SDK_TURN_SECONDS", 8)
    if cutoff == "server":
        monkeypatch.setattr(api, "SERVER_MODEL_SECONDS", 2)
    elif cutoff == "client":
        monkeypatch.setattr(hosted, "CLIENT_MODEL_SECONDS", 2)
    def provider_http(handler, _):
        entered.set()
        assert release.wait(25)
        try:
            send_bytes(handler, stream([{}], finish_reason="stop").content)
        finally:
            returned.set()
    supervisor = hosted.AssistantSupervisor(tmp_path / "host",
        runtime_path=Path(os.environ["M2_TEST_RUNTIME"]))
    assignment = LocalAssignment.model_validate_json(c.native_claim.assignment.model_dump_json())
    budget_key = control_key("budget", c.binding.owner.root_budget_id)
    before = c.store.read_snapshot((budget_key,))[budget_key].payload["data"]
    with model_stack(c, provider_http) as provider:
        try:
            started = time.monotonic()
            supervisor.start_assistant(assignment, c.hosted)
            supervisor.join(timeout=18)
            elapsed = time.monotonic() - started
            assert entered.is_set() and not returned.is_set()
            assert (7.5 if cutoff == "sdk" else 1.5) < elapsed < 18 and len(provider.seen) == 1
            state = supervisor.observe(c.launch_id)
            assert state.phase == "spawn_unknown" and state.child_pid is None and state.sdk_stop is None
            with supervisor.journal.connect() as db:
                first = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (c.launch_id,)).fetchone()[0]
            assert json.loads(first)["upstream_error_phase"] == {
                "server": "server_model_deadline", "client": "client_model_timeout", "sdk": "sdk_turn",
            }[cutoff]
            with pytest.raises(Problem):
                supervisor.start_assistant(assignment, c.hosted)
            assert len(supervisor.threads) == 1 and len(provider.seen) == 1
            with supervisor.journal.connect() as db:
                assert db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (c.launch_id,)).fetchone()[0] == first
        finally:
            release.set()
            supervisor.join(timeout=10)
            assert returned.wait(3)
        saved = c.store.read_inventory()
        session = _row(saved.rows, key("session", c.assignment.session_id), _Session)
        assert session.state == "waiting" and session.model_requests == 1 and session.context_read is False
        assert saved.rows[budget_key].payload["data"]["remaining_units"] == before["remaining_units"] - 1
        assert key("result", c.binding.work.work_id) not in saved.rows
        assert len(provider.seen) == 1
        print(f"cutoff={cutoff}; actual-sdk-return={elapsed:.3f}s; injected-turn=8s; "
              "in-flight-provider-at-return=True; SDK-children=1")
