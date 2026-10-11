"""Original-session keepalive uses read-only ingress; no SDK process or live service."""

import asyncio
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from azure.ai.projects.models import AgentSessionResource, AgentVersionDetails
from azure.core.exceptions import HttpResponseError
from fastapi.testclient import TestClient
import httpx
import pytest

from shared_context.behavior_foundry import FoundryHttpClient
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_assistant_hosted import AssistantHostedRuntime, AssistantSupervisor
from shared_context.behavior_hosted import LocalAssignment, create_host
from shared_context.errors import Problem
from test_behavior_foundry import TARGET, INVOCATION, Credential
from test_conversation_transfer_startup import claimed_turn, synthetic_standing_v1
from test_instruction_observer_resume import observer_case
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_agent_message_continuation import start_peer, claim_peer, message_turn, peer_runtime, routing_peer


def platform_session(status="active", **changes):
    return AgentSessionResource({
        "agent_session_id": TARGET.session_id,
        "version_indicator": {"type": "version_ref", "agent_version": TARGET.agent_version},
        "status": status, "created_at": 1, "last_accessed_at": 2, "expires_at": 2_000_000_000,
        **changes,
    })


@pytest.mark.parametrize("status", ["active", "idle", "failed", "deleting", "deleted", "expired"])
def test_session_status_keeps_original_platform_state_without_create_stop_or_invocation(monkeypatch, status):
    seen = []
    value = platform_session(status)

    def get_session(agent_name, session_id):
        seen.append((agent_name, session_id))
        return value

    @contextmanager
    def project(**options):
        assert options["endpoint"] == TARGET.project_endpoint
        assert all(options[name] == 0 for name in ("retry_total", "retry_connect", "retry_read", "retry_status"))
        yield SimpleNamespace(agents=SimpleNamespace(get_session=get_session))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", project)
    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(
        lambda _: pytest.fail("session metadata must not invoke the Hosted process")))
    try:
        assert client.session_status() is value
        assert seen == [(TARGET.agent_name, TARGET.session_id)]
    finally:
        client.close()


@pytest.mark.parametrize("seconds,expected", [(120, 120), (900, 900), (3600, 3600), (None, 900)])
def test_idle_timeout_comes_from_the_original_version_not_the_latest(monkeypatch, seconds, expected):
    calls = []
    definition = {"kind": "hosted", "cpu": "1", "memory": "2Gi"}
    if seconds is not None:
        definition["session_configuration"] = {"idle_timeout_seconds": seconds}
    value = AgentVersionDetails({
        "id": "original-version", "name": TARGET.agent_name, "version": TARGET.agent_version,
        "created_at": 1, "metadata": {}, "definition": definition,
    })

    def get_version(name, version):
        calls.append((name, version))
        return value

    @contextmanager
    def project(**options):
        assert options["retry_total"] == 0
        yield SimpleNamespace(agents=SimpleNamespace(get_version=get_version))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", project)
    client = FoundryHttpClient(TARGET, Credential())
    try:
        assert client.session_idle_timeout() == expected
        assert calls == [(TARGET.agent_name, TARGET.agent_version)]
    finally:
        client.close()


@pytest.mark.parametrize("failure,expected", [
    ("read", (503, "CONVERSATION_SESSION_UNAVAILABLE")),
    ("identity", (409, "CONVERSATION_SESSION_CHANGED")),
    ("version", (409, "CONVERSATION_SESSION_CHANGED")),
])
def test_session_read_failure_and_mismatch_are_not_stopping_proof(monkeypatch, failure, expected):
    seen = []

    def get_session(*args):
        seen.append(args)
        if failure == "read":
            raise HttpResponseError("PRIVATE_PLATFORM_MESSAGE")
        return platform_session("idle", **(
            {"agent_session_id": "another-session"} if failure == "identity" else
            {"version_indicator": {"type": "version_ref", "agent_version": "99"}}))

    @contextmanager
    def project(**_options):
        yield SimpleNamespace(agents=SimpleNamespace(get_session=get_session))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", project)
    client = FoundryHttpClient(TARGET, Credential())
    try:
        with pytest.raises(Problem) as error:
            client.session_status()
        assert (error.value.status, error.value.body["error"]["code"]) == expected
        assert "PRIVATE" not in str(error.value.body) and len(seen) == 1
    finally:
        client.close()


