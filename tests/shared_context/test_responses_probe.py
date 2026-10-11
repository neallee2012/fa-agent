"""Real pinned host/store, synthetic input, no Azure or model traffic."""

import asyncio
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from azure.ai.agentserver.core import (
    AgentServerHost, FoundryAgentRequestContext, reset_request_context, set_request_context,
)
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.ai.agentserver.core.tasks import resilient_tasks_enabled, set_resilient_tasks_enabled
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.ai.agentserver.responses import ResponsesAgentServerHost, TextResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from shared_context.behavior_hosted import create_host
from shared_context.behavior_wire import HostedIdentity, NATIVE_LAUNCH_PERMIT_HEADER
from shared_context.errors import Problem
from shared_context.responses_probe import (
    HEADER, INPUTS, PROFILE, PROBE_AGENT_ID, PROBE_AGENT_NAME, PROBE_PROJECT_ENDPOINT,
    N6ResponsesProbe, ProbeInterrupt, digest_input,
)

IDENTITY = HostedIdentity(
    agent_id=PROBE_AGENT_ID, agent_name=PROBE_AGENT_NAME, agent_version="12", session_id="n6-offline-session",
)
RID = "n6-" + "1" * 32
CID = "conv_offline"
PLATFORM = {
    "x-agent-session-id": IDENTITY.session_id, "x-agent-invocation-id": "n6-platform-invocation",
    "x-agent-foundry-call-id": "PRIVATE-opaque-call", "x-agent-user-id": "PRIVATE-opaque-user",
}


@pytest.fixture
def local(monkeypatch, tmp_path):
    before = resilient_tasks_enabled()
    set_resilient_tasks_enabled(False)
    for name in ("FOUNDRY_HOSTING_ENVIRONMENT", "FOUNDRY_PROJECT_ENDPOINT", "FA_N0_STATE_PROBE",
                 "FA_NATIVE_TURNS", "FA_RESPONSES_ENABLED", "FA_N6_RESPONSES_PROBE",
                 "FA_N6_RESPONSES_INTERRUPT_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    root = str(tmp_path / "sdk")
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", "\\\\?\\" + root if os.name == "nt" else root)
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", IDENTITY.agent_name)
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", IDENTITY.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_GUID", "PRIVATE-platform-incarnation")
    owned = tmp_path / "journal"
    owned.mkdir()
    yield SimpleNamespace(identity=IDENTITY, start=Mock(return_value={"status": "accepted"}),
                          supervisor=SimpleNamespace(journal=SimpleNamespace(root=owned)))
    set_resilient_tasks_enabled(before)


def payload(phase="observe", cid=CID):
    return {"input": INPUTS[phase], "conversation": cid, "agent_session_id": IDENTITY.session_id,
            "background": True, "store": True,
            "metadata": {"fa_agent_id": PROBE_AGENT_ID, "fa_team_id": "OFFLINE-team"}}


def headers(phase="observe", rid=RID, cid=CID):
    return {**PLATFORM, HEADER: PROFILE, "x-client-fa-rid": rid, "x-client-fa-cid": cid,
            "x-client-fa-submit-digest": digest_input(INPUTS[phase], cid)}


@contextmanager
def platform_context():
    token = set_request_context(FoundryAgentRequestContext(
        session_id=IDENTITY.session_id, call_id=PLATFORM["x-agent-foundry-call-id"],
        user_id=PLATFORM["x-agent-user-id"],
    ))
    try:
        yield
    finally:
        reset_request_context(token)


async def collect(probe, *, phase="observe", rid=RID, cid=CID, recovery=False, request=None):
    context = SimpleNamespace(
        response_id="resp_offline", conversation_id=cid, is_recovery=recovery,
        client_headers=headers(phase, rid, cid), shutdown=asyncio.Event(),
    )
    with platform_context():
        return [event async for event in probe.handler(request or payload(phase, cid), context, asyncio.Event())]


def result(events):
    response = events[-1]["response"]
    assert response["status"] == "completed", response.get("error")
    return json.loads(response["output"][0]["content"][0]["text"])


def test_default_off_never_initializes_responses_or_changes_native_platform_ids(local, monkeypatch):
    original_state_root = os.environ.get("AGENTSERVER_STATE_ROOT")
    monkeypatch.setattr(ResponsesAgentServerHost, "__init__", lambda *a, **k: pytest.fail("Responses is off"))
    with TestClient(create_host(local)) as client:
        response = client.post("/invocations", headers={**PLATFORM, NATIVE_LAUNCH_PERMIT_HEADER: "a" * 64},
                               json={"binding_id": "original", "launch_id": "original-launch"})
        assert response.status_code == 202
        assert client.post("/responses", json=payload()).status_code == 404
        disabled = client.post("/invocations", headers=headers(), json={"operation": "read_receipt", "request_id": RID})
        assert disabled.status_code == 403
    assert local.start.call_args.args[1:] == ("n6-platform-invocation", IDENTITY.session_id)
    assert not resilient_tasks_enabled()
    assert os.environ.get("AGENTSERVER_STATE_ROOT") == original_state_root


