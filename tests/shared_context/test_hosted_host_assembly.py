"""create_host's startup order, invocation lanes and shutdown; no model, runtime process or Azure."""

import asyncio
import logging
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.ai.agentserver import core, invocations
from azure.ai.agentserver.responses import ResponsesAgentServerHost
from starlette.testclient import TestClient

import shared_context
from shared_context import behavior_foundry_probe, behavior_hosted, native_sdk, native_turns, responses_probe
from shared_context import behavior_wire as wire
from shared_context.behavior_foundry_probe import NativeStateProbe, PROBE_HEADER
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NATIVE_TURN_HEADER
from shared_context.errors import Problem
from shared_context.responses_probe import N6ResponsesProbe, ProbeRequestFieldsMiddleware, ProbeSessionOnlyMiddleware
from test_responses_probe import IDENTITY, PLATFORM, local

OTHER = IDENTITY.model_copy(update={"session_id": "another-session"})
PERMIT = {wire.NATIVE_LAUNCH_PERMIT_HEADER: "a" * 64}
PROBE_ID = "b" * 64
PROBE = {wire.NATIVE_PROBE_HEADER: PROBE_ID}
INVOCATION = "/invocations/inv-1"
LAUNCH = {"binding_id": "binding-1", "launch_id": "launch-1"}


def code(error):
    return error.value.body["error"]["code"]


def refused(runtime, **options):
    with pytest.raises(Problem) as error:
        create_host(runtime, **options)
    return error


def factory(*_):
    async def handler(request, context, cancellation_signal):
        raise AssertionError("not called")
    return handler


def test_the_native_identity_is_checked_before_the_responses_flag(local, monkeypatch):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "yes")
    assert code(refused(local, native_runtime=SimpleNamespace(identity=OTHER))) == "NATIVE_STATE_SCOPE"


def test_the_flag_is_checked_before_the_probe_requirement(local, monkeypatch):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "yes")
    monkeypatch.setenv("FA_N6_RESPONSES_PROBE", "probe")
    assert code(refused(local)) == "RESPONSES_FLAG_INVALID"


@pytest.mark.parametrize("flag", ["", "0"])
@pytest.mark.parametrize("name", ["FA_N6_RESPONSES_PROBE", "FA_N6_RESPONSES_INTERRUPT_SESSION_ID"])
def test_either_test_probe_needs_explicit_enablement_before_any_probe_is_built(local, monkeypatch, flag, name):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", flag)
    monkeypatch.setenv(name, "probe")
    monkeypatch.setattr(NativeStateProbe, "from_environment", lambda _: pytest.fail("built a probe"))
    assert code(refused(local)) == "N6_PROBE_DISABLED"


def test_resilient_tasks_stay_off_without_a_probe_or_native_runtime(local):
    """The ADR-0007 switch: default off."""
    create_host(local)
    assert core.tasks.resilient_tasks_enabled() is False


def test_a_state_probe_alone_turns_resilient_tasks_on(local, monkeypatch):
    monkeypatch.setattr(NativeStateProbe, "from_environment", lambda identity: object())
    create_host(local)
    assert core.tasks.resilient_tasks_enabled() is True


def test_a_native_runtime_alone_turns_resilient_tasks_on(local):
    create_host(local, native_runtime=SimpleNamespace(identity=IDENTITY))
    assert core.tasks.resilient_tasks_enabled() is True


def test_an_unimportable_native_wire_refuses_before_any_startup_write(local, monkeypatch):
    # ``from . import x`` only imports when the package has no such attribute, and None makes that import fail.
    monkeypatch.delattr(shared_context, "behavior_native_turn_wire", raising=False)
    monkeypatch.setitem(sys.modules, "shared_context.behavior_native_turn_wire", None)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    with pytest.raises(ImportError):
        create_host(local, native_runtime=SimpleNamespace(identity=IDENTITY), responses_handler_factory=factory)
    assert core.tasks.resilient_tasks_enabled() is False
    assert not (local.supervisor.journal.root / "agentserver").exists()


