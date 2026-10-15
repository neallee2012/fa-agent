"""ADR-0007 N3: checkpoints in the Agent's State store and a verified $HOME hot copy.

Offline: the pinned SDK's own local State store backend (AGENTSERVER_STATE_ROOT under tmp_path); no network or token.
"""

import asyncio
import base64
import json
import logging

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore, FoundryStorageApiError
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError, ServiceResponseError

from shared_context import native_checkpoint as native
from shared_context.behavior_sdk_state import NATIVE_SEGMENT_BYTES, NativeCheckpointScope, SdkState
from shared_context.errors import Problem

SCOPE = NativeCheckpointScope(agent_id="fa-agent-cc96", conversation_id="cid-1", sdk_session_id="sdk-1")
STORE = native.conversation_store_name("cid-1")


@pytest.fixture(autouse=True)
def local_store(monkeypatch, tmp_path):
    monkeypatch.delenv("FOUNDRY_HOSTING_ENVIRONMENT", raising=False)
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path / "OFFLINE-state"))


def state(sid="sdk-1", tool_output="Original tool result"):
    return SdkState(sdk_session_id=sid, files={
        "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": sid}}) + "\n",
        "workspace.yaml": "id: " + sid + "\n",
        "temp/tool-output.txt": tool_output,
    }, directories=("temp",))


def run(coroutine):
    return asyncio.run(coroutine)


def problem(coroutine):
    with pytest.raises(Problem) as raised:
        run(coroutine)
    return raised.value.status, raised.value.body["error"]["code"]


async def opened(create=True):
    return await native.open_durable_store(STORE, create=create)


class Counting:
    """The real local store, with item reads counted and chosen operations failing like an unreachable service."""

    def __init__(self, store, *, fail=(), error=None, written=False):
        self.store, self.fail, self.reads, self.written = store, set(fail), 0, written
        self.error = error or FoundryStorageApiError("OFFLINE unreachable", status_code=503)

    async def create_item(self, key, value):
        if "create" in self.fail:
            if self.written:  # saved, but the acknowledgment was lost
                await self.store.create_item(key, value)
            raise self.error
        return await self.store.create_item(key, value)

    async def get_item(self, key):
        self.reads += 1
        if "get" in self.fail:
            raise self.error
        return await self.store.get_item(key)


UNCONFIRMED = [
    FoundryStorageApiError("OFFLINE unreachable", status_code=503), ServiceRequestError("OFFLINE connect"),
    ServiceResponseError("OFFLINE response lost"), ClientAuthenticationError("OFFLINE token"), OSError("OFFLINE"),
]


def test_stores_never_expire_and_a_read_never_creates_one():
    assert problem(opened(create=False)) == (404, "NATIVE_STATE_STORE_MISSING")
    store = run(opened())
    assert run(store.get()).item_ttl_seconds == -1
    assert run(opened(create=False)) is not None

    async def default_ttl():
        await FoundryStateStore.get_or_create("fa/v1/conv/expiring")
    run(default_ttl())
    for create in (True, False):
        with pytest.raises(Problem) as refused:
            run(native.open_durable_store("fa/v1/conv/expiring", create=create))
        assert (refused.value.status, refused.value.body["error"]["code"]) == (409, "NATIVE_STATE_STORE_TTL")


def test_save_writes_verified_content_addressed_segments_and_loads_them_back():
    big = state(tool_output="x" * (NATIVE_SEGMENT_BYTES * 2 + 1000))
    store = run(opened())
    ref = run(native.save_checkpoint(store, SCOPE, big))
    assert (ref.codec, ref.segments, ref.byte_length, ref.sha256) == (
        "native-session-fs-v1", 3, len(big.encoded()), big.sha256)
    for index in range(3):
        item = run(store.get_item(native.segment_key(ref.sha256, index)))
        assert item.value["kind"] == native.SEGMENT_KIND and item.value["index"] == index
    assert run(native.load_checkpoint(store, SCOPE, ref)) == big
    assert run(native.verify_checkpoint(store, SCOPE, ref)) == big
    # A retried save after a lost acknowledgment converges on the same reference.
    assert run(native.save_checkpoint(store, SCOPE, big)) == ref


def test_responses_checkpoints_keep_their_own_codec():
    responses = SdkState.model_validate_json(json.dumps({**state().model_dump(mode="json"),
                                             "codec": "session-fs-responses-v1", "model_provider": "openai-responses"}))
    scope = SCOPE.model_copy(update={"model_provider": "openai-responses"})
    ref = run(native.save_checkpoint(run(opened()), scope, responses))
    assert ref.codec == "native-session-fs-responses-v1" and ref.model_provider == "openai-responses"
    assert problem(native.save_checkpoint(run(opened()), SCOPE, responses)) == (403, "ASSISTANT_CHECKPOINT_SCOPE")