def test_real_mixins_share_one_core_lifespan_and_injected_handler(local, monkeypatch):
    from azure.ai.agentserver.core.tasks import _manager

    events, supplied = [], []
    startup, shutdown = _manager.TaskManager.startup, _manager.TaskManager.shutdown

    async def start(manager):
        events.append("sdk-start")
        await startup(manager)

    async def stop(manager):
        events.append("sdk-stop")
        await shutdown(manager)

    async def initialize():
        events.append("native-initialize")

    async def recover():
        events.append("native-recover")

    native = SimpleNamespace(identity=IDENTITY, initialize=initialize, recover_owned=recover)

    def factory(runtime, native_runtime):
        supplied.append((runtime, native_runtime))

        async def handler(request, context, cancellation_signal):
            assert isinstance(cancellation_signal, asyncio.Event)
            return TextResponse(context, request, text="synthetic")

        return handler

    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(_manager.TaskManager, "startup", start)
    monkeypatch.setattr(_manager.TaskManager, "shutdown", stop)
    app = create_host(local, native_runtime=native, responses_handler_factory=factory)
    assert type(app).__name__ == "FaHost"
    assert isinstance(app, InvocationAgentServerHost) and isinstance(app, ResponsesAgentServerHost)
    assert type(app).__mro__.count(AgentServerHost) == 1
    assert supplied == [(local, native)]
    with TestClient(app) as client:
        response = client.post("/responses", headers=PLATFORM, json={"input": "synthetic", "store": False})
        assert response.status_code == 200, response.text
        assert response.json()["output"][0]["content"][0]["text"] == "synthetic"
        assert events == ["native-initialize", "sdk-start", "native-recover"]
        assert client.get("/readiness").status_code == 200
    assert events == ["native-initialize", "sdk-start", "native-recover", "sdk-stop"]


def test_real_dual_host_uses_bootstrap_owned_root_when_home_mkdir_is_denied(local, monkeypatch, tmp_path):
    import azure.identity.aio
    from shared_context.behavior_assistant_hosted import AssistantSupervisor

    home = tmp_path / "platform-home"
    home.mkdir()
    owned = home / ".fa-assistant-v12"
    executable = tmp_path / "OFFLINE-never-executed"
    executable.write_text("synthetic", encoding="utf-8")
    supervisor = AssistantSupervisor(owned, runtime_path=executable)
    hosted = SimpleNamespace(identity=IDENTITY, supervisor=supervisor)
    journal_before = supervisor.journal.path.read_bytes()
    mode_before = home.stat().st_mode
    monkeypatch.delenv("AGENTSERVER_STATE_ROOT")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-hosted-constructor")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", PROBE_PROJECT_ENDPOINT)
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_GUID", "c" * 32)

    class OfflineCredential:
        async def get_token(self, *args, **kwargs):
            pytest.fail("the root/lifespan test must not request a real token")
        async def close(self):
            pass

    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", OfflineCredential)
    mkdir = Path.mkdir
    created = []

    def bootstrap_permissions(path, *args, **kwargs):
        if path.is_relative_to(home) and not path.is_relative_to(owned) and not path.exists():
            raise PermissionError(errno.EACCES, "OFFLINE HOME is not app-writable", str(path))
        created.append(path)
        return mkdir(path, *args, **kwargs)

    def factory(runtime, native):
        assert runtime is hosted and native is None

        async def handler(request, context, cancellation_signal):
            return TextResponse(context, request, text="synthetic")

        return handler

    monkeypatch.setattr(Path, "mkdir", bootstrap_permissions)
    with TestClient(create_host(hosted, responses_handler_factory=factory)) as client:
        response = client.post("/responses", headers=PLATFORM, json={"input": "synthetic", "store": False})
        assert response.status_code == 200
    state_root = Path(os.environ["AGENTSERVER_STATE_ROOT"])
    assert state_root.is_relative_to(owned) and (state_root / "streams").is_dir()
    assert all(path.is_relative_to(owned) for path in created)
    assert not (home / ".agentserver").exists()
    assert supervisor.journal.path.read_bytes() == journal_before and home.stat().st_mode == mode_before


@pytest.mark.parametrize("enabled", [False, True])
def test_official_websocket_remains_available_with_and_without_responses(local, monkeypatch, enabled):
    def factory(runtime, native):
        async def handler(request, context, cancellation_signal):
            return TextResponse(context, request, text="synthetic")
        return handler

    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1" if enabled else "0")
    app = create_host(local, responses_handler_factory=factory)

    @app.ws_handler
    async def echo(websocket):
        await websocket.send_bytes(await websocket.receive_bytes())

    with TestClient(app) as client, client.websocket_connect("/invocations_ws", headers=PLATFORM) as websocket:
        websocket.send_bytes(b"synthetic-frame")
        assert websocket.receive_bytes() == b"synthetic-frame"


