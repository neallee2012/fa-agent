"""Real host/store fault boundaries: an unknown write never authorizes another SDK execution."""

import copy

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.core.exceptions import ServiceResponseError

from shared_context.behavior_native_turn_wire import NativeCancel
from test_native_response_streaming import sse_events
from test_native_responses import response_submit, responses, stored_rid
from test_native_turns import IDENTITY, local_env


@pytest.mark.parametrize("changed,code", [
    ("rid", "NATIVE_REQUEST_INVALID"), ("cid", "NATIVE_REQUEST_INVALID"),
    ("digest", "NATIVE_REQUEST_INVALID"), ("input", "NATIVE_RID_CONFLICT"),
    ("session", "NATIVE_STATE_SCOPE"), ("background", "NATIVE_REQUEST_INVALID"),
    ("store", "NATIVE_REQUEST_INVALID"),
])
def test_response_headers_input_and_platform_session_cannot_replace_the_saved_submission(responses, changed, code):
    assert responses.invoke(response_submit()).status_code == 202
    original = responses.call(responses.runtime.read, "rid-1")
    before = responses.call(stored_rid, "rid-1")
    body = {"input": original.command.content, "background": True, "store": True,
            "agent_session_id": IDENTITY.session_id}
    headers = {
        "x-agent-session-id": IDENTITY.session_id, "x-client-fa-rid": "rid-1",
        "x-client-fa-cid": original.conversation_id, "x-client-fa-submit-digest": original.submit_digest,
    }
    if changed == "input":
        body["input"] = "Another instruction"
    elif changed in {"background", "store"}:
        body[changed] = False
    elif changed == "session":
        body["agent_session_id"] = "another-session"
    else:
        headers.pop({"rid": "x-client-fa-rid", "cid": "x-client-fa-cid",
                     "digest": "x-client-fa-submit-digest"}[changed])
    response = responses.client.post("/responses", json=body, headers=headers)
    if changed == "store":
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "unsupported_parameter"
        assert response.json()["error"]["param"] == "background"
    else:
        assert response.status_code == 200, response.text
        result = response.json() if body["background"] is False else responses.response_result(response.json()["id"])
        assert result["status"] == "failed"
        assert result["error"]["code"] == code
    after = responses.call(stored_rid, "rid-1")
    assert after.etag == before.etag and after.value == before.value
    assert not responses.sdk.calls


@pytest.mark.parametrize("applied", [False, True], ids=["not-applied", "lost-ack"])
def test_slot_write_uncertainty_keeps_the_same_accepted_intent(responses, monkeypatch, applied):
    assert responses.invoke(response_submit("first")).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "first")).status_code == 200
    first = responses.wait("first", "completed")
    write = FoundryStateStore.set_item
    attempts = []

    async def uncertain(store, key, value, **options):
        if key == "meta" and value.get("inflight_rid") == "following" and not attempts:
            attempts.append(value)
            if applied:
                await write(store, key, value, **options)
            raise ServiceResponseError("OFFLINE slot acknowledgment is unconfirmed")
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", uncertain)
    request = response_submit("following", first.conversation_id)
    response = responses.invoke(request)
    assert response.status_code == 202, response.text
    retained = responses.call(responses.runtime.read, "following")
    assert retained.status == "accepted" and retained.command == request.command
    assert retained.dispatch.state == ("needs_response" if applied else "not_taken")
    assert retained.response_id is None and len(responses.sdk.calls) == 1
    assert responses.invoke(request).status_code == 202
    ready = responses.call(responses.runtime.read, "following")
    assert ready.dispatch.state == "needs_response" and ready.dispatch.previous_response_id == first.response_id
    assert responses.respond(ready).status_code == 200
    responses.wait("following", "completed")
    assert [call[0] for call in responses.sdk.calls] == ["first", "following"]


@pytest.mark.parametrize("applied", [False, True], ids=["not-applied", "lost-ack"])
def test_started_response_id_and_run_token_are_one_cas(responses, monkeypatch, applied):
    write = FoundryStateStore.set_item
    attempted = []

    async def uncertain(store, key, value, **options):
        if key == "rid:rid-1" and value["receipt"]["status"] == "started" and not attempted:
            attempted.append(copy.deepcopy(value))
            if applied:
                await write(store, key, value, **options)
            raise ServiceResponseError("OFFLINE started acknowledgment is unconfirmed")
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", uncertain)
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    created = responses.respond(accepted)
    original = responses.wait("rid-1", "completed" if applied else "unknown")
    assert attempted[0]["run_token"]
    assert attempted[0]["receipt"]["response_id"] == created.json()["id"]
    saved = responses.call(stored_rid, "rid-1")
    if applied:
        assert saved.value["run_token"] == attempted[0]["run_token"]
        assert original.response_id == created.json()["id"]
    else:
        assert original.failure_code == "NATIVE_STATE_STORE_UNKNOWN"
        assert original.response_id is None and saved.value["run_token"] is None
    replay = responses.respond(accepted)
    responses.response_result(replay.json()["id"])
    assert len(responses.sdk.calls) == (1 if applied else 0)
    assert len(attempted) == 1


