"""ADR-0009 phase 4 U3/U9 (G0b reserved, 4a⑧ implemented): ``session_checkpoint_check`` and ``session_prerestore``.

The destination session runs them before it is the current SID, so neither is a ``_RECORD_READS`` operation. Both read
the latest checkpoint the next turn would restore; neither writes a receipt or starts the SDK. Prerestore leaves it in
the local hot copy, so the next turn does not read it from the store again.
"""

import asyncio
import os
from pathlib import Path

import pytest

from shared_context import native_checkpoint
from shared_context.behavior_native_turn_wire import NativeSessionCheckpointCheck, NativeSessionPrerestore
from shared_context.native_checkpoint import conversation_store_name, segment_key
from shared_context.native_turns import TURNS_STORE, _RECORD_READS
from test_native_profile_snapshot import _v2_baseline
from test_native_turn_wire import submit
from test_native_turns import harness, local_env, native_retry_policy  # noqa: F401

COMMANDS = (NativeSessionCheckpointCheck, NativeSessionPrerestore)


def test_the_v2_baseline_holds_both_commands_outside_the_record_reads():
    mapping = _v2_baseline()["native_request"]["discriminator"]["mapping"]
    for command in COMMANDS:
        assert command().operation in mapping and command().operation not in _RECORD_READS


def _completed(harness, monkeypatch, tmp_path, rid="rid-1"):
    """One completed turn; then a fresh hot copy, as on the destination session, and a count of store restores."""
    assert harness.invoke(submit(rid)).status_code == 202
    cid = harness.wait(rid, "completed").conversation_id

    async def checkpoint_sha():
        async with harness.runtime._store(conversation_store_name(cid)) as store:
            return (await store.get_item("turn:1")).value["checkpoints"][0]["sha256"]

    sha = harness.call(checkpoint_sha)
    cache = str((tmp_path / "OFFLINE-destination-cache").resolve())
    harness.runtime.cache_root = Path("\\\\?\\" + cache if os.name == "nt" else cache)
    restores, read = [], native_checkpoint._read_segments

    async def counted(store, ref, **kwargs):
        if kwargs.get("unknown") != "SDK_STATE_UPLOAD_UNKNOWN":  # a save reads its own upload back; not a restore
            restores.append(ref.sha256)
        return await read(store, ref, **kwargs)

    monkeypatch.setattr(native_checkpoint, "_read_segments", counted)
    return cid, sha, restores


def _etags(harness, cid, rid="rid-1"):
    async def read():
        async with harness.runtime._store(TURNS_STORE) as turns, harness.runtime._store(conversation_store_name(cid)) as conversation:
            return (await turns.get_item("rid:" + rid)).etag, (await conversation.get_item("meta")).etag

    return harness.call(read)


async def _rewrite(harness, cid, key, change):
    async with harness.runtime._store(conversation_store_name(cid)) as store:
        item = await store.get_item(key)
        value = dict(item.value)
        change(value)
        await store.set_item(key, value, if_match=item.etag)


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: command().operation)
@pytest.mark.parametrize("state, expected", [
    ("good", "ok"), ("segment_differs", "unreadable"), ("unknown_codec", "unsupported")])
def test_each_conversation_gets_its_own_result_without_the_sdk(
        harness, monkeypatch, tmp_path, command, state, expected):  # noqa: F811
    cid, sha, restores = _completed(harness, monkeypatch, tmp_path)
    if state == "segment_differs":
        harness.call(_rewrite, harness, cid, segment_key(sha, 0), lambda value: value.update(data="eA=="))
    if state == "unknown_codec":  # written by a newer image: a codec this image does not know (rule 8)
        harness.call(_rewrite, harness, cid, "turn:1", lambda value: value.update(checkpoints=[
            {**value["checkpoints"][0], "codec": "native-session-fs-v9"}]))
    before, calls = _etags(harness, cid), len(harness.sdk.calls)
    response = harness.invoke(command(conversation_ids=(cid,)))
    assert response.status_code == 200, response.text
    assert response.json()["checkpoints"] == {cid: expected} and response.json()["toolboxes"] == 0
    assert len(harness.sdk.calls) == calls and _etags(harness, cid) == before
    # the store is read for the answer; a reference this image refuses reads no segment
    assert restores == ([] if state == "unknown_codec" else [sha])