@pytest.mark.parametrize("flag,probe,code", [
    ("yes", "", "RESPONSES_FLAG_INVALID"),
    ("0", PROFILE, "N6_PROBE_DISABLED"),
    ("1", "", "RESPONSES_HANDLER_UNAVAILABLE"),
])
def test_no_implicit_production_handler(local, monkeypatch, flag, probe, code):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", flag)
    monkeypatch.setenv("FA_N6_RESPONSES_PROBE", probe)
    with pytest.raises(Problem) as error:
        create_host(local)
    assert error.value.body["error"]["code"] == code


@pytest.mark.parametrize("change", ["agent", "project", "local", "profile"])
def test_probe_environment_never_widens_to_other_agents(local, monkeypatch, change):
    monkeypatch.setenv("FA_N6_RESPONSES_PROBE", PROFILE)
    monkeypatch.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-hosted")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", PROBE_PROJECT_ENDPOINT)
    monkeypatch.delenv("AGENTSERVER_TASKS_BACKEND")
    identity = IDENTITY
    if change == "agent":
        identity = identity.model_copy(update={"agent_id": "another-agent"})
    elif change == "project":
        monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://offline.invalid")
    elif change == "local":
        monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    else:
        monkeypatch.setenv("FA_N6_RESPONSES_PROBE", "unrecognized")
    with pytest.raises(Problem):
        N6ResponsesProbe.from_environment(identity)