def test_diagnostic_state_and_another_scope_are_refused():
    diagnostic = state().model_copy(update={"codec": "session-fs-diag-empty-v1"})
    assert problem(native.save_checkpoint(run(opened()), SCOPE, diagnostic)) == (409, "SDK_STATE_UNSUPPORTED_FORMAT")
    assert problem(native.save_checkpoint(run(opened()), SCOPE, state("sdk-other"))) == (403, "ASSISTANT_CHECKPOINT_SCOPE")
    store = run(opened())
    ref = run(native.save_checkpoint(store, SCOPE, state()))
    for other in (SCOPE.model_copy(update={"conversation_id": "cid-2"}), SCOPE.model_copy(update={"agent_id": "b"})):
        assert problem(native.load_checkpoint(store, other, ref)) == (403, "ASSISTANT_CHECKPOINT_SCOPE")
        assert problem(native.verify_checkpoint(store, other, ref)) == (403, "ASSISTANT_CHECKPOINT_SCOPE")


def test_an_existing_different_segment_is_never_overwritten_or_accepted():
    store = run(opened())
    original = state()
    ref = run(native.save_checkpoint(run(opened()), SCOPE, original))
    key = native.segment_key(ref.sha256, 0)
    run(store.delete_item(key))
    forged = {**native._segment(ref, 0, b"forged"), "data": base64.b64encode(b"forged").decode()}
    run(store.create_item(key, forged))
    assert problem(native.save_checkpoint(store, SCOPE, original)) == (409, "SDK_STATE_INCOMPLETE")
    assert run(store.get_item(key)).value == forged


def test_a_missing_or_altered_segment_is_incomplete_not_restored():
    store = run(opened())
    ref = run(native.save_checkpoint(store, SCOPE, state(tool_output="y" * (NATIVE_SEGMENT_BYTES + 10))))
    last = native.segment_key(ref.sha256, 1)
    value = run(store.get_item(last)).value
    run(store.set_item(last, {**value, "data": base64.b64encode(b"z" * 10).decode()}))
    assert problem(native.verify_checkpoint(store, SCOPE, ref)) == (409, "SDK_STATE_INCOMPLETE")
    run(store.delete_item(last))
    assert problem(native.verify_checkpoint(store, SCOPE, ref)) == (409, "SDK_STATE_INCOMPLETE")
    assert problem(native.load_checkpoint(store, SCOPE, ref)) == (409, "SDK_STATE_INCOMPLETE")


@pytest.mark.parametrize("error", UNCONFIRMED, ids=lambda error: type(error).__name__)
def test_unknown_store_outcomes_are_never_reported_as_saved_or_missing(error):
    store = run(opened())
    assert problem(native.save_checkpoint(Counting(store, fail={"create"}, error=error), SCOPE, state())) == (
        503, "SDK_STATE_UPLOAD_UNKNOWN")
    assert problem(native.save_checkpoint(Counting(store, fail={"get"}, error=error), SCOPE, state())) == (
        503, "SDK_STATE_UPLOAD_UNKNOWN")
    ref = run(native.save_checkpoint(store, SCOPE, state()))
    assert problem(native.verify_checkpoint(Counting(store, fail={"get"}, error=error), SCOPE, ref)) == (
        503, "NATIVE_STATE_STORE_UNKNOWN")
    assert problem(native.load_checkpoint(Counting(store, fail={"get"}, error=error), SCOPE, ref)) == (
        503, "NATIVE_STATE_STORE_UNKNOWN")


def test_a_saved_segment_whose_acknowledgment_was_lost_converges_on_retry():
    store = run(opened())
    lost = Counting(store, fail={"create"}, error=ServiceResponseError("OFFLINE response lost"), written=True)
    assert problem(native.save_checkpoint(lost, SCOPE, state())) == (503, "SDK_STATE_UPLOAD_UNKNOWN")
    # The same content-addressed key is accepted on retry; nothing is duplicated or overwritten.
    assert run(native.save_checkpoint(store, SCOPE, state())).sha256 == state().sha256


@pytest.mark.parametrize("error", UNCONFIRMED, ids=lambda error: type(error).__name__)
def test_an_unconfirmed_open_is_unknown_and_closes_its_client(monkeypatch, error):
    run(opened())
    closed = []

    async def unreachable(self):
        raise error

    async def close(self):
        closed.append(self)
    monkeypatch.setattr(FoundryStateStore, "get", unreachable)
    monkeypatch.setattr(FoundryStateStore, "aclose", close)
    for create in (True, False):
        assert problem(native.open_durable_store(STORE, create=create)) == (503, "NATIVE_STATE_STORE_UNKNOWN")
    assert len(closed) == 2


