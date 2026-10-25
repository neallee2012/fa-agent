"""TN-1 Team note sharing: a human's own verbatim suggestion is created once in the Team memory scope; Team Agents
recall Team notes on human turns. Off by default.

The scope, pin, reference-check, ``_shown`` and no-resend tests run per backend in test_memory_backend_contract.py
(ADR-0011 §8); this file keeps the capture, factory and Foundry-only wiring.

Offline: official local chain/store, named SDK and memory-service doubles; no network, model or token service.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context import behavior_private_memory as memory
from shared_context import native_memory_candidate as candidates
from shared_context import native_sdk
from shared_context import native_team_memory as team
from shared_context.behavior_assistant_hosted import AssistantSupervisor
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_capture import TEAM_MEMORY_ENV, team_memory_capture
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture, TeamMemoryCapture, team_memory_scope
from shared_context.m2_copilot import ToolFailure
from shared_context.native_agent_tools import NativeAgentRelayClient
from shared_context.native_checkpoint import open_durable_store
from shared_context.native_private_memory import NativePrivateMemory
from shared_context.native_turns import EFFECTS_STORE, NativeTurnRuntime
from test_native_capabilities import selected_capture
from test_native_turn_wire import agent_receive, capture, receipt_for, submit
from test_native_turns import Harness, IDENTITY, local_env

ENDPOINT = "https://fa-acct.services.ai.azure.com/api/projects/fa-project"
ENV = {memory.STORE_ENV: "fa-memory-store", memory.ENDPOINT_ENV: ENDPOINT, memory.AGENT_ENV: "OFFLINE-agent",
       team.TEAM_ENV: "1"}
TOKEN = "OFFLINE-memory-token"
SCOPE = team_memory_scope("OFFLINE-tenant", "OFFLINE-team")
OTHER = team_memory_scope("OFFLINE-tenant", "OFFLINE-other-team")
TEXT = "Please remember: our release window is Friday 18:00. Thanks!"
QUOTE = "our release window is Friday 18:00"


async def token():
    return TOKEN


def service(sent, *, create_status=200, search_status=200, found=None):
    found = [{"memory_id": "t1", "content": "[OFFLINE-peer, 2026-10-01] Use repo X"}] if found is None else found

    def handle(request):
        sent.append(request)
        if request.url.path.endswith("/items"):
            return httpx.Response(create_status, json={"memory_id": "m1"})
        if request.url.path.endswith(":update_memories"):
            return httpx.Response(202, json={"update_id": "u1", "status": "queued"})
        return httpx.Response(search_status, json={"search_id": "s1", "usage": {"embedding_tokens": 3}, "memories": [
            {"memory_item": item} for item in found]})
    return httpx.MockTransport(handle)


def team_memory(sent, **kwargs):
    return team.NativeTeamMemory.from_environment(token, environ=ENV, transport=service(sent, **kwargs))


def bodies(sent, suffix):
    return [json.loads(request.content) for request in sent if request.url.path.endswith(suffix)]


def with_team(value: NativeTurnCapture, scope=SCOPE) -> NativeTurnCapture:
    return value.model_copy(update={"team_memory": None if scope is None else TeamMemoryCapture(scope=scope)})


def memory_receipt(text="q", *, scope=SCOPE, rid="rid-1", cid="cid-1", status="started", agent=False):
    request = agent_receive(rid, cid) if agent else submit(rid, cid, text=text)
    request = request.model_copy(update={"capture": with_team(request.capture, scope)})
    return receipt_for(request, status)


def stage(decision="candidate", reason="durable_fact", supersedes=None):
    return {"decision": decision, "reason": reason if decision == "candidate" else "no_durable_information",
            **({"quote": QUOTE} if decision == "candidate" else {}),
            **({"supersedes": supersedes} if supersedes is not None else {})}


@pytest.fixture(autouse=True)
def fresh_turn_refs():
    team._TURN_REFS.clear()
    yield
    team._TURN_REFS.clear()


def run_turn(monkeypatch, tmp_path, sent, *, decision="candidate", scope=SCOPE, memories=None, reason="durable_fact",
             supersedes=None, created=None):
    """One human native turn with the C5 candidate tool; the SDK double stages ``decision``.

    ``created`` lists the creates sent so far (default: Foundry ``/items`` bodies in ``sent``).
    """
    memories = team_memory(sent) if memories is None else memories
    created = (lambda: bodies(sent, "/items")) if created is None else created
    prompts, failures = [], []

    async def sdk(**options):
        prompts.append(options["prompt"])
        spec = next(tool for tool in options["tools"] if tool.name == candidates.TOOL)
        try:
            await spec.handler({"session_id": options["sdk_session_id"], "tool_call_id": "OFFLINE-call",
                                "tool_name": spec.name, "arguments": stage(decision, reason, supersedes)})
        except ToolFailure as failure:
            failures.append(failure.public_code)
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "OFFLINE answer",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url=ENDPOINT + "/openai/v1", token_provider=lambda: "OFFLINE-model-token",
        capability_providers=(candidates.PROVIDER,), team_memory=memories)

    async def completed(receipt, _result):
        await memories.completed(receipt)
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight, on_completed=completed)
    executor.reader = runtime
    request = NativeSubmit(command=submit(text=TEXT).command, capture=with_team(selected_capture(candidates.ENTRY_ID),
                                                                                 scope))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        assert harness.invoke(request).status_code == 202
        receipt = harness.wait("rid-1", "completed")
        deadline = time.monotonic() + 8
        while decision == "candidate" and not failures and not created() and time.monotonic() < deadline:
            time.sleep(0.01)
        harness.call(memories.completed, receipt)  # the official terminal task may retry every post-turn action
    return SimpleNamespace(prompts=prompts, receipt=receipt, failures=failures)


def test_the_api_captures_the_team_scope_only_when_enabled_and_old_bytes_stay_unchanged():
    assert team_memory_capture("t", "team-a", environ={}) is None
    assert team_memory_capture("t", "team-a", environ={TEAM_MEMORY_ENV: "true"}) is None
    enabled = team_memory_capture("t", "team-a", environ={TEAM_MEMORY_ENV: "1"})
    assert enabled.scope == team_memory_scope("t", "team-a") != team_memory_scope("t", "team-b")
    assert enabled.scope != team_memory_scope("other-tenant", "team-a")
    original = capture()
    assert "team_memory" not in original.model_dump_json()
    assert with_team(original, None).sha256 == original.sha256
    restored = NativeTurnCapture.model_validate_json(with_team(original).model_dump_json())
    assert restored.team_memory.scope == SCOPE
    with pytest.raises(ValueError):
        TeamMemoryCapture(scope="fap1-" + "0" * 48)


def test_the_candidate_tool_says_a_changed_way_of_working_is_a_stable_preference():
    description = asyncio.run(candidates.bind(SimpleNamespace(receipt=SimpleNamespace(source=None)), ()))
    assert "changes or corrects how they want work done, that is also a stable_preference" in (
        description.tools[0].description)


def test_two_shown_notes_with_the_same_short_reference_name_neither():
    team._remember_refs("rid-1", memory.Recalled(("a", "b", "c"), ids=("x-ref00001", "y-ref00001", None)))
    assert team.recalled_note("rid-1", "note:ref00001") is None


def test_a_note_names_its_source_request_for_the_backend(local_env, monkeypatch, tmp_path):
    seen = []
    original = memory.PrivateMemory.create

    async def create(self, *args, **kwargs):
        seen.append(kwargs.get("request_id"))
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(memory.PrivateMemory, "create", create)
    sent = []
    run_turn(monkeypatch, tmp_path, sent)
    assert seen == ["rid-1"] and len(bodies(sent, "/items")) == 1


def test_without_the_api_scope_nothing_is_searched_or_written(local_env, monkeypatch, tmp_path):
    sent = []
    run_turn(monkeypatch, tmp_path, sent, scope=None)
    assert sent == []


def create_effects_store():
    async def effects():
        async with await open_durable_store(EFFECTS_STORE, create=True):
            pass
    asyncio.run(effects())


def test_a_follow_up_with_the_scope_newly_enabled_continues_the_original_conversation(local_env, monkeypatch, tmp_path):
    sent = []
    memories = team_memory(sent)

    async def sdk(**options):
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "OFFLINE answer",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}
    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url=ENDPOINT + "/openai/v1", token_provider=lambda: "OFFLINE-model-token", team_memory=memories)
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        assert harness.invoke(submit()).status_code == 202
        first = harness.wait("rid-1", "completed")
        second = NativeSubmit(command=submit("rid-2", first.conversation_id, text="Next").command,
                              capture=with_team(capture()))
        assert harness.invoke(second).status_code == 202
        assert harness.wait("rid-2", "completed").turn_number == 2
    assert [body["scope"] for body in bodies(sent, ":search_memories")] == [SCOPE]


def test_the_factory_shares_before_private_memory_and_a_failure_never_blocks_the_relay(local_env, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runtime_path = tmp_path / "OFFLINE-not-executed"
    runtime_path.write_text("OFFLINE SDK boundary only", encoding="utf-8")
    hosted = SimpleNamespace(
        identity=IDENTITY, supervisor=AssistantSupervisor(home / ".fa-assistant-v12", runtime_path=runtime_path),
        api=SimpleNamespace(api_url="https://api.offline.invalid", audience="api://OFFLINE"),
    )
    order = []

    async def remembered(self, receipt, reader=None):
        order.append("private-memory")

    async def shared(self, receipt):
        order.append("team-note")
        raise OSError("OFFLINE store transport")

    async def relayed(self, receipt, result):
        order.append("relay")

    async def settled(*_args):
        return None
    monkeypatch.setattr(NativePrivateMemory, "completed", remembered)
    monkeypatch.setattr(team.NativeTeamMemory, "_share", shared)
    monkeypatch.setattr(NativeAgentRelayClient, "completed", relayed)
    monkeypatch.setattr(native_sdk.NativeUsageReporter, "__call__", settled)
    monkeypatch.setattr(native_sdk.NativeSdkExecutor, "notify_terminal", settled)
    receipt = SimpleNamespace(status="completed", result=None, request_id="OFFLINE-rid")

    async def finished(current):
        await current.on_terminal(receipt)  # the peer result goes out ahead of usage and memory
        await current.on_completed(receipt, None)
    with monkeypatch.context() as environment:
        for name, value in {"FA_NATIVE_TURNS": "foundry-native-v1", "FOUNDRY_HOSTING_ENVIRONMENT": "OFFLINE-platform",
                            "AGENTSERVER_TASKS_BACKEND": "hosted", "HOME": str(home),
                            "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(runtime_path), "FOUNDRY_PROJECT_ENDPOINT": ENDPOINT,
                            "FOUNDRY_AGENT_NAME": "OFFLINE-agent", memory.STORE_ENV: "fa-memory-store",
                            team.TEAM_ENV: "1"}.items():
            environment.setenv(name, value)
        runtime = native_sdk.native_runtime_from_environment(hosted)
        environment.delenv(team.TEAM_ENV)
        off = native_sdk.native_runtime_from_environment(hosted)
    assert isinstance(runtime.executor.team_memory, team.NativeTeamMemory)
    asyncio.run(finished(runtime))
    # ADR-0011: the one Team note before the slower private update; a Team failure still lets the private one run.
    assert order == ["relay", "team-note", "private-memory"]
    assert off.executor.team_memory is None
    asyncio.run(finished(off))
    assert order == ["relay", "team-note", "private-memory", "relay", "private-memory"]


@pytest.mark.parametrize("group_fails", [False, True])
def test_the_group_post_goes_first_then_the_team_note_then_private_memory(local_env, monkeypatch, tmp_path,
                                                                         group_fails):
    """ADR-0011 phase 1 (also in public mode): the user-visible group post is not delayed by memory; memory still
    runs when it fails, and its failure still reaches the notification task so the group post is retried."""
    from shared_context.native_group_tools import NativeGroupClient
    home = tmp_path / "home"
    home.mkdir()
    runtime_path = tmp_path / "OFFLINE-not-executed"
    runtime_path.write_text("OFFLINE SDK boundary only", encoding="utf-8")
    hosted = SimpleNamespace(
        identity=IDENTITY, supervisor=AssistantSupervisor(home / ".fa-assistant-v12", runtime_path=runtime_path),
        api=SimpleNamespace(api_url="https://api.offline.invalid", audience="api://OFFLINE"),
    )
    order = []

    async def posted(self, receipt):
        order.append("group")
        if group_fails:
            raise OSError("OFFLINE group transport")

    async def shared(self, receipt):
        order.append("team-note")

    async def remembered(self, receipt, reader=None):
        order.append("private-memory")
    monkeypatch.setattr(NativeGroupClient, "complete", posted)
    monkeypatch.setattr(team.NativeTeamMemory, "completed", shared)
    monkeypatch.setattr(NativePrivateMemory, "completed", remembered)
    monkeypatch.delenv("FA_NATIVE_HITL_ENABLED", raising=False)
    with monkeypatch.context() as environment:
        for name, value in {"FA_NATIVE_TURNS": "foundry-native-v1", "FOUNDRY_HOSTING_ENVIRONMENT": "OFFLINE-platform",
                            "AGENTSERVER_TASKS_BACKEND": "hosted", "HOME": str(home),
                            "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(runtime_path), "FOUNDRY_PROJECT_ENDPOINT": ENDPOINT,
                            "FOUNDRY_AGENT_NAME": "OFFLINE-agent", memory.STORE_ENV: "fa-memory-store",
                            team.TEAM_ENV: "1", "FA_NATIVE_GROUPS_ENABLED": "1"}.items():
            environment.setenv(name, value)
        runtime = native_sdk.native_runtime_from_environment(hosted)
    receipt = SimpleNamespace(status="completed", result=None, request_id="OFFLINE-rid")
    if group_fails:
        with pytest.raises(OSError):
            asyncio.run(runtime.on_completed(receipt, None))
    else:
        asyncio.run(runtime.on_completed(receipt, None))
    assert order == ["group", "team-note", "private-memory"]
