"""ADR-0007 C3: private memory around a native human turn (recall before the SDK, update once after completion).

The recall, update, claim and unavailable-search tests run per backend in test_memory_backend_contract.py
(ADR-0011 §8); this file keeps the native factory wiring and Foundry-only details.

Offline: official local chain/store, named SDK and memory-service doubles; no network, model or token service.
"""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx

from shared_context import behavior_private_memory as memory
from shared_context import native_sdk
from shared_context.behavior_assistant_hosted import AssistantSupervisor
from shared_context.native_agent_tools import NativeAgentRelayClient
from shared_context.native_checkpoint import open_durable_store
from shared_context.native_private_memory import NativePrivateMemory
from shared_context.native_turns import EFFECTS_STORE
from test_native_turn_wire import receipt_for, submit
from test_native_turns import IDENTITY, local_env

ENDPOINT = "https://fa-acct.services.ai.azure.com/api/projects/fa-project"
ENV = {memory.STORE_ENV: "fa-memory-store", memory.ENDPOINT_ENV: ENDPOINT, memory.AGENT_ENV: "OFFLINE-agent"}
TOKEN = "OFFLINE-memory-token"


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


def updates(sent):
    return [json.loads(request.content) for request in sent if request.url.path.endswith(":update_memories")]


async def sdk(**options):
    options["session_files"].files.update({
        "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
        "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
    })
    return {"final_message": "OFFLINE answer",
            "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}


def test_an_unexpected_private_memory_failure_never_raises(local_env, monkeypatch, caplog):
    """The hook runs after the group post in ``finally``; raising there would replace the group failure."""
    async def broken(self, request_id):
        raise RuntimeError("OFFLINE unexpected")
    monkeypatch.setattr(NativePrivateMemory, "_claim", broken)
    sent = []
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(private(sent).completed(receipt_for(submit(text="hi"), "completed"))) is None
    assert sent == [] and "FA_PRIVATE_MEMORY_UPDATE status=not_sent exception_type=RuntimeError" in caplog.text


def test_the_native_factory_runs_private_memory_after_the_original_completion_relay(local_env, monkeypatch, tmp_path):
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

    async def settled(*_args):
        return None
    monkeypatch.setattr(NativePrivateMemory, "completed", remembered)
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
                            "FOUNDRY_AGENT_NAME": "OFFLINE-agent", memory.STORE_ENV: "fa-memory-store"}.items():
            environment.setenv(name, value)
        runtime = native_sdk.native_runtime_from_environment(hosted)
        environment.delenv(memory.STORE_ENV)
        unconfigured = native_sdk.native_runtime_from_environment(hosted)
    assert isinstance(runtime.executor.private_memory, NativePrivateMemory)
    assert runtime.executor.private_memory.memory.scope == memory.scope_for("OFFLINE-agent")
    asyncio.run(finished(runtime))
    assert order == ["relay", "private-memory"]
    assert unconfigured.executor.private_memory is None
    asyncio.run(finished(unconfigured))
    assert order == ["relay", "private-memory", "relay"]