def test_startup_reads_and_writes_in_one_fixed_order(local, monkeypatch):
    events = []
    native = SimpleNamespace(identity=IDENTITY)

    def seen(name, value=None):
        def call(*_args, **_kwargs):
            events.append(name)
            return value
        return call

    host_init = ResponsesAgentServerHost.__init__

    def built(self, *args, **kwargs):
        assert (local.supervisor.journal.root / "agentserver").is_dir()
        events.append(("host", os.environ["AGENTSERVER_STATE_ROOT"]))
        host_init(self, *args, **kwargs)

    def handler_factory(runtime, native_runtime):
        events.append("handler")
        assert runtime is local and native_runtime is native
        return factory()

    guarded = behavior_hosted.guard

    def guard(*paths):
        events.append("guard")
        guarded(*paths)

    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v3")
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(native_sdk, "native_runtime_from_environment", seen("native", native))
    monkeypatch.setattr(NativeStateProbe, "from_environment", seen("state-probe"))
    monkeypatch.setattr(core.tasks, "set_resilient_tasks_enabled", seen("resilient"))
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", seen("n6-probe"))
    monkeypatch.setattr(behavior_hosted, "guard", guard)
    monkeypatch.setattr(ResponsesAgentServerHost, "__init__", built)
    create_host(local, responses_handler_factory=handler_factory)
    state_root = local.supervisor.journal.root / "agentserver"
    # First occurrences: the Responses host also turns resilient tasks on for itself.
    assert list(dict.fromkeys(events)) == [
        "native", "state-probe", "resilient", "n6-probe", "guard", ("host", str(state_root)), "handler"]
    assert state_root.is_dir()


def test_an_explicit_native_runtime_is_not_replaced_by_the_environment(local, monkeypatch):
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v3")
    monkeypatch.setattr(native_sdk, "native_runtime_from_environment", lambda _: pytest.fail("replaced"))
    create_host(local, native_runtime=SimpleNamespace(identity=IDENTITY))


def test_a_test_probe_cannot_share_the_lane_with_a_production_handler(local, monkeypatch):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: N6ResponsesProbe(identity))
    assert code(refused(local, responses_handler_factory=factory)) == "RESPONSES_HANDLER_CONFLICT"
    # The lane is settled before the private root is touched.
    assert code(refused(SimpleNamespace(identity=IDENTITY), responses_handler_factory=factory)) == (
        "RESPONSES_HANDLER_CONFLICT")


def test_enabled_responses_need_a_handler_before_the_private_root_is_touched(local, monkeypatch):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    assert code(refused(SimpleNamespace(identity=IDENTITY))) == "RESPONSES_HANDLER_UNAVAILABLE"


@pytest.mark.parametrize("interrupt", [False, True])
def test_only_the_interrupt_session_refuses_native_turns(local, monkeypatch, interrupt):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    probe = N6ResponsesProbe(IDENTITY, interrupt_session=interrupt)
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    native = SimpleNamespace(identity=IDENTITY)
    if interrupt:
        assert code(refused(local, native_runtime=native)) == "N6_PROBE_SESSION_BUSY"
    else:
        create_host(local, native_runtime=native)


@pytest.mark.parametrize("supervisor", ["missing", "relative"])
def test_the_responses_state_root_must_be_the_absolute_owned_root(local, monkeypatch, supervisor):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    before = os.environ["AGENTSERVER_STATE_ROOT"]
    runtime = (SimpleNamespace(identity=IDENTITY) if supervisor == "missing" else SimpleNamespace(
        identity=IDENTITY, supervisor=SimpleNamespace(journal=SimpleNamespace(root=type(
            local.supervisor.journal.root)("relative")))))
    assert code(refused(runtime, responses_handler_factory=factory)) == "RESPONSES_STATE_ROOT_UNAVAILABLE"
    assert os.environ["AGENTSERVER_STATE_ROOT"] == before


