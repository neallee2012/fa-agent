"""ADR-0011 phase 2: Cosmos-only Hosted memory behavior (extraction checks, the API relay wire, lock, usage).

The behavior both backends share runs in test_memory_backend_contract.py.

Offline: official local store and HTTP doubles for the FA API relay, the embedding and the extraction model; no
network, model or token service.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import time
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_private_memory as memory
from shared_context import memory_extraction_prompt as prompt
from shared_context import native_sdk
from shared_context.cosmos_private_memory import CosmosPrivateMemory
from shared_context.errors import Problem
from shared_context.native_checkpoint import conversation_store_name, open_durable_store
from shared_context.native_private_memory import NativePrivateMemory, claim_key
from shared_context.native_turns import EFFECTS_STORE
from test_native_turn_wire import receipt_for, submit
from test_native_turns import IDENTITY, local_env  # noqa: F401

HUMAN = "我住在高雄，以後回答都先給結論。"
ENDPOINT = "https://fa-acct.services.ai.azure.com/api/projects/fa-project"
API = "https://api.offline.invalid"
AGENT = "OFFLINE-agent"
MODEL = "gpt-4.1-mini"
PRIVATE = memory.scope_for(AGENT)
MODEL_TOKEN, API_TOKEN = "OFFLINE-model-token", "OFFLINE-api-token"
ENV = {memory.BACKEND_ENV: "cosmos", memory.EXTRACTION_MODEL_ENV: MODEL, memory.ENDPOINT_ENV: ENDPOINT,
       memory.AGENT_ENV: AGENT}
# The project path answers 404 for embeddings; the account root serves them (2026-10-24 private e2e).
EMBEDDINGS = "https://fa-acct.services.ai.azure.com/openai/v1/embeddings"
RESPONSES = ENDPOINT + "/openai/v1/responses"
# The phase 3 relay (memory_relay.PREFIX, PR #966).
SEARCH = API + "/team-data/v1/memory/search"
ITEMS = API + "/team-data/v1/memory/items"
UPDATE_ID = "fau1-" + "a" * 40
OCT_1 = 1790812800  # 2026-10-01T00:00:00Z
USAGE = {"schema_version": "fa-turn-model-usage-v1", "usage": [{
    "model": MODEL, "wire_api": "responses", "requests": 1, "measured_requests": 1, "input_tokens": 300,
    "output_tokens": 20, "reasoning_tokens": 0}]}
UNKNOWN_USAGE = {"schema_version": "fa-turn-model-usage-v1", "usage": [{
    "model": MODEL, "wire_api": "responses", "requests": 1, "measured_requests": 0, "input_tokens": None,
    "output_tokens": None, "reasoning_tokens": None}]}
FORMAT = {"type": "json_schema", "name": "fa_memory_extraction", "strict": True, "schema": prompt.SCHEMA}


def vector(number):
    """The double's embedding: the call's sequence number first, so a request names the text it embedded."""
    return [float(number)] + [0.0] * 1535


def echo(turn):
    return {"profile": [{"content": turn["human"], "evidence": turn["human"], "supersedes": None}], "episode": None}


def item_id(request_id, slot):
    """A ``fam1-`` ID as the API derives one; this double only needs it fixed per RID and slot."""
    return "fam1-" + hashlib.sha256(f"{request_id}:{slot}".encode()).hexdigest()[:40]


def found(items, status=200):
    return httpx.Response(status, json={"memories": [{"memory_item": item} for item in items]})


def written(body, status=201):
    note = [item["slot"] for item in body["items"]] == ["note"]
    return httpx.Response(200, json={
        "update_id": None if note else UPDATE_ID,
        "items": [{"slot": item["slot"], "id": item_id(body["source_request_id"], item["slot"]), "status": status}
                  for item in body["items"]],
        "corrections": [{"slot": item["slot"], "supersedes": item["supersedes"],
                         "result": "superseded" if status == 201 else "skipped"}
                        for item in body["items"] if "supersedes" in item]})


class CosmosRelay:
    """The FA API memory relay, the embedding and the extraction model, recording every request.

    ``answer(operation, body)`` may replace a relay answer (operation ``search``, ``update`` or ``create``); a written
    batch joins ``profiles``, as Cosmos would return it to the next read.
    """

    def __init__(self, profiles=(), nearest=None, extraction=echo, answer=None, embedding=None):
        self.requests, self.texts = [], {}
        self.profiles, self.nearest = list(profiles), nearest
        self.extraction, self.answer, self.embedding = extraction, answer, embedding

    def transport(self):
        return httpx.MockTransport(self.handle)

    @staticmethod
    def operation(request):
        if str(request.url) == SEARCH:
            return "search"
        if str(request.url) == ITEMS:
            return "create" if [item["slot"] for item in json.loads(request.content)["items"]] == ["note"] else "update"
        return {EMBEDDINGS: "embedding", RESPONSES: "model"}.get(str(request.url), "other")

    def handle(self, request):
        self.requests.append(request)
        body, operation = json.loads(request.content), self.operation(request)
        if operation == "embedding":
            if self.embedding is not None and (refused := self.embedding(body)) is not None:
                return refused
            data = []
            for index, text in enumerate(body["input"]):
                self.texts[float(len(self.texts) + 1)] = text
                data.append({"object": "embedding", "index": index, "embedding": vector(len(self.texts))})
            return httpx.Response(200, json={"object": "list", "data": data, "model": "text-embedding-3-small",
                                             "usage": {"prompt_tokens": 7, "total_tokens": 7}})
        if operation == "model":
            result = self.extraction(json.loads(body["input"][0]["content"][0]["text"].split("\n", 1)[1]))
            if isinstance(result, httpx.Response):
                return result
            return httpx.Response(200, json={
                "id": "resp-offline", "status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": json.dumps(result, ensure_ascii=False)}]}],
                "usage": {"input_tokens": 300, "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 0}}})
        if self.answer is not None and (response := self.answer(operation, body)) is not None:
            return response
        if operation == "search":
            items = self.profiles if body["mode"] == "static" or self.nearest is None else self.nearest
            return found(items[:body["top"]])
        if operation in {"update", "create"}:
            self.profiles = [{"memory_id": item_id(body["source_request_id"], item["slot"]), "kind": item["kind"],
                              "content": item["content"], "updated_at": item["updated_at"]}
                             for item in body["items"]] + self.profiles
            return written(body)
        return httpx.Response(404, json={})

    def sent(self, operation):
        return [json.loads(request.content) for request in self.requests if self.operation(request) == operation]

    def ops(self):
        return [self.operation(request) for request in self.requests]


def provider(value):
    async def token():
        return value
    return token


def cosmos(relay, *, token=MODEL_TOKEN, api_token=API_TOKEN):
    return memory.PrivateMemory.from_environment(provider(token), environ=ENV, transport=relay.transport(),
                                                 api=(API, provider(api_token)))


def output(profile=(), episode=None, **extra):
    return json.dumps({"profile": list(profile), "episode": episode, **extra}, ensure_ascii=False)


def item(content, evidence, supersedes=None, **extra):
    return {"content": content, "evidence": evidence, "supersedes": supersedes, **extra}


def test_the_kind_comes_from_the_field_and_extra_keys_are_ignored():
    kept, dropped = prompt.checked(output(
        [item("使用者住在高雄", "我住在高雄", kind="travel_plan", confidence=0.9)],
        "使用者談到住處與回答格式", kind="preference"), set(), HUMAN)
    assert kept == [("p0", "user_profile", "使用者住在高雄", None), ("e0", "chat_summary", "使用者談到住處與回答格式", None)]
    assert dropped == []


def test_evidence_must_be_the_humans_own_words_and_only_that_item_is_dropped():
    kept, dropped = prompt.checked(output([
        item("使用者偏好 X", "使用者偏好 X"),  # a sentence from the reply, not the human's words
        item("使用者希望先給結論", "以後回答都先給結論"),
        item("使用者是管理員", ""),
    ]), set(), HUMAN)
    assert kept == [("p1", "user_profile", "使用者希望先給結論", None)]
    assert dropped == ["evidence_mismatch", "evidence_mismatch"]
    # Only NFKC and runs of whitespace are normalized on both sides.
    spaced = "I live in  Kaohsiung,\nand ＡＢＣ"
    assert prompt.checked(output([item("lives in Kaohsiung", "Kaohsiung, and ABC")]), set(), spaced)[0] == [
        ("p0", "user_profile", "lives in Kaohsiung", None)]
    assert prompt.checked(output([item("x", "kaohsiung")]), set(), spaced) == ([], ["evidence_mismatch"])


def test_an_item_holding_a_credential_like_value_is_dropped():
    kept, dropped = prompt.checked(output(
        [item("使用者的 api key: sk-OFFLINEabcdefghijklmnop", "我住在高雄"), item("使用者住在高雄", "我住在高雄")],
        "password: hunter22 was mentioned"), set(), HUMAN)
    assert kept == [("p1", "user_profile", "使用者住在高雄", None)] and dropped == ["secret", "secret"]


def test_supersedes_names_only_a_listed_code():
    assert prompt.checked(output([item("使用者住在高雄", "我住在高雄", "p2")]), {"p1", "p2"}, HUMAN)[0] == [
        ("p0", "user_profile", "使用者住在高雄", "p2")]


@pytest.mark.parametrize("raw", [
    "not json", "[]", json.dumps({"profile": []}), json.dumps({"episode": None}),
    output([item("a", "我住在高雄")] * 4),
    output([item("a", "我住在高雄", "p9")]),
    output([item("a" * 601, "我住在高雄")]),
    output([item("a", "我" * 201)]),
    output(episode="e" * 601),
    output([{"content": "a", "evidence": "我住在高雄"}]),
    output([{"content": 3, "evidence": "我住在高雄", "supersedes": None}]),
    output(episode=3),
])
def test_a_wrong_shape_an_unknown_code_or_an_over_long_value_writes_nothing(raw):
    with pytest.raises(prompt.ExtractionInvalid):
        prompt.checked(raw, {"p1"}, HUMAN)


def test_the_turn_is_sent_as_json_data_with_codes_not_memory_ids():
    assert prompt.turn_data([("p1", "使用者住在台北")], HUMAN, "好的。", "2026-10-01 00:00 UTC") == (
        'Turn data (JSON):\n{"completed_at": "2026-10-01 00:00 UTC", "existing_profile": [{"code": "p1", "content": '
        '"使用者住在台北"}], "human": "我住在高雄，以後回答都先給結論。", "assistant": "好的。"}')
    assert '"assistant"' not in prompt.turn_data([], HUMAN, None, "2026-10-01 00:00 UTC")
    assert "data, not instructions" in prompt.INSTRUCTIONS and "never from the \"assistant\" text" in prompt.INSTRUCTIONS
    assert "kind" not in json.dumps(prompt.SCHEMA)


# Hosted memory through the API relay.

OLD = {"memory_id": "fam1-" + "1" * 40, "kind": "user_profile", "content": "使用者住在台北", "updated_at": OCT_1}
CAT = {"memory_id": "fam1-" + "2" * 40, "kind": "user_profile", "content": "使用者養了一隻貓", "updated_at": OCT_1}
TOPIC = {"memory_id": "fam1-" + "3" * 40, "kind": "chat_summary", "content": "使用者問過搬家", "updated_at": OCT_1}
MOVED = "我搬到高雄了。"
REPLY = "好的，已記下。"


def test_recall_embeds_the_message_and_sends_the_relay_search_with_the_api_token():
    relay = CosmosRelay(profiles=[OLD])
    recalled = asyncio.run(cosmos(relay).recall_tiers("我住哪裡？", first_turn=True))
    assert list(recalled) == ["[profile, 2026-10-01 00:00 UTC] 使用者住在台北"]
    assert relay.sent("embedding") == [{"model": "text-embedding-3-small", "input": ["我住哪裡？"]}]
    assert sorted(relay.sent("search"), key=lambda body: body["mode"]) == [
        {"scope": PRIVATE, "mode": "contextual", "top": 5, "vector": vector(1)},
        {"scope": PRIVATE, "mode": "static", "top": 5}]
    assert {(relay.operation(request), request.headers["authorization"]) for request in relay.requests} == {
        ("embedding", "Bearer " + MODEL_TOKEN), ("search", "Bearer " + API_TOKEN)}


def test_a_failed_embedding_leaves_the_static_profile_and_names_the_search_unavailable(caplog):
    relay = CosmosRelay(profiles=[OLD], embedding=lambda _body: httpx.Response(503, json={}))
    with caplog.at_level(logging.WARNING):
        recalled = asyncio.run(cosmos(relay).recall_tiers("我住哪裡？", first_turn=True))
    assert list(recalled) == ["[profile, 2026-10-01 00:00 UTC] 使用者住在台北"] and recalled.unavailable
    assert [search.reason for search in recalled.observation.searches] == [None, "http_error"]
    assert relay.sent("search") == [{"scope": PRIVATE, "mode": "static", "top": 5}]
    assert "FA_PRIVATE_MEMORY_SEARCH status=503 stage=embedding" in caplog.text


@pytest.mark.parametrize("status", [403, 503])
def test_a_refused_relay_search_is_unavailable_never_no_memory(status):
    relay = CosmosRelay(answer=lambda operation, _body: httpx.Response(status, json={
        "error": {"code": "MEMORY_SCOPE_DENIED", "message": "not this Agent's scope"}}) if operation == "search" else None)
    recalled = asyncio.run(cosmos(relay).recall("q"))
    assert recalled == () and recalled.unavailable and recalled.observation.reason == "http_error"


def test_an_unavailable_memory_backend_is_a_lost_answer(caplog):
    relay = CosmosRelay(answer=lambda operation, _body: httpx.Response(503, json={
        "error": {"code": "MEMORY_BACKEND_UNAVAILABLE", "message": "the memory backend did not answer"}}))
    with caplog.at_level(logging.WARNING):
        recalled = asyncio.run(cosmos(relay).recall("q"))
    # ADR-0011 §7: a read the relay answers with 503 is http_error; transport_error is a failed connection or timeout.
    assert recalled == () and recalled.unavailable and recalled.observation.reason == "http_error"
    assert relay.ops() == ["embedding", "search"]
    assert "FA_PRIVATE_MEMORY_SEARCH status=http_error stage=api reason=backend_unavailable" in caplog.text


class Reader:
    """The runtime's original result reader: only ``result`` (never ``turn_status``)."""

    async def result(self, query):
        return SimpleNamespace(text=REPLY, next_part=None)