def test_keepalive_reuses_original_get_invocation_without_a_probe_or_start():
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, headers={
            "x-agent-invocation-id": INVOCATION, "x-agent-session-id": TARGET.session_id,
        }, json={"launch_id": "original-launch", "invocation_id": INVOCATION,
                 "session_id": TARGET.session_id, "read_only": True})

    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(receive))
    checks = []
    try:
        client.keepalive(INVOCATION, "original-launch", before_send=lambda: checks.append(True))
        request, = requests
        assert request.method == "GET" and request.url.path.endswith("/invocations/" + INVOCATION)
        assert request.content == b"" and dict(request.url.params) == {"api-version": "v1"}
        assert request.headers[wire.NATIVE_KEEPALIVE_HEADER] == "original-launch"
        assert wire.NATIVE_PROBE_HEADER not in request.headers
        assert wire.NATIVE_LAUNCH_PERMIT_HEADER not in request.headers
        assert checks == [True]
        assert client.last_session_request() is not None
        assert client.for_target(TARGET).last_session_request() == client.last_session_request()
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["lost-response", "http-error", "wrong-session", "wrong-launch", "not-readonly"])
def test_keepalive_failure_never_resends_or_accepts_a_foreign_observation(failure):
    requests = []

    def receive(request):
        requests.append(request)
        if failure == "lost-response":
            raise httpx.ReadTimeout("PRIVATE_RESPONSE_DETAILS")
        value = {"launch_id": "original-launch", "invocation_id": INVOCATION,
                 "session_id": TARGET.session_id, "read_only": True}
        if failure == "wrong-session":
            value["session_id"] = "different-session"
        elif failure == "wrong-launch":
            value["launch_id"] = "different-launch"
        elif failure == "not-readonly":
            value["read_only"] = False
        return httpx.Response(503 if failure == "http-error" else 200, headers={
            "x-agent-invocation-id": INVOCATION, "x-agent-session-id": TARGET.session_id,
        }, json=value)

    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(receive))
    try:
        with pytest.raises(Problem) as refused:
            client.keepalive(INVOCATION, "original-launch", before_send=lambda: None)
        assert len(requests) == 1 and requests[0].method == "GET"
        assert "PRIVATE_RESPONSE_DETAILS" not in str(refused.value.body)
    finally:
        client.close()


@pytest.fixture
def hosted_keepalive(observer_case, tmp_path, monkeypatch):
    c, loop = observer_case
    opened = claimed_turn(c, "original-kept-alive")
    assignment = LocalAssignment.model_validate(opened.claimed.assignment.model_dump())
    supervisor = AssistantSupervisor(tmp_path / "owned-hosted", runtime_path=Path(__file__))
    supervisor.supervisor_id = assignment.process_id
    supervisor.journal.reserve_claim(assignment.invocation_id, assignment.launch_id, opened.binding.binding_id,
                                     assignment.process_id, "a" * 64)
    supervisor.journal.reserve(assignment, supervisor.supervisor_id)
    cancelled = Event()
    supervisor.threads[assignment.launch_id] = SimpleNamespace(is_alive=lambda: True)
    supervisor.clients[assignment.launch_id] = SimpleNamespace(assignment=opened.assignment)
    supervisor.sessions[assignment.launch_id] = SimpleNamespace(_cancel_requested=cancelled)

    def no_api(*args, **kwargs):
        pytest.fail("keepalive must not resolve/report a probe, claim, control or start")

    host = AssistantHostedRuntime(assignment.identity, supervisor,
        SimpleNamespace(claim=no_api, resolve_probe=no_api, report=no_api))
    host.report_tokens[assignment.launch_id] = opened.claimed.report_token
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", assignment.identity.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_ID", assignment.identity.agent_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", assignment.identity.agent_name)
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", assignment.identity.agent_version)
    with TestClient(create_host(host), backend_options={"loop_factory": lambda: loop}) as client:
        local = _LocalHttp(client)
        ingress = []

        def receive(request):
            ingress.append(request)
            headers = dict(request.headers)
            headers["x-agent-invocation-id"] = assignment.invocation_id
            headers["x-agent-session-id"] = assignment.identity.session_id
            return local.handle_request(httpx.Request(
                request.method, request.url.copy_with(path=request.url.path.split("/endpoint/protocols", 1)[1]),
                headers=headers, content=request.content))

        foundry = FoundryHttpClient(opened.launch.target, Credential(), transport=httpx.MockTransport(receive))
        try:
            yield SimpleNamespace(c=c, opened=opened, assignment=assignment, supervisor=supervisor,
                host=host, client=client, foundry=foundry, ingress=ingress, cancelled=cancelled)
        finally:
            foundry.close()