def middleware(app):
    return {item.cls for item in app.user_middleware}


def test_the_production_handler_gets_no_probe_middleware(local, monkeypatch):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    installed = middleware(create_host(local, responses_handler_factory=factory))
    assert not installed & {ProbeRequestFieldsMiddleware, ProbeSessionOnlyMiddleware}


@pytest.mark.parametrize("interrupt", [False, True])
def test_a_test_probe_adds_its_field_guard_and_only_interruption_adds_the_session_guard(local, monkeypatch, interrupt):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    probe = N6ResponsesProbe(IDENTITY, interrupt_session=interrupt)
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    installed = middleware(create_host(local))
    assert ProbeRequestFieldsMiddleware in installed
    assert (ProbeSessionOnlyMiddleware in installed) is interrupt


@pytest.fixture
def lanes(local):
    local.start = Mock(return_value={"status": "accepted"})
    local.observe, local.keepalive = Mock(return_value=IDENTITY), Mock(return_value=IDENTITY)
    with TestClient(create_host(local)) as client:
        yield SimpleNamespace(runtime=local, client=client)


def post(lanes, *, headers=None, **options):
    return lanes.client.post("/invocations", headers={**PLATFORM, **PERMIT, **(headers or {})}, **options)


def test_start_passes_the_typed_selector_and_the_platform_ids(lanes):
    response = post(lanes, json=LAUNCH)
    assert (response.status_code, response.json()) == (202, {"status": "accepted"})
    payload, invocation_id, session_id = lanes.runtime.start.call_args.args
    assert (payload.binding_id, payload.launch_id) == ("binding-1", "launch-1")
    assert payload.launch_permit.get_secret_value() == "a" * 64
    assert (invocation_id, session_id) == (PLATFORM["x-agent-invocation-id"], IDENTITY.session_id)


@pytest.mark.parametrize("content,status,error", [
    (b" " * 4097, 413, "HOSTED_INVOCATION_LIMIT"),
    (b'{"binding_id":"a","binding_id":"b","launch_id":"c"}', 422, "HOSTED_INVOCATION_INVALID"),
    (b'{"binding_id":"a"}', 422, "HOSTED_INVOCATION_INVALID"),
    (b"\xff", 422, "HOSTED_INVOCATION_INVALID"),
])
def test_start_refuses_a_bad_body_before_the_runtime(lanes, content, status, error):
    response = post(lanes, content=content)
    assert (response.status_code, response.json()["error"]["code"]) == (status, error)
    lanes.runtime.start.assert_not_called()


def test_a_body_of_exactly_the_bound_is_read(lanes):
    raw = b'{"binding_id":"binding-1","launch_id":"launch-1"}'
    assert post(lanes, content=raw + b" " * (4096 - len(raw))).status_code == 202


@pytest.mark.parametrize("permit", ["A" * 64, "a" * 63])
def test_start_needs_one_well_formed_permit(lanes, permit):
    response = post(lanes, json=LAUNCH, headers={wire.NATIVE_LAUNCH_PERMIT_HEADER: permit})
    assert (response.status_code, response.json()["error"]["code"]) == (403, "HOSTED_CAPABILITY_INVALID")
    lanes.runtime.start.assert_not_called()


def test_start_refuses_a_repeated_permit(lanes):
    response = lanes.client.post("/invocations", json=LAUNCH, headers=[
        *PLATFORM.items(), (wire.NATIVE_LAUNCH_PERMIT_HEADER, "a" * 64), (wire.NATIVE_LAUNCH_PERMIT_HEADER, "a" * 64)])
    assert (response.status_code, response.json()["error"]["code"]) == (403, "HOSTED_CAPABILITY_INVALID")


