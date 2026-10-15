"""Teams relay catch-up: a reply the relay stopped waiting for comes with the owner's next final reply and is cleared
once the relay acknowledges it (unacknowledged, it comes again: a rare repeat, never a loss); Hosted/platform fake and
the in-memory Table only."""

import hashlib

import httpx
import pytest

from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeReceipt
from shared_context.behavior_team_registry import resolve_agent_scope
from shared_context.errors import Problem
from shared_context.native_teams_relay import CATCH_UP_CUT, CHAT_PARTITION, REPLY_UTF16_BYTES, Chat
from test_native_teams_relay import RELAY, STRANGER, complete, relay, rid  # noqa: F401
from test_native_responses_api import responses_api  # noqa: F401
from test_native_turn_api import native_api  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401

HEADING = "先前的回覆（剛完成）："
RUNNING = "這一輪還在跑；完成後，回覆會附在你下一則訊息的回覆前面，也可以先到工作台看。"


@pytest.fixture
def chat(relay, monkeypatch):
    """The relay fixture with one result text per turn (the shared fake has one text for every turn)."""
    env, texts = relay.env, {}
    inner = env.case.foundry.http._transport.handler

    def hosted(request):
        if request.url.path.endswith("/invocations"):
            command = NATIVE_REQUEST.validate_json(request.content)
            if command.operation == "turn_status" and command.request_id in texts:
                env.case.remote.single_text = texts[command.request_id]
        return inner(request)

    monkeypatch.setattr(env.case.foundry.http, "_transport", httpx.MockTransport(hosted))

    def finish(activity, text, chat="conv_teams_a"):
        texts[rid(activity, chat)] = text
        complete(env, rid(activity, chat), text)

    def fail(activity, chat="conv_teams_a"):
        original = env.case.remote.records[rid(activity, chat)]
        env.case.remote.records[rid(activity, chat)] = NativeReceipt.model_validate(
            {**original.model_dump(), "status": "unknown", "dispatch": None})

    def later(seconds=200):
        relay.now[0] += seconds

    agent_id = env.case.remote.target.agent_id
    key = "tp-" + hashlib.sha256(f"{RELAY}|{agent_id}|{relay.owner}".encode()).hexdigest()[:40]

    def pending():
        [row] = [value.payload for (_, row), value in relay.records.data.items() if row == key]
        return [entry["rid"] for entry in row["pending"]]

    relay.finish, relay.fail, relay.later, relay.pending = finish, fail, later, pending
    relay.acks = True  # This relay acknowledges carried replies (X-FA-Relay-Ack: 1).
    return relay


def _chat_key(chat):
    return "tc-" + hashlib.sha256(f"{RELAY}|{chat}".encode()).hexdigest()[:40]


def ask(c, activity, text, answer, **extra):
    """One Teams message the relay waits for: its first poll runs the turn, the turn ends, the next poll answers."""
    assert c.post(activity, text, **extra).json()["state"] == "running"
    c.finish(activity, answer, **extra)
    return c.post(activity, text, **extra).json()


def test_a_reply_past_the_wait_comes_once_with_the_next_message(chat):
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    chat.later()  # The relay stopped at 110 s and said so; the turn ends afterwards.
    chat.finish("act-1", "三件事。")
    reply = ask(chat, "act-2", "下一步？", "先寫測試。")
    assert reply == {"state": "done", "text": f"{HEADING}\n「今天的待辦？」\n三件事。\n\n——\n先寫測試。",
                     "confirm": [rid("act-2"), rid("act-1")]}
    # The relay's next poll of the same message (its response was lost) gets the very same text.
    assert chat.post("act-2", "下一步？").json() == reply
    assert chat.ack(reply["confirm"]).json() == {"cleared": 2}
    assert chat.pending() == []
    chat.later()
    third = ask(chat, "act-3", "還有嗎？", "沒有了。")
    assert third == {"state": "done", "text": "沒有了。", "confirm": [rid("act-3")]}


