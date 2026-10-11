"""MEM-05/07 (MEM-57a): native memory keeps the official receipt per RID and recalls bounded, dated, newest-first data.

Offline: official local store, named memory-service doubles; no network, model or token service.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_private_memory as memory
from shared_context import native_team_memory as team
from shared_context.behavior_native_turn_wire import TeamMemoryCapture, team_memory_scope
from shared_context.native_checkpoint import open_durable_store
from shared_context.native_private_memory import Claimed, NativePrivateMemory, claim_key, record_receipt
from shared_context.native_turns import EFFECTS_STORE
from test_native_team_memory import ENV, bodies, run_turn, token
from test_native_turns import local_env

OCT_1 = 1790812800  # 2026-10-01T00:00:00Z
OCT_2 = OCT_1 + 86400


def effects_item(key):
    async def read():
        async with await open_durable_store(EFFECTS_STORE, create=False) as store:
            return await store.get_item(key)
    return asyncio.run(read())


def create_effects_store():
    async def create():
        async with await open_durable_store(EFFECTS_STORE, create=True):
            pass
    asyncio.run(create())


def update_service(sent, *, status=202, body=None):
    def handle(request):
        sent.append(request)
        return httpx.Response(status, json={"update_id": "upd_1", "status": "queued"} if body is None else body)
    return httpx.MockTransport(handle)


def human(rid="rid-1"):
    return SimpleNamespace(source=None, status="completed", request_id=rid, command=SimpleNamespace(content="hi"))


def private(sent, **kwargs):
    return NativePrivateMemory.from_environment(token, environ=ENV, transport=update_service(sent, **kwargs))


def test_the_private_update_keeps_its_official_update_id_once_and_is_never_resent(local_env):
    create_effects_store()
    sent = []
    memories = private(sent)
    asyncio.run(memories.completed(human()))
    item = effects_item(claim_key("rid-1"))
    assert item.value == {"kind": "fa-private-memory-update-claim-v1", "request_id": "rid-1",
                          "update_id": "upd_1", "accepted_status": "202"}
    # The official terminal task may retry every post-turn action; the update and its receipt are not repeated.
    asyncio.run(memories.completed(human()))
    assert len(sent) == 1 and effects_item(claim_key("rid-1")).etag == item.etag


@pytest.mark.parametrize("status, body", [(500, None), (202, {"update_id": "has spaces"}), (202, {"status": "queued"})])
def test_without_a_usable_official_id_the_claim_stays_as_it_was(local_env, caplog, status, body):
    create_effects_store()
    sent = []
    with caplog.at_level(logging.WARNING):
        asyncio.run(private(sent, status=status, body=body).completed(human()))
    assert len(sent) == 1
    assert effects_item(claim_key("rid-1")).value == {"kind": "fa-private-memory-update-claim-v1", "request_id": "rid-1"}
    assert "receipt=not_recorded reason=no_id" in caplog.text


def test_a_failed_receipt_write_never_raises_or_overwrites_a_changed_claim(local_env, caplog):
    create_effects_store()
    key = claim_key("rid-1")

    async def changed_claim():
        async with await open_durable_store(EFFECTS_STORE, create=False) as store:
            created = await store.create_item(key, {"kind": "fa-private-memory-update-claim-v1", "request_id": "rid-1"})
            await store.set_item(key, {"kind": "fa-private-memory-update-claim-v1", "request_id": "rid-1",
                                       "other": "kept"}, if_match=created.etag)
            return Claimed({"kind": "fa-private-memory-update-claim-v1", "request_id": "rid-1"}, created.etag)
    stale = asyncio.run(changed_claim())
    with caplog.at_level(logging.WARNING):
        asyncio.run(record_receipt(key, stale, {"update_id": "upd_1"}, None, "FA_PRIVATE_MEMORY_UPDATE"))
        asyncio.run(record_receipt(key, stale._replace(etag=None), {"update_id": "upd_1"}, None,
                                   "FA_PRIVATE_MEMORY_UPDATE"))
    assert effects_item(key).value["other"] == "kept" and "update_id" not in effects_item(key).value
    assert "receipt=not_recorded exception_type=" in caplog.text and "receipt=not_recorded reason=no_etag" in caplog.text


def test_a_shared_team_note_keeps_its_official_memory_id(local_env, monkeypatch, tmp_path):
    sent = []
    run_turn(monkeypatch, tmp_path, sent)
    assert len(bodies(sent, "/items")) == 1
    item = effects_item(team.claim_key("rid-1"))
    assert item.value["memory_id"] == "m1" and item.value["kind"] == "fa-team-note-claim-v1"


def search_service(sent, memories):
    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={"search_id": "s1", "usage": {"embedding_tokens": 2}, "memories": [
            {"memory_item": item} for item in memories]})
    return httpx.MockTransport(handle)


def configured(sent, memories):
    return memory.PrivateMemory.from_environment(token, environ=ENV, transport=search_service(sent, memories))


def test_recall_is_newest_first_dated_and_names_what_the_bound_left_out(caplog):
    sent = []
    found = [
        {"memory_id": "a", "content": "The release window is Thursday", "updated_at": OCT_1},
        {"memory_id": "b", "content": "undated item keeps its search order", "updated_at": "not-unix"},
        {"memory_id": "c", "content": "The release window is Friday", "updated_at": OCT_2},
        {"memory_id": "d", "content": "x" * memory.MAX_MEMORY_CHARS, "updated_at": OCT_1 - 86400},
    ]
    with caplog.at_level(logging.WARNING):
        recalled = asyncio.run(configured(sent, found).search("scope-1", "when", event="FA_PRIVATE_MEMORY_SEARCH",
                                                              max_memories=5, max_recall_chars=160))
    assert list(recalled) == ["[2026-10-02 00:00 UTC] The release window is Friday",
                              "[2026-10-01 00:00 UTC] The release window is Thursday",
                              "undated item keeps its search order"]
    assert recalled.omitted == 1
    assert 'omitted={"bound_chars": 1}' in caplog.text
    prompt = memory.with_recalled("Q", recalled)
    assert prompt.splitlines()[0] == memory.PREFIX
    assert "(1 more found but omitted to stay within the size limit.)" in prompt and prompt.endswith("\n\nQ")
    undated = asyncio.run(configured(sent, found[:1]).search("scope-1", "when", event="E", dated=False))
    assert list(undated) == ["The release window is Thursday"] and undated.omitted == 0


def test_team_notes_are_labeled_historical_and_newest_first(local_env):
    create_effects_store()
    sent = []
    memories = team.NativeTeamMemory(configured(sent, [
        {"memory_id": "old", "content": "[agent-a, 2026-10-01] deploy on Friday", "updated_at": OCT_1},
        {"memory_id": "new", "content": "[agent-b, 2026-10-02] deploy on Monday instead", "updated_at": OCT_2},
    ]), "OFFLINE-agent")
    receipt = SimpleNamespace(source=None, command=SimpleNamespace(content="when do we deploy"), capture=SimpleNamespace(
        team_memory=TeamMemoryCapture(scope=team_memory_scope("OFFLINE-tenant", "OFFLINE-team"))))
    recalled = asyncio.run(memories.recall(receipt))
    assert list(recalled) == ["note:new [agent-b, 2026-10-02] deploy on Monday instead",
                              "note:old [agent-a, 2026-10-01] deploy on Friday"]
    assert "may be outdated" in team.PREFIX and "newer time wins" in team.PREFIX
    assert json.loads(sent[0].content)["options"] == {"max_memories": team.MAX_TEAM_MEMORIES}


def seed_previous_turn(previous_update_id="upd_prev"):
    """Conversation cid-1 whose turn 1 (rid-1) recorded its official update ID, as MEM-57a writes it."""
    from shared_context.native_checkpoint import conversation_store_name

    async def seed():
        async with await open_durable_store(conversation_store_name("cid-1"), create=True) as store:
            await store.create_item("turn:1", {"request_id": "rid-1"})
        async with await open_durable_store(EFFECTS_STORE, create=True) as store:
            await store.create_item(claim_key("rid-1"), {"kind": "fa-private-memory-update-claim-v1",
                                                         "request_id": "rid-1", "update_id": previous_update_id})
    asyncio.run(seed())


def second_turn():
    return SimpleNamespace(source=None, status="completed", request_id="rid-2", conversation_id="cid-1",
                           turn_number=2, command=SimpleNamespace(content="更正：其實叫小霧"))


def chain_service(sent, *, chained_status=202):
    def handle(request):
        sent.append(json.loads(request.content))
        if "previous_update_id" in sent[-1]:
            return httpx.Response(chained_status, json={"update_id": "upd_2", "status": "queued"})
        return httpx.Response(202, json={"update_id": "upd_2b", "status": "queued"})
    return httpx.MockTransport(handle)


def test_a_correction_is_chained_to_the_previous_turns_official_update(local_env):
    seed_previous_turn()
    sent = []
    memories = NativePrivateMemory.from_environment(token, environ=ENV, transport=chain_service(sent))
    asyncio.run(memories.completed(second_turn()))
    assert [body.get("previous_update_id") for body in sent] == ["upd_prev"]
    assert sent[0]["items"][0]["content"][0]["text"] == "更正：其實叫小霧"
    assert effects_item(claim_key("rid-2")).value["update_id"] == "upd_2"


@pytest.mark.parametrize("chained_status, expected", [(400, [True, False]), (500, [True])])
def test_only_a_definite_refusal_of_the_chain_is_sent_once_unchained(local_env, caplog, chained_status, expected):
    seed_previous_turn()
    sent = []
    memories = NativePrivateMemory.from_environment(token, environ=ENV,
                                                    transport=chain_service(sent, chained_status=chained_status))
    with caplog.at_level(logging.WARNING):
        asyncio.run(memories.completed(second_turn()))
        asyncio.run(memories.completed(second_turn()))  # a retried post-turn hook never resends
    assert ["previous_update_id" in body for body in sent] == expected
    if chained_status == 400:
        assert "chain=refused" in caplog.text and effects_item(claim_key("rid-2")).value["update_id"] == "upd_2b"


def test_the_first_turn_and_an_unrecorded_previous_turn_are_sent_unchained(local_env):
    create_effects_store()
    sent = []
    memories = NativePrivateMemory.from_environment(token, environ=ENV, transport=chain_service(sent))
    asyncio.run(memories.completed(SimpleNamespace(**{**vars(second_turn()), "turn_number": 1, "request_id": "rid-9"})))
    asyncio.run(memories.completed(second_turn()))  # cid-1 has no turn:1 record in this store
    assert ["previous_update_id" in body for body in sent] == [False, False]


def test_team_notes_are_stamped_to_the_minute_and_the_tool_asks_to_suggest_corrections():
    import datetime
    from shared_context import native_memory_candidate as candidates

    stamped = team.note_content("fa-context-a", "其實叫小霧", datetime.datetime(
        2026, 10, 2, 11, 25, 59, tzinfo=datetime.timezone.utc))
    assert stamped == "[fa-context-a, 2026-10-02 11:25 UTC] 其實叫小霧"
    binding = asyncio.run(candidates.bind(SimpleNamespace(receipt=SimpleNamespace(source=None)), ()))
    assert "When the user corrects something they said earlier, suggest the corrected statement" in (
        binding.tools[0].description)


class SavedReply:
    """The runtime's original result reader: one page per saved text part."""

    def __init__(self, parts, *, fail=False):
        self.parts, self.fail, self.read = parts, fail, []

    async def result(self, command):
        self.read.append((command.conversation_id, command.request_id, command.part))
        if self.fail:
            raise OSError("OFFLINE store transport")
        last = command.part + 1 == len(self.parts)
        return SimpleNamespace(text=self.parts[command.part], next_part=None if last else command.part + 1)


