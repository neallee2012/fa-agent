"""ADR-0011 §8: the memory backend contract. Every ``backend`` test runs once per long-term memory backend.

``foundry`` is today's Memory Store over HTTP doubles; ``cosmos`` (phase 2) is the FA API memory relay double plus
the embedding and extraction model doubles (``test_cosmos_private_memory.CosmosRelay``). A backend double answers by
operation (``search``/``update``/``create``) and reports what it was sent in backend-neutral tuples; after each test
it checks that every request it saw had its backend's URL, token and body shape (Foundry: the exact official body).
Where the backends differ by design (Cosmos reads the profile before it extracts, and skips a write it cannot
embed), the expected value is named per backend. Request shapes and chaining that only Foundry has stay in test_private_memory.py,
test_native_private_memory.py and test_native_memory_lineage.py. Named after the ADR's
``memory_backend_contract.py`` with the ``test_`` prefix so default collection and the CI shards pick it up.

Offline: official local store, named SDK and memory-service doubles; no network, model or token service.
"""

import asyncio
import json
import logging
import socket
import time
from types import SimpleNamespace
from typing import NamedTuple

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context import behavior_private_memory as memory
from shared_context import native_memory_candidate as candidates
from shared_context import native_private_memory as native_memory
from shared_context import native_sdk
from shared_context import native_team_memory as team
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import TeamMemoryCapture
from shared_context.native_checkpoint import open_durable_store
from shared_context.native_private_memory import NativePrivateMemory, claim_key
from shared_context.native_turns import EFFECTS_STORE, NativeTurnRuntime
from test_cosmos_private_memory import API as COSMOS_API, CosmosRelay
from test_native_private_memory import sdk
from test_native_team_memory import (
    OTHER, QUOTE, SCOPE, TEXT, create_effects_store, fresh_turn_refs, memory_receipt, run_turn)
from test_native_turn_wire import agent_receive, receipt_for, submit
from test_native_turns import Harness, IDENTITY, local_env

ENDPOINT = "https://fa-acct.services.ai.azure.com/api/projects/fa-project"
STORE = "fa-memory-store"
AGENT = "OFFLINE-agent"
TOKEN = "OFFLINE-memory-token"
PRIVATE = memory.scope_for(AGENT)
OCT_1 = 1790812800  # 2026-10-01T00:00:00Z
OCT_2 = OCT_1 + 86400


async def token():
    return TOKEN


class Search(NamedTuple):
    scope: str
    text: str | None  # None is the static read
    max_memories: int


class Update(NamedTuple):
    scope: str
    text: str
    reply: str | None


class Create(NamedTuple):
    scope: str
    content: str
    kind: str


def found(items, status=200):
    """A search answer; ADR-0011 §1: the Cosmos API route returns the Memory Store's ``memories[].memory_item``."""
    return httpx.Response(status, json={"search_id": "s1", "memories": [{"memory_item": item} for item in items]})


