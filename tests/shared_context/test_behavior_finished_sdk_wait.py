"""Real SDK + local API/provider sockets, durable finish and owned-stop phases."""
import asyncio
from datetime import timedelta
import json
import threading
from time import monotonic

import httpx
import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant import key, _Session, _row
from shared_context.behavior_recovery import ClaimedSdkRecovery, MultiModelRecoveryScope, ModelIntentState
from shared_context.errors import Problem
from test_behavior_assistant_claim_boundaries import hosted_flow, reviewed
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_behavior_model_deadlines import asgi_server
from test_behavior_model_diagnostics import LocalHTTP, LocalAsyncHTTP, socket_server, send_bytes
from test_behavior_assistant_sse import stream
from test_behavior_second_model_conflicts import controller_write
from test_behavior_unclaimed_closure import normal_report
from test_m3_auth import jwk_client, signing_key


pytestmark = pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)


@pytest.fixture
def finished_flow(hosted_flow, monkeypatch, request):
    from copilot import CopilotClient
    from copilot.session import CopilotSession
    h, c = hosted_flow, hosted_flow.c
    supplied = getattr(request, "param", "none")
    fault = supplied["fault"] if isinstance(supplied, dict) else supplied
    model_count = supplied.get("models", 3) if isinstance(supplied, dict) else 3
    calls, phases = [], []
    completed, release, history_wait = threading.Event(), threading.Event(), threading.Event()
    started = monotonic()
    monkeypatch.setattr(hosted, "SDK_TURN_SECONDS", 12)
    original_wait, original_events, original_stop = CopilotSession.send_and_wait, CopilotSession.get_events, CopilotClient.stop
    original_dispatch = CopilotSession._dispatch_event
    def mark(phase):
        phases.append({"phase": phase, "elapsed_ms": round((monotonic() - started) * 1000, 3)})
    async def wait(self, *args, **kwargs):
        mark("send_and_wait.begin")
        try:
            return await original_wait(self, *args, **kwargs)
        finally:
            mark("send_and_wait.end")
    async def events(self):
        mark("get_events.begin")
        original = await original_events(self)
        if fault == "history":
            history_wait.set()
            await asyncio.Event().wait()
        mark("get_events.end")
        return original
    async def stop(self):
        mark("owned_stop.begin")
        child = self._cli_process
        try:
            await original_stop(self)
        finally:
            mark("owned_stop.end")
            if child is not None:
                phases.append({"phase": "owned_stop.poll", "exited": child.poll() is not None,
                               "pid_observed": child.pid is not None, "exit_code": child.returncode})
    def dispatch(self, event):
        event_type = getattr(event.type, "value", event.type)
        if event_type in {"tool.execution_complete", "assistant.turn_end", "session.idle", "session.shutdown"}:
            mark(event_type)
        if fault == "idle-event" and event_type == "session.idle" and completed.is_set():
            mark("session.idle.delivery.withheld")
            return
        original_dispatch(self, event)
    monkeypatch.setattr(CopilotSession, "send_and_wait", wait)
    monkeypatch.setattr(CopilotSession, "get_events", events)
    monkeypatch.setattr(CopilotClient, "stop", stop)
    monkeypatch.setattr(CopilotSession, "_dispatch_event", dispatch)
    replies = [
        {"tool_calls": [{"index": 0, "function": {"name": "read_context", "arguments": "{}"}}]},
        {"tool_calls": [{"index": 0, "function": {"name": "save_result",
            "arguments": '{"item_count":3,"completed_count":2,"remaining_count":1}'}}]},
        {"tool_calls": [{"index": 0, "function": {"name": "finish_task", "arguments": "{}"}}]},
    ]
    replies = [replies[0]] * (model_count - 3) + replies
    def provider(handler, _):
        index = len(calls)
        calls.append("model")
        assert index < model_count, "no new model after the terminal tool"
        send_bytes(handler, stream([replies[index]]).content)
    app = c.client.app
    async def api(scope, receive, send):
        if fault == "late-finish-commit" and scope.get("path", "").endswith("/finish"):
            mark("finish.request.received")
            while not release.is_set():
                await asyncio.sleep(0.02)
        async def response(message):
            if scope.get("path", "").endswith("/finish") and message["type"] == "http.response.start":
                assert message["status"] == 200
                completed.set()
                mark("finish.http200.ready")
                if fault == "finish-response":
                    while not release.is_set():
                        await asyncio.sleep(0.02)
            await send(message)
        await app(scope, receive, response)
    with socket_server(provider) as endpoint, asgi_server(api) as port:
        c.composition.assistant.provider.transport = LocalAsyncHTTP(endpoint.port)
        h.api.http.close()
        h.api.http = httpx.Client(transport=LocalHTTP(port), trust_env=False,
                                 follow_redirects=False, timeout=httpx.Timeout(20, connect=5))
        try:
            h.host.start(h.payload, "offline-finished-invocation", c.identity.session_id)
            h.supervisor.join(timeout=35)
            if fault == "late-finish-commit":
                mark("supervisor.returned.before_finish")
                release.set()
                assert completed.wait(5)
            yield h, fault, calls, phases, completed, history_wait
        finally:
            release.set()
            h.supervisor.join(timeout=10)


