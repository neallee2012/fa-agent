"""The workbench's 重設對話 on a legacy (non-native) thread: one journaled reset POST, replay, refusals, unknowns.

The console, journal, thread store and HTTP route are real. The two conversation reads and the backend answer are
local doubles: no legacy-lane conversation harness exists, and the reset contract sits in the bridge.
"""

import asyncio
import json

import pytest

from web_test_console.adapter import ConsoleError, canonical, sha
from web_test_console.freeform import ConversationProjection
from test_web_test_console_native import (
    instruction_models, jwk_client, native_api, native_console, signing_key, synthetic_standing_v1,
)

CID = "conversation-reset-1"
SESSION = "fa-chat-original"


def _reply(request_id, **changes):
    return {"conversation_id": CID, "archived": True, "reset_request_id": request_id, "hosted_session_id": SESSION,
            "job_id": "original-job", "session_stop": "confirmed", **changes}


@pytest.fixture
def second_loop():
    """Made before the startup fixture forbids sockets: the first UI's portal already runs its shared loop."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def legacy(second_loop, native_console, monkeypatch):
    c = native_console
    ui = c.open(enabled=False, path=c.tmp / "legacy.sqlite3", loop_factory=lambda: second_loop)
    console = ui.console
    thread = console.instructions.store.current()["thread_id"]
    assert not console.native_thread(thread)
    projection = ConversationProjection(conversation_id=CID, sdk_session_id=None, turns=(), can_follow_up=False,
                                        follow_up_blocker=None, hosted_session_id=SESSION, can_reset=True)
    case = type("Case", (), {})()
    case.ui, case.console, case.thread, case.projection, case.sent, case.answers = ui, console, thread, projection, [], []
    case.conversation = {thread: CID}
    monkeypatch.setattr(console.instructions, "progress_conversation", lambda thread_id: case.conversation.get(thread_id))
    monkeypatch.setattr(console.instructions, "refresh_conversation",
                        lambda token, cid: case.projection if cid == CID else pytest.fail(cid))

    def send(method, path, body, token, **kwargs):
        case.sent.append((method, path, body))
        answer = case.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return (answer if isinstance(answer, bytes) else json.dumps(answer).encode()), {}
    monkeypatch.setattr(console.instructions.adapter, "send", send)
    case.request_id = sha(canonical(["reset", console.config.console_id, thread, CID]))
    case.reset = lambda thread_id=None: ui.client.post(
        f"/api/instructions/{thread_id or thread}/reset", json={}, headers=ui.headers)
    return case


def _error(response):
    return response.status_code, response.json()["error"]["code"]


def _journal(case):
    return case.console.journal.get(case.thread, "reset")


def test_a_native_thread_cannot_be_reset_here(native_console):
    c = native_console
    thread = c.ui.console.instructions.store.current()["thread_id"]
    assert c.ui.console.native_thread(thread)
    response = c.ui.client.post(f"/api/instructions/{thread}/reset", json={}, headers=c.ui.headers)
    assert _error(response) == (403, "ACTION_NOT_ALLOWED")


def test_a_thread_without_a_conversation_has_nothing_to_reset(legacy):
    legacy.conversation.clear()
    assert _error(legacy.reset()) == (403, "ACTION_NOT_ALLOWED")
    assert _journal(legacy) is None and legacy.sent == []


def test_a_console_without_instructions_offers_no_reset(legacy, monkeypatch):
    monkeypatch.setattr(legacy.console, "instructions", None)
    assert _error(legacy.reset()) == (403, "ACTION_NOT_ALLOWED") and legacy.sent == []