@pytest.mark.parametrize("conversation_form", ["string", "object"])
def test_real_background_host_preserves_three_headers_and_incarnation_hash(local, monkeypatch, conversation_form):
    probe = N6ResponsesProbe(IDENTITY)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    request = payload()
    if conversation_form == "object":
        request["conversation"] = {"id": CID}
    with TestClient(create_host(local)) as client:
        response = client.post("/responses", headers=headers(), json=request)
        assert response.status_code == 200, response.text
        response_id = response.json()["id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            stored = client.get("/responses/" + response_id, headers=PLATFORM)
            assert stored.status_code == 200, stored.text
            if stored.json()["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        body = stored.json()
        assert body["status"] == "completed", body.get("error")
        observed = json.loads(body["output"][0]["content"][0]["text"])
        assert observed["record"]["request_id"] == headers()["x-client-fa-rid"]
        assert observed["record"]["client_cid"] == headers()["x-client-fa-cid"]
        assert observed["record"]["submit_digest"] == headers()["x-client-fa-submit-digest"]
        assert observed["observed"]["session_id"] == IDENTITY.session_id
        assert observed["observed"]["request_session_id"] == IDENTITY.session_id
        assert observed["observed"]["session_guid_present"]
        assert "PRIVATE-" not in json.dumps(observed)
    local.start.assert_not_called()


@pytest.mark.parametrize("change,field,reason", [
    ("header", "probe_header", "missing_or_mismatch"),
    ("input", "input", "fixed_literal_required"),
    ("input-items", "input", "string_required"),
    ("background", "background", "true_required"),
    ("stream", "stream", "false_required"),
    ("cid", "cid", "header_mismatch"),
    ("no-cid", "cid", "string_required"),
    ("digest", "digest", "digest_mismatch"),
    ("metadata", "metadata", "invalid_shape"),
    ("identity", "identity", "invalid_shape"),
    ("rid", "record", "invalid_shape"),
])
def test_public_host_preserves_each_rejection_and_logs_only_fixed_field_reason(
        local, monkeypatch, caplog, change, field, reason):
    probe = N6ResponsesProbe(IDENTITY)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    body, supplied = payload(), headers()
    body.update(instructions="PRIVATE-unused-instructions", ignored_extra={"value": "PRIVATE-unused-value"})
    if change == "header":
        supplied[HEADER] = "PRIVATE-invalid-profile"
    elif change == "input":
        body["input"] = "PRIVATE-nonsynthetic-input"
    elif change == "input-items":
        body["input"] = [{"role": "user", "content": [{"type": "input_text", "text": INPUTS["observe"]}]}]
    elif change == "background":
        body[change] = False
    elif change == "stream":
        body["stream"] = True
    elif change == "cid":
        supplied["x-client-fa-cid"] = "PRIVATE-another-cid"
    elif change == "no-cid":
        body.pop("conversation")
    elif change == "digest":
        supplied["x-client-fa-submit-digest"] = "0" * 64
    elif change == "metadata":
        body["metadata"]["unrequested"] = "PRIVATE-metadata"
    elif change == "identity":
        probe.identity = IDENTITY.model_copy(update={"agent_version": "PRIVATE-invalid-version"})
    else:
        supplied["x-client-fa-rid"] = "PRIVATE-invalid-rid"

    async def no_probe_state(*args, **kwargs):
        pytest.fail("a rejected probe must not create its synthetic State store or RID")

    monkeypatch.setattr(FoundryStateStore, "get_or_create", no_probe_state)
    with TestClient(create_host(local)) as client:
        response = client.post("/responses", headers=supplied, json=body)
        assert response.status_code == 200, response.text
        if change == "stream":
            events = [json.loads(line.removeprefix("data:").strip())
                      for line in response.text.splitlines() if line.startswith("data:")]
            observed = next(event["response"] for event in events if event.get("type") == "response.failed")
        else:
            observed = response.json()
        deadline = time.monotonic() + 8
        while observed["status"] not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
            time.sleep(0.01)
            observed = client.get("/responses/" + observed["id"], headers=PLATFORM).json()
        assert observed["status"] == "failed" and observed["error"]["message"] == "N6_PROBE_REQUEST_INVALID"
        # Even a fully supplied metadata request has an empty rejection response; it is not a request echo.
        assert observed.get("metadata") in ({}, None)
        assert "PRIVATE" not in json.dumps(observed)
    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("FA_N6_PROBE_FAILED")]
    assert len(lines) == 1 and f"field={field} reason={reason}" in lines[0]
    assert "PRIVATE" not in caplog.text
    local.start.assert_not_called()


def test_sdk_store_guard_and_defensive_probe_store_guard_both_remain_rejections(local, monkeypatch, caplog):
    probe = N6ResponsesProbe(IDENTITY)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    request = {**payload(), "store": False}
    with TestClient(create_host(local)) as client:
        response = client.post("/responses", headers=headers(), json=request)
        assert response.status_code == 400 and response.json()["error"]["code"] == "unsupported_parameter"
    assert not any(record.getMessage().startswith("FA_N6_PROBE_FAILED") for record in caplog.records)
    terminal = asyncio.run(collect(probe, request=request))[-1]["response"]
    assert terminal["status"] == "failed" and terminal["error"]["message"] == "N6_PROBE_REQUEST_INVALID"
    assert "field=store reason=true_required" in caplog.text


@pytest.mark.parametrize("with_content", [False, True])
def test_unused_standard_and_unknown_fields_do_not_change_record_or_echo_values(local, monkeypatch, caplog, with_content):
    from azure.ai.agentserver.responses import CreateResponse

    probe = N6ResponsesProbe(IDENTITY)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    unknown = {f"z_probe_extra_{index:02}": {"nested": "PRIVATE-unknown-value"} for index in reversed(range(25))}
    extras = {
        "tools": [{"type": "function", "name": "unused_function", "description": "PRIVATE-tool",
                   "parameters": {"type": "object", "properties": {}}, "strict": False}],
        "instructions": "PRIVATE-instructions",
        "model": "PRIVATE-unused-model",
        "user": "PRIVATE-not-a-caller",
        "safety_identifier": "PRIVATE-safety-id",
        "temperature": 0.2,
        "top_p": 0.9,
        "parallel_tool_calls": True,
        "text": {"format": {"type": "text"}},
        "reasoning": {"effort": "low"},
        "agent_reference": {"type": "agent_reference", "name": "PRIVATE-not-this-agent", "version": "1"},
    }
    if not with_content:
        extras.update(tools=[], instructions="")
    assert set(extras) <= set(CreateResponse.__annotations__)

    def completed(client, request):
        response = client.post("/responses", headers=headers(), json=request)
        assert response.status_code == 200, response.text
        value = response.json()
        deadline = time.monotonic() + 8
        while value["status"] not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
            time.sleep(0.01)
            value = client.get("/responses/" + value["id"], headers=PLATFORM).json()
        assert value["status"] == "completed", value.get("error")
        return value, json.loads(value["output"][0]["content"][0]["text"])

    with TestClient(create_host(local)) as client:
        first, original = completed(client, {**payload(), **extras, **unknown})
        assert original["state_write_performed"] is True and "PRIVATE" not in json.dumps(first)
        _, plain = completed(client, payload())
        assert plain["record"] == original["record"] and plain["state_write_performed"] is False
        caplog.clear()
        response, repeated = completed(client, {**payload(), **extras, **unknown})
        assert repeated["record"] == original["record"]
        assert repeated["state_write_performed"] is False
        assert repeated["record"]["synthetic_effect_count"] == 1
        assert response["model"] == "n6-probe"
        assert response["agent_reference"]["name"] == IDENTITY.agent_name
        assert response.get("instructions") in (None, "")
        assert response.get("tools") in (None, [])
        assert "PRIVATE" not in json.dumps(response)
    ignored = [record.getMessage() for record in caplog.records
               if record.getMessage().startswith("FA_N6_PROBE_IGNORED")]
    names = next(message.split("unknown_keys=", 1)[1] for message in ignored if "unknown_keys=" in message)
    assert json.loads(names) == sorted(unknown)[:20]
    for field in ("tools", "instructions"):
        assert (f"FA_N6_PROBE_IGNORED field={field} nonempty=true" in ignored) is with_content
    assert "PRIVATE" not in caplog.text
    local.start.assert_not_called()


@pytest.mark.parametrize("platform_id", [False, True])
def test_unknown_body_response_id_cannot_override_sdk_or_platform_identity(local, monkeypatch, caplog, platform_id):
    from azure.ai.agentserver.responses._id_generator import IdGenerator

    probe = N6ResponsesProbe(IDENTITY)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    body_id, supplied_id = IdGenerator.new_response_id(CID), IdGenerator.new_response_id(CID)
    supplied = headers()
    if platform_id:
        supplied["x-agent-response-id"] = supplied_id
    with TestClient(create_host(local)) as client:
        response = client.post("/responses", headers=supplied, json={**payload(), "response_id": body_id})
        assert response.status_code == 200, response.text
        observed = response.json()
        deadline = time.monotonic() + 8
        while observed["status"] not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
            time.sleep(0.01)
            observed = client.get("/responses/" + observed["id"], headers=supplied).json()
        assert observed["status"] == "completed", observed.get("error")
        assert observed["id"] != body_id
        if platform_id:
            assert observed["id"] == supplied_id
        assert body_id not in json.dumps(observed)
    assert body_id not in caplog.text


def test_production_factory_keeps_sdk_body_identity_behavior(local, monkeypatch):
    from azure.ai.agentserver.responses._id_generator import IdGenerator

    body_id = IdGenerator.new_response_id(CID)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")

    def factory(runtime, native):
        async def handler(request, context, cancellation_signal):
            assert request["response_id"] == body_id and context.response_id == body_id
            return TextResponse(context, request, text="synthetic")
        return handler

    with TestClient(create_host(local, responses_handler_factory=factory)) as client:
        response = client.post("/responses", headers=PLATFORM, json={
            "input": "synthetic", "response_id": body_id, "store": False,
        })
        assert response.status_code == 200 and response.json()["id"] == body_id


@pytest.mark.parametrize("route", ["invocations", "response-get", "websocket"])
def test_probe_filter_is_transparent_outside_response_posts(route):
    from shared_context.responses_probe import ProbeRequestFieldsMiddleware

    scope = {"type": "http", "method": "POST", "path": "/invocations", "headers": [(b"content-length", b"12")]}
    if route == "response-get":
        scope.update(method="GET", path="/responses/original")
    elif route == "websocket":
        scope.update(type="websocket", path="/invocations_ws")
    message = {"type": "http.request", "body": b"original-data", "more_body": False}
    seen = []

    async def receive():
        return message

    async def app(current, body, send):
        seen.append((current, await body()))

    asyncio.run(ProbeRequestFieldsMiddleware(app)(scope, receive, None))
    assert seen == [(scope, message)]


@pytest.mark.parametrize("last", [
    {"type": "http.request", "body": b"invalid-json", "more_body": False},
    {"type": "http.disconnect"},
])
def test_invalid_json_or_disconnected_body_reaches_original_sdk_boundary_unchanged(last):
    from shared_context.responses_probe import ProbeRequestFieldsMiddleware

    scope = {"type": "http", "method": "POST", "path": "/responses", "headers": [(b"content-length", b"20")]}
    messages = [{"type": "http.request", "body": b'{"input":', "more_body": True}, last]
    source = iter(messages)
    seen = []

    async def receive():
        return next(source)

    async def app(current, body, send):
        seen.append((current, [await body(), await body()]))

    asyncio.run(ProbeRequestFieldsMiddleware(app)(scope, receive, None))
    assert seen == [(scope, messages)]


def test_recovery_and_duplicate_read_original_record_without_another_write(local, monkeypatch):
    probe = N6ResponsesProbe(IDENTITY)
    original = result(asyncio.run(collect(probe)))

    async def forbidden(*args, **kwargs):
        pytest.fail("recovery must only read the original RID")

    monkeypatch.setattr(FoundryStateStore, "get_or_create", forbidden)
    monkeypatch.setattr(FoundryStateStore, "create_item", forbidden)
    monkeypatch.setattr(FoundryStateStore, "set_item", forbidden)
    recovered = result(asyncio.run(collect(N6ResponsesProbe(IDENTITY), recovery=True)))
    assert recovered["record"] == original["record"]
    assert recovered["is_recovery"] and not recovered["state_write_performed"]
    assert recovered["record"]["synthetic_effect_count"] == 1


def test_same_rid_duplicate_has_no_second_synthetic_effect(local):
    probe = N6ResponsesProbe(IDENTITY)
    first = result(asyncio.run(collect(probe)))
    duplicate = result(asyncio.run(collect(probe)))
    assert duplicate["record"] == first["record"] and not duplicate["state_write_performed"]


def test_missing_original_recovery_cannot_create_state(local, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("a missing recovered RID cannot be recreated")
    monkeypatch.setattr(FoundryStateStore, "get_or_create", forbidden)
    terminal = asyncio.run(collect(N6ResponsesProbe(IDENTITY), recovery=True))[-1]["response"]
    assert terminal["status"] == "failed"


@pytest.mark.parametrize("change", ["input", "metadata", "foreground"])
def test_non_synthetic_requests_never_write_or_start_work(local, monkeypatch, change):
    request = payload()
    if change == "input":
        request["input"] = "PRIVATE-real-user-input"
    elif change == "metadata":
        request["metadata"]["fa_agent_id"] = "another-agent"
    elif change == "foreground":
        request["background"] = False

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid input must not reach the State store")
    monkeypatch.setattr(FoundryStateStore, "get_or_create", forbidden)
    terminal = asyncio.run(collect(N6ResponsesProbe(IDENTITY), request=request))[-1]["response"]
    assert terminal["status"] == "failed" and terminal["error"]["message"] == "N6_PROBE_REQUEST_INVALID"
    assert "PRIVATE-" not in json.dumps(terminal)


def test_foundry_cid_not_fitting_fa_identifier_is_observed_not_truncated(local):
    cid = "conv_" + "a" * 80
    observed = result(asyncio.run(collect(N6ResponsesProbe(IDENTITY), cid=cid)))
    assert observed["record"]["client_cid"] == cid and observed["cid_fa_identifier"] is False


@pytest.mark.parametrize("failure,status,reason,with_call_id", [
    ("storage", 429, "http_status=429", True),
    ("transport", None, "exception_type=ServiceRequestError", True),
    ("credential", None, "exception_type=CredentialUnavailableError", False),
    ("unlisted", None, "exception_type=UNCLASSIFIED", True),
    ("storage", "PRIVATE-status", "exception_type=FoundryStorageError", True),
])
def test_agent_read_unknown_reports_only_safe_diagnostics_through_invocations(
        local, monkeypatch, caplog, failure, status, reason, with_call_id):
    from azure.ai.agentserver.core.storage import FoundryStorageError
    from azure.core.exceptions import ServiceRequestError
    from azure.identity import CredentialUnavailableError

    probe = N6ResponsesProbe(IDENTITY)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    attempts = []
    error = {
        "storage": FoundryStorageError("PRIVATE-message https://private.invalid/?token=PRIVATE-token",
                                      status_code=status, response_body={"secret": "PRIVATE-body"}),
        "transport": ServiceRequestError("PRIVATE-message token=PRIVATE-token"),
        "credential": CredentialUnavailableError("PRIVATE-message token=PRIVATE-token"),
        "unlisted": type("PRIVATE-exception-name", (OSError,), {})("PRIVATE-message"),
    }[failure]

    async def unreadable(store):
        attempts.append("state_store_read")
        raise error

    async def no_items(*args, **kwargs):
        pytest.fail("an unreadable original RID cannot reach conversation items")

    monkeypatch.setattr(FoundryStateStore, "get", unreadable)
    monkeypatch.setattr(probe, "read_conversation", no_items)
    incoming = headers()
    if not with_call_id:
        incoming.pop("x-agent-foundry-call-id")
    with TestClient(create_host(local)) as client:
        response = client.post("/invocations", headers=incoming,
                               json={"operation": "read_conversation", "request_id": RID})
    message = reason + "; call_id_present=" + str(with_call_id).lower() + "; identity_kind=not_observed"
    assert response.status_code == 503
    assert response.json() == {"error": {
        "code": "N6_PROBE_READ_UNKNOWN", "reason": reason, "message": message,
        "call_id_present": with_call_id, "identity_kind": "not_observed",
    }}
    assert attempts == ["state_store_read"]
    assert "FA_N6_PROBE_READ_FAILED code=N6_PROBE_READ_UNKNOWN " + message in caplog.text
    assert "PRIVATE-" not in response.text + caplog.text
    assert "private.invalid" not in response.text + caplog.text
    local.start.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_agent_read_exception_denials_keep_original_status_and_stop_body(local, monkeypatch, status):
    from azure.ai.agentserver.core.storage import FoundryStorageError

    probe = N6ResponsesProbe(IDENTITY)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    attempts = []

    async def denied(store):
        attempts.append(status)
        raise FoundryStorageError("PRIVATE-denial", status_code=status)

    monkeypatch.setattr(FoundryStateStore, "get", denied)
    with TestClient(create_host(local)) as client:
        response = client.post("/invocations", headers=headers(),
                               json={"operation": "read_conversation", "request_id": RID})
    assert response.status_code == status
    assert response.json() == {"error": {"code": "N6_PROBE_STOP"}}
    assert attempts == [status]
    local.start.assert_not_called()


@pytest.fixture
def agent_credential(monkeypatch):
    import azure.identity.aio

    class Credential:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get_token(self, scope):
            assert scope == "https://ai.azure.com/.default"
            return SimpleNamespace(token="PRIVATE-agent-token")

    monkeypatch.setattr(azure.identity.aio, "ManagedIdentityCredential", Credential)


@pytest.mark.parametrize("ca_source", ["file", "directory", "certifi"])
def test_agent_read_transport_uses_verified_ca_without_environment_proxy(
        local, agent_credential, monkeypatch, tmp_path, ca_source):
    import certifi
    import ssl

    probe = N6ResponsesProbe(IDENTITY)
    with platform_context():
        record = probe.request_record(payload(), SimpleNamespace(
            conversation_id=CID, response_id="resp_offline", client_headers=headers(),
        ))
    original_record = record.model_dump(mode="json")
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://must-not-use.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "")
    if ca_source == "file":
        ca_file = tmp_path / "probe-ca.pem"
        ca_file.write_bytes(Path(certifi.where()).read_bytes())
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
        expected_ca = {"cafile": str(ca_file)}
    elif ca_source == "directory":
        ca_dir = tmp_path / "probe-ca"
        ca_dir.mkdir()
        monkeypatch.setenv("SSL_CERT_DIR", str(ca_dir))
        expected_ca = {"capath": str(ca_dir)}
    else:
        expected_ca = {"cafile": certifi.where()}
    ssl_calls, transport_calls, clients, requests = [], [], [], []
    create_ssl_context, transport_type, client_type = ssl.create_default_context, httpx.AsyncHTTPTransport, httpx.AsyncClient

    def ssl_context(**kwargs):
        context = create_ssl_context(**kwargs)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        ssl_calls.append(kwargs)
        return context

    async def no_network(*args, **kwargs):
        pytest.fail("this regression must not use a real network transport")

    def server(request):
        requests.append(request)
        assert request.method == "GET" and not request.content
        assert request.url.path.endswith("/" + CID + "/items")
        assert request.headers["x-agent-foundry-call-id"] == PLATFORM["x-agent-foundry-call-id"]
        assert request.extensions["timeout"] == {"connect": 30, "read": 30, "write": 30, "pool": 30}
        return httpx.Response(200, json={"data": []})

    def transport(**kwargs):
        assert kwargs == {"retries": 0}
        transport_calls.append(kwargs)
        instance = transport_type(**kwargs)
        monkeypatch.setattr(instance, "handle_async_request", httpx.MockTransport(server).handle_async_request)
        return instance

    def client(**kwargs):
        assert isinstance(kwargs.get("transport"), transport_type)
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False and kwargs["timeout"] == 30
        clients.append(kwargs)
        return client_type(**kwargs)

    monkeypatch.setattr(ssl, "create_default_context", ssl_context)
    monkeypatch.setattr(transport_type, "handle_async_request", no_network)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    with platform_context():
        status, observed = asyncio.run(probe.read_conversation(record))
    assert status == 200 and observed["code"] == "N6_D3_READ_OBSERVED"
    assert observed["request_id"] == RID and observed["conversation_id"] == CID and observed["call_id_present"]
    assert record.model_dump(mode="json") == original_record and record.session_id == IDENTITY.session_id
    assert ssl_calls == [expected_ca] and transport_calls == [{"retries": 0}]
    assert len(clients) == len(requests) == 1


def test_agent_read_connect_error_stays_unknown_without_repeating_the_get(
        local, agent_credential, monkeypatch, caplog):
    probe = N6ResponsesProbe(IDENTITY)
    with platform_context():
        record = probe.request_record(payload(), SimpleNamespace(
            conversation_id=CID, response_id="resp_offline", client_headers=headers(),
        ))
    original_record = record.model_dump(mode="json")
    calls, reads, transports = [], [], []

    async def read_original(request_id):
        reads.append(request_id)
        assert request_id == record.request_id
        return record

    def server(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path.endswith("/" + CID + "/items")
        assert request.headers["x-agent-foundry-call-id"] == PLATFORM["x-agent-foundry-call-id"]
        raise httpx.ConnectError("PRIVATE-connection-detail", request=request)

    def transport(**kwargs):
        assert kwargs == {"retries": 0}
        transports.append(kwargs)
        return httpx.MockTransport(server)

    async def no_network(*args, **kwargs):
        pytest.fail("this regression must not use a real network transport")

    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    monkeypatch.setattr(probe, "read", read_original)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    with TestClient(create_host(local)) as client:
        response = client.post("/invocations", headers=headers(),
                               json={"operation": "read_conversation", "request_id": RID})
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "N6_PROBE_READ_UNKNOWN", "reason": "exception_type=ConnectError",
        "call_id_present": True, "identity_kind": "not_observed",
        "message": "exception_type=ConnectError; call_id_present=true; identity_kind=not_observed",
    }
    assert reads == [RID] and transports == [{"retries": 0}] and len(calls) == 1
    assert record.model_dump(mode="json") == original_record and record.session_id == IDENTITY.session_id
    assert "PRIVATE-" not in response.text + caplog.text
    local.start.assert_not_called()


@pytest.mark.parametrize("status", [200, 401, 403, 404, 302, 502])
def test_agent_read_is_one_exact_get_with_original_caller_context_and_no_private_output(
        local, agent_credential, monkeypatch, status):
    probe = N6ResponsesProbe(IDENTITY)
    asyncio.run(collect(probe))
    record = asyncio.run(probe.read(RID))
    calls = []

    def server(request):
        calls.append(request)
        assert request.method == "GET" and not request.content
        assert request.url.path.endswith("/" + PROBE_AGENT_NAME + "/endpoint/protocols/openai/conversations/" + CID + "/items")
        assert request.headers["x-agent-foundry-call-id"] == PLATFORM["x-agent-foundry-call-id"]
        assert request.headers["Authorization"] == "Bearer PRIVATE-agent-token"
        return httpx.Response(status, json={"data": [{"content": "PRIVATE-conversation"}]},
                              headers={"location": "https://never-follow.invalid", "x-ms-request-id": "offline-upstream"})

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(server))
    with platform_context():
        response_status, observed = asyncio.run(probe.read_conversation(record))
    assert len(calls) == 1 and observed["call_id_present"]
    assert response_status == (status if status in {200, 401, 403, 404} else 502)
    assert observed["identity_kind"] == "agent_bearer_with_platform_caller_context"
    assert "PRIVATE-" not in json.dumps(observed)
    assert observed["code"] == ("N6_D3_READ_OBSERVED" if status == 200 else "N6_D3_STOP")


def test_interruption_can_only_target_own_single_held_synthetic_rid(local):
    probe = N6ResponsesProbe(IDENTITY, interrupt_session=True)
    probe.holding = asyncio.run(_record(probe)).model_copy(update={"phase": "hold"})
    command = ProbeInterrupt(operation="interrupt", request_id=RID, session_id=IDENTITY.session_id, process_id=os.getpid())
    probe.require_interrupt(command)
    for changed in ({"process_id": os.getpid() + 1}, {"session_id": "another-session"}, {"request_id": "n6-" + "2" * 32}):
        with pytest.raises(Problem):
            probe.require_interrupt(command.model_copy(update=changed))
    probe.interrupt_session = False
    with pytest.raises(Problem):
        probe.require_interrupt(command)


async def _record(probe):
    await collect(probe)
    return await probe.read(RID)


def test_dedicated_interrupt_session_cannot_accept_native_or_legacy_work(local, monkeypatch):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    probe = N6ResponsesProbe(IDENTITY, interrupt_session=True)
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    with pytest.raises(Problem):
        create_host(local, native_runtime=SimpleNamespace(identity=IDENTITY))
    with TestClient(create_host(local)) as client:
        refused = client.post("/invocations", headers=PLATFORM, json={"binding_id": "other", "launch_id": "other"})
        assert refused.status_code == 403
        assert client.get("/invocations/other", headers=PLATFORM).status_code == 403
        assert client.post("/invocations/other/cancel", headers=PLATFORM).status_code == 403
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/invocations_ws"):
                pytest.fail("the interruption session cannot host a desktop WebSocket")
        assert closed.value.code == 4403
    local.start.assert_not_called()


def test_interrupt_uses_real_invocations_background_response_but_only_its_exact_process(local, monkeypatch):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    probe = N6ResponsesProbe(IDENTITY, interrupt_session=True)
    probe.holding = asyncio.run(_record(probe)).model_copy(update={"phase": "hold"})
    monkeypatch.setattr(N6ResponsesProbe, "from_environment", lambda identity: probe)
    interrupted = []

    async def synthetic_exit(command):
        probe.require_interrupt(command)
        interrupted.append(command.process_id)

    monkeypatch.setattr(probe, "interrupt", synthetic_exit)
    with TestClient(create_host(local)) as client:
        command = {"operation": "interrupt", "request_id": RID,
                   "session_id": IDENTITY.session_id, "process_id": os.getpid()}
        wrong = client.post("/invocations", headers=headers(), json={**command, "process_id": os.getpid() + 1})
        assert wrong.status_code == 409 and not interrupted
        response = client.post("/invocations", headers=headers(), json=command)
        assert response.status_code == 202 and response.json()["status"] == "interrupt_scheduled"
        assert interrupted == [os.getpid()]


def test_four_real_agent_manifests_declare_both_transports_without_enabling_handlers():
    root = Path(__file__).resolve().parents[2] / "deploy" / "shared-context" / "src"
    for name in ("a", "b", "c", "d"):
        content = (root / ("fa-context-" + name) / "agent.yaml").read_text(encoding="utf-8")
        assert content.count("protocol: invocations") == 1 and content.count("protocol: responses") == 1
        assert content.count("version: 2.0.0") == 2 and "FA_RESPONSES_ENABLED" not in content