def test_a_relay_that_never_acknowledges_gets_todays_replies_and_nothing_is_recorded(chat):
    chat.acks = False  # A relay deployed before catch-up: no X-FA-Relay-Ack header.
    assert chat.post("act-1", "今天的待辦？").json() == {"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}
    chat.later()
    chat.finish("act-1", "三件事。")
    assert ask(chat, "act-2", "下一步？", "先寫測試。") == {"state": "done", "text": "先寫測試。"}
    assert ask(chat, "act-3", "還有嗎？", "沒有了。") == {"state": "done", "text": "沒有了。"}
    assert [key[:3] for _, key in chat.records.data] == ["tc-"]
    # The same relay after its upgrade: nothing old is replayed, its own late replies are carried from now on.
    chat.acks = True
    assert chat.post("act-4", "再一件？").json() == {"state": "running", "text": RUNNING}
    chat.later()
    chat.finish("act-4", "好了。")
    assert ask(chat, "act-5", "然後？", "休息。")["text"] == f"{HEADING}\n「再一件？」\n好了。\n\n——\n休息。"


def test_two_waiting_replies_come_in_the_order_they_were_sent(chat):
    assert chat.post("act-1", "第一件？").json()["state"] == "running"
    assert chat.post("act-2", "第二件？").json()["state"] == "running"
    chat.later()
    chat.finish("act-1", "一。")
    chat.fail("act-2")
    reply = ask(chat, "act-3", "第三件？", "三。")
    assert reply["text"] == (f"{HEADING}\n「第一件？」\n一。\n\n「第二件？」\n（這一則沒有完成，請到工作台查看。）\n\n——\n三。")
    assert reply["confirm"] == [rid("act-3"), rid("act-1"), rid("act-2")]


def test_a_reply_still_running_stays_recorded_and_a_timed_out_message_carries_everything(chat):
    assert chat.post("act-1", "慢的那件？").json()["state"] == "running"
    chat.later()
    reply = ask(chat, "act-2", "快的那件？", "好了。")
    assert reply == {"state": "done", "text": "好了。", "confirm": [rid("act-2")]}
    assert chat.ack(reply["confirm"]).json() == {"cleared": 1}
    assert chat.pending() == [rid("act-1")]
    chat.finish("act-1", "慢的也好了。")
    chat.later()
    # The next message runs past the wait too: a running reply carries nothing and the relay shows its own text.
    assert chat.post("act-3", "再一件？").json() == {"state": "running", "text": RUNNING}
    assert chat.pending() == [rid("act-1"), rid("act-3")]
    chat.later()
    chat.finish("act-3", "第三件好了。")
    reply = ask(chat, "act-4", "都好了嗎？", "都好了。")
    assert reply["text"] == (f"{HEADING}\n「慢的那件？」\n慢的也好了。\n\n「再一件？」\n第三件好了。\n\n——\n都好了。")
    assert reply["confirm"] == [rid("act-4"), rid("act-1"), rid("act-3")]


def test_an_unacknowledged_reply_is_kept_and_offered_again_after_its_lease(chat):
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    chat.later()
    chat.finish("act-1", "三件事。")
    first = ask(chat, "act-2", "下一步？", "先寫測試。")
    assert first["confirm"] == [rid("act-2"), rid("act-1")]
    # No acknowledgement: the delivery is unknown, so both records stay.
    assert chat.pending() == [rid("act-1"), rid("act-2")]
    chat.later(10)
    # While act-2's relay may still be handing it on, another message does not repeat it.
    third = ask(chat, "act-3", "然後？", "然後休息。")
    assert third == {"state": "done", "text": "然後休息。", "confirm": [rid("act-3")]}
    assert chat.ack(third["confirm"]).json() == {"cleared": 1}
    chat.later()
    again = ask(chat, "act-4", "還有嗎？", "沒有了。")
    assert again["text"] == (f"{HEADING}\n「今天的待辦？」\n三件事。\n\n「下一步？」\n先寫測試。\n\n——\n沒有了。")
    assert set(again["confirm"]) == {rid("act-4"), rid("act-1"), rid("act-2")}
    assert chat.ack(again["confirm"]).json() == {"cleared": 3}
    assert chat.pending() == []


def test_a_new_conversation_still_gets_the_reply_and_no_other_caller_clears_it(chat):
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    chat.later()
    chat.finish("act-1", "三件事。")
    assert chat.ack([rid("act-1")], user=STRANGER).status_code == 403
    assert chat.ack([rid("act-1")], oid=STRANGER).status_code == 403
    assert chat.post("act-8", "別人的訊息", user=STRANGER).json()["state"] == "refused"
    assert chat.pending() == [rid("act-1")]
    # /foundry_new_preview started a new platform conversation: the owner's late reply comes with its first message.
    other = ask(chat, "act-9", "新對話", "這裡是 B。", chat="conv_teams_b")
    assert other == {"state": "done", "text": f"{HEADING}\n「今天的待辦？」\n三件事。\n\n——\n這裡是 B。",
                     "confirm": [rid("act-9", "conv_teams_b"), rid("act-1")]}
    assert chat.ack(other["confirm"], chat="conv_teams_b").json() == {"cleared": 2}
    assert chat.pending() == []
    # Every row lives in this Agent's Team partition only; another Team's relay has its own partition and key.
    composition = chat.env.case.composition
    scope = resolve_agent_scope(composition.runtime.store, composition.config, chat.env.case.remote.target.agent_id)
    partitions = {partition for partition, _ in chat.records.data}
    assert partitions == {CHAT_PARTITION.format(tenant=scope.tenant_id, team=scope.team_id)}
    assert sorted(row[:3] for _, row in chat.records.data) == ["tc-", "tc-", "tp-"]


def test_catch_up_never_changes_the_teams_chat_row_an_older_api_reads(chat):
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    [(partition, row)] = [place for place in chat.records.data if place[1] == _chat_key("conv_teams_a")]
    before = chat.records.get(partition, row)
    chat.later()
    chat.finish("act-1", "三件事。")
    reply = ask(chat, "act-2", "下一步？", "先寫測試。")
    assert reply["confirm"] == [rid("act-2"), rid("act-1")]
    assert chat.ack(reply["confirm"]).json() == {"cleared": 2}
    after = chat.records.get(partition, row)
    # Same bytes and ETag; the exact key set the API before catch-up checks (else 502 TEAMS_CHAT_INVALID).
    assert (after.payload, after.etag) == (before.payload, before.etag)
    assert set(after.payload) == {"schema", "key", "agent_id", "conversation_id"}
    composition = chat.env.case.composition
    agent_id = chat.env.case.remote.target.agent_id
    scope = resolve_agent_scope(composition.runtime.store, composition.config, agent_id)
    assert Chat(chat.records, scope, agent_id, row).conversation_id() == after.payload["conversation_id"]


def test_a_concurrent_write_during_clearing_keeps_both_changes(chat, monkeypatch):
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    chat.finish("act-1", "三件事。")
    done = chat.post("act-1", "今天的待辦？").json()
    assert done["confirm"] == [rid("act-1")]
    replace, raced = chat.records.replace, []

    def concurrent_poll_first(team, row, payload, etag):
        if not raced:
            # Between this ack's read and write, act-2's first poll records it (the ack's ETag is now stale).
            raced.append(True)
            current = chat.records.get(team, row)
            replace(team, row, {**current.payload, "pending": current.payload["pending"] + [
                {"rid": rid("act-2"), "to": rid("act-2"), "at": chat.now[0]}]}, current.etag)
        return replace(team, row, payload, etag)

    monkeypatch.setattr(chat.records, "replace", concurrent_poll_first)
    assert chat.ack(done["confirm"]).json() == {"cleared": 1}
    assert raced and chat.pending() == [rid("act-2")]


def test_a_reply_another_message_took_first_is_not_shown_twice(chat, monkeypatch):
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    chat.later()
    chat.finish("act-1", "三件事。")
    assert chat.post("act-3", "然後？").json()["state"] == "running"
    chat.finish("act-3", "然後休息。")
    replace, raced = chat.records.replace, []

    def other_message_first(team, row, payload, etag):
        if not raced and any(entry["to"] == rid("act-3") and entry["rid"] == rid("act-1")
                             for entry in payload.get("pending", [])):
            # act-2's final reply leased act-1 between this message's read and its own lease write.
            raced.append(True)
            current = chat.records.get(team, row)
            replace(team, row, {**current.payload, "pending": [
                {**entry, "to": rid("act-2"), "at": chat.now[0]} if entry["rid"] == rid("act-1") else entry
                for entry in current.payload["pending"]]}, current.etag)
        return replace(team, row, payload, etag)

    monkeypatch.setattr(chat.records, "replace", other_message_first)
    reply = chat.post("act-3", "然後？").json()
    assert raced and reply == {"state": "done", "text": "然後休息。", "confirm": [rid("act-3")]}


def test_a_long_earlier_reply_is_cut_with_a_workbench_pointer_and_the_rest_waits(chat):
    assert chat.post("act-1", "長的那件？").json()["state"] == "running"
    assert chat.post("act-2", "短的那件？").json()["state"] == "running"
    chat.later()
    chat.finish("act-1", "長" * 13_000)  # 26,000 UTF-16 bytes: more than one reply may carry.
    chat.finish("act-2", "短。")
    reply = ask(chat, "act-3", "第三件？", "三。")
    text = reply["text"]
    assert len(text.encode("utf-16-le")) <= REPLY_UTF16_BYTES
    assert CATCH_UP_CUT in text and "「短的那件？」" not in text and text.endswith("\n\n——\n三。")
    assert "還有更早的回覆，會附在你下一則訊息的回覆裡，也可以到工作台查看。" in text
    assert reply["confirm"] == [rid("act-3"), rid("act-1")]
    assert chat.ack(reply["confirm"]).json() == {"cleared": 2}
    nxt = ask(chat, "act-4", "第四件？", "四。")
    assert nxt["text"] == f"{HEADING}\n「短的那件？」\n短。\n\n——\n四。"


APPROVAL = "這一步需要你批准（shell）。允許請回覆「允許 301355」，拒絕請回覆「拒絕 301355」；也可以到工作台處理。"


def test_a_recorded_turn_that_waits_for_approval_shows_its_codes_until_handled(chat):
    assert chat.post("act-1", "寄信給 Neal？").json()["state"] == "running"
    chat.later()
    chat.hitl.card("a" * 64, rid("act-1"))  # After the relay stopped waiting, the turn asks for approval.
    reply = ask(chat, "act-2", "下一步？", "先寫測試。")
    assert reply == {"state": "done", "text": f"先前的訊息還在等你處理：\n「寄信給 Neal？」\n{APPROVAL}\n\n——\n先寫測試。",
                     "confirm": [rid("act-2")]}
    assert chat.ack(reply["confirm"]).json() == {"cleared": 1}
    assert chat.pending() == [rid("act-1")]
    again = ask(chat, "act-3", "還有嗎？", "沒有了。")
    assert again["text"] == f"先前的訊息還在等你處理：\n「寄信給 Neal？」\n{APPROVAL}\n\n——\n沒有了。"
    chat.ack(again["confirm"])
    # 「允許 301355」 handles the card; the turn still runs, so its reply comes with a later message.
    assert chat.decide("301355", activity="act-4").json() == {"state": "running", "text": RUNNING}
    chat.finish("act-1", "寄好了。")
    chat.later()
    last = ask(chat, "act-5", "好了嗎？", "好了。")
    assert last["text"] == f"{HEADING}\n「寄信給 Neal？」\n寄好了。\n\n——\n好了。"
    assert last["confirm"] == [rid("act-5"), rid("act-1")]


def test_a_fixed_decision_answer_is_never_buried_under_earlier_replies(chat):
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    chat.later()
    chat.finish("act-1", "三件事。")
    no_card = chat.decide("999999", activity="act-2").json()
    assert no_card == {"state": "failed", "text": "這個代碼沒有對應的待批准項目（可能已處理或過期）。"}
    assert chat.pending() == [rid("act-1")]


def test_approval_codes_and_a_decision_result_come_before_earlier_replies(chat):
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    chat.later()
    chat.finish("act-1", "三件事。")
    assert chat.post("act-2", "寄信？").json()["state"] == "running"
    chat.hitl.card("a" * 64, rid("act-2"))
    asking = chat.post("act-2", "寄信？").json()
    assert asking == {"state": "needs_approval", "text": f"{APPROVAL}\n\n——\n{HEADING}\n「今天的待辦？」\n三件事。",
                      "confirm": [rid("act-1")]}
    assert chat.ack(asking["confirm"]).json() == {"cleared": 1}
    assert chat.post("act-3", "另一件？").json()["state"] == "running"
    chat.later()
    chat.finish("act-3", "另一件好了。")
    denied = chat.decide("301355", "deny", activity="act-4").json()
    assert denied == {"state": "done", "text": f"已拒絕，這一步沒有執行。\n\n——\n{HEADING}\n「另一件？」\n另一件好了。",
                      "confirm": [rid("act-3")]}


def test_an_approval_code_carried_into_a_new_conversation_works_there(chat):
    assert chat.post("act-1", "寄信給 Neal？").json()["state"] == "running"
    chat.later()
    chat.hitl.card("a" * 64, rid("act-1"))  # A card of conv_teams_a's conversation.
    shown = ask(chat, "act-2", "新對話", "這裡是 B。", chat="conv_teams_b")
    assert shown["text"] == f"先前的訊息還在等你處理：\n「寄信給 Neal？」\n{APPROVAL}\n\n——\n這裡是 B。"
    # 「允許 301355」 typed where the code was shown decides that card, not 「這個代碼沒有對應的待批准項目」.
    assert chat.decide("301355", activity="act-3", chat="conv_teams_b").json() == {"state": "running", "text": RUNNING}
    [(_, command)] = chat.hitl.submitted
    assert command.pending_id == "a" * 64 and command.choice == "allow_once"


def test_an_approval_code_as_the_first_message_of_a_new_conversation_works(chat):
    assert chat.post("act-1", "寄信給 Neal？").json()["state"] == "running"
    chat.later()
    chat.hitl.card("a" * 64, rid("act-1"))
    rows = sorted(chat.records.data)
    # /foundry_new_preview, then the code at once: no TeamsChat row for this chat yet, still not 「找不到這段對話」.
    denied = chat.decide("301355", "deny", activity="act-2", chat="conv_teams_c").json()
    assert denied == {"state": "done", "text": "已拒絕，這一步沒有執行。"}
    assert len(chat.hitl.submitted) == 1 and sorted(chat.records.data) == rows


def test_a_lower_cap_keeps_the_newest_records_instead_of_locking_the_owner_out(chat, monkeypatch):
    for n in range(1, 7):
        assert chat.post(f"act-{n}", f"第{n}件？").json()["state"] == "running"
    assert len(chat.pending()) == 6
    monkeypatch.setattr("shared_context.native_teams_relay.PENDING_MAX", 4)  # Neal lowers the cap from 8 to 4.
    chat.later()
    for n in range(1, 7):
        chat.finish(f"act-{n}", f"{n}。")
    reply = ask(chat, "act-7", "第七件？", "七。")
    # Recording act-7 keeps the newest four (act-4..act-7); act-1..act-3 stay in the workbench.
    assert reply["text"] == f"{HEADING}\n「第4件？」\n4。\n\n「第5件？」\n5。\n\n「第6件？」\n6。\n\n——\n七。"
    assert reply["confirm"] == [rid("act-7"), rid("act-4"), rid("act-5"), rid("act-6")]
    assert chat.ack(reply["confirm"]).json() == {"cleared": 4}
    assert chat.pending() == []


def test_a_record_older_than_a_day_is_not_shown_as_just_finished(chat):
    # E.g. recorded, then the relay was rolled back (no acks) and upgraded again a day later.
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    chat.finish("act-1", "三件事。")
    chat.later(24 * 3600)
    reply = ask(chat, "act-2", "下一步？", "先寫測試。")
    assert reply == {"state": "done", "text": "先寫測試。", "confirm": [rid("act-2")]}
    assert chat.pending() == [rid("act-2")]  # act-1 dropped from the row; its reply stays in the workbench.


@pytest.mark.parametrize("code", ["TABLE_UNAVAILABLE", "ETAG_CONFLICT"])
def test_a_failed_record_write_never_fails_the_turns_own_reply(chat, monkeypatch, code):
    create, replace = chat.records.create, chat.records.replace

    def failing(write):
        def wrapped(team, row, *args):
            if row.startswith("tp-"):
                raise Problem(503 if code == "TABLE_UNAVAILABLE" else 412, code, "synthetic TeamsPending write failure")
            return write(team, row, *args)
        return wrapped

    monkeypatch.setattr(chat.records, "create", failing(create))
    monkeypatch.setattr(chat.records, "replace", failing(replace))
    # Not recorded, so no promise about the next message: the plain running text points to the workbench.
    assert chat.post("act-1", "今天的待辦？").json() == {"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}
    chat.finish("act-1", "三件事。")
    assert chat.post("act-1", "今天的待辦？").json() == {"state": "done", "text": "三件事。"}


def test_a_card_neither_in_this_conversation_nor_recorded_is_still_not_found(chat):
    assert chat.post("act-1", "今天的待辦？").json()["state"] == "running"
    chat.hitl.card("c" * 64, rid("act-9"), conversation_id="conv-someone-else")
    no_card = chat.decide("698122", activity="act-2").json()
    assert no_card == {"state": "failed", "text": "這個代碼沒有對應的待批准項目（可能已處理或過期）。"}
    assert chat.decide("698122", activity="act-3", chat="conv_teams_c").json() == no_card
    assert chat.hitl.submitted == []
