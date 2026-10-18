"""Idle-only control while the original two Hosted lanes await a real typed question, approval or Connect card."""

from datetime import datetime, timezone
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
import pytest

from conftest import A, TENANT
from shared_context import native_sdk
from shared_context.behavior_native_turn_wire import (
    NativeRead, NativeSessionCut, NativeSessionFollow, NativeSessionRead, NativeSessionFilesOmission)
from shared_context.errors import Problem
from shared_context.native_environment import follow_session
from shared_context.native_environment_cut import cut_session
from shared_context.native_hitl_wire import NativeHitlTurn
from shared_context.native_turns import NativeTurnRuntime
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import hitl, post
from test_native_hitl_h2 import MODEL_ROOT, api_client
from test_native_hitl_questions import _lane_sdk, answer, page, read_question
from test_native_responses import response_submit, responses
from test_native_session_bootstrap import prepare
from test_native_turn_wire import submit
from test_native_turns import IDENTITY, OfflineSdk, local_env


def _cut_waits(harness, probe, request_id, state):
    """ADR-0009 gate 6 for ``NativeSessionCut``: the open card keeps its receipt ``started``, so the cut is busy and
    writes nothing (the Agent ETag is unchanged)."""
    view = harness.call(cut_session, probe, NativeSessionCut(
        request_id=request_id, expected_session_id=IDENTITY.session_id, expected_etag=state["etag"]))
    assert (view.state, view.reason) == ("current", "accepted_or_started") and not probe.executor.calls
    assert harness.invoke(NativeSessionRead()).json()["etag"] == state["etag"]


@pytest.mark.parametrize("lane", ["foundry-native-v1", "foundry-responses-v1"])
def test_idle_follow_cannot_close_the_original_question_or_turn(hitl, responses, tmp_path, monkeypatch, lane):
    hitl.now = datetime.now(timezone.utc)
    original_scope = hitl.config.agent_scope
    hitl.config.targets += (SimpleNamespace(agent_id=IDENTITY.agent_id),)
    hitl.config.agent_scope = lambda identifier: (
        SimpleNamespace(tenant_id=TENANT, team_id=A.team, identity=A) if identifier == IDENTITY.agent_id
        else original_scope(identifier))
    asked = []
    _lane_sdk(monkeypatch, asked)

    def no_model(_):
        pytest.fail("this question and follow control must not start a model")

    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=Path(__file__), api_url="https://api.offline.invalid",
        model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE", model_transport=httpx.MockTransport(no_model),
        hitl_client=api_client(hitl))
    executor.reader = responses.runtime
    responses.runtime.executor, responses.runtime.preflight = executor, executor.preflight
    assert prepare(responses).status_code == 200
    command = response_submit() if lane == "foundry-responses-v1" else submit()
    assert responses.invoke(command).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    if lane == "foundry-responses-v1":
        assert responses.respond(accepted).status_code == 200
    turn = NativeHitlTurn(agent_id=IDENTITY.agent_id, conversation_id=accepted.conversation_id, request_id="rid-1")
    deadline = time.monotonic() + 8
    while not (listed := page(hitl, turn))["attention"]:
        assert time.monotonic() < deadline, "the original question must reach the owner"
        time.sleep(0.01)
    item = listed["attention"][0]
    question = read_question(hitl, item["item_id"]).json()
    original = responses.invoke(NativeRead(request_id="rid-1")).json()
    state = responses.invoke(NativeSessionRead()).json()
    destination = IDENTITY.model_copy(update={"session_id": "idle-follow-destination", "agent_version": "2"})
    probe = NativeTurnRuntime(destination, OfflineSdk())
    _cut_waits(responses, probe, "idle-cut-question", state)
    pending = responses.call(follow_session, probe, NativeSessionFollow(
        request_id="idle-follow-question", expected_session_id=IDENTITY.session_id, expected_etag=state["etag"],
        stage="prepare", idle_only=True))
    assert pending.reason == "accepted_or_started" and not probe.executor.calls
    assert responses.invoke(NativeRead(request_id="rid-1")).json() == original
    assert read_question(hitl, item["item_id"]).json() == question
    with pytest.raises(Problem) as omission:
        responses.call(probe.dispatch, NativeSessionFilesOmission(
            request_id="no-files-cannot-close-question",
            follow=NativeSessionFollow(
                request_id="idle-follow-question", expected_session_id=IDENTITY.session_id,
                expected_etag=state["etag"], stage="prepare", idle_only=True, files_required=True),
            missing="destination"))
    assert omission.value.body["error"]["code"] == "NATIVE_FILES_CONFIRMATION_REQUIRED"
    assert responses.invoke(NativeRead(request_id="rid-1")).json() == original
    assert read_question(hitl, item["item_id"]).json() == question
    reply = answer({"question_id": item["item_id"], "question_digest": item["question"]["question_digest"]})
    assert post(hitl, "/answers", reply, read_only=True).status_code == 200
    completed = responses.wait("rid-1", "completed")
    assert completed.conversation_id == accepted.conversation_id and len(asked) == 1
    assert asked[0]["reply"] == {"answer": "blue", "wasFreeform": False}