def test_the_update_carries_the_saved_final_reply_bounded_and_only_once(local_env):
    create_effects_store()
    sent = []
    memories = private(sent)
    reader = SavedReply(["a" * 3000, "b" * 3000, "c" * 10])
    receipt = SimpleNamespace(**{**vars(human()), "conversation_id": "cid-1"})
    asyncio.run(memories.completed(receipt, reader))
    asyncio.run(memories.completed(receipt, reader))  # a retried post-turn hook reads and sends nothing
    agent = SimpleNamespace(**{**vars(receipt), "source": object(), "request_id": "rid-2"})
    asyncio.run(memories.completed(agent, reader))
    assert len(sent) == 1 and reader.read == [("cid-1", "rid-1", 0), ("cid-1", "rid-1", 1)]
    items = json.loads(sent[0].content)["items"]
    assert items[0]["role"] == "user" and items[0]["content"][0]["text"] == "hi"
    assert items[1] == {"type": "message", "role": "assistant", "content": "a" * 3000 + "b" * 1000}
    assert memory.MAX_REPLY_CHARS == 4000


def test_an_unreadable_reply_sends_only_the_users_words_once(local_env, caplog):
    create_effects_store()
    sent = []
    receipt = SimpleNamespace(**{**vars(human()), "conversation_id": "cid-1"})
    with caplog.at_level(logging.WARNING):
        asyncio.run(private(sent).completed(receipt, SavedReply([], fail=True)))
    assert [len(json.loads(request.content)["items"]) for request in sent] == [1]
    assert "reply=unavailable exception_type=OSError" in caplog.text and "reply=False" in caplog.text