def test_the_native_factory_passes_the_api_connection_and_names_why_memory_is_off(local_env, monkeypatch, tmp_path,
                                                                                    caplog):
    """ADR-0011: both memories get the API connection, and the unavailable reason follows the backend."""
    from shared_context.native_team_memory import TEAM_ENV
    home = tmp_path / "home"
    home.mkdir()
    runtime_path = tmp_path / "OFFLINE-not-executed"
    runtime_path.write_text("OFFLINE SDK boundary only", encoding="utf-8")
    hosted = SimpleNamespace(
        identity=IDENTITY, supervisor=AssistantSupervisor(home / ".fa-assistant-v12", runtime_path=runtime_path),
        api=SimpleNamespace(api_url="https://api.offline.invalid", audience="api://OFFLINE"),
    )
    runtimes = {}
    with monkeypatch.context() as environment, caplog.at_level(logging.WARNING):
        for name, value in {"FA_NATIVE_TURNS": "foundry-native-v1", "FOUNDRY_HOSTING_ENVIRONMENT": "OFFLINE-platform",
                            "AGENTSERVER_TASKS_BACKEND": "hosted", "HOME": str(home),
                            "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(runtime_path), "FOUNDRY_PROJECT_ENDPOINT": ENDPOINT,
                            "FOUNDRY_AGENT_NAME": "OFFLINE-agent", memory.STORE_ENV: "fa-memory-store",
                            TEAM_ENV: "1", memory.BACKEND_ENV: "cosmos"}.items():
            environment.setenv(name, value)
        runtimes["cosmos"] = native_sdk.native_runtime_from_environment(hosted)
        environment.setenv(memory.EXTRACTION_MODEL_ENV, "gpt-4.1-mini")
        runtimes["cosmos-model"] = native_sdk.native_runtime_from_environment(hosted)
        environment.delenv(memory.BACKEND_ENV)
        environment.delenv(memory.STORE_ENV)
        runtimes["foundry-off"] = native_sdk.native_runtime_from_environment(hosted)
        environment.delenv(TEAM_ENV)
        runtimes["team-off"] = native_sdk.native_runtime_from_environment(hosted)
    # The native lane passes the API connection, so cosmos is on once its extraction model is named.
    assert {name: (type(runtime.executor.private_memory).__name__, type(runtime.executor.team_memory).__name__)
            for name, runtime in runtimes.items()} == {
        "cosmos": ("NoneType", "NoneType"), "cosmos-model": ("NativePrivateMemory", "NativeTeamMemory"),
        "foundry-off": ("NoneType", "NoneType"), "team-off": ("NoneType", "NoneType")}
    cosmos = runtimes["cosmos-model"].executor
    assert type(cosmos.private_memory.memory).__name__ == "CosmosPrivateMemory"
    assert type(cosmos.team_memory.memory).__name__ == "CosmosPrivateMemory"
    # The extraction usage goes to the same Agent-authenticated reporter as the turn's own usage.
    assert cosmos.private_memory.usage_reporter.__func__ is native_sdk.NativeUsageReporter.report
    assert cosmos.private_memory.usage_reporter.__self__.url == (
        f"https://api.offline.invalid/agents/{IDENTITY.agent_id}/work-policy/v1/turn-usage")
    assert "FA_PRIVATE_MEMORY_INACTIVE" not in caplog.text
    assert {name: runtime.executor.memory_unavailable_reasons for name, runtime in runtimes.items()} == {
        "cosmos": ("not_configured", "not_configured"),
        "cosmos-model": ("configuration_invalid", "configuration_invalid"),
        "foundry-off": ("not_configured", "not_configured"), "team-off": ("not_configured", "disabled")}


def test_the_update_names_its_source_request_for_the_backend(local_env, monkeypatch):
    seen = []
    original = memory.PrivateMemory.update_chained

    async def update_chained(self, *args, **kwargs):
        seen.append(kwargs.get("request_id"))
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(memory.PrivateMemory, "update_chained", update_chained)

    async def effects():
        async with await open_durable_store(EFFECTS_STORE, create=True):
            pass
    asyncio.run(effects())
    sent = []
    asyncio.run(private(sent).completed(receipt_for(submit(text="hi"), "completed")))
    assert seen == ["rid-1"] and len(updates(sent)) == 1


def test_private_memory_accepts_the_native_async_token_and_still_the_old_sync_one():
    for provider in (token, lambda: TOKEN):
        sent = []
        configured = memory.PrivateMemory.from_environment(provider, environ=ENV, transport=service(sent))
        assert asyncio.run(configured.recall("q")) == ("The cat is Migao, 3 years old",)
        assert sent[0].headers["authorization"] == "Bearer " + TOKEN


