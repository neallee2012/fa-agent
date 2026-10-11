"""Requests other Agents sent to this Agent, read from its own official State store; never an API index."""

from .behavior_native_turn_wire import NativeReceivedItem, NativeReceivedPage, NativeReceivedRead
from .errors import Problem


async def read_received(runtime, command: NativeReceivedRead) -> NativeReceivedPage:
    """One newest-first page of the original receipts; each Agent request opened its own conversation here.

    Uses the pinned agentserver ``list_keys`` page and the existing ``rid:`` receipts, so nothing new is written.
    """
    from .native_turns import TURNS_STORE

    items = []
    async with runtime._store(TURNS_STORE) as store:
        page = await store.list_keys(limit=command.limit, after=command.after)
        if len(page.keys) > command.limit or page.has_more and (not page.last_id or page.last_id == command.after):
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the original receipt page did not advance")
        for key in page.keys:
            if not key.key.startswith("rid:"):
                continue
            receipt = runtime._check_rid(await store.get_item(key.key), key.key[4:]).value.receipt
            source = receipt.source
            # Only a request that opened its own conversation here; replies and notices join existing ones.
            if (source is None or source.kind != "agent_request" or receipt.continuation is not None
                    or receipt.command.conversation_id is not None or receipt.status == "busy_refused"):
                continue
            items.append(NativeReceivedItem(
                request_id=receipt.request_id, conversation_id=receipt.conversation_id,
                state_profile=receipt.state_profile, status=receipt.status,
                sender_agent_id=source.sender.agent_id, message_kind=source.message_kind,
            ))
    return NativeReceivedPage(agent_id=runtime.identity.agent_id, items=tuple(items),
                              next_after=page.last_id if page.has_more else None)
