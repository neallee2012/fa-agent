"""Console -> signed N2 API -> official N1 local task/store; only SDK/model I/O is synthetic."""

import asyncio
from dataclasses import replace
import os
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from azure.ai.agentserver.core.tasks import resilient_tasks_enabled, set_resilient_tasks_enabled
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER
from shared_context.behavior_native_sessions import native_session_id, native_sessions
from shared_context.behavior_native_turn_api import PREFIX
from shared_context.behavior_turn_usage import TURN_USAGE_SCHEMA, ModelUsage, TurnModelUsage
from shared_context.behavior_wire import HostedIdentity
from shared_context.native_turns import NativeTurnRuntime
from test_native_turns import OfflineSdk
from test_web_test_console_native import native_console, first, send, post, progress
from test_native_turn_api import native_api
from test_conversation_transfer_startup import synthetic_standing_v1, read_startup
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


class OfflineModel(OfflineSdk):
    async def __call__(self, receipt, scope, previous, control):
        result = await super().__call__(receipt, scope, previous, control)
        return replace(result, usage=TurnModelUsage(schema_version=TURN_USAGE_SCHEMA, usage=(
            ModelUsage(model=receipt.capture.model.id, wire_api="responses", requests=1, measured_requests=1,
                       input_tokens=10, output_tokens=5),
        )))


@pytest.fixture
def official_console(request, tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    case = request.getfixturevalue("native_console")
    old_enabled = resilient_tasks_enabled()
    root = str((tmp_path / "OFFLINE-native-storage").resolve())
    monkeypatch.delenv("FOUNDRY_HOSTING_ENVIRONMENT", raising=False)
    monkeypatch.delenv("FA_N0_STATE_PROBE", raising=False)
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", "\\\\?\\" + root if os.name == "nt" else root)
    # Since 5bda50d8 an unprepared bootstrap runs on the server-selected build's reader session. Offline, that is
    # this same build and its reader session already exists, so the local host serves that session.
    original = case.api.foundry.target
    client_id = case.api.composition.config.identity.managed_identity_client_id
    target = original.model_copy(update={"session_id": native_session_id(
        original, client_id, version=original.agent_version)})
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: original.agent_version)
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session",
                        lambda client: client.target if client.target.session_id == target.session_id else None)
    monkeypatch.setattr("shared_context.behavior_native_sessions._require_native_version", lambda _client: None)
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", target.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", target.agent_name)
    identity = HostedIdentity(agent_id=target.agent_id, agent_name=target.agent_name,
                              agent_version=target.agent_version, session_id=target.session_id)
    sdk = OfflineModel()
    sdk.text = "完整答覆\n" * 30_000
    runtime = NativeTurnRuntime(identity, sdk)
    calls = []
    old_http = case.api.foundry.http
    try:
        with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime),
                        backend_options={"loop_factory": lambda: loop}) as host:
            def platform(request):
                assert "x-agent-invocation-id" not in request.headers
                assert "x-agent-foundry-call-id" not in request.headers
                assert request.headers[NATIVE_TURN_HEADER] == NATIVE_STATE_PROFILE
                calls.append(request)
                response = host.post("/invocations", params={"agent_session_id": target.session_id},
                    content=request.content, headers={
                        "Content-Type": "application/json", NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE,
                        "x-agent-invocation-id": "OFFLINE-platform-" + str(len(calls)),
                        "x-agent-foundry-call-id": "OFFLINE-original-platform-context",
                    })
                return httpx.Response(response.status_code, headers=response.headers, content=response.content)
            with httpx.Client(transport=httpx.MockTransport(platform)) as client:
                case.api.foundry.http = client
                native_sessions(case.api.composition).prepared = None
                prepared = case.api.client.post(PREFIX + "/prepare", headers=case.api.headers,
                                                json={"request_id": "fixture-prepared"})
                assert prepared.status_code == 200, prepared.text
                yield SimpleNamespace(case=case, runtime=runtime, sdk=sdk, host=host, calls=calls)
    finally:
        case.api.foundry.http = old_http
        set_resilient_tasks_enabled(old_enabled)
        if not loop.is_closed():
            loop.close()


def wait(official, rid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        receipt = official.host.portal.call(official.runtime.read, rid)
        if receipt.status == "completed":
            return receipt
        if receipt.status in {"unknown", "busy_refused"}:
            pytest.fail(f"{receipt.status}: {receipt.failure_code}")
        time.sleep(0.01)
    pytest.fail("The official local task did not complete its synthetic SDK turn")


def test_real_native_chain_console_two_turns_and_cold_client_readback(official_console):
    official, c = official_console, official_console.case
    draft = first(c)
    assert send(c, draft).status_code == 200
    saved = wait(official, draft["request_id"])
    assert saved.result.text_parts > 1
    observed = progress(c, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    first_task = next(t for t in observed.json()["tasks"] if t["task_id"] == draft["request_id"])
    assert first_task["reply"]["data"]["text"] == official.sdk.text
    assert observed.json()["composer"]["can_follow_up"]
    next_ = post(c, "/api/instructions/" + draft["thread_id"] + "/next")
    assert next_.status_code == 200, next_.text
    following = next_.json()["composer"]
    sent = send(c, following, "Continue the original conversation")
    assert sent.status_code == 200, sent.text
    second = wait(official, following["request_id"])
    assert second.conversation_id == saved.conversation_id and second.turn_number == 2
    assert official.sdk.calls[1][2] is not None
    assert official.sdk.max_active == 1 and len(official.sdk.calls) == 2
    assert {call[3] for call in official.sdk.calls} == {"OFFLINE-original-platform-context"}
    assert progress(c, draft["thread_id"]).status_code == 200
    # A fresh client uses its original native lane and command, without creating another SDK turn.
    from web_test_console.bridge import Console
    restarted = Console(c.config, c.tmp / "native.sqlite3", backend=c.ui.console.backend, clock=lambda: c.c.h.time,
        token_provider=lambda: c.api.headers["Authorization"].removeprefix("Bearer "),
        instruction_connection=c.connection.model_copy(update={"state_profile": None}))
    restarted.authenticate()
    reread = restarted.instruction_progress(draft["thread_id"])
    assert reread["composer"]["hosted_session_id"] == saved.hosted_session_id
    assert all(task["reply"]["data"]["text"] == official.sdk.text
               for task in reread["tasks"] if task.get("state_profile") == "foundry-native-v1")  # the workbench lane
    assert len(official.sdk.calls) == 2 and c.c.control.records == {}
    read_startup(c.c)