@pytest.mark.parametrize("prerestored", [True, False])
def test_prerestore_makes_the_next_turn_skip_the_cold_restore(harness, monkeypatch, tmp_path, prerestored):  # noqa: F811
    cid, sha, restores = _completed(harness, monkeypatch, tmp_path)
    if prerestored:
        response = harness.invoke(NativeSessionPrerestore(conversation_ids=(cid,)))
        assert response.status_code == 200 and response.json()["checkpoints"] == {cid: "ok"}, response.text
        assert restores == [sha]
    restores.clear()
    calls = len(harness.sdk.calls)
    assert harness.invoke(submit("rid-2", cid, text="Continue after the switch")).status_code == 202
    assert harness.wait("rid-2", "completed").turn_number == 2
    assert len(harness.sdk.calls) == calls + 1 and harness.sdk.calls[-1][2] is not None  # it still resumed
    assert restores.count(sha) == (0 if prerestored else 1)


def test_prerestore_is_idempotent(harness, monkeypatch, tmp_path):  # noqa: F811
    cid, sha, restores = _completed(harness, monkeypatch, tmp_path)
    before, calls = _etags(harness, cid), len(harness.sdk.calls)
    answers = [harness.invoke(NativeSessionPrerestore(conversation_ids=(cid,))) for _ in range(3)]
    assert [answer.status_code for answer in answers] == [200] * 3
    assert all(answer.json() == answers[0].json() for answer in answers)
    assert answers[0].json()["checkpoints"] == {cid: "ok"}
    assert restores == [sha]  # the repeats take the hot copy
    assert len(harness.sdk.calls) == calls and _etags(harness, cid) == before


def test_the_check_reads_the_store_even_after_a_prerestore(harness, monkeypatch, tmp_path):  # noqa: F811
    cid, sha, restores = _completed(harness, monkeypatch, tmp_path)
    assert harness.invoke(NativeSessionPrerestore(conversation_ids=(cid,))).json()["checkpoints"] == {cid: "ok"}
    harness.call(_rewrite, harness, cid, segment_key(sha, 0), lambda value: value.update(data="eA=="))
    response = harness.invoke(NativeSessionCheckpointCheck(conversation_ids=(cid,)))
    assert response.json()["checkpoints"] == {cid: "unreadable"} and restores == [sha, sha]


def test_a_destination_that_is_not_yet_the_current_sid_still_answers(harness, monkeypatch, tmp_path):  # noqa: F811
    """ADR-0009 4b: before the switch the Agent record names the source SID; a record read would answer 409 here."""
    from shared_context.behavior_native_turn_wire import (
        NATIVE_LOCATOR_BINDING_ROW, NATIVE_LOCATOR_HEADER, NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER, NativePrewarm)
    from shared_context.native_turns import _changed, _json

    cid, _, _ = _completed(harness, monkeypatch, tmp_path)

    async def source_is_current():
        current, etag = await harness.runtime._agent_record()
        async with harness.runtime._store(TURNS_STORE) as store:
            await store.set_item("agent", _json(_changed(current, hosted_session_id="OFFLINE-source-session")),
                                 if_match=etag)

    harness.call(source_is_current)

    def located(command):
        return harness.client.post("/invocations", json=command.model_dump(mode="json"), headers={
            NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, NATIVE_LOCATOR_HEADER: NATIVE_LOCATOR_BINDING_ROW,
            "x-agent-session-id": harness.runtime.identity.session_id, "x-agent-invocation-id": "platform-located"})

    assert located(NativePrewarm()).json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    for command in COMMANDS:
        response = located(command(conversation_ids=(cid,)))
        assert response.status_code == 200 and response.json()["checkpoints"] == {cid: "ok"}, response.text


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: command().operation)
def test_no_conversation_named_checks_nothing(harness, command):  # noqa: F811
    response = harness.invoke(command())
    assert response.status_code == 200, response.text
    assert response.json()["checkpoints"] == {} and harness.sdk.calls == []