class Foundry:
    """Today's Memory Store. ``answer(operation, parsed)`` returns a response, None for the default, or raises."""

    name = "foundry"
    env = {memory.STORE_ENV: STORE, memory.ENDPOINT_ENV: ENDPOINT, memory.AGENT_ENV: AGENT}
    accepted = {"update": "202", "create": "200"}
    unsent = "unsent_or_unknown"
    _PATHS = {":search_memories": "search", ":update_memories": "update", "/items": "create"}

    def __init__(self):
        self.requests = []

    def memory(self, answer=None, *, token_provider=token):
        def handle(request):
            self.requests.append(request)
            operation = self.operation(request)
            response = None if answer is None else answer(operation, self.parse(operation, request))
            return response if response is not None else self.ok(operation) if operation != "search" else found([])
        return memory.PrivateMemory.from_environment(token_provider, environ=self.env,
                                                     transport=httpx.MockTransport(handle))

    @staticmethod
    def ok(operation, identifier=None):
        if operation == "update":
            return httpx.Response(202, json={"update_id": identifier or "u1", "status": "queued"})
        return httpx.Response(200, json={"memory_id": identifier or "m1"})

    def operation(self, request):
        return next((name for suffix, name in self._PATHS.items() if request.url.path.endswith(suffix)), "other")

    @staticmethod
    def parse(operation, request):
        body = json.loads(request.content)
        if operation == "search":
            items = body.get("items")
            return Search(body["scope"], None if items is None else items[0]["content"][0]["text"],
                          body["options"]["max_memories"])
        if operation == "update":
            items = body["items"]
            return Update(body["scope"], items[0]["content"][0]["text"], items[1]["content"] if len(items) > 1 else None)
        if operation == "create":
            return Create(body["scope"], body["content"], body["kind"])
        return None

    @staticmethod
    def wire(parsed):
        def user(text):
            # Literal official shapes, not the production helpers, so a changed role or part type fails here.
            return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}

        """The exact official body for ``parsed`` (unchained; chaining is Foundry-only and tested there)."""
        if isinstance(parsed, Search):
            body = {"scope": parsed.scope, "options": {"max_memories": parsed.max_memories}}
            return body if parsed.text is None else {**body, "items": [user(parsed.text)]}
        if isinstance(parsed, Update):
            return {"scope": parsed.scope, "update_delay": 0, "items": [user(parsed.text)] + (
                [{"type": "message", "role": "assistant", "content": parsed.reply}] if parsed.reply is not None else [])}
        return parsed._asdict()

    def check(self):
        paths = {name: suffix for suffix, name in self._PATHS.items()}
        for request in self.requests:
            operation = self.operation(request)
            assert operation != "other" and request.method == "POST"
            assert str(request.url) == f"{ENDPOINT}/memory_stores/{STORE}{paths[operation]}?api-version=v1"
            assert request.headers["authorization"] == "Bearer " + TOKEN
            assert request.headers["foundry-features"] == "MemoryStores=V1Preview"
            assert json.loads(request.content) == self.wire(self.parse(operation, request))

    def ops(self):
        return [self.operation(request) for request in self.requests]

    def _parsed(self, name):
        return [self.parse(name, request) for request in self.requests if self.operation(request) == name]

    def searches(self):
        return self._parsed("search")

    def updates(self):
        return self._parsed("update")

    def creates(self):
        return self._parsed("create")


class Cosmos:
    """ADR-0011 ``private`` mode: the FA API relay, the embedding and the extraction model (which echoes the human
    words as one profile item). ``answer`` gets the relay operations only; ``ops`` lists only them."""

    name = "cosmos"
    env = {memory.BACKEND_ENV: "cosmos", memory.EXTRACTION_MODEL_ENV: "gpt-4.1-mini", memory.ENDPOINT_ENV: ENDPOINT,
           memory.AGENT_ENV: AGENT}
    accepted = {"update": "201", "create": "201"}
    # Without a token the first step (an embedding) cannot run, so nothing is extracted or written.
    unsent = "skipped"

    def __init__(self):
        self.relay = CosmosRelay()
        self.requests = self.relay.requests

    def memory(self, answer=None, *, token_provider=token):
        if answer is not None:
            self.relay.answer = lambda operation, _body: answer(operation, self._parse(len(self.requests) - 1))
        return memory.PrivateMemory.from_environment(token_provider, environ=self.env, transport=self.relay.transport(),
                                                     api=(COSMOS_API, token_provider))

    @staticmethod
    def ok(operation, identifier=None):
        note = operation == "create"
        return httpx.Response(200, json={"update_id": None if note else identifier or "fau1-" + "0" * 40, "items": [
            {"slot": "note" if note else "p0", "id": identifier or "fam1-" + "0" * 40, "status": 201}]})

    def _parse(self, index):
        request = self.requests[index]
        operation, body = self.relay.operation(request), json.loads(request.content)
        if operation == "search":
            text = None if body["mode"] == "static" else self.relay.texts[body["vector"][0]]
            return Search(body["scope"], text, body["top"])
        if operation == "update":
            # The words this write was extracted from: the extraction request just before it.
            model = next(json.loads(earlier.content) for earlier in reversed(self.requests[:index])
                         if self.relay.operation(earlier) == "model")
            turn = json.loads(model["input"][0]["content"][0]["text"].split("\n", 1)[1])
            return Update(body["scope"], turn["human"], turn.get("assistant"))
        if operation == "create":
            return Create(body["scope"], body["items"][0]["content"], body["items"][0]["kind"])
        return None

    def check(self):
        for request in self.requests:
            operation, body = self.relay.operation(request), json.loads(request.content)
            assert operation != "other" and request.method == "POST"
            assert request.headers["authorization"] == "Bearer " + TOKEN
            if operation == "embedding":
                assert set(body) == {"model", "input"} and body["model"] == "text-embedding-3-small"
            elif operation == "model":
                assert body["model"] == "gpt-4.1-mini" and body["store"] is False and body["temperature"] == 0
            elif operation == "search":
                assert set(body) == {"scope", "mode", "top"} | ({"vector"} if body["mode"] == "contextual" else set())
                assert body["mode"] == "static" or len(body["vector"]) == 1536
            else:
                assert set(body) == {"scope", "source_request_id", "items"} and 1 <= len(body["items"]) <= 4
                assert all({"slot", "kind", "content", "embedding", "updated_at"} <= set(item)
                           <= {"slot", "kind", "content", "embedding", "updated_at", "supersedes"}
                           for item in body["items"])

    def ops(self):
        return [operation for operation in self.relay.ops() if operation in {"search", "update", "create"}]

    def _parsed(self, name):
        return [self._parse(index) for index, request in enumerate(self.requests)
                if self.relay.operation(request) == name]

    def searches(self):
        return self._parsed("search")

    def updates(self):
        return self._parsed("update")

    def creates(self):
        return self._parsed("create")