@pytest.mark.parametrize("failure", [OSError("SYNTHETIC"), sqlite3.Error("SYNTHETIC"), ValueError("SYNTHETIC")])
def test_a_local_failure_is_an_unknown_outcome_on_every_action(lanes, failure):
    lanes.runtime.start.side_effect = lanes.runtime.observe.side_effect = failure
    for response in (post(lanes, json=LAUNCH), lanes.client.get(INVOCATION, headers={**PLATFORM, **PROBE}),
                     lanes.client.post(INVOCATION + "/cancel", headers={**PLATFORM, **PROBE})):
        assert response.status_code == 503, response.text
        assert response.json() == {"error": {"code": "HOSTED_EXECUTOR_UNKNOWN",
                                             "message": "executor outcome remains unknown"}}


def test_a_problem_keeps_its_status_and_body(lanes):
    lanes.runtime.observe.side_effect = Problem(409, "HOSTED_PROCESS_UNOWNED", "kept")
    response = lanes.client.get(INVOCATION, headers={**PLATFORM, **PROBE})
    assert (response.status_code, response.json()["error"]["code"]) == (409, "HOSTED_PROCESS_UNOWNED")


@pytest.mark.parametrize("path,cancellation", [(INVOCATION, False), (INVOCATION + "/cancel", True)])
def test_observe_and_cancel_forward_one_fresh_probe(lanes, path, cancellation):
    send = lanes.client.get if not cancellation else lanes.client.post
    response = send(path, headers={**PLATFORM, **PROBE})
    assert response.status_code == 200 and response.json() == IDENTITY.model_dump(mode="json")
    lanes.runtime.observe.assert_called_once_with("inv-1", IDENTITY.session_id, PROBE_ID, cancellation=cancellation)


@pytest.mark.parametrize("headers", [{}, {wire.NATIVE_PROBE_HEADER: "B" * 64}, {wire.NATIVE_PROBE_HEADER: "b" * 63}])
def test_observe_needs_a_well_formed_probe(lanes, headers):
    response = lanes.client.get(INVOCATION, headers={**PLATFORM, **headers})
    assert (response.status_code, response.json()["error"]["code"]) == (403, "HOSTED_PROBE_INVALID")
    lanes.runtime.observe.assert_not_called()


def test_observe_refuses_a_repeated_probe(lanes):
    response = lanes.client.get(INVOCATION, headers=[
        *PLATFORM.items(), (wire.NATIVE_PROBE_HEADER, PROBE_ID), (wire.NATIVE_PROBE_HEADER, PROBE_ID)])
    assert (response.status_code, response.json()["error"]["code"]) == (403, "HOSTED_PROBE_INVALID")


def test_keepalive_reads_the_original_launch_without_a_probe(lanes):
    response = lanes.client.get(INVOCATION, headers={**PLATFORM, wire.NATIVE_KEEPALIVE_HEADER: "launch-1"})
    assert response.status_code == 200 and response.json() == IDENTITY.model_dump(mode="json")
    lanes.runtime.keepalive.assert_called_once_with("inv-1", IDENTITY.session_id, "launch-1")
    lanes.runtime.observe.assert_not_called()


@pytest.mark.parametrize("send", ["cancel", "probe", "format", "repeat", "body"])
def test_keepalive_is_only_the_read_only_observe(lanes, send):
    headers = [*PLATFORM.items(), (wire.NATIVE_KEEPALIVE_HEADER, "-bad" if send == "format" else "launch-1")]
    if send == "probe":
        headers.append((wire.NATIVE_PROBE_HEADER, PROBE_ID))
    if send == "repeat":
        headers.append((wire.NATIVE_KEEPALIVE_HEADER, "launch-2"))
    if send == "cancel":
        response = lanes.client.post(INVOCATION + "/cancel", headers=headers)
    else:
        response = lanes.client.request("GET", INVOCATION, headers=headers,
                                        content=b"x" if send == "body" else None)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "HOSTED_KEEPALIVE_INVALID")
    lanes.runtime.keepalive.assert_not_called()


@pytest.fixture
def native_lane(local, monkeypatch):
    calls = []

    async def respond(request, runtime, **options):
        from starlette.responses import JSONResponse
        calls.append((request.method, runtime, options))
        return JSONResponse({"native": True})

    monkeypatch.setattr(native_turns, "native_response", respond)
    return calls