def test_existing_owned_exit_path_preserves_real_success_and_normal_pull(finished_flow):
    h, fault, calls, phases, completed, _ = finished_flow
    c = h.c
    assert fault == "none" and completed.is_set() and calls == ["model"] * 3
    result = h.supervisor.observe(c.launch_id)
    assert result.phase == "sealed" and result.sdk_stop.graceful and result.outcome == "succeeded"
    controller_write(h, 2)
    c.native.workers.forward_result(c.binding.job_id, c.binding.owner, c.composition.runtime.dep.delivery)
    normal_report(c)
    assert c.native.workers.inspect(c.binding.job_id).state == "succeeded"
    assert c.native.launch(c.launch_id).phase == "sealed"
    print(json.dumps(phases, sort_keys=True))


@pytest.mark.parametrize("finished_flow", ["finish-response", "history", "idle-event", "late-finish-commit"], indirect=True)
def test_sdk_timeout_keeps_finished_result_but_does_not_invent_a_sealed_exit(finished_flow):
    h, fault, calls, phases, completed, history_wait = finished_flow
    c = h.c
    sid = key("session-id", c.starts[0].binding.start_request_id)
    inventory = c.store.read_inventory()
    session = _row(inventory.rows, key("session", sid), _Session)
    assert completed.is_set() and calls == ["model"] * 3
    assert session.state == "ready_to_finalize" and session.model_requests == 3 and session.context_read
    assert key("result", c.binding.work.work_id) in inventory.rows
    assert inventory.rows[key("artifact", c.binding.work.work_id)].payload["data"]["stage"] == "saved"
    if fault == "history":
        assert history_wait.is_set()
    with h.supervisor.journal.connect() as db:
        row = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (c.launch_id,)).fetchone()
        diagnostic = None if row is None else json.loads(row[0])
    print(json.dumps({"fault": fault, "phases": phases, "first_safe_failure": diagnostic}, sort_keys=True))
    assert h.supervisor.observe(c.launch_id).phase == "spawn_unknown"
    assert diagnostic["upstream_error_phase"] == "sdk_turn" and diagnostic["exception_type"] == "TimeoutError"
    assert any(phase["phase"] == "owned_stop.poll" and phase["exited"] for phase in phases)


def test_current_cancel_scopes_do_not_discard_existing_success_result(finished_flow):
    h, _, calls, _, completed, _ = finished_flow
    c = h.c
    assert completed.is_set() and calls == ["model"] * 3
    # Before a native report, the Table launch remains claimed despite real
    # local owned-exit proof. Current cancellation schemas must not overwrite it.
    inventory = c.store.read_inventory()
    sid = key("session-id", c.starts[0].binding.start_request_id)
    intents = sorted((ModelIntentState(request_sha256=record.payload["data"]["request_sha256"],
                        consumed=record.payload["data"]["consumed"])
                      for row_id, record in inventory.rows.items()
                      if isinstance(d := record.payload["data"].get("request_sha256"), str)
                      and row_id == key("model-intent", sid, d)), key=lambda item: item.request_sha256)
    scope = MultiModelRecoveryScope(recovery_id="finished-original", configuration_sha256=c.config.sha256,
        job_id=c.binding.job_id, launch_id=c.launch_id, operator_oid=c.config.identity.human_oid,
        expires_at=c.clock[0] + timedelta(minutes=10), recovery_phase="after_sdk_model_requests",
        model_intents=tuple(intents))
    def no_stop(*_):
        pytest.fail("no approved finished-result stop path exists in the old scope")
    service = ClaimedSdkRecovery(c.config, c.native, scope, no_stop,
                                delivery=c.composition.runtime.dep.delivery, clock=lambda: c.clock[0])
    with pytest.raises(Problem) as blocked:
        service.plan(c.principal)
    assert blocked.value.body["error"]["code"] == "RECOVERY_MODEL_SET_CHANGED"
    assert c.store.read_inventory() == inventory


def test_original_graceful_success_still_needs_its_distinct_retirement_contract(finished_flow):
    h, _, calls, _, completed, _ = finished_flow
    c = h.c
    assert completed.is_set() and h.supervisor.observe(c.launch_id).phase == "sealed"
    controller_write(h, 2)
    c.native.workers.forward_result(c.binding.job_id, c.binding.owner, c.composition.runtime.dep.delivery)
    normal_report(c)
    assert c.native.workers.inspect(c.binding.job_id).state == "succeeded" and len(calls) == 3