@pytest.fixture(params=["foundry", "cosmos"])
def backend(request):
    current = {"foundry": Foundry, "cosmos": Cosmos}[request.param]()
    yield current
    current.check()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import _socket

    def forbidden(sock, address):
        # asyncio's own event-loop self-pipe on Windows uses loopback; any other connection fails.
        if address[0] not in {"127.0.0.1", "::1"}:
            pytest.fail("memory contract tests must not reach the network")
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def answering(search=None, *, create=None):
    """Searches answered with ``search`` (items, or a callable of the parsed search); writes accepted or ``create``."""
    def answer(operation, parsed):
        if operation == "search":
            return found(search(parsed) if callable(search) else search or [])
        return create if operation == "create" else None
    return answer


# Private memory (from test_private_memory.py).


def test_recall_sends_the_user_text_and_bounds_what_it_returns(backend, caplog):
    with caplog.at_level(logging.WARNING):
        recalled = asyncio.run(backend.memory(answering([
            {"memory_id": "m1", "content": "Prefers  short\nanswers", "kind": "user_profile"},
            {"memory_id": "m2", "content": "Works on FA", "kind": "user_profile"}])).recall("What do I prefer?"))
    assert recalled == ("Prefers short answers", "Works on FA")
    assert backend.searches() == [Search(PRIVATE, "What do I prefer?", memory.MAX_MEMORIES)]
    assert TOKEN not in caplog.text
    # Hosted keeps only WARNING and above: the success line must survive that filter.
    assert "FA_PRIVATE_MEMORY_SEARCH status=200 memories=2" in caplog.text
    bounded = asyncio.run(backend.memory(answering([{"content": "x" * 900}] * 9)).recall("q"))
    assert len(bounded) == 3 and all(len(item) == memory.MAX_MEMORY_CHARS for item in bounded)


def failing(status=None, search=None, error=None):
    def answer(operation, _parsed):
        if error is not None:
            raise error
        if status is not None:
            return httpx.Response(status, json={"error": {"code": "denied", "message": TOKEN}})
        return httpx.Response(200, json=search)
    return answer


@pytest.mark.parametrize("kwargs, logged", [
    ({"status": 401}, "status=401"), ({"status": 403}, "status=403"), ({"status": 500}, "status=500"),
    ({"search": {"memories": [{"memory_item": {"content": 3}}]}}, "status=response_invalid"),
    ({"search": {"unexpected": True}}, "status=response_invalid"),
    ({"error": httpx.ConnectError("offline")}, "status=transport_error"),
])
def test_a_failed_recall_continues_without_memory(backend, kwargs, logged, caplog):
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(backend.memory(failing(**kwargs)).recall("q")) == ()
    assert backend.ops() == ["search"] and logged in caplog.text and TOKEN not in caplog.text


def test_without_a_token_nothing_is_sent(backend, caplog):
    def no_token():
        raise RuntimeError("no identity")
    configured = backend.memory(token_provider=no_token)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(configured.recall("q")) == ()
        assert asyncio.run(configured.update_chained("q", None, request_id="rid-1")) == (backend.unsent, None)
        assert asyncio.run(configured.create(SCOPE, "q", "chat_summary", event="FA_TEAM_MEMORY_CREATE",
                                             request_id="rid-1")) == (backend.unsent, None)
    assert backend.ops() == [] and caplog.text.count("status=token_unavailable") == 3