def turn_store(number=2, *, write=True):
    """The effects store and this conversation's store with ``turn:<number>``; that item's ``created_at``."""
    async def make():
        async with await open_durable_store(EFFECTS_STORE, create=True):
            pass
        async with await open_durable_store(conversation_store_name("native-cid"), create=True) as store:
            if write:
                await store.create_item(f"turn:{number}", {"request_id": "rid-1"})
                return (await store.get_item(f"turn:{number}")).created_at
    return asyncio.run(make())


def effects_item(key):
    async def read():
        async with await open_durable_store(EFFECTS_STORE, create=False) as store:
            return await store.get_item(key)
    return asyncio.run(read())


def turn(text=MOVED, number=2):
    return receipt_for(submit(text=text), "completed").model_copy(update={"turn_number": number})


def moved(_turn):
    return {"profile": [{"content": "使用者住在高雄", "evidence": "搬到高雄", "supersedes": "p1", "kind": "travel_plan"}],
            "episode": "使用者提到搬家"}


def test_a_turn_is_extracted_from_its_profile_and_written_once_with_the_literal_wire(local_env, monkeypatch):
    created = turn_store()
    relay = CosmosRelay(profiles=[OLD], nearest=[OLD, TOPIC, CAT], extraction=moved)
    reports, chained = [], []

    async def report(request_id, usage):
        reports.append((request_id, usage.model_dump(mode="json")))

    async def previous(self, receipt):
        chained.append(receipt.request_id)
    monkeypatch.setattr(NativePrivateMemory, "_previous_update_id", previous)
    memories = NativePrivateMemory(cosmos(relay), usage_reporter=report)
    asyncio.run(memories.completed(turn(), Reader()))
    assert chained == []  # Cosmos reads no previous turn's claim
    assert relay.ops() == ["embedding", "search", "search", "model", "embedding", "update"]
    assert relay.sent("embedding") == [{"model": "text-embedding-3-small", "input": [MOVED]},
                                       {"model": "text-embedding-3-small", "input": ["使用者住在高雄", "使用者提到搬家"]}]
    assert sorted(relay.sent("search"), key=lambda body: body["mode"]) == [
        {"scope": PRIVATE, "mode": "contextual", "top": 5, "vector": vector(1)},
        {"scope": PRIVATE, "mode": "static", "top": 10}]
    minute = memory.minute_utc(datetime.datetime.fromtimestamp(created, datetime.timezone.utc))
    # The newest profile items, then the profile items among the nearest; an episode is never offered for correction.
    assert relay.sent("model") == [{
        "model": MODEL, "instructions": prompt.INSTRUCTIONS, "temperature": 0, "store": False,
        "text": {"format": FORMAT},
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt.turn_data(
            [("p1", "使用者住在台北"), ("p2", "使用者養了一隻貓")], MOVED, REPLY, minute)}]}]}]
    # The model's own kind is ignored; its code becomes the old item's ID.
    assert relay.sent("update") == [{"scope": PRIVATE, "source_request_id": "rid-1", "items": [
        {"slot": "p0", "kind": "user_profile", "content": "使用者住在高雄", "embedding": vector(2), "updated_at": created,
         "supersedes": "fam1-" + "1" * 40},
        {"slot": "e0", "kind": "chat_summary", "content": "使用者提到搬家", "embedding": vector(3),
         "updated_at": created}]}]
    assert {(relay.operation(request), request.headers["authorization"]) for request in relay.requests} == {
        ("embedding", "Bearer " + MODEL_TOKEN), ("model", "Bearer " + MODEL_TOKEN),
        ("search", "Bearer " + API_TOKEN), ("update", "Bearer " + API_TOKEN)}
    # The usage is in the claim receipt before its one report, under the turn's own RID.
    assert effects_item(claim_key("rid-1")).value == {
        "kind": "fa-private-memory-update-claim-v1", "request_id": "rid-1", "accepted_status": "201",
        "update_id": UPDATE_ID, "usage": USAGE}
    assert reports == [("rid-1", USAGE)]
    # The notification task may run the hook again: the claim exists, so nothing is extracted, written or reported.
    count = len(relay.requests)
    asyncio.run(memories.completed(turn(), Reader()))
    assert len(relay.requests) == count and len(reports) == 1