def test_actual_host_keepalive_reads_original_owner_without_writing_or_sending_a_report(hosted_keepalive):
    h = hosted_keepalive
    before = h.supervisor.journal.path.read_bytes()
    guard = h.c.h.runtime.store._guard()
    result = h.foundry.keepalive(h.assignment.invocation_id, h.assignment.launch_id, before_send=lambda: None)
    assert result.model_dump() == {
        "launch_id": h.assignment.launch_id, "invocation_id": h.assignment.invocation_id,
        "session_id": h.assignment.identity.session_id, "read_only": True,
    }
    assert h.supervisor.journal.path.read_bytes() == before
    assert h.c.h.runtime.store._guard() == guard
    assert len(h.ingress) == 1 and h.ingress[0].method == "GET"
    assert h.c.sessions.calls == []


def test_host_keepalive_refuses_wrong_or_inactive_owners_without_control_writes(hosted_keepalive, monkeypatch):
    h = hosted_keepalive
    before = h.supervisor.journal.path.read_bytes()
    guard = h.c.h.runtime.store._guard()
    for condition in ("process", "thread", "claim", "engine", "cancel", "launch"):
        with monkeypatch.context() as patch:
            if condition == "process":
                patch.setattr(h.supervisor, "supervisor_id", "replacement-process")
            elif condition == "thread":
                patch.setitem(h.supervisor.threads, h.assignment.launch_id, SimpleNamespace(is_alive=lambda: False))
            elif condition == "claim":
                patch.delitem(h.host.report_tokens, h.assignment.launch_id)
            elif condition == "engine":
                patch.delitem(h.supervisor.sessions, h.assignment.launch_id)
            elif condition == "cancel":
                h.cancelled.set()
            with pytest.raises(Problem) as error:
                h.foundry.keepalive(h.assignment.invocation_id,
                    "another-launch" if condition == "launch" else h.assignment.launch_id, before_send=lambda: None)
            assert error.value.body["error"]["code"] == "FOUNDRY_INVOCATION_UNKNOWN"
            h.cancelled.clear()
        assert h.supervisor.journal.path.read_bytes() == before
        assert h.c.h.runtime.store._guard() == guard
    assert h.c.sessions.calls == []


def test_host_keepalive_rejects_control_headers_body_and_cancel_route(hosted_keepalive):
    h = hosted_keepalive
    path = "/invocations/" + h.assignment.invocation_id
    headers = {
        "x-agent-invocation-id": h.assignment.invocation_id,
        "x-agent-session-id": h.assignment.identity.session_id,
        wire.NATIVE_KEEPALIVE_HEADER: h.assignment.launch_id,
    }
    before = h.supervisor.journal.path.read_bytes()
    for method, route, values, content in (
        ("GET", path, {**headers, wire.NATIVE_PROBE_HEADER: "a" * 64}, b""),
        ("GET", path, headers, b"{}"),
        ("POST", path + "/cancel", headers, b"{}"),
    ):
        response = h.client.request(method, route, headers=values, content=content)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "HOSTED_KEEPALIVE_INVALID"
    assert h.supervisor.journal.path.read_bytes() == before