@pytest.mark.parametrize("outcome", ["accepted", "refused", "lost"])
def test_a_write_is_sent_once_and_never_retried(backend, outcome, caplog):
    def answer(operation, _parsed):
        if operation == "search":
            return None  # Cosmos reads the profile before it extracts; only the write fails here
        if outcome == "lost":
            raise httpx.ReadTimeout("lost")
        return None if outcome == "accepted" else httpx.Response(401, json={})
    configured = backend.memory(answer)
    with caplog.at_level(logging.WARNING):
        updated, update_id = asyncio.run(configured.update_chained("I am vegetarian", None, request_id="rid-1"))
        created, memory_id = asyncio.run(configured.create(SCOPE, "note", "chat_summary",
                                                           event="FA_TEAM_MEMORY_CREATE", request_id="rid-2"))
    assert (updated, created) == {"accepted": (backend.accepted["update"], backend.accepted["create"]),
                                  "refused": ("401", "401"), "lost": ("unsent_or_unknown",) * 2}[outcome]
    assert (update_id is not None, memory_id is not None) == (outcome == "accepted",) * 2
    assert backend.updates() == [Update(PRIVATE, "I am vegetarian", None)]
    assert backend.creates() == [Create(SCOPE, "note", "chat_summary")]
    if outcome != "lost":
        assert f"FA_PRIVATE_MEMORY_UPDATE status={updated}" in caplog.text
        assert f"FA_TEAM_MEMORY_CREATE status={created}" in caplog.text


def test_recalled_memories_are_labeled_data_before_the_unchanged_prompt():
    assert memory.with_recalled("hello", ()) == "hello"
    assert memory.with_recalled("hello", ("a", "b")) == memory.PREFIX + "\n- a\n- b\n\nhello"


def test_memories_that_would_push_literal_files_past_their_bound_are_left_out(caplog):
    from shared_context.behavior_text_files import UserTextFile, validate_user_files
    # A Chinese note near the original bound still fits alone, as the API already checked.
    files = (UserTextFile(name="note.txt", text="記" * 2400),)
    validate_user_files("hello", files)
    memories = ("偏好" * 290,) * 3
    with caplog.at_level(logging.WARNING):
        assert memory.with_recalled("hello", memories, files) == "hello"
    assert "status=skipped_size" in caplog.text
    small = (UserTextFile(name="note.txt", text="short"),)
    assert memory.with_recalled("hello", memories, small).endswith("\n\nhello")


# Native private memory (from test_native_private_memory.py).


def test_a_native_human_turn_recalls_first_and_updates_once_after_completion(backend, local_env, monkeypatch,
                                                                             tmp_path):
    prompts = []
    memories = NativePrivateMemory(backend.memory(answering([
        {"memory_id": "m1", "content": "The cat is Migao, 3 years old"}])))

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
        # Cosmos also reads the profile (static and by this message) before it extracts.
        ops = {"foundry": ["search", "search", "update"], "cosmos": ["search"] * 4 + ["update"]}[backend.name]
        deadline = time.monotonic() + 8
        while len(backend.ops()) < len(ops) and time.monotonic() < deadline:
            time.sleep(0.01)
        # First turn: the static profile read plus the message search; one item both return is listed once.
        assert prompts == [native_memory.PREFIX + "\n- The cat is Migao, 3 years old\n\n" + request.command.content]
        assert sorted(backend.ops()) == ops
        reads = (len(ops) - 1) // 2  # each read is one static search and one message search
        assert sorted(search.text is not None for search in backend.searches()) == [False] * reads + [True] * reads
        # S1b: one update with the user's words and the saved final reply.
        assert backend.updates() == [Update(PRIVATE, request.command.content, "OFFLINE answer")]
        # The official terminal-notification task retries all post-turn actions; the update is never resent.
        h.call(memories.completed, receipt, runtime)
        assert len(backend.updates()) == 1


def test_agent_messages_and_unfinished_turns_use_no_private_memory(backend, local_env):
    memories = NativePrivateMemory(backend.memory())
    agent = receipt_for(agent_receive(), "completed")
    unknown = receipt_for(submit("rid-2", text="human"), "unknown")
    assert asyncio.run(memories.recall(agent)) == ()
    asyncio.run(memories.completed(agent))
    asyncio.run(memories.completed(unknown))
    assert backend.ops() == []


def test_an_existing_or_unconfirmed_claim_sends_nothing(backend, local_env, caplog):
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
        asyncio.run(NativePrivateMemory(backend.memory()).completed(receipt))
        asyncio.run(NativePrivateMemory(backend.memory(), LostIdentity).completed(
            receipt_for(submit("rid-2", text="hi"), "completed")))
    assert backend.ops() == []
    assert "status=already_claimed" in caplog.text and "status=claim_unknown" in caplog.text


PROFILE = {"memory_id": "p1", "kind": "user_profile", "content": "Prefers the conclusion first", "updated_at": OCT_1}
EPISODE = {"memory_id": "e1", "kind": "chat_summary", "content": "Lyra moved to Postgres", "updated_at": OCT_2}