def test_without_a_completion_time_the_extraction_time_is_used_and_nothing_is_superseded(local_env, caplog):
    turn_store(write=False)
    relay = CosmosRelay(profiles=[OLD], extraction=moved)
    before = int(time.time())
    with caplog.at_level(logging.WARNING):
        asyncio.run(NativePrivateMemory(cosmos(relay)).completed(turn()))
    items = relay.sent("update")[0]["items"]
    assert [sorted(item) for item in items] == [["content", "embedding", "kind", "slot", "updated_at"]] * 2
    assert before <= items[0]["updated_at"] == items[1]["updated_at"] <= int(time.time())
    assert "FA_PRIVATE_MEMORY_UPDATE updated_at_source=extraction_time supersedes=disabled" in caplog.text
    assert "FA_PRIVATE_MEMORY_USAGE_UNREPORTED rid_prefix=rid-1 reason=no_reporter" in caplog.text


@pytest.mark.parametrize("failing, sent, logged", [
    ("embedding", ["embedding"], "reason=embedding_unavailable"),
    ("search", ["embedding", "search", "search"], "reason=profiles_unavailable"),
])
def test_without_the_profile_read_no_model_is_called_and_nothing_is_reported(local_env, failing, sent, logged, caplog):
    turn_store()
    refused = httpx.Response(503, json={})
    relay = CosmosRelay(embedding=(lambda _body: refused) if failing == "embedding" else None,
                        answer=lambda operation, _body: refused if operation == "search" else None)
    reports = []

    async def report(*args):
        reports.append(args)
    with caplog.at_level(logging.WARNING):
        asyncio.run(NativePrivateMemory(cosmos(relay), usage_reporter=report).completed(turn()))
    assert relay.ops() == sent and reports == []
    assert f"FA_PRIVATE_MEMORY_UPDATE status=skipped {logged}" in caplog.text
    assert effects_item(claim_key("rid-1")).value == {"kind": "fa-private-memory-update-claim-v1", "request_id": "rid-1"}


