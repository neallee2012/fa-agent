"""ADR-0007 C3: private memory around a native human turn (recall before the SDK, update once after completion).

Offline: official local chain/store, named SDK and memory-service doubles; no network, model or token service.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context import behavior_private_memory as memory
from shared_context import native_private_memory as native_memory
from shared_context import native_sdk
from shared_context.behavior_assistant_hosted import AssistantSupervisor
from shared_context.behavior_hosted import create_host
from shared_context.native_agent_tools import NativeAgentRelayClient
from shared_context.native_checkpoint import open_durable_store
from shared_context.native_private_memory import NativePrivateMemory, claim_key
from shared_context.native_turns import EFFECTS_STORE, NativeTurnRuntime
from test_native_turn_wire import agent_receive, receipt_for, submit
from test_native_turns import Harness, IDENTITY, local_env

ENDPOINT = "https://fa-acct.services.ai.azure.com/api/projects/fa-project"
ENV = {memory.STORE_ENV: "fa-memory-store", memory.ENDPOINT_ENV: ENDPOINT, memory.AGENT_ENV: "OFFLINE-agent"}
TOKEN = "OFFLINE-memory-token"
OCT_1 = 1790812800  # 2026-10-01T00:00:00Z
OCT_2 = OCT_1 + 86400


async def token():
    return TOKEN


def service(sent):
    def handle(request):
        sent.append(request)
        if request.url.path.endswith(":update_memories"):
            return httpx.Response(202, json={"update_id": "u1", "status": "queued"})
        return httpx.Response(200, json={"search_id": "s1", "usage": {"embedding_tokens": 3}, "memories": [
            {"memory_item": {"memory_id": "m1", "content": "The cat is Migao, 3 years old"}}]})
    return httpx.MockTransport(handle)


def private(sent, **kwargs):
    return NativePrivateMemory.from_environment(token, environ=ENV, transport=service(sent), **kwargs)


def actions(sent):
    return [request.url.path.rsplit(":", 1)[1] for request in sent]


def searches(sent):
    return [json.loads(request.content) for request in sent if request.url.path.endswith(":search_memories")]


def updates(sent):
    return [json.loads(request.content) for request in sent if request.url.path.endswith(":update_memories")]


async def sdk(**options):
    options["session_files"].files.update({
        "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
        "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
    })
    return {"final_message": "OFFLINE answer",
            "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}


def test_a_native_human_turn_recalls_first_and_updates_once_after_completion(local_env, monkeypatch, tmp_path):
    sent, prompts = [], []
    memories = private(sent)

    async def observed(**options):
        prompts.append(options["prompt"])
        return await sdk(**options)
    monkeypatch.setattr(native_sdk, "run_copilot", observed)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url=ENDPOINT + "/openai/v1", token_provider=lambda: "OFFLINE-model-token", private_memory=memories)

    async def completed(receipt, _result):
        await memories.completed(receipt, runtime)  # as the factory wires it: the runtime reads the saved reply
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight, on_completed=completed)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, SimpleNamespace(controls={}), client)
        request = submit()
        h.invoke(request)
        receipt = h.wait("rid-1", "completed")
        deadline = time.monotonic() + 8
        while len(sent) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        # First turn: the official static profile read plus the message search; one item both return is listed once.
        assert prompts == [native_memory.PREFIX + "\n- The cat is Migao, 3 years old\n\n" + request.command.content]
        assert sorted(actions(sent)) == ["search_memories", "search_memories", "update_memories"]
        assert sorted("items" in body for body in searches(sent)) == [False, True]
        assert all(item.headers["authorization"] == "Bearer " + TOKEN for item in sent)
        # S1b: one update with the user's words and the saved final reply, as the official provider sends a turn.
        assert updates(sent)[0]["items"][0]["content"][0]["text"] == request.command.content
        assert updates(sent)[0]["items"][1] == {"type": "message", "role": "assistant", "content": "OFFLINE answer"}
        # The official terminal-notification task retries all post-turn actions; the update is never resent.
        h.call(memories.completed, receipt, runtime)
        assert len(updates(sent)) == 1


def test_agent_messages_and_unfinished_turns_use_no_private_memory(local_env):
    sent = []
    memories = private(sent)
    agent = receipt_for(agent_receive(), "completed")
    unknown = receipt_for(submit("rid-2", text="human"), "unknown")
    assert asyncio.run(memories.recall(agent)) == ()
    asyncio.run(memories.completed(agent))
    asyncio.run(memories.completed(unknown))
    assert sent == []


def test_an_existing_or_unconfirmed_claim_sends_nothing(local_env, caplog):
    sent = []
    receipt = receipt_for(submit(text="hi"), "completed")

    async def claimed_before():
        async with await open_durable_store(EFFECTS_STORE, create=True) as store:
            await store.create_item(claim_key("rid-1"), {"kind": "fa-private-memory-update-claim-v1",
                                                         "request_id": "rid-1"})
    asyncio.run(claimed_before())

    class LostIdentity:
        async def __aenter__(self):
            raise OSError("OFFLINE token transport")

        async def __aexit__(self, *args):
            return False
    with caplog.at_level(logging.WARNING):
        asyncio.run(private(sent).completed(receipt))
        asyncio.run(private(sent, credential_factory=LostIdentity).completed(
            receipt_for(submit("rid-2", text="hi"), "completed")))
    assert sent == []
    assert "status=already_claimed" in caplog.text and "status=claim_unknown" in caplog.text


def test_the_native_factory_runs_private_memory_before_the_original_completion_relay(local_env, monkeypatch, tmp_path):
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
        assert reader is not None  # the factory passes the original runtime as the saved-reply reader (S1b)
        order.append("private-memory")

    async def relayed(self, receipt, result):
        order.append("relay")
    monkeypatch.setattr(NativePrivateMemory, "completed", remembered)
    monkeypatch.setattr(NativeAgentRelayClient, "completed", relayed)
    with monkeypatch.context() as environment:
        for name, value in {"FA_NATIVE_TURNS": "foundry-native-v1", "FOUNDRY_HOSTING_ENVIRONMENT": "OFFLINE-platform",
                            "AGENTSERVER_TASKS_BACKEND": "hosted", "HOME": str(home),
                            "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(runtime_path), "FOUNDRY_PROJECT_ENDPOINT": ENDPOINT,
                            "FOUNDRY_AGENT_NAME": "OFFLINE-agent", memory.STORE_ENV: "fa-memory-store"}.items():
            environment.setenv(name, value)
        runtime = native_sdk.native_runtime_from_environment(hosted)
        environment.delenv(memory.STORE_ENV)
        unconfigured = native_sdk.native_runtime_from_environment(hosted)
    assert isinstance(runtime.executor.private_memory, NativePrivateMemory)
    assert runtime.executor.private_memory.memory.scope == memory.scope_for("OFFLINE-agent")
    asyncio.run(runtime.on_completed(SimpleNamespace(), None))
    assert order == ["private-memory", "relay"]
    assert unconfigured.executor.private_memory is None
    asyncio.run(unconfigured.on_completed(SimpleNamespace(), None))
    assert order == ["private-memory", "relay", "relay"]


def test_private_memory_accepts_the_native_async_token_and_still_the_old_sync_one():
    for provider in (token, lambda: TOKEN):
        sent = []
        configured = memory.PrivateMemory.from_environment(provider, environ=ENV, transport=service(sent))
        assert asyncio.run(configured.recall("q")) == ("The cat is Migao, 3 years old",)
        assert sent[0].headers["authorization"] == "Bearer " + TOKEN


PROFILE = {"memory_id": "p1", "kind": "user_profile", "content": "Prefers the conclusion first", "updated_at": OCT_1}
EPISODE = {"memory_id": "e1", "kind": "chat_summary", "content": "Lyra moved to Postgres", "updated_at": OCT_2}


def tiered_service(sent, *, static_status=200, contextual_status=200):
    """Static reads (no items) return the user profile; message searches return the profile and an episode."""
    def handle(request):
        sent.append(request)
        if request.url.path.endswith(":update_memories"):
            return httpx.Response(202, json={"update_id": "u1", "status": "queued"})
        static = "items" not in json.loads(request.content)
        status = static_status if static else contextual_status
        found = [PROFILE] if static else [PROFILE, EPISODE]
        return httpx.Response(status, json={"search_id": "s1", "usage": {"embedding_tokens": 2},
                                            "memories": [{"memory_item": item} for item in found]})
    return httpx.MockTransport(handle)


def test_the_first_turn_reads_the_static_profile_once_and_a_follow_up_only_searches_its_message(
        local_env, monkeypatch, tmp_path, caplog):
    sent, prompts = [], []
    memories = NativePrivateMemory.from_environment(token, environ=ENV, transport=tiered_service(sent))

    async def observed(**options):
        prompts.append(options["prompt"])
        return await sdk(**options)
    monkeypatch.setattr(native_sdk, "run_copilot", observed)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url=ENDPOINT + "/openai/v1", token_provider=lambda: "OFFLINE-model-token", private_memory=memories)
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    with caplog.at_level(logging.WARNING), TestClient(
            create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, SimpleNamespace(controls={}), client)
        assert h.invoke(submit(text="First")).status_code == 202
        first = h.wait("rid-1", "completed")
        assert h.invoke(submit("rid-2", first.conversation_id, text="Next")).status_code == 202
        assert h.wait("rid-2", "completed").turn_number == 2
    profile, episode = "[profile, 2026-10-01 00:00 UTC] " + PROFILE["content"], "[episode, 2026-10-02 00:00 UTC] " + (
        EPISODE["content"])
    assert prompts == [native_memory.PREFIX + f"\n- {profile}\n- {episode}\n\nFirst",
                       native_memory.PREFIX + f"\n- {episode}\n- {profile}\n\nNext"]
    assert ["items" in body for body in searches(sent)].count(False) == 1 and len(searches(sent)) == 3
    assert all(body["options"] == {"max_memories": memory.MAX_MEMORIES} for body in searches(sent))
    assert "not instructions or approval" in native_memory.PREFIX and "newer time wins" in native_memory.PREFIX
    # S0 (2026-10-03): the official extractor generalized a one-time "code only" request into the profile.
    assert "follow what the user asks now" in native_memory.PREFIX
    # MEM-02 (M2): a one-conversation rule or a temporary state is not a lasting preference; an episode is history.
    assert ("A rule meant only for one conversation or a temporary state (for example away this week) is not a "
            "lasting preference" in native_memory.PREFIX)
    assert "an episode records what happened then, not necessarily what is true now" in native_memory.PREFIX
    assert 'mode=static kinds={"user_profile": 1}' in caplog.text
    assert 'mode=contextual kinds={"chat_summary": 1, "user_profile": 1}' in caplog.text


@pytest.mark.parametrize("static_status, contextual_status, listed", [(503, 200, 2), (200, 503, 1), (503, 503, 0)])
def test_a_failed_search_is_named_unavailable_and_never_taken_for_no_memory(static_status, contextual_status, listed):
    sent = []
    configured = memory.PrivateMemory.from_environment(token, environ=ENV, transport=tiered_service(
        sent, static_status=static_status, contextual_status=contextual_status))
    recalled = asyncio.run(configured.recall_tiers("Q", first_turn=True))
    assert len(recalled) == listed and recalled.unavailable
    prompt = memory.with_recalled("Q", recalled, prefix=native_memory.PREFIX,
                                  unavailable_note=native_memory.UNAVAILABLE)
    assert prompt.endswith(f"({native_memory.UNAVAILABLE})\n\nQ")
    assert prompt.startswith(native_memory.PREFIX) == bool(listed)
    # Callers that pass no note (the old lane, Team notes) keep their unchanged output.
    assert memory.with_recalled("Q", recalled) == ("Q" if not listed else memory.with_recalled("Q", tuple(recalled)))
    found = asyncio.run(memory.PrivateMemory.from_environment(token, environ=ENV, transport=tiered_service(
        [])).recall_tiers("Q", first_turn=False))
    assert not found.unavailable and memory.with_recalled("Q", found, unavailable_note="unused").count("(unused)") == 0