def tiered(*, static_status=200, contextual_status=200):
    """Static reads return the user profile; message searches return the profile and an episode."""
    def answer(operation, parsed):
        if operation != "search":
            return None
        static = parsed.text is None
        return found([PROFILE] if static else [PROFILE, EPISODE], static_status if static else contextual_status)
    return answer


def test_the_first_turn_reads_the_static_profile_once_and_a_follow_up_only_searches_its_message(
        backend, local_env, monkeypatch, tmp_path, caplog):
    prompts = []
    memories = NativePrivateMemory(backend.memory(tiered()))

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
    searches = backend.searches()
    assert [search.text is None for search in searches].count(True) == 1 and len(searches) == 3
    assert all(search.max_memories == memory.MAX_MEMORIES for search in searches)
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
def test_a_failed_search_is_named_unavailable_and_never_taken_for_no_memory(backend, static_status,
                                                                           contextual_status, listed):
    configured = backend.memory(tiered(static_status=static_status, contextual_status=contextual_status))
    recalled = asyncio.run(configured.recall_tiers("Q", first_turn=True))
    assert len(recalled) == listed and recalled.unavailable
    prompt = memory.with_recalled("Q", recalled, prefix=native_memory.PREFIX,
                                  unavailable_note=native_memory.UNAVAILABLE)
    assert prompt.endswith(f"({native_memory.UNAVAILABLE})\n\nQ")
    assert prompt.startswith(native_memory.PREFIX) == bool(listed)
    # Callers that pass no note (the old lane, Team notes) keep their unchanged output.
    assert memory.with_recalled("Q", recalled) == ("Q" if not listed else memory.with_recalled("Q", tuple(recalled)))
    found_now = asyncio.run(backend.memory(tiered()).recall_tiers("Q", first_turn=False))
    assert not found_now.unavailable and memory.with_recalled(
        "Q", found_now, unavailable_note="unused").count("(unused)") == 0


# Team memory: scope, pin, reference checks, _shown and no resend (from test_native_team_memory.py).

PEER_NOTE = {"memory_id": "t1", "content": "[OFFLINE-peer, 2026-10-01] Use repo X"}


def team_memory(backend, answer=None):
    return team.NativeTeamMemory(backend.memory(answering([PEER_NOTE]) if answer is None else answer), AGENT)


def shared_turn(backend, monkeypatch, tmp_path, *, answer=None, **kwargs):
    return run_turn(monkeypatch, tmp_path, backend.requests, memories=team_memory(backend, answer),
                    created=backend.creates, **kwargs)


def test_a_human_suggestion_is_shared_once_verbatim_and_recalled_as_labeled_data(backend, local_env, monkeypatch,
                                                                                 tmp_path):
    turn = shared_turn(backend, monkeypatch, tmp_path)
    searches, creates = backend.searches(), backend.creates()
    # First turn: the static read plus the message search, both in the captured Team scope.
    assert [search.scope for search in searches] == [SCOPE, SCOPE]
    assert sorted(search.text is not None for search in searches) == [False, True]
    assert next(search for search in searches if search.text is not None).text == TEXT
    # One item both searches return is listed once, with its short reference.
    assert turn.prompts == [team.PREFIX + "\n- note:t1 [OFFLINE-peer, 2026-10-01] Use repo X\n\n" + TEXT]
    # One verbatim item in the captured Team scope, even though the completion hook ran twice.
    assert len(creates) == 1 and creates[0].scope == SCOPE and creates[0].kind == "chat_summary"
    assert creates[0].content.startswith("[OFFLINE-agent, ") and creates[0].content.endswith(", src rid-1] " + QUOTE)
    assert not backend.updates()


@pytest.mark.parametrize("reason, kind", [("stable_preference", "user_profile"), ("durable_fact", "chat_summary")])
def test_a_preference_note_is_a_user_profile_item_and_a_fact_is_a_chat_summary(backend, local_env, monkeypatch,
                                                                                 tmp_path, reason, kind):
    shared_turn(backend, monkeypatch, tmp_path, reason=reason)
    assert [create.kind for create in backend.creates()] == [kind]
    description = candidates.bind(SimpleNamespace(receipt=SimpleNamespace(source=None)), ())
    description = asyncio.run(description).tools[0].description
    assert "stable_preference" in description and "durable_fact" in description
    assert "must share, review" not in description