ROUTINE_DRAFT = {"local_time": "09:00", "time_zone": "UTC", "content": "Check the synthetic page."}


async def _open_card(kind, gate, sid):
    """Raise one card of ``kind`` through the turn's own HITL runtime, by the entry its SDK callback uses."""
    from shared_context.behavior_delegation_wire import DelegationPrepare, DelegationTask
    from shared_context.native_delegation import delegation_material
    from shared_context.native_hitl_wire import ConnectorConsent
    from test_native_hitl_h2 import permission_request

    def request(tool, arguments, **kwargs):
        return permission_request({"tool_name": tool, "tool_call_id": kind + "-call", "arguments": arguments}, **kwargs)

    if kind == "tool":
        return await gate.permission(request("run_command", {"command": "echo synthetic"}), {"session_id": sid})
    if kind == "direct_mcp":
        return await gate.permission(request("search", {"q": "synthetic"}, kind="mcp", server="offline-mcp"),
                                     {"session_id": sid})
    if kind == "routine":
        gate.ask_owner_to_schedule("propose_routine")
        return await gate.permission(request("propose_routine", dict(ROUTINE_DRAFT)), {"session_id": sid})
    if kind == "delegation":
        # What ``NativeDelegationRuntime._hitl_prepare`` does before the SDK may start the child.
        arguments = {"name": "offline-child", "description": "Check a synthetic sample", "prompt": "Read this sample.",
                     "agent_type": "fa-delegate-v1", "mode": "sync"}
        command = DelegationPrepare(tool_call_id="delegation-call", arguments=DelegationTask(**arguments))
        material = delegation_material(gate.context.receipt, sid, command.arguments.model_dump(mode="json"))
        claim = await gate.effects.reserve(command.tool_call_id, material)
        return await gate.delegation(command, claim, material)
    return await gate.connect(ConnectorConsent(
        call=gate.consent_call(), need="consent", connector_id="teams", toolbox_name="fa-connector-team-one-teams",
        server_label="teams_read", step="tools/list", consent_urls=("https://consent.example.test/login?state=x",)))


def _card_sdk(monkeypatch, kind, ended):
    """The two Hosted handlers with a synthetic SDK session whose one step waits on one card of ``kind``."""
    import asyncio
    import json as jsonlib
    import subprocess
    from threading import Event
    from unittest.mock import create_autospec
    from copilot import CopilotClient
    from copilot.client import GetStatusResponse
    from copilot.session import CopilotSession
    from copilot.session_events import AssistantMessageData, SessionIdleData
    from shared_context import m2_copilot, native_hitl_runtime

    gates = []

    class Recorded(native_hitl_runtime.NativeHitlRuntime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            gates.append(self)

    monkeypatch.setattr(native_hitl_runtime, "NativeHitlRuntime", Recorded)

    def denied(*args, **kwargs):
        pytest.fail("no SDK executable or real subprocess is permitted")

    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)

    def factory(**connection):
        client = create_autospec(CopilotClient, instance=True)
        exited = Event()
        client._cli_process = SimpleNamespace(pid=999, poll=lambda: 0 if exited.is_set() else None, returncode=0)
        client.stop.side_effect = exited.set
        client.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)

        async def create(**options):
            sid = options["session_id"]
            session = create_autospec(CopilotSession, instance=True)
            session.session_id = sid
            session.get_events.return_value = []
            handlers = []
            session.on.side_effect = lambda handler: handlers.append(handler) or (lambda: handlers.remove(handler))

            async def send(prompt, **kwargs):
                try:
                    ended.append(await _open_card(kind, gates[-1], sid))
                except Problem as error:
                    ended.append(error.body["error"]["code"])
                fs = options["create_session_fs_handler"](SimpleNamespace(session_id=sid))
                await fs.write_file("/session-state/events.jsonl", jsonlib.dumps(
                    {"type": "session.start", "data": {"sessionId": sid}}) + "\n")
                await fs.write_file("/session-state/workspace.yaml", "id: " + sid + "\n")
                for handler in handlers:
                    handler(SimpleNamespace(data=AssistantMessageData(content="OFFLINE refused", message_id="offline")))
                    handler(SimpleNamespace(data=SessionIdleData()))
                return "offline-original-message"

            session.send.side_effect = send
            return session

        client.create_session.side_effect = create
        return client

    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=factory))


