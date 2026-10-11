"""Typing prewarm on Hosted (README#typing-prewarm): the toolbox lists ahead of a turn and the turn's bind reuses it.

Offline: the toolbox is the official BYO JSON-RPC shape over an httpx transport (test_native_toolbox.Toolbox); the
dispatch cases run the real core local task/store host with the synthetic SDK of test_native_turns.
"""

import asyncio
import json
import logging
from contextlib import AsyncExitStack

import httpx
import pytest

from shared_context import native_toolbox
from shared_context.behavior_native_turn_wire import NativePrewarm, NativePrewarmToolbox, NativePrewarmView
from shared_context.native_capabilities import bind_capabilities
from shared_context.native_turns import TURNS_STORE
from test_native_toolbox import ENDPOINT, Toolbox, bind_on, context, methods
from test_native_turns import IDENTITY, harness, local_env, native_retry_policy, submit  # noqa: F401

ORG = NativePrewarmToolbox(toolbox="fa-org-tools")


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(native_toolbox, "_CALLS", native_toolbox._Calls())


class Gated(httpx.AsyncBaseTransport):
    """The toolbox, with each ``tools/list`` held until the test releases it (and optionally refused)."""

    def __init__(self, toolbox):
        self.toolbox, self.listing, self.release, self.refuse = toolbox, None, None, 0

    async def handle_async_request(self, request):
        await request.aread()
        if request.method == "POST" and json.loads(request.content)["method"] == "tools/list":
            self.listing.set()
            await self.release.wait()
            if self.refuse:
                self.refuse -= 1
                self.toolbox.requests.append((request, json.loads(request.content)))
                return httpx.Response(503)
        return self.toolbox.handle(request)


def agent_token():
    return "agent-token"


async def bind(provider, ctx):
    async with AsyncExitStack() as stack:
        return {spec.name for binding in await bind_capabilities(ctx, (provider,), stack) for spec in binding.tools}


def test_a_prewarm_lists_the_version_without_a_call_id_and_the_next_turn_binds_from_it(monkeypatch, caplog):
    import azure.ai.agentserver.core as core
    from azure.ai.agentserver.core import FoundryAgentRequestContext
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    # Even inside a request that has a call id (the prewarm invocation), the listing is not a turn and sends none.
    monkeypatch.setattr(core, "get_request_context", lambda: FoundryAgentRequestContext(call_id="prewarm-call"))
    asyncio.run(provider.warm(agent_token, (ORG,)))
    assert methods(toolbox) == ["initialize", "notifications/initialized", "tools/list"]
    assert all("x-agent-foundry-call-id" not in request.headers for request, _ in toolbox.requests)
    assert [str(request.url) for request in toolbox.gets] == [ENDPOINT + "/toolboxes/fa-org-tools?api-version=v1"]
    assert "FA_NATIVE_PREWARM toolboxes=1 listed=1 kept=0 listing=0 failed=0" in caplog.text
    monkeypatch.setattr(core, "get_request_context", lambda: FoundryAgentRequestContext(call_id="turn-call-1"))
    sent = len(toolbox.requests)
    tools = bind_on(provider, context())
    # The turn still reads the current default version once, then binds from the prewarmed list: no MCP request.
    assert set(tools) == {"WebIQ-web", "WebIQ-browse"} and len(toolbox.requests) == sent and len(toolbox.gets) == 2
    # A second prewarm of a kept version lists nothing again.
    asyncio.run(provider.warm(agent_token, (NativePrewarmToolbox(toolbox="fa-org-tools", version="1"),)))
    assert len(toolbox.requests) == sent and "kept=1" in caplog.text


def test_a_turn_binding_while_the_prewarm_lists_waits_for_it_and_the_version_is_listed_once(caplog):
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()
    gated = Gated(toolbox)
    provider = native_toolbox.toolbox_provider(transport=gated)

    async def run():
        gated.listing, gated.release = asyncio.Event(), asyncio.Event()
        warming = asyncio.ensure_future(provider.warm(agent_token, (ORG,)))
        await gated.listing.wait()
        gated.listing.clear()
        binding = asyncio.ensure_future(bind(provider, context()))
        await asyncio.sleep(0.05)
        # The turn waits for the prewarm's list in flight; it starts no second listing of the same version.
        assert not binding.done() and not gated.listing.is_set()
        gated.release.set()
        tools = await binding
        await warming
        return tools
    assert asyncio.run(run()) == {"WebIQ-web", "WebIQ-browse"}
    assert methods(toolbox).count("tools/list") == 1 and methods(toolbox).count("initialize") == 1
    waits = [json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
             if record.getMessage().startswith("FA_TIMING ")
             and "native_bind_toolbox_prewarm_wait" in record.getMessage()]
    assert len(waits) == 1 and waits[0]["outcome"] == "returned"


def test_a_failed_prewarm_is_silent_and_the_waiting_turn_lists_the_version_itself(caplog):
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()
    gated = Gated(toolbox)
    gated.refuse = 1
    provider = native_toolbox.toolbox_provider(transport=gated)

    async def run():
        gated.listing, gated.release = asyncio.Event(), asyncio.Event()
        warming = asyncio.ensure_future(provider.warm(agent_token, (ORG,)))
        await gated.listing.wait()
        binding = asyncio.ensure_future(bind(provider, context()))
        await asyncio.sleep(0.05)
        gated.release.set()
        tools = await binding
        # The prewarm never raises; its failure is only counted.
        assert await warming is None
        return tools
    assert asyncio.run(run()) == {"WebIQ-web", "WebIQ-browse"}
    assert methods(toolbox).count("tools/list") == 2
    assert "FA_NATIVE_PREWARM toolboxes=1 listed=0 kept=0 listing=0 failed=1" in caplog.text