def test_a_matching_hot_copy_saves_store_reads_but_never_replaces_verification(tmp_path):
    cache = tmp_path / "home" / "native-checkpoints"
    store = run(opened())
    ref = run(native.save_checkpoint(store, SCOPE, state(), cache_root=cache))
    counted = Counting(store)
    assert run(native.load_checkpoint(counted, SCOPE, ref, cache_root=cache)) == state() and counted.reads == 0
    # verify_checkpoint always reads the store: a hot copy cannot hide a missing remote segment.
    run(store.delete_item(native.segment_key(ref.sha256, 0)))
    assert run(native.load_checkpoint(counted, SCOPE, ref, cache_root=cache)) == state()
    assert problem(native.verify_checkpoint(counted, SCOPE, ref)) == (409, "SDK_STATE_INCOMPLETE") and counted.reads == 1


def test_a_corrupt_or_stale_hot_copy_falls_back_to_the_store(tmp_path, caplog):
    cache = tmp_path / "home" / "native-checkpoints"
    store = run(opened())
    first = run(native.save_checkpoint(store, SCOPE, state(), cache_root=cache))
    second = run(native.save_checkpoint(store, SCOPE, state(tool_output="newer"), cache_root=cache))
    # Only the conversation's latest verified checkpoint is kept on $HOME.
    assert [path.name for path in native._cache_path(cache, second).parent.iterdir()] == [second.sha256 + ".json"]
    native._cache_path(cache, second).write_bytes(b"x" * second.byte_length)
    counted = Counting(store)
    assert run(native.load_checkpoint(counted, SCOPE, second, cache_root=cache)) == state(tool_output="newer")
    assert counted.reads == second.segments and native._read_cache(cache, second) is not None
    assert run(native.load_checkpoint(counted, SCOPE, first, cache_root=cache)) == state()
    blocked = tmp_path / "file-instead-of-directory"
    blocked.write_text("not a directory")
    with caplog.at_level(logging.WARNING):
        assert run(native.save_checkpoint(store, SCOPE, state(tool_output="third"), cache_root=blocked)).segments == 1
    assert "status=write_failed" in caplog.text