@pytest.mark.parametrize("fail_first", [False, True], ids=["long-active-turn", "failed-keepalive"])
def test_long_turn_keeps_original_ingress_for_multiple_intervals_without_any_job_or_guard_write(
    hosted_keepalive, monkeypatch, fail_first, caplog,
):
    from shared_context import behavior_foundry as foundry
    from shared_context import behavior_session_keepalive as keepalive
    from shared_context import behavior_assistant

    h = hosted_keepalive
    clock = [0.0]
    monkeypatch.setattr(keepalive, "monotonic", lambda: clock[0])
    monkeypatch.setattr(foundry, "monotonic", lambda: clock[0])
    monkeypatch.setattr(behavior_assistant, "monotonic", lambda: clock[0])
    h.c.authority._instruction_activity[h.opened.binding.job_id] = (h.opened.session.execution, 0.0)
    composition = h.c.h.composition
    monkeypatch.setattr(composition, "clients", (h.foundry,))
    h.foundry._session_requests[h.foundry.target] = 0.0
    metadata = AgentSessionResource({
        "agent_session_id": h.assignment.identity.session_id,
        "version_indicator": {"type": "version_ref", "agent_version": h.assignment.identity.agent_version},
        "status": "active", "created_at": 1, "last_accessed_at": 2, "expires_at": 2_000_000_000,
    })
    monkeypatch.setattr(FoundryHttpClient, "session_status", lambda _: metadata)
    monkeypatch.setattr(FoundryHttpClient, "session_idle_timeout", lambda _: 900)
    original = h.host.keepalive
    attempts = []

    def answer(*args):
        attempts.append(args)
        if fail_first and len(attempts) == 1:
            raise Problem(503, "HOSTED_KEEPALIVE_UNAVAILABLE", "PRIVATE_FAILURE_DETAILS")
        return original(*args)

    monkeypatch.setattr(h.host, "keepalive", answer)
    state = keepalive.InstructionSessionKeepalive(composition, h.opened.binding.binding_id)
    rows = deepcopy(h.c.h.table.backing.rows)
    journal = h.supervisor.journal.path.read_bytes()
    for elapsed, expected in ((0, 0), (149, 0), (150, 1), (151, 1), (300, 2), (451, 3), (901, 4), (1501, 5)):
        clock[0] = float(elapsed)
        state.tick()
        assert len(h.ingress) == expected, (elapsed, caplog.text)
        assert h.c.h.table.backing.rows == rows
        assert h.supervisor.journal.path.read_bytes() == journal
    assert attempts == [(h.assignment.invocation_id, h.assignment.identity.session_id, h.assignment.launch_id)] * 5
    assert "PRIVATE_FAILURE_DETAILS" not in caplog.text
    if fail_first:
        assert "HOSTED_SESSION_KEEPALIVE_UNAVAILABLE" in caplog.text
    h.c.h.runtime.controller.step(h.opened.binding.binding_id)
    assert h.c.h.table.backing.rows == rows, "confirmed keepalive must preserve the #212 no-query path"
    assert h.c.sessions.calls == []


@pytest.mark.parametrize("reason", ["cancelled", "lost", "idle", "failed", "deleted", "expired", "status-unavailable"])
def test_observer_does_not_keep_cancelled_lost_or_nonactive_sessions_alive(hosted_keepalive, monkeypatch, reason):
    from shared_context import behavior_session_keepalive as keepalive

    h = hosted_keepalive
    composition = h.c.h.composition
    monkeypatch.setattr(composition, "clients", (h.foundry,))
    monkeypatch.setattr(FoundryHttpClient, "session_idle_timeout", lambda _: 900)
    lost = {h.opened.binding.binding_id} if reason == "lost" else set()
    monkeypatch.setattr(composition, "_instruction_lost", lost, raising=False)
    if reason == "cancelled":
        h.c.h.runtime.dep.workers.cancel(h.opened.binding.job_id, h.opened.binding.owner,
                                         expected_binding=h.opened.session.execution)

    def status(_):
        if reason == "status-unavailable":
            raise Problem(503, "CONVERSATION_SESSION_UNAVAILABLE", "not a stopped-session proof")
        return SimpleNamespace(status=reason if reason in {"idle", "failed", "deleted", "expired"} else "active")

    monkeypatch.setattr(FoundryHttpClient, "session_status", status)
    rows = deepcopy(h.c.h.table.backing.rows)
    keepalive.InstructionSessionKeepalive(composition, h.opened.binding.binding_id).tick()
    assert h.ingress == [] and h.c.h.table.backing.rows == rows
    assert lost == ({h.opened.binding.binding_id} if reason == "lost" else set())
    assert h.c.sessions.calls == []