@pytest.mark.parametrize("checkpoint", ["complete", "missing", "corrupt"])
def test_started_reentry_only_publishes_a_fully_verified_saved_turn(responses, monkeypatch, checkpoint):
    import shared_context.native_turns as native_turns

    write = FoundryStateStore.set_item
    attempts = []
    # Every in-turn save attempt (audit #1 retries) and the re-entry after them lose the completed CAS.
    lost = len(native_turns.SAVE_RETRY_SECONDS) + 2
    monkeypatch.setattr(native_turns, "SAVE_RETRY_SECONDS", (0.01,) * len(native_turns.SAVE_RETRY_SECONDS))

    async def completed_unconfirmed(store, key, value, **options):
        if key == "rid:rid-1" and value["receipt"]["status"] == "completed" and len(attempts) < lost:
            attempts.append(value)
            raise ServiceResponseError("OFFLINE terminal CAS was not applied")
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", completed_unconfirmed)
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    created = responses.respond(accepted)
    failure = responses.response_result(created.json()["id"])
    assert failure["status"] == "failed" and failure["error"]["code"] == "NATIVE_STATE_STORE_UNKNOWN"
    saved = responses.call(responses.runtime.read, "rid-1")
    assert saved.status == "started" and saved.response_id == created.json()["id"]
    assert len(attempts) == lost and len(responses.sdk.calls) == 1
    read = FoundryStateStore.get_item

    async def unavailable(store, key):
        item = await read(store, key)
        if key.startswith("ckpt:") and checkpoint != "complete":
            if checkpoint == "missing":
                return None
            item = copy.deepcopy(item)
            item.value["segment_sha256"] = "0" * 64
        return item

    monkeypatch.setattr(FoundryStateStore, "get_item", unavailable)
    replay = responses.respond(accepted)
    result = responses.response_result(replay.json()["id"])
    assert result["status"] == ("completed" if checkpoint == "complete" else "failed")
    retained = responses.call(responses.runtime.read, "rid-1")
    assert retained.status == ("completed" if checkpoint == "complete" else "unknown")
    if checkpoint != "complete":
        assert retained.failure_code == "SDK_STATE_INCOMPLETE"
    assert retained.response_id == created.json()["id"]
    assert len(responses.sdk.calls) == 1


@pytest.mark.parametrize("sdk_outcome", ["raises", "returns"])
def test_fenced_unknown_write_failure_is_explicit_not_a_successful_receipt(
        responses, monkeypatch, sdk_outcome):
    write = FoundryStateStore.set_item
    attempts = []

    async def unavailable(store, key, value, **options):
        if key == "rid:rid-1" and value["receipt"]["status"] == "unknown":
            attempts.append(value)
            raise ServiceResponseError("OFFLINE unknown CAS unavailable")
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", unavailable)
    responses.sdk.block = sdk_outcome == "raises"
    execute = responses.runtime.executor
    started = []

    async def interrupt(*args):
        started.append(args[0])
        context, signal = responses.contexts["rid-1"][0]
        assert not context.client_cancelled
        signal.set()
        return await execute(*args)

    responses.runtime.executor = interrupt
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    response = responses.respond(accepted, stream=True)
    events = sse_events(response)
    assert events[-1]["type"] == "response.failed"
    failed = events[-1]["response"]
    assert failed["status"] == "failed" and failed["error"]["code"] == "NATIVE_STATE_STORE_UNKNOWN"
    retained = responses.call(responses.runtime.read, "rid-1")
    assert retained == started[0] and retained.failure_code is None
    assert attempts and attempts[0]["receipt"]["failure_code"] == "NATIVE_TURN_UNKNOWN"
    assert len(responses.sdk.calls) == 1
    assert len(attempts) == 1


@pytest.mark.parametrize("sdk_outcome", ["raises", "returns"])
def test_cancel_winning_the_unknown_cas_is_merged_and_the_rid_ends_unknown_not_started(
        responses, monkeypatch, sdk_outcome):
    """Audit #3: the unknown write is merged onto the cancel that won its CAS (same run token and SID), so the RID ends
    ``unknown`` with ``cancel_requested`` kept, instead of staying ``started`` and holding the conversation."""
    write = FoundryStateStore.set_item
    races = []
    unknown_attempts = []

    async def cancel_wins(store, key, value, **options):
        if key == "rid:rid-1" and value["receipt"]["status"] == "unknown":
            unknown_attempts.append(value)
            if not races:
                races.append(True)
                await responses.runtime.cancel(NativeCancel(
                    request_id="rid-1", conversation_id=value["receipt"]["conversation_id"]))
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", cancel_wins)
    execute = responses.runtime.executor
    responses.sdk.block = sdk_outcome == "raises"

    async def interrupt(*args):
        responses.contexts["rid-1"][0][1].set()
        return await execute(*args)

    responses.runtime.executor = interrupt
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    response = responses.respond(accepted, stream=True)
    terminal = sse_events(response)[-1]
    assert terminal["type"] == "response.failed"
    assert terminal["response"]["error"]["code"] == "NATIVE_TURN_UNKNOWN"
    retained = responses.call(responses.runtime.read, "rid-1")
    assert retained.status == "unknown" and retained.cancel_requested
    assert retained.failure_code == "NATIVE_TURN_UNKNOWN" and races == [True]
    assert len(responses.sdk.calls) == 1
    assert len(unknown_attempts) == 2