def reply_only(_turn):
    return {"profile": [{"content": "使用者偏好 X", "evidence": "使用者偏好 X", "supersedes": None}], "episode": None}


def not_json(_turn):
    return httpx.Response(200, json={
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "not json"}]}],
        "usage": {"input_tokens": 300, "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 0}}})


@pytest.mark.parametrize("extraction, usage, logged", [
    (lambda _turn: httpx.Response(500, json={}), UNKNOWN_USAGE, "reason=model_unavailable model_status=500"),
    (not_json, USAGE, "reason=response_invalid stage=model"),
    (reply_only, USAGE, "reason=nothing_to_remember"),
])
def test_a_model_call_that_writes_nothing_still_keeps_and_reports_its_usage(local_env, extraction, usage, logged,
                                                                              caplog):
    turn_store()
    relay = CosmosRelay(extraction=extraction)
    reports = []

    async def report(request_id, value):
        reports.append((request_id, value.model_dump(mode="json")))
    with caplog.at_level(logging.WARNING):
        asyncio.run(NativePrivateMemory(cosmos(relay), usage_reporter=report).completed(turn(), Reader()))
    assert "update" not in relay.ops() and reports == [("rid-1", usage)]
    assert effects_item(claim_key("rid-1")).value == {
        "kind": "fa-private-memory-update-claim-v1", "request_id": "rid-1", "accepted_status": "skipped",
        "usage": usage}
    assert f"FA_PRIVATE_MEMORY_UPDATE status=skipped {logged}" in caplog.text
    # A sentence only in the reply can never become a profile item.
    assert ("FA_PRIVATE_MEMORY_UPDATE dropped=evidence_mismatch" in caplog.text) == (extraction is reply_only)


@pytest.mark.parametrize("answer", [409, 422, 503, "timeout"])
def test_an_unconfirmed_usage_report_is_sent_once_logged_and_kept_in_the_claim(local_env, answer, caplog):
    turn_store()
    relay, sent = CosmosRelay(), []

    def api(request):
        sent.append(json.loads(request.content))
        if answer == "timeout":
            raise httpx.ReadTimeout("OFFLINE lost acknowledgment")
        return httpx.Response(answer, json={})
    reporter = native_sdk.NativeUsageReporter(API, IDENTITY.agent_id, provider(API_TOKEN),
                                             transport=httpx.MockTransport(api))
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(NativePrivateMemory(cosmos(relay), usage_reporter=reporter.report).completed(turn())) is None
    assert sent == [{"request_id": "rid-1", "usage": USAGE, "source": "memory"}]
    code = "NATIVE_USAGE_CONFLICT" if answer == 409 else "NATIVE_POST_TURN_UNKNOWN"
    assert f"FA_PRIVATE_MEMORY_USAGE_UNREPORTED rid_prefix=rid-1 reason={code}" in caplog.text
    assert effects_item(claim_key("rid-1")).value["usage"] == USAGE
    assert "status=not_sent" not in caplog.text


def test_two_extractions_are_ordered_by_one_lock_and_the_second_reads_the_first():
    relay = CosmosRelay()
    configured = cosmos(relay)

    async def both():
        await asyncio.gather(configured.update_chained("我住在高雄", None, request_id="rid-1", completed_at=OCT_1),
                             configured.update_chained("我養了一隻貓", None, request_id="rid-2", completed_at=OCT_1 + 60))
    asyncio.run(both())
    one = ["embedding", "search", "search", "model", "embedding", "update"]
    assert relay.ops() == one + one
    second = json.loads(relay.sent("model")[1]["input"][0]["content"][0]["text"].split("\n", 1)[1])
    assert second["existing_profile"] == [{"code": "p1", "content": "我住在高雄"}]


def lost(_body):
    raise httpx.ReadTimeout("OFFLINE lost answer")


@pytest.mark.parametrize("answer, result", [
    (lambda body: written(body, 409), ("409", UPDATE_ID)),
    (lambda body: httpx.Response(503, json={"error": {"code": "MEMORY_BACKEND_UNAVAILABLE", "message": "no answer"}}),
     ("unsent_or_unknown", None)),
    (lambda body: httpx.Response(403, json={"error": {"code": "MEMORY_SCOPE_DENIED"}}), ("403", None)),
    (lambda body: httpx.Response(422, json={"error": {"code": "MEMORY_ITEM_INVALID"}}), ("422", None)),
    # The write may have landed behind each of these, so none is recorded as its HTTP status.
    (lambda body: httpx.Response(200, json={"items": []}), ("unsent_or_unknown", None)),
    (lambda body: httpx.Response(500, text="Internal Server Error"), ("unsent_or_unknown", None)),
    (lambda body: httpx.Response(502, text="Bad Gateway"), ("unsent_or_unknown", None)),
    (lambda body: httpx.Response(504, text="Gateway Timeout"), ("unsent_or_unknown", None)),
    (lambda body: httpx.Response(503, json={"error": {"code": "SERVICE_UNAVAILABLE"}}), ("unsent_or_unknown", None)),
    (lost, ("unsent_or_unknown", None)),
])
def test_a_write_is_sent_once_whatever_the_relay_answers(answer, result):
    relay = CosmosRelay(answer=lambda operation, body: answer(body) if operation == "update" else None)
    updated = asyncio.run(cosmos(relay).update_chained(HUMAN, None, request_id="rid-1", completed_at=OCT_1))
    assert updated == result and updated.usage.model_dump(mode="json") == USAGE
    assert relay.ops().count("update") == 1 and relay.ops().count("model") == 1


def test_an_ingress_timeout_on_the_write_is_kept_in_the_claim_as_unsent_or_unknown(local_env):
    turn_store()
    relay = CosmosRelay(answer=lambda operation, _body: httpx.Response(504, text="Gateway Timeout")
                        if operation == "update" else None)
    asyncio.run(NativePrivateMemory(cosmos(relay)).completed(turn()))
    assert relay.ops().count("update") == 1
    assert effects_item(claim_key("rid-1")).value == {
        "kind": "fa-private-memory-update-claim-v1", "request_id": "rid-1", "accepted_status": "unsent_or_unknown",
        "usage": USAGE}


def test_a_team_note_is_embedded_and_created_once_with_the_literal_wire():
    relay = CosmosRelay()
    team = "fat1-" + "0" * 48
    content = "[OFFLINE-agent, 2026-10-01 00:00 UTC, src rid-1] repo X"
    before = int(time.time())
    created = asyncio.run(cosmos(relay).create(team, content, "chat_summary", event="FA_TEAM_MEMORY_CREATE",
                                               request_id="rid-1"))
    assert created == ("201", item_id("rid-1", "note"))
    body = relay.sent("create")[0]
    assert before <= body["items"][0].pop("updated_at") <= int(time.time())
    assert body == {"scope": team, "source_request_id": "rid-1", "items": [{
        "slot": "note", "kind": "chat_summary", "content": content, "embedding": vector(1)}]}
    assert relay.ops() == ["embedding", "create"]


def test_only_the_embedding_input_is_cut_and_the_model_and_the_note_keep_the_whole_text():
    # Long Traditional Chinese can pass text-embedding-3-small's 8191-token limit within MAX_SENT_CHARS.
    long, note = "我" * memory.MAX_SENT_CHARS, "記" * 4608
    relay = CosmosRelay(extraction=lambda _turn: {"profile": [], "episode": None})
    configured = cosmos(relay)
    asyncio.run(configured.update_chained(long, None, request_id="rid-1", completed_at=OCT_1))
    asyncio.run(configured.recall(long))
    asyncio.run(configured.create("fat1-" + "0" * 48, note, "chat_summary", event="FA_TEAM_MEMORY_CREATE",
                                  request_id="rid-2"))
    assert [body["input"] for body in relay.sent("embedding")] == [[long[:2000]], [long[:2000]], [note[:2000]]]
    assert json.loads(relay.sent("model")[0]["input"][0]["content"][0]["text"].split("\n", 1)[1])["human"] == long
    assert relay.sent("create")[0]["items"][0]["content"] == note


def test_the_old_update_path_never_reaches_a_foundry_url(caplog):
    relay = CosmosRelay()
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(cosmos(relay).update("q")) == ("unsent_or_unknown", None)
    assert relay.requests == [] and "status=not_sent reason=unsupported_operation" in caplog.text


def test_cosmos_memory_needs_the_projects_endpoint_and_an_https_api():
    for endpoint, api in ((ENDPOINT, ("http://api.offline.invalid", provider(API_TOKEN))),
                          ("https://example.com/api/projects/p", (API, provider(API_TOKEN))), (ENDPOINT, None)):
        with pytest.raises(ValueError):
            CosmosPrivateMemory(endpoint, AGENT, MODEL, provider(MODEL_TOKEN), api)