@pytest.mark.parametrize("configured,threshold", [(120, 40), (None, 30)])
def test_short_or_unreadable_idle_setting_uses_an_earlier_conservative_interval(
    hosted_keepalive, monkeypatch, configured, threshold, caplog,
):
    from shared_context import behavior_foundry as foundry
    from shared_context import behavior_session_keepalive as keepalive

    h = hosted_keepalive
    clock = [0.0]
    monkeypatch.setattr(keepalive, "monotonic", lambda: clock[0])
    monkeypatch.setattr(foundry, "monotonic", lambda: clock[0])
    monkeypatch.setattr(h.c.h.composition, "clients", (h.foundry,))
    h.foundry._session_requests[h.foundry.target] = 0.0
    monkeypatch.setattr(FoundryHttpClient, "session_status", lambda _: SimpleNamespace(status="active"))

    def setting(_):
        if configured is None:
            raise Problem(503, "HOSTED_IDLE_TIMEOUT_UNAVAILABLE", "unavailable configuration")
        return configured

    monkeypatch.setattr(FoundryHttpClient, "session_idle_timeout", setting)
    reader = keepalive.InstructionSessionKeepalive(h.c.h.composition, h.opened.binding.binding_id)
    for elapsed, expected in ((0, 0), (threshold - 1, 0), (threshold, 1)):
        clock[0] = float(elapsed)
        reader.tick()
        assert len(h.ingress) == expected
    assert reader.interval == threshold and threshold < 120
    if configured is None:
        assert "HOSTED_SESSION_KEEPALIVE_INTERVAL_FALLBACK" in caplog.text


def test_cancel_during_platform_read_is_rechecked_before_the_inbound_request(hosted_keepalive, monkeypatch):
    from shared_context import behavior_session_keepalive as keepalive

    h = hosted_keepalive
    monkeypatch.setattr(h.c.h.composition, "clients", (h.foundry,))
    monkeypatch.setattr(FoundryHttpClient, "session_idle_timeout", lambda _: 900)
    after_cancel = []

    def read_status(_):
        h.c.h.runtime.dep.workers.cancel(h.opened.binding.job_id, h.opened.binding.owner,
                                         expected_binding=h.opened.session.execution)
        after_cancel.append(deepcopy(h.c.h.table.backing.rows))
        return SimpleNamespace(status="active")

    monkeypatch.setattr(FoundryHttpClient, "session_status", read_status)
    keepalive.InstructionSessionKeepalive(h.c.h.composition, h.opened.binding.binding_id).tick()
    assert h.ingress == [] and len(after_cancel) == 1
    assert h.c.h.table.backing.rows == after_cancel[0]
    assert h.c.sessions.calls == []


def test_existing_inbound_traffic_defers_the_next_keepalive(hosted_keepalive, monkeypatch):
    from shared_context import behavior_foundry as foundry
    from shared_context import behavior_session_keepalive as keepalive

    h = hosted_keepalive
    clock = [0.0]
    monkeypatch.setattr(keepalive, "monotonic", lambda: clock[0])
    monkeypatch.setattr(foundry, "monotonic", lambda: clock[0])
    monkeypatch.setattr(h.c.h.composition, "clients", (h.foundry,))
    monkeypatch.setattr(FoundryHttpClient, "session_idle_timeout", lambda _: 900)
    monkeypatch.setattr(FoundryHttpClient, "session_status", lambda _: SimpleNamespace(status="active"))
    h.foundry._session_requests[h.foundry.target] = 0.0
    reader = keepalive.InstructionSessionKeepalive(h.c.h.composition, h.opened.binding.binding_id)
    reader.tick()
    clock[0] = 100.0
    h.foundry.keepalive(h.assignment.invocation_id, h.assignment.launch_id, before_send=lambda: None)
    for elapsed, expected in ((150, 1), (249, 1), (250, 2)):
        clock[0] = float(elapsed)
        reader.tick()
        assert len(h.ingress) == expected