def test_a_skip_shares_nothing(backend, local_env, monkeypatch, tmp_path):
    shared_turn(backend, monkeypatch, tmp_path, decision="no_candidate")
    assert backend.creates() == [] and len(backend.searches()) == 2


def test_a_failed_team_search_is_named_unavailable_and_the_turn_continues(backend, local_env, monkeypatch, tmp_path):
    def answer(operation, _parsed):
        return found([PEER_NOTE], 503) if operation == "search" else None
    turn = shared_turn(backend, monkeypatch, tmp_path, answer=answer)
    assert turn.prompts == [f"({team.UNAVAILABLE})\n\n" + TEXT]
    assert turn.receipt.status == "completed" and len(backend.creates()) == 1


PREFERENCE = {"memory_id": "n1", "kind": "user_profile", "content": "[agent-a, 2026-10-01 00:00 UTC] conclusion first",
              "updated_at": OCT_1}
FACT = {"memory_id": "n2", "kind": "chat_summary", "content": "[agent-b, 2026-10-02 00:00 UTC] Lyra uses Postgres",
        "updated_at": OCT_2}


def test_the_first_turn_reads_the_teams_preferences_and_later_turns_only_relevant_notes(backend, local_env):
    create_effects_store()
    memories = team_memory(backend, answering(lambda search: [PREFERENCE] if search.text is None else [PREFERENCE, FACT]))
    receipt = memory_receipt("what database")
    preference = "note:n1 [preference] " + PREFERENCE["content"]
    fact = "note:n2 [team note] " + FACT["content"]
    assert list(asyncio.run(memories.recall(receipt, first_turn=True))) == [preference, fact]
    assert list(asyncio.run(memories.recall(receipt))) == [fact, preference]
    searches = backend.searches()
    assert [search.text is not None for search in searches] in ([False, True, True], [True, False, True])
    # The static read asks for more preference notes than it shows; message searches ask for the shown bound.
    assert sorted(search.max_memories for search in searches) == [
        team.MAX_TEAM_MEMORIES, team.MAX_TEAM_MEMORIES, team.MAX_TEAM_STATIC_FETCH]
    assert next(search for search in searches if search.text is None).max_memories == team.MAX_TEAM_STATIC_FETCH


def test_the_static_read_keeps_the_newest_preferences_so_a_newer_correction_is_not_left_out(backend, local_env):
    """S3 (2026-10-03): with more preference notes than shown, the newest three by updated_at are kept."""
    create_effects_store()
    notes = [{"memory_id": f"p{index}", "kind": "user_profile", "updated_at": OCT_1 + index * 60,
              "content": f"[agent-a, 2026-10-01 00:{index:02d} UTC] preference {index}"} for index in range(6)]
    # Which items the service returns past the limit is unspecified: this double honors the limit, oldest first.
    memories = team_memory(backend, answering(lambda search: notes[:search.max_memories] if search.text is None else []))
    recalled = asyncio.run(memories.recall(memory_receipt("hello"), first_turn=True))
    assert list(recalled) == [f"note:p{index} [preference] " + notes[index]["content"] for index in (5, 4, 3)]
    assert recalled.omitted == 3


def test_a_correction_names_its_source_and_a_note_this_turn_showed_and_never_updates_or_deletes(
        backend, local_env, monkeypatch, tmp_path):
    """MEM-02: the new note names its source RID and the exact earlier note it corrects; the old note is untouched."""
    turn = shared_turn(backend, monkeypatch, tmp_path, reason="stable_preference", supersedes="note:t1")
    creates = backend.creates()
    assert turn.failures == [] and turn.receipt.status == "completed"
    assert len(creates) == 1 and creates[0].kind == "user_profile"
    assert creates[0].content.endswith(", src rid-1, corrects t1] " + QUOTE)
    assert set(backend.ops()) == {"create", "search"}
    tool = asyncio.run(candidates.bind(SimpleNamespace(receipt=SimpleNamespace(source=None)), ())).tools[0]
    assert "never invent a reference" in tool.description