@pytest.mark.parametrize("kind", ["tool", "direct_mcp", "delegation", "routine", "connect"])
@pytest.mark.parametrize("lane", ["foundry-native-v1", "foundry-responses-v1"])
def test_idle_follow_cannot_close_an_open_approval_or_connect_card_or_its_turn(
        hitl, responses, tmp_path, monkeypatch, lane, kind):
    """ADR-0009 gate 6: every approval category and the Connect card resume only in the original SID's RAM, so while
    one is open its turn's receipt stays ``started`` and an idle-only follow refuses (``accepted_or_started``) without
    touching the receipt or the card; the owner's refusal then ends the turn there."""
    import json as jsonlib
    from shared_context.behavior_native_turn_wire import NativeSubmit
    from shared_context.m2_copilot import ToolOutput, ToolSpec
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
    from shared_context.native_hitl_wire import ConnectorConsentDecision, NativeHitlPendingQuery
    from test_native_capabilities import OfflineResult, selected_capture
    from test_native_hitl import decision
    from test_native_hitl_h2 import verdict

    hitl.now = datetime.now(timezone.utc)
    original_scope = hitl.config.agent_scope
    hitl.config.targets += (SimpleNamespace(agent_id=IDENTITY.agent_id),)
    hitl.config.agent_scope = lambda identifier: (
        SimpleNamespace(tenant_id=TENANT, team_id=A.team, identity=A) if identifier == IDENTITY.agent_id
        else original_scope(identifier))
    ended = []
    _card_sdk(monkeypatch, kind, ended)

    def model(request):
        body = jsonlib.loads(request.content)
        assert body.get("text", {}).get("format", {}).get("name") == "fa_auto_review", "only Auto Review may run"
        event = {"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": jsonlib.dumps(verdict("ask"))}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1}}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("data: " + jsonlib.dumps(event) + "\n\n").encode())

    async def bind(context, grants):
        async def effect(_packet):
            pytest.fail("a refused card never runs its tool")
        return NativeCapabilityBinding(tools=(ToolSpec("run_command", "OFFLINE effect", {
            "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
            "additionalProperties": False}, effect),), result_schemas={"run_command": OfflineResult})

    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=Path(__file__), api_url="https://api.offline.invalid",
        model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE", model_transport=httpx.MockTransport(model),
        hitl_client=api_client(hitl), capability_providers=(NativeCapabilityProvider(
            accepts=lambda grant: grant.entry_id == "fa-shell-v1", bind=bind),))
    executor.reader = responses.runtime
    responses.runtime.executor, responses.runtime.preflight = executor, executor.preflight
    assert prepare(responses).status_code == 200
    command = response_submit() if lane == "foundry-responses-v1" else submit()
    if kind == "tool":
        data = command.model_dump(mode="json")
        data["capture"] = selected_capture("fa-shell-v1").model_dump(mode="json")
        command = NativeSubmit.model_validate_json(jsonlib.dumps(data))
    assert responses.invoke(command).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    if lane == "foundry-responses-v1":
        assert responses.respond(accepted).status_code == 200
    turn = NativeHitlTurn(agent_id=IDENTITY.agent_id, conversation_id=accepted.conversation_id, request_id="rid-1")
    listing = "/turn-consents" if kind == "connect" else "/turn-pending"
    deadline = time.monotonic() + 8
    while not (listed := post(hitl, listing, NativeHitlPendingQuery(turn=turn)).json())["attention"]:
        assert time.monotonic() < deadline, "the original card must reach the owner"
        time.sleep(0.01)
    item = listed["attention"][0]
    path = ("/consents/" if kind == "connect" else "/pending/") + item["item_id"]
    card = hitl.client.get("/native-hitl/v1" + path, headers=hitl.headers()).json()
    if kind != "connect":
        assert card["pending"]["command"]["action"]["category"] == {
            "tool": "shell", "direct_mcp": "mcp"}.get(kind, kind)
    original = responses.invoke(NativeRead(request_id="rid-1")).json()
    assert original["status"] == "started"
    state = responses.invoke(NativeSessionRead()).json()
    destination = IDENTITY.model_copy(update={"session_id": "idle-follow-destination", "agent_version": "2"})
    probe = NativeTurnRuntime(destination, OfflineSdk())
    _cut_waits(responses, probe, "idle-cut-" + kind, state)
    pending = responses.call(follow_session, probe, NativeSessionFollow(
        request_id="idle-follow-" + kind, expected_session_id=IDENTITY.session_id, expected_etag=state["etag"],
        stage="prepare", idle_only=True))
    assert pending.reason == "accepted_or_started" and not probe.executor.calls
    assert responses.invoke(NativeRead(request_id="rid-1")).json() == original
    assert hitl.client.get("/native-hitl/v1" + path, headers=hitl.headers()).json() == card
    if kind == "connect":
        refused = post(hitl, "/consent-decisions", ConnectorConsentDecision(
            request_id="owner-decline", consent_id=item["item_id"], consent_digest=item["consent"]["consent_digest"],
            choice="decline"), read_only=True)
    else:
        refused = post(hitl, "/decisions", decision(hitl, item["approval"], choice="deny"))
    assert refused.status_code == 200, refused.text
    completed = responses.wait("rid-1", "completed")
    assert completed.conversation_id == accepted.conversation_id
    assert ended == [(False, "NATIVE_HITL_HUMAN_DENIED") if kind in {"tool", "direct_mcp", "routine"}
                     else "NATIVE_HITL_HUMAN_DENIED"]
