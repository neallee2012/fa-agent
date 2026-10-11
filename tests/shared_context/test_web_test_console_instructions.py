"""Local durable-message seam only; no claim of backend/chat integration."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from web_test_console.adapter import ConsoleError
from web_test_console.bridge import Journal
from web_test_console.instructions import InstructionStore, check_content


def test_original_text_and_message_id_survive_restart_without_resending(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path, SimpleNamespace(sha256="a" * 64))
    journal.begin("old", "fetch", {"created_at": "2026-01-01T00:00:00+00:00"})
    with journal.connect() as db:
        old = db.execute("SELECT * FROM intents").fetchall()
    clock = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = InstructionStore(journal, clock)
    message = store.current()
    text = "  請說明這個問題。\n保留換行與表情 🧭  "
    store.freeze(message["request_id"], text)
    body={"request_id": message["request_id"], "content": text}
    store.prepare(body)
    journal.begin(message["request_id"], "submit", {"body":body,"created_at":clock().isoformat()})
    restart = InstructionStore(Journal(path, SimpleNamespace(sha256="a" * 64)), clock)
    assert restart.current()["request_id"] == message["request_id"]
    assert restart.get(message["request_id"])["content"] == text
    with pytest.raises(ConsoleError):
        journal.begin(message["request_id"], "submit", {"body":body,"created_at":clock().isoformat()})
    with pytest.raises(ConsoleError):
        restart.freeze(message["request_id"], text.strip())
    with journal.connect() as db:
        assert db.execute("SELECT * FROM intents WHERE task_id='old'").fetchall() == old
    next_message = restart.new()
    assert next_message["request_id"] != message["request_id"]
    assert restart.new() == next_message
    assert len(restart.all()) == 1
    assert restart.command(message["request_id"])==body


def test_quoted_utf8_limit_preserves_original_not_trimmed_or_whitelisted():
    assert check_content("a" * 8190) == "a" * 8190
    assert check_content("🙂" * 2047 + "ab") == "🙂" * 2047 + "ab"
    assert check_content("  非 metrics 問題\n🙂") == "  非 metrics 問題\n🙂"
    for value in ("a" * 8191, "\n" * 4096 + "x", "🙂" * 2048, " \n\t", None, "\ud800"):
        with pytest.raises(ConsoleError):
            check_content(value)


def test_independent_local_threads_keep_their_own_drafts_and_original_messages(tmp_path):
    journal=Journal(tmp_path/"multi.sqlite3",SimpleNamespace(sha256="a"*64))
    clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc)
    store=InstructionStore(journal,clock)
    first=store.current()
    store.freeze(first["request_id"],"原對話A")
    second=store.new_thread()
    store.freeze(second["request_id"],"獨立對話B")
    assert first["thread_id"]!=second["thread_id"]
    assert store.current(first["thread_id"])["content"]=="原對話A"
    assert store.current(second["thread_id"])["content"]=="獨立對話B"
    following=store.new(first["thread_id"])
    assert following["thread_id"]==first["thread_id"]
    assert following["request_id"] not in {first["request_id"],second["request_id"]}
    assert store.current(second["thread_id"])["request_id"]==second["request_id"]
    restarted=InstructionStore(journal,clock)
    assert restarted.current(first["thread_id"])==following
    assert restarted.all(first["thread_id"])[0]["content"]=="原對話A"
    assert restarted.all(second["thread_id"])[0]["content"]=="獨立對話B"


def test_confirmed_reset_creates_one_new_thread_and_keeps_original_messages(tmp_path):
    journal = Journal(tmp_path / "reset.sqlite3", SimpleNamespace(sha256="a"*64))
    clock = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = InstructionStore(journal, clock)
    old = store.current()
    store.freeze(old["request_id"], "Original retained conversation")
    journal.begin(old["thread_id"], "reset", {
        "conversation_id": "original-conversation", "body": {"request_id": "reset-once"},
        "created_at": clock().isoformat()})
    receipt = {"conversation_id": "original-conversation", "reset_request_id": "reset-once",
        "archived": True, "hosted_session_id": "fa-chat-old", "job_id": "old-job", "session_stop": "confirmed"}
    fresh = store.complete_reset(old["thread_id"], receipt)
    assert fresh["thread_id"] != old["thread_id"] and fresh["content"] is None
    assert store.get(old["request_id"])["content"] == "Original retained conversation"
    assert store.complete_reset(old["thread_id"], receipt) == fresh
    assert len(store.threads()) == 2