def test_an_invented_reference_saves_nothing(backend, local_env, monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        turn = shared_turn(backend, monkeypatch, tmp_path, supersedes="note:zz")
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and turn.receipt.status == "completed"
    assert backend.creates() == []
    assert "FA_TEAM_MEMORY_CREATE status=not_sent reason=supersedes_unknown" in caplog.text


@pytest.mark.parametrize("supersedes", ["t1", "note:", "note:" + "t" * 9, "note:t1]"])
def test_a_malformed_reference_saves_nothing(backend, local_env, monkeypatch, tmp_path, supersedes):
    turn = shared_turn(backend, monkeypatch, tmp_path, supersedes=supersedes)
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and backend.creates() == []


def test_a_reference_shown_only_to_another_request_saves_nothing(backend, local_env, monkeypatch, tmp_path, caplog):
    team._remember_refs("rid-0", memory.Recalled(("note",), ids=("t1",)))
    with caplog.at_level(logging.WARNING):
        turn = shared_turn(backend, monkeypatch, tmp_path, supersedes="note:t1", answer=answering([
            {"memory_id": "t2", "content": "[OFFLINE-peer, 2026-10-01] Use repo Y"}]))
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and backend.creates() == []
    assert "FA_TEAM_MEMORY_CREATE status=not_sent reason=supersedes_unknown" in caplog.text


def test_a_reference_under_another_team_scope_saves_nothing(backend, local_env, monkeypatch, tmp_path, caplog):
    async def bound_elsewhere():
        async with await open_durable_store(EFFECTS_STORE, create=True) as store:
            await store.create_item(team.SCOPE_KEY, {"kind": "fa-team-memory-scope-v1", "scope": OTHER})
    asyncio.run(bound_elsewhere())
    with caplog.at_level(logging.WARNING):
        turn = shared_turn(backend, monkeypatch, tmp_path, supersedes="note:t1")
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and backend.ops() == []
    assert "FA_TEAM_MEMORY_SCOPE status=mismatch" in caplog.text
    assert "FA_TEAM_MEMORY_CREATE status=not_sent reason=supersedes_unknown" in caplog.text


def test_a_skip_cannot_name_a_correction(backend, local_env, monkeypatch, tmp_path):
    turn = shared_turn(backend, monkeypatch, tmp_path, decision="no_candidate", supersedes="note:t1")
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and backend.creates() == []


def test_a_recall_leaves_out_a_note_whose_correction_it_also_holds(backend, local_env, caplog):
    create_effects_store()
    old = {"memory_id": "mem-old-0001", "kind": "user_profile", "updated_at": OCT_1,
           "content": "[agent-a, 2026-10-01 00:00 UTC, src rid-6] conclusion first"}
    new = {"memory_id": "mem-new-0001", "kind": "user_profile", "updated_at": OCT_2,
           "content": "[agent-b, 2026-10-02 00:00 UTC, src rid-7, corrects mem-old-0001] conclusion last"}
    # The user's own quoted words can never name a correction; only the note's header can.
    quoted = {"memory_id": "mem-quoted01", "kind": "chat_summary", "updated_at": OCT_2,
              "content": "[agent-c, 2026-10-02 00:00 UTC, src rid-8] the doc says [x, corrects mem-old-0001]"}
    current = [new, old, quoted]
    memories = team_memory(backend, answering(lambda _search: current))

    def human(request_id):
        return memory_receipt("conclusion", rid=request_id)
    with caplog.at_level(logging.WARNING):
        recalled = asyncio.run(memories.recall(human("rid-9")))
    assert list(recalled) == [
        "note:new-0001 [preference] [agent-b, 2026-10-02 00:00 UTC, src rid-7, corrects note:old-0001] conclusion last",
        "note:quoted01 [team note] " + quoted["content"]]
    assert [source.memory_id for source in recalled.observation.sources] == ["mem-new-0001", "mem-quoted01"]
    assert 'FA_TEAM_MEMORY_SEARCH omitted={"superseded": 1}' in caplog.text
    # Only a note this turn showed can be named; the left-out note cannot.
    assert team.recalled_note("rid-9", "note:new-0001") == "mem-new-0001"
    assert team.recalled_note("rid-9", "note:old-0001") is None and team.recalled_note("rid-1", "note:new-0001") is None
    # Without its correction in the same recall the earlier note is shown as before (newer time still wins).
    current[:] = [old]
    assert list(asyncio.run(memories.recall(human("rid-10")))) == ["note:old-0001 [preference] " + old["content"]]
    current[:] = [quoted, old]
    assert len(asyncio.run(memories.recall(human("rid-11")))) == 2


def test_agent_messages_and_unfinished_turns_neither_read_nor_write(backend, local_env):
    create_effects_store()  # a usable Team binding, so only the human-turn rule keeps an Agent message out
    memories = team_memory(backend)
    receipt = memory_receipt("human", status="completed")
    agent = memory_receipt("human", status="completed", agent=True)
    unknown = memory_receipt("human", status="unknown")
    assert asyncio.run(memories.recall(agent)) == ()
    asyncio.run(memories.completed(agent))
    asyncio.run(memories.completed(unknown))
    assert backend.ops() == []
    assert asyncio.run(memories.recall(receipt)) != ()  # the same human receipt does search


def test_a_different_later_team_scope_is_never_searched_or_written(backend, local_env, caplog):
    create_effects_store()
    memories = team_memory(backend)
    assert asyncio.run(memories.recall(memory_receipt())) != ()
    # A new process re-reads the create-only binding; configuration drift cannot reach another Team.
    later = team_memory(backend)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(later.recall(memory_receipt(scope=OTHER))) == ()
        asyncio.run(later.completed(memory_receipt(scope=OTHER, rid="rid-9", cid="cid-9", status="completed")))
    assert [search.scope for search in backend.searches()] == [SCOPE]
    assert backend.creates() == []
    assert "FA_TEAM_MEMORY_SCOPE status=mismatch" in caplog.text


def test_an_unknown_create_is_not_resent(backend, local_env, monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        shared_turn(backend, monkeypatch, tmp_path, answer=answering(
            [PEER_NOTE], create=httpx.Response(500, json={"memory_id": "m1"})))
    assert len(backend.creates()) == 1
    assert "FA_TEAM_MEMORY_CREATE status=500" in caplog.text and "status=already_claimed" in caplog.text


# Lineage: newest first, dated, the bound named, and the Team note's memory_id (from test_native_memory_lineage.py).


def effects_item(key):
    async def read():
        async with await open_durable_store(EFFECTS_STORE, create=False) as store:
            return await store.get_item(key)
    return asyncio.run(read())


def test_recall_is_newest_first_dated_and_names_what_the_bound_left_out(backend, caplog):
    items = [
        {"memory_id": "a", "content": "The release window is Thursday", "updated_at": OCT_1},
        {"memory_id": "b", "content": "undated item keeps its search order", "updated_at": "not-unix"},
        {"memory_id": "c", "content": "The release window is Friday", "updated_at": OCT_2},
        {"memory_id": "d", "content": "x" * memory.MAX_MEMORY_CHARS, "updated_at": OCT_1 - 86400},
    ]
    with caplog.at_level(logging.WARNING):
        recalled = asyncio.run(backend.memory(answering(items)).search(
            PRIVATE, "when", event="FA_PRIVATE_MEMORY_SEARCH", max_memories=5, max_recall_chars=160))
    assert list(recalled) == ["[2026-10-02 00:00 UTC] The release window is Friday",
                              "[2026-10-01 00:00 UTC] The release window is Thursday",
                              "undated item keeps its search order"]
    assert recalled.omitted == 1
    assert 'omitted={"bound_chars": 1}' in caplog.text
    prompt = memory.with_recalled("Q", recalled)
    assert prompt.splitlines()[0] == memory.PREFIX
    assert "(1 more found but omitted to stay within the size limit.)" in prompt and prompt.endswith("\n\nQ")
    undated = asyncio.run(backend.memory(answering(items[:1])).search(PRIVATE, "when", event="E", dated=False))
    assert list(undated) == ["The release window is Thursday"] and undated.omitted == 0


def test_team_notes_are_labeled_historical_and_newest_first(backend, local_env):
    create_effects_store()
    memories = team_memory(backend, answering([
        {"memory_id": "old", "content": "[agent-a, 2026-10-01] deploy on Friday", "updated_at": OCT_1},
        {"memory_id": "new", "content": "[agent-b, 2026-10-02] deploy on Monday instead", "updated_at": OCT_2},
    ]))
    receipt = SimpleNamespace(source=None, human_origin=True, command=SimpleNamespace(content="when do we deploy"),
                              capture=SimpleNamespace(team_memory=TeamMemoryCapture(scope=SCOPE)))
    recalled = asyncio.run(memories.recall(receipt))
    assert list(recalled) == ["note:new [agent-b, 2026-10-02] deploy on Monday instead",
                              "note:old [agent-a, 2026-10-01] deploy on Friday"]
    assert "may be outdated" in team.PREFIX and "newer time wins" in team.PREFIX
    assert backend.searches()[0].max_memories == team.MAX_TEAM_MEMORIES


def test_a_shared_team_note_keeps_its_official_memory_id(backend, local_env, monkeypatch, tmp_path):
    shared_turn(backend, monkeypatch, tmp_path, answer=answering([PEER_NOTE], create=backend.ok("create", "m1")))
    assert len(backend.creates()) == 1
    item = effects_item(team.claim_key("rid-1"))
    assert item.value["memory_id"] == "m1" and item.value["kind"] == "fa-team-note-claim-v1"
