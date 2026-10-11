"""B's own list of conversations other Agents opened in it: the official State store page and its API route."""

import json

from shared_context.behavior_native_turn_wire import NativeReceivedPage, NativeReceivedRead
from test_native_turn_api import PREFIX, native_api  # noqa: F401
from test_native_turn_wire import agent_receive, submit
from test_native_turns import harness, local_env, native_retry_policy  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401


def received(harness, **selectors):
    response = harness.invoke(NativeReceivedRead(**selectors))
    assert response.status_code == 200, response.text
    return NativeReceivedPage.model_validate_json(response.content)


def test_received_lists_only_agent_requests_newest_first_from_the_original_receipts(harness):
    """Stage 2026-10-05: B's reply showed under A, but B listed only its owner's conversations."""
    harness.invoke(submit("human-rid"))
    human = harness.wait("human-rid", "completed")
    for rid in ("message-1", "message-2"):
        assert harness.invoke(agent_receive(rid=rid)).status_code == 202
        harness.wait(rid, "completed")
    # A message delivered into an existing conversation did not open one here.
    assert harness.invoke(agent_receive(rid="into-existing", cid=human.conversation_id)).status_code == 202
    harness.wait("into-existing", "completed")
    page = received(harness)
    assert page.agent_id == harness.runtime.identity.agent_id and page.next_after is None
    assert [item.request_id for item in page.items] == ["message-2", "message-1"]
    first = harness.call(harness.runtime.read, "message-1")
    assert page.items[1].model_dump() == {
        "request_id": "message-1", "conversation_id": first.conversation_id, "state_profile": first.state_profile,
        "status": "completed", "sender_agent_id": "sender-agent", "message_kind": "request"}
    assert page.items[0].conversation_id != page.items[1].conversation_id
    # Metadata only: the Agent brief stays in the original receipt and conversation reads.
    assert "Original Agent message" not in page.model_dump_json()

    seen, after = [], None
    for _ in range(8):
        page = received(harness, limit=1, **({} if after is None else {"after": after}))
        seen.extend(item.request_id for item in page.items)
        if page.next_after is None:
            break
        after = page.next_after
    assert seen == ["message-2", "message-1"] and page.next_after is None


def test_received_route_reads_one_owner_page_and_refuses_a_page_that_does_not_advance(native_api):
    case = native_api
    agent = case.remote.target.agent_id
    pages = {None: NativeReceivedPage(agent_id=agent, items=(), next_after="cursor-1"),
             "cursor-1": NativeReceivedPage(agent_id=agent, items=(), next_after="cursor-1")}
    case.remote.reply_override = lambda command: pages[command.after]
    first = case.client.get(PREFIX + "/received", headers=case.headers)
    assert first.status_code == 200, first.text
    assert first.json() == {"agent_id": agent, "items": [], "next_after": "cursor-1"}
    stuck = case.client.get(PREFIX + "/received?after=cursor-1&limit=5", headers=case.headers)
    assert stuck.status_code == 502 and stuck.json()["error"]["code"] == "NATIVE_STATE_INVALID"
    assert [command.model_dump(mode="json") for _, command in case.remote.calls
            if command.operation == "received"] == [
        {"operation": "received", "after": None, "limit": 50},
        {"operation": "received", "after": "cursor-1", "limit": 5}]
    case.remote.reply_override = lambda command: NativeReceivedPage(agent_id="another-agent", items=(), next_after=None)
    other = case.client.get(PREFIX + "/received", headers=case.headers)
    assert other.status_code == 502 and other.json()["error"]["code"] == "NATIVE_STATE_SCOPE"
    for query in ("?limit=0", "?limit=51", "?unexpected=1"):
        assert case.client.get(PREFIX + "/received" + query, headers=case.headers).status_code == 422
    assert all(command.operation not in {"submit", "agent_receive"} for _, command in case.remote.calls)
    assert json.loads(first.text)["items"] == []