@pytest.mark.parametrize("path,method,options", [
    ("/invocations", "POST", {}),
    (INVOCATION, "GET", {"action": "observe"}),
    (INVOCATION + "/cancel", "POST", {"action": "cancel"}),
])
def test_the_native_header_routes_every_action_to_native_turns(local, native_lane, path, method, options):
    with TestClient(create_host(local)) as client:
        response = client.request(method, path, headers={**PLATFORM, NATIVE_TURN_HEADER: "any"})
    assert response.json() == {"native": True}
    assert native_lane == [(method, None, options)]


def test_the_state_probe_header_wins_over_the_native_header(local, native_lane, monkeypatch):
    seen = []

    async def answer(request, probe):
        from starlette.responses import JSONResponse
        seen.append(probe)
        return JSONResponse({"state": True})

    marker = object()
    monkeypatch.setattr(behavior_foundry_probe, "probe_response", answer)
    monkeypatch.setattr(NativeStateProbe, "from_environment", lambda identity: marker)
    with TestClient(create_host(local)) as client:
        response = client.post("/invocations", headers={**PLATFORM, PROBE_HEADER: "x", NATIVE_TURN_HEADER: "any"})
    assert response.json() == {"state": True} and seen == [marker] and not native_lane


def test_the_responses_probe_header_goes_first_with_the_built_probe(local, native_lane, monkeypatch):
    seen = []

    async def answer(request, probe):
        from starlette.responses import JSONResponse
        seen.append(probe)
        return JSONResponse({"n6": True})

    monkeypatch.setattr(responses_probe, "probe_response", answer)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    probe = N6ResponsesProbe(IDENTITY, interrupt_session=True)
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    with TestClient(create_host(local)) as client:
        response = client.post("/invocations", headers={**PLATFORM, "x-client-fa-n6-probe": "x",
                                                        NATIVE_TURN_HEADER: "any"})
    assert response.json() == {"n6": True} and seen == [probe] and not native_lane


@pytest.mark.parametrize("path,method", [("/invocations", "POST"), (INVOCATION, "GET"),
                                         (INVOCATION + "/cancel", "POST")])
def test_an_interrupt_session_answers_no_lane_at_all(local, native_lane, monkeypatch, path, method):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    probe = N6ResponsesProbe(IDENTITY, interrupt_session=True)
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    with TestClient(create_host(local)) as client:
        response = client.request(method, path, headers={**PLATFORM, NATIVE_TURN_HEADER: "any"})
    assert response.status_code == 403 and response.json() == {"error": {"code": "N6_PROBE_SESSION_ONLY"}}
    assert not native_lane


def dispatches(caplog):
    return [record.getMessage() for record in caplog.records if "FA_HOSTED_DISPATCH" in record.getMessage()]


def test_legacy_traffic_without_a_native_runtime_writes_no_dispatch_log(lanes, caplog):
    caplog.set_level(logging.WARNING)
    assert post(lanes, json=LAUNCH).status_code == 202
    assert dispatches(caplog) == []


def test_the_dispatch_log_names_the_probe_lane(local, caplog, monkeypatch):
    async def answer(request, probe):
        from starlette.responses import JSONResponse
        return JSONResponse({})

    monkeypatch.setattr(behavior_foundry_probe, "probe_response", answer)
    caplog.set_level(logging.WARNING)
    with TestClient(create_host(local, native_runtime=None)) as client:
        client.post("/invocations", headers={**PLATFORM, PROBE_HEADER: "x"})
    assert dispatches(caplog) == []

    async def nothing():
        return None

    runtime = SimpleNamespace(identity=IDENTITY, initialize=nothing, recover_owned=nothing)
    with TestClient(create_host(local, native_runtime=runtime)) as client:
        client.post("/invocations", headers={**PLATFORM, PROBE_HEADER: "x"})
    message, = dispatches(caplog)
    assert '"lane": "probe"' in message and '"native_header_count": 0' in message
    assert f'"agent_id": "{IDENTITY.agent_id}"' in message and f'"session_id": "{IDENTITY.session_id}"' in message