class Overlapping(Counting):
    """Counts how many segment writes and reads are in flight at once."""

    def __init__(self, store):
        super().__init__(store)
        self.active = self.most_writes = self.most_reads = 0

    async def create_item(self, key, value):
        self.active += 1
        self.most_writes = max(self.most_writes, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().create_item(key, value)
        finally:
            self.active -= 1

    async def get_item(self, key):
        self.active += 1
        self.most_reads = max(self.most_reads, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().get_item(key)
        finally:
            self.active -= 1


def test_segments_are_written_and_read_back_concurrently_but_bounded():
    """Audit finding 17: segments went one at a time; a 6-segment checkpoint now takes about two round trips."""
    big = state(tool_output="x" * (NATIVE_SEGMENT_BYTES * 5 + 10))
    store = run(opened())
    overlapping = Overlapping(store)
    ref = run(native.save_checkpoint(overlapping, SCOPE, big))
    assert ref.segments == 6 and overlapping.reads == 6
    assert 1 < overlapping.most_writes <= 4 and 1 < overlapping.most_reads <= 4
    assert run(native.verify_checkpoint(Overlapping(store), SCOPE, ref)) == big


def test_a_concurrent_readback_still_refuses_an_altered_segment():
    store = run(opened())
    big = state(tool_output="y" * (NATIVE_SEGMENT_BYTES * 3 + 10))
    ref = run(native.save_checkpoint(store, SCOPE, big))
    middle = native.segment_key(ref.sha256, 2)
    value = run(store.get_item(middle)).value
    run(store.set_item(middle, {**value, "data": base64.b64encode(b"z" * 10).decode()}))
    assert problem(native.verify_checkpoint(store, SCOPE, ref)) == (409, "SDK_STATE_INCOMPLETE")
    assert problem(native.save_checkpoint(store, SCOPE, big)) == (409, "SDK_STATE_INCOMPLETE")


def test_the_hot_copy_keeps_only_the_most_recently_used_conversations(tmp_path, monkeypatch):
    """Audit finding 18: the hot copy pruned only inside a conversation folder, so $HOME grew with every conversation."""
    import os

    monkeypatch.setenv("FA_NATIVE_CHECKPOINT_CACHE_CONVERSATIONS", "2")
    cache = tmp_path / "home" / "native-checkpoints"
    unrelated = cache / "not-a-conversation"
    unrelated.mkdir(parents=True)
    store = run(opened())
    refs = {}
    for age, cid in enumerate(("cid-a", "cid-b", "cid-c", "cid-d")):
        scope = SCOPE.model_copy(update={"conversation_id": cid})
        refs[cid] = (scope, run(native.save_checkpoint(store, scope, state(tool_output=cid), cache_root=cache)))
        if cid in {"cid-a", "cid-b"}:
            os.utime(native._cache_path(cache, refs[cid][1]).parent, (1_000_000 + age, 1_000_000 + age))
    kept = {path.name for path in cache.iterdir()}
    assert kept == {native._cache_path(cache, refs[cid][1]).parent.name for cid in ("cid-c", "cid-d")} | {unrelated.name}
    # A cache hit marks its folder as recently used; a pruned conversation is only a store read away.
    folder = native._cache_path(cache, refs["cid-c"][1]).parent
    os.utime(folder, (1_000_000, 1_000_000))
    scope, ref = refs["cid-c"]
    counted = Counting(store)
    assert run(native.load_checkpoint(counted, scope, ref, cache_root=cache)) == state(tool_output="cid-c")
    assert counted.reads == 0 and folder.stat().st_mtime > 1_000_000
    scope, ref = refs["cid-a"]
    assert run(native.load_checkpoint(counted, scope, ref, cache_root=cache)) == state(tool_output="cid-a")
    assert counted.reads == ref.segments


def test_a_hot_copy_folder_removed_meanwhile_is_skipped_and_the_prune_goes_on(tmp_path, monkeypatch, caplog):
    """A folder that vanishes between the listing's ``is_dir`` and its ``stat`` is not a write failure, and the
    other folders over the bound are still pruned."""
    import errno
    import os
    from pathlib import Path

    monkeypatch.setenv("FA_NATIVE_CHECKPOINT_CACHE_CONVERSATIONS", "2")
    cache = tmp_path / "home" / "native-checkpoints"
    store = run(opened())
    folders = {}
    for age, cid in enumerate(("cid-a", "cid-b")):
        scope = SCOPE.model_copy(update={"conversation_id": cid})
        folders[cid] = native._cache_path(cache, run(native.save_checkpoint(
            store, scope, state(tool_output=cid), cache_root=cache))).parent
        os.utime(folders[cid], (1_000_000 + age, 1_000_000 + age))
    ghost = cache / ("f" * 32)
    ghost.mkdir()
    real_stat, seen = Path.stat, []

    def racing(path, *, follow_symlinks=True):
        if path.name == ghost.name and follow_symlinks:
            seen.append(path)
            if len(seen) > 1:  # is_dir saw it; it is gone by the time its mtime is read
                raise FileNotFoundError(errno.ENOENT, "OFFLINE removed meanwhile", str(path))
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", racing)
    scope = SCOPE.model_copy(update={"conversation_id": "cid-c"})
    with caplog.at_level(logging.WARNING):
        ref = run(native.save_checkpoint(store, scope, state(tool_output="cid-c"), cache_root=cache))
    assert len(seen) >= 2 and "status=write_failed" not in caplog.text
    assert {path.name for path in cache.iterdir()} == {
        folders["cid-b"].name, native._cache_path(cache, ref).parent.name, ghost.name}


def test_a_hot_copy_backlog_is_pruned_a_bounded_number_per_write_oldest_first(tmp_path, monkeypatch):
    """The first write after an upgrade may find many folders; each write removes at most a few, oldest first."""
    import os

    monkeypatch.setenv("FA_NATIVE_CHECKPOINT_CACHE_CONVERSATIONS", "2")
    monkeypatch.setattr(native, "CACHE_PRUNE_PER_WRITE", 3)
    cache = tmp_path / "home" / "native-checkpoints"
    backlog = [cache / f"{index:032x}" for index in range(6)]
    for age, folder in enumerate(backlog):
        folder.mkdir(parents=True)
        (folder / "old.json").write_bytes(b"{}")
        os.utime(folder, (1_000_000 + age, 1_000_000 + age))
    store = run(opened())
    run(native.save_checkpoint(store, SCOPE, state(), cache_root=cache))
    assert sorted(folder.name for folder in backlog if folder.exists()) == [folder.name for folder in backlog[3:]]
    run(native.save_checkpoint(store, SCOPE, state(tool_output="second"), cache_root=cache))
    assert [folder.name for folder in backlog if folder.exists()] == [backlog[5].name]