def test_follow_calls_readonly_tick_before_each_original_step(monkeypatch):
    from shared_context.behavior_server import ServerComposition
    from shared_context.behavior_session_keepalive import InstructionSessionKeepalive
    calls = []

    def tick(self):
        calls.append(("keepalive", self.binding_id))

    monkeypatch.setattr(InstructionSessionKeepalive, "tick", tick)

    async def run():
        loop = asyncio.get_running_loop()
        steps = []

        def step(binding_id):
            assert calls[-1] == ("keepalive", binding_id)
            calls.append(("step", binding_id))
            steps.append(binding_id)
            if len(steps) < 3:
                loop.call_soon_threadsafe(composition.instruction_ready, binding_id)
            return SimpleNamespace(state="starting" if len(steps) < 3 else "succeeded",
                                   result_forwarded=len(steps) == 3)

        runtime = SimpleNamespace(controller=SimpleNamespace(step=step), work_policy=None,
            dep=SimpleNamespace(delivery=SimpleNamespace(contract=SimpleNamespace(review_interval_seconds=60))))
        composition = ServerComposition(None, runtime, None, None, (), ExitStack())
        try:
            composition.launch_instruction("original-binding")
            await asyncio.wait_for(composition._instruction_tasks["original-binding"], timeout=2)
            assert calls == [("keepalive", "original-binding"), ("step", "original-binding")] * 3
        finally:
            await composition.stop_instruction_observers()
            composition.close()

    asyncio.run(run())


@pytest.mark.parametrize("receiver", ["registered", "running", "cancelled"])
def test_original_a_observer_keeps_only_its_running_b_peer_alive_without_advancing_the_queue(
    message_turn, signing_key, monkeypatch, receiver,
):
    from shared_context import behavior_foundry as foundry
    from shared_context import behavior_session_keepalive as keepalive

    m = message_turn
    if receiver == "registered":
        response = m.client.post(m.path, json=m.command, headers=m.headers)
        assert response.status_code == 200
        b_record = next(iter(m.b.runtime.work_policy.records.values()))
        b_launch = None
    else:
        started = start_peer(m)
        claimed = claim_peer(m, started, signing_key)
        b_record, b_launch = started.receiver, started.launch
        if receiver == "cancelled":
            m.b.runtime.dep.workers.cancel(b_record.binding.job_id, b_record.binding.owner,
                                          expected_binding=claimed.session.execution)
    calls = []
    a_target = m.turn.launch.target
    b_target = m.b.runtime.dep.native_launches.targets_for(b_record.binding.job_id)[0]
    invocations = {a_target.agent_name: m.turn.claimed.assignment.invocation_id}
    if b_launch is not None:
        invocations[b_target.agent_name] = "original-b-invocation"
    targets = {target.agent_name: target for target in (a_target, b_target)}

    def receive(request):
        name = request.url.path.split("/agents/")[1].split("/")[0]
        target = targets[name]
        calls.append((name, request.method))
        return httpx.Response(200, headers={"x-agent-invocation-id": invocations[name]}, json={
            "launch_id": request.headers[wire.NATIVE_KEEPALIVE_HEADER],
            "invocation_id": invocations[name], "session_id": target.session_id, "read_only": True,
        })

    a_client = FoundryHttpClient(a_target, Credential(), transport=httpx.MockTransport(receive))
    b_client = FoundryHttpClient(b_target, Credential(), transport=httpx.MockTransport(receive))
    monkeypatch.setattr(m.a, "clients", (a_client,))
    monkeypatch.setattr(m.b, "clients", (b_client,))
    monkeypatch.setattr(foundry, "monotonic", lambda: 200.0)
    monkeypatch.setattr(keepalive, "monotonic", lambda: 200.0)
    monkeypatch.setattr(FoundryHttpClient, "session_status", lambda _: SimpleNamespace(status="active"))
    monkeypatch.setattr(FoundryHttpClient, "session_idle_timeout", lambda _: 900)
    before = deepcopy(m.c.h.table.backing.rows)
    try:
        reader = keepalive.InstructionSessionKeepalive(m.a, m.turn.binding.binding_id)
        reader.tick()
        assert calls == [(a_target.agent_name, "GET")] + (
            [(b_target.agent_name, "GET")] if receiver == "running" else [])
        assert m.c.h.table.backing.rows == before
        assert m.c.sessions.calls == []
    finally:
        a_client.close()
        b_client.close()