class FakeHost:
    """Exposes the lifespan the SDK would have installed, so shutdown can be run on its own."""

    def __init__(self, **_):
        self.router = SimpleNamespace(lifespan_context=self.sdk_lifespan)
        self.events = []

    @asynccontextmanager
    async def sdk_lifespan(self, _application):
        self.events.append("sdk-start")
        try:
            yield
        finally:
            self.events.append("sdk-stop")

    def invoke_handler(self, handler):
        return handler

    get_invocation_handler = cancel_invocation_handler = invoke_handler


class Native:
    def __init__(self, events, *, recover=None, close=None, executor_close=None):
        self.identity, self.events = IDENTITY, events
        self.recover, self.close, self.executor_close = recover, close, executor_close
        self.executor = SimpleNamespace(aclose=self.stop_executor)

    async def initialize(self):
        self.events.append("initialize")

    async def recover_owned(self):
        self.events.append("recover")
        if self.recover:
            raise self.recover

    async def aclose(self):
        self.events.append("close")
        if self.close:
            raise self.close

    async def stop_executor(self):
        self.events.append("stop-executor")
        if self.executor_close:
            raise self.executor_close


def run_lifespan(app):
    async def run():
        async with app.router.lifespan_context(app):
            app.events.append("serving")
    asyncio.run(run())


@pytest.fixture
def host(local, monkeypatch):
    monkeypatch.setattr(invocations, "InvocationAgentServerHost", FakeHost)
    return lambda native: create_host(local, native_runtime=native)


def test_native_startup_wraps_the_sdk_and_drains_twice_on_shutdown(host):
    events = []
    app = host(Native(events))
    app.events = events
    run_lifespan(app)
    assert events == ["initialize", "sdk-start", "recover", "serving", "sdk-stop",
                      "close", "stop-executor", "close"]


def test_a_failed_recovery_still_stops_everything(host):
    events = []
    app = host(Native(events, recover=RuntimeError("SYNTHETIC")))
    app.events = events
    with pytest.raises(RuntimeError, match="SYNTHETIC"):
        run_lifespan(app)
    assert events == ["initialize", "sdk-start", "recover", "sdk-stop", "close", "stop-executor", "close"]


def test_shutdown_failures_are_logged_and_do_not_stop_the_next_step(host, caplog):
    events = []
    app = host(Native(events, close=RuntimeError("SYNTHETIC"), executor_close=RuntimeError("SYNTHETIC")))
    app.events = events
    run_lifespan(app)
    assert events[-3:] == ["close", "stop-executor", "close"]
    assert [record.getMessage() for record in caplog.records] == [
        "NATIVE_RUNTIME_SHUTDOWN_UNCONFIRMED", "RESIDENT_SDK_SHUTDOWN_UNCONFIRMED",
        "NATIVE_RUNTIME_SHUTDOWN_UNCONFIRMED"]
    assert "SYNTHETIC" not in caplog.text


def test_a_native_runtime_without_shutdown_hooks_stops_cleanly(host, caplog):
    events = []

    async def initialize():
        events.append("initialize")

    async def recover_owned():
        events.append("recover")

    app = host(SimpleNamespace(identity=IDENTITY, initialize=initialize, recover_owned=recover_owned))
    app.events = events
    run_lifespan(app)
    assert events == ["initialize", "sdk-start", "recover", "serving", "sdk-stop"]
    assert not caplog.records


def test_without_a_native_runtime_the_sdk_lifespan_is_untouched(local, monkeypatch):
    monkeypatch.setattr(invocations, "InvocationAgentServerHost", FakeHost)
    app = create_host(local)
    assert app.router.lifespan_context == app.sdk_lifespan