def _counted_restores(monkeypatch):
    restores, read = [], native_checkpoint._read_segments

    async def counted(store, ref, **kwargs):
        if kwargs.get("unknown") != "SDK_STATE_UPLOAD_UNKNOWN":
            restores.append(ref.sha256)
        return await read(store, ref, **kwargs)

    monkeypatch.setattr(native_checkpoint, "_read_segments", counted)
    return restores


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: command().operation)
@pytest.mark.parametrize("other", ["never_created", "queued"])
def test_a_conversation_without_a_store_owes_nothing_and_fails_no_other(
        harness, monkeypatch, tmp_path, command, other):  # noqa: F811
    """ADR-0009 §7a (r): never allocated (no State store, or a receipt still queued) owes no checkpoint; per conversation."""
    cid, _, _ = _completed(harness, monkeypatch, tmp_path)
    waiting = "OFFLINE-never-allocated"
    if other == "queued":
        harness.sdk.block = True
        assert harness.invoke(submit("rid-2")).status_code == 202
        harness.wait("rid-2", "started")
        queued = harness.invoke(submit("rid-3"))  # accepted behind rid-2: no turn numbered yet
        assert queued.status_code == 202 and queued.json()["status"] == "accepted", queued.text
        waiting = queued.json()["conversation_id"]
    response = harness.invoke(command(conversation_ids=(cid, waiting)))
    assert response.status_code == 200, response.text
    assert response.json()["checkpoints"] == {cid: "ok", waiting: "ok"}


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: command().operation)
def test_a_first_turn_still_running_owes_no_checkpoint(harness, monkeypatch, command):  # noqa: F811
    restores = _counted_restores(monkeypatch)
    harness.sdk.block = True
    assert harness.invoke(submit()).status_code == 202
    cid = harness.wait("rid-1", "started").conversation_id
    response = harness.invoke(command(conversation_ids=(cid,)))
    assert response.status_code == 200 and response.json()["checkpoints"] == {cid: "ok"}, response.text
    assert restores == []


def test_the_check_verifies_base_ckpt_while_the_last_turn_is_not_completed(harness, monkeypatch, tmp_path):  # noqa: F811
    cid, sha, restores = _completed(harness, monkeypatch, tmp_path)
    harness.sdk.block = True
    assert harness.invoke(submit("rid-2", cid, text="Continue")).status_code == 202
    harness.wait("rid-2", "started")
    restores.clear()
    check = NativeSessionCheckpointCheck(conversation_ids=(cid,))
    assert harness.invoke(check).json()["checkpoints"] == {cid: "ok"} and restores == [sha]
    harness.call(_rewrite, harness, cid, segment_key(sha, 0), lambda value: value.update(data="eA=="))
    assert harness.invoke(check).json()["checkpoints"] == {cid: "unreadable"}


def test_a_prerestore_leaves_a_running_turn_and_its_hot_copy_alone(harness, monkeypatch, tmp_path):  # noqa: F811
    cid, sha, restores = _completed(harness, monkeypatch, tmp_path)
    harness.sdk.block = True
    assert harness.invoke(submit("rid-2", cid, text="Continue")).status_code == 202
    harness.wait("rid-2", "started")
    # stands for the newer checkpoint the running turn keeps here; the one it restored from is gone
    [restored] = Path(harness.runtime.cache_root).glob("*/" + sha + ".json")
    newer = restored.with_name("f" * 64 + ".json")
    restored.rename(newer)
    restores.clear()
    response = harness.invoke(NativeSessionPrerestore(conversation_ids=(cid,)))
    assert response.status_code == 200 and response.json()["checkpoints"] == {cid: "ok"}, response.text
    assert restores == [] and newer.is_file() and not restored.exists()


def test_a_checkpoint_of_another_scope_is_that_conversations_unreadable(harness, monkeypatch, tmp_path):  # noqa: F811
    """403 ASSISTANT_CHECKPOINT_SCOPE (the meta's base_ckpt names another SDK session) is one conversation's answer."""
    cid, _, _ = _completed(harness, monkeypatch, tmp_path)
    harness.sdk.block = True
    assert harness.invoke(submit("rid-2", cid, text="Continue")).status_code == 202
    harness.wait("rid-2", "started")
    harness.call(_rewrite, harness, cid, "meta", lambda value: value.update(base_ckpt={
        **value["base_ckpt"], "sdk_session_id": "OFFLINE-other-sdk-session"}))
    response = harness.invoke(NativeSessionCheckpointCheck(conversation_ids=(cid, "OFFLINE-never-allocated")))
    assert response.status_code == 200, response.text
    assert response.json()["checkpoints"] == {cid: "unreadable", "OFFLINE-never-allocated": "ok"}


def test_a_prerestore_segment_read_is_bounded_like_the_check(harness, monkeypatch, tmp_path):  # noqa: F811
    from shared_context import native_environment

    cid, _, _ = _completed(harness, monkeypatch, tmp_path)
    monkeypatch.setattr(native_environment, "_CHECK_READ_SECONDS", 0.5)
    read = native_checkpoint._read_segments

    class Stalled:
        async def get_item(self, key):
            await asyncio.sleep(3)

    async def stalled(store, ref, **kwargs):
        return await read(Stalled(), ref, **kwargs)

    monkeypatch.setattr(native_checkpoint, "_read_segments", stalled)
    response = harness.invoke(NativeSessionPrerestore(conversation_ids=(cid,)))
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "NATIVE_STATE_STORE_UNKNOWN"