def test_a_prewarm_that_cannot_read_the_default_version_lists_nothing_and_never_raises(caplog):
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()
    toolbox.get_reply = lambda request: httpx.Response(503)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    pinned = NativePrewarmToolbox(toolbox="fa-team-team-one", version="2")
    asyncio.run(provider.warm(agent_token, (ORG, pinned)))
    # The unread default lists nothing (a turn would not keep that list either); the pinned version still lists.
    assert methods(toolbox).count("tools/list") == 1
    assert {request.url.path.split("/versions/")[1].split("/")[0] for request, _ in toolbox.requests} == {"2"}
    assert "FA_NATIVE_PREWARM toolboxes=2 listed=1 kept=0 listing=0 failed=1" in caplog.text


def test_without_a_project_endpoint_a_prewarm_only_counts_a_failure(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT")
    toolbox = Toolbox()
    asyncio.run(native_toolbox.toolbox_provider(transport=toolbox.transport).warm(agent_token, (ORG,)))
    assert toolbox.requests == [] and toolbox.gets == []
    assert "FA_NATIVE_PREWARM toolboxes=1 listed=0 kept=0 listing=0 failed=1" in caplog.text


def test_the_executor_starts_each_warm_in_the_background_and_returns_at_once(tmp_path):
    from shared_context import native_sdk
    from shared_context.native_capabilities import NativeCapabilityProvider
    started, releases = [], []

    async def warm(token_provider, entries):
        started.append((token_provider, entries))
        release = asyncio.Event()
        releases.append(release)
        await release.wait()

    provider = NativeCapabilityProvider(accepts=lambda grant: False, bind=None, name="toolbox", warm=warm)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "unused", api_url="https://api.offline.invalid",
        model_url=ENDPOINT + "/openai/v1", token_provider=agent_token,
        capability_providers=(native_sdk.approved_skills_provider(), provider))

    async def run():
        assert executor.prewarm(()) == 0 and not executor._prewarming
        assert executor.prewarm((ORG,)) == 1
        await asyncio.sleep(0)
        assert started == [(agent_token, (ORG,))] and len(executor._prewarming) == 1
        releases[0].set()
        await asyncio.sleep(0.01)
        assert not executor._prewarming
        executor.prewarm((ORG,))
        await asyncio.sleep(0)
        running = next(iter(executor._prewarming))
        # VM shutdown stops a listing still running.
        await executor.aclose()
        await asyncio.sleep(0)
        return running.cancelled(), executor._prewarming
    assert asyncio.run(run()) == (True, set())


def test_the_registered_toolbox_provider_is_the_one_that_warms():
    from shared_context.native_sdk import native_capability_providers
    providers = native_capability_providers("https://api.offline.invalid", agent_token)
    assert [provider.name for provider in providers if provider.warm is not None] == ["toolbox"]


def store_keys(harness):
    async def keys():
        async with harness.runtime._store(TURNS_STORE) as store:
            page = await store.list_keys(limit=100)
            return sorted(key.key for key in page.keys)
    return harness.call(keys)


def test_a_hosted_prewarm_starts_listing_and_takes_no_task_record_or_queue(harness):
    warmed = []
    harness.runtime.executor.prewarm = lambda toolboxes: warmed.append(toolboxes) or len(toolboxes)
    before = store_keys(harness)
    command = NativePrewarm(toolboxes=(ORG, NativePrewarmToolbox(toolbox="fa-team-team-one", version="2")))
    response = harness.invoke(command)
    assert response.status_code == 200, response.text
    view = NativePrewarmView.model_validate_json(response.content)
    assert (view.agent_id, view.hosted_session_id, view.toolboxes) == (IDENTITY.agent_id, IDENTITY.session_id, 2)
    assert warmed == [command.toolboxes]
    assert store_keys(harness) == before and harness.sdk.calls == []


def test_a_prewarm_while_the_agents_turn_runs_answers_at_once_and_the_turn_is_untouched(harness):
    warmed = []
    harness.runtime.executor.prewarm = lambda toolboxes: warmed.append(toolboxes) or len(toolboxes)
    harness.sdk.block = True
    assert harness.invoke(submit()).status_code == 202
    harness.wait("rid-1", "started")
    response = harness.invoke(NativePrewarm(toolboxes=(ORG,)))
    assert response.status_code == 200 and warmed == [(ORG,)]
    assert not harness.sdk.controls["rid-1"].stop_requested() and len(harness.sdk.calls) == 1
    harness.sdk.block = False
    assert harness.wait("rid-1", "completed").status == "completed"


def test_a_replaced_session_is_not_warmed(harness):
    warmed = []
    harness.runtime.executor.prewarm = lambda toolboxes: warmed.append(toolboxes) or len(toolboxes)
    harness.runtime.replaced = lambda current: True
    response = harness.invoke(NativePrewarm(toolboxes=(ORG,)))
    assert response.status_code == 409 and response.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert warmed == []


def test_without_an_executor_prewarm_the_session_still_answers_and_lists_nothing(harness):
    response = harness.invoke(NativePrewarm(toolboxes=(ORG,)))
    assert response.status_code == 200 and response.json()["toolboxes"] == 0


def test_a_prewarm_names_each_toolbox_version_once():
    assert NativePrewarm().toolboxes == ()
    with pytest.raises(ValueError):
        NativePrewarm(toolboxes=(ORG, ORG))
    with pytest.raises(ValueError):
        NativePrewarmToolbox(toolbox="../x")
