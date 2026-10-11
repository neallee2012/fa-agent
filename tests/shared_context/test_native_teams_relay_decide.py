"""Teams relay approval commands against the real HITL service (fake Table only): code lookup and the one decision."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from shared_context.errors import Problem
from shared_context.native_hitl_attention import approval_code, find_approval_ids
from shared_context.native_teams_relay import RelayDecision, RelayReply, TeamsRelay
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_hitl import hitl, read  # noqa: F401
from test_native_hitl_attention import attention, pending  # noqa: F401

NO_CARD = RelayReply(state="failed", text="這個代碼沒有對應的待批准項目（可能已處理或過期）。")


def decision(code, choice="allow_once", activity_id="act-2"):
    return RelayDecision(activity_id=activity_id, chat_id="teams-chat", user_oid="o", choice=choice, code=code)


def service_of(c):
    return TeamsRelay(c.composition, {}, None, hitl=c.service)


def test_a_code_finds_the_card_before_and_after_it_is_decided(attention):
    c = attention
    _, a = pending(c, "native-a", c.agent, "rid-a")
    _, b = pending(c, "native-a", c.agent, "rid-b", call_id="tool-two")
    code = approval_code(a["pending_id"])
    assert find_approval_ids(c.service, c.owner, code) == [a["pending_id"]]
    assert find_approval_ids(c.service, c.owner, "%06d" % ((int(code) + 1) % 1_000_000)) == []
    outcome = service_of(c)._decide(c.owner, "native-a", "cid-one", decision(code))
    assert (outcome.agent_id, outcome.conversation_id, outcome.request_id) == ("native-a", "cid-one", "rid-a")
    assert find_approval_ids(c.service, c.owner, code) == [a["pending_id"]]
    assert read(c, a["pending_id"]).json()["status"] == "allow"
    assert read(c, b["pending_id"]).json()["status"] == "pending"


def test_the_same_choice_again_is_the_same_turn_and_the_other_choice_is_already_handled(attention):
    c = attention
    _, a = pending(c, "native-a", c.agent, "rid-a")
    code, relay = approval_code(a["pending_id"]), service_of(c)
    first = relay._decide(c.owner, "native-a", "cid-one", decision(code, "deny"))
    again = relay._decide(c.owner, "native-a", "cid-one", decision(code, "deny"))
    assert first == again and first.request_id == "rid-a"
    opposite = relay._decide(c.owner, "native-a", "cid-one", decision(code, "allow_once"))
    assert opposite == RelayReply(state="failed", text="這個項目已處理過。")
    assert read(c, a["pending_id"]).json()["status"] == "deny"


def test_an_expired_card_and_a_card_of_another_conversation_are_refused(attention):
    c = attention
    _, a = pending(c, "native-a", c.agent, "rid-a")
    code, relay = approval_code(a["pending_id"]), service_of(c)
    assert relay._decide(c.owner, "native-a", "cid-other", decision(code)) == NO_CARD
    c.now += timedelta(minutes=11)
    assert relay._decide(c.owner, "native-a", "cid-one", decision(code)) == RelayReply(
        state="failed", text="這個項目已過期。")
    assert read(c, a["pending_id"]).json()["status"] == "pending"


def test_a_lost_commit_on_a_still_pending_card_asks_for_the_message_again(attention):
    c = attention
    _, a = pending(c, "native-a", c.agent, "rid-a")
    code, relay = approval_code(a["pending_id"]), service_of(c)
    submit, count = c.table.submit_transaction, 0

    def unrelated_write_wins_the_decision_commit(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            c.table.before_submit = lambda: pending(c, "native-a", c.agent, "rid-b", call_id="tool-two")
        return submit(*args, **kwargs)

    c.table.submit_transaction = unrelated_write_wins_the_decision_commit
    assert relay._decide(c.owner, "native-a", "cid-one", decision(code)) == RelayReply(
        state="failed", text="這次沒有送成，請再傳一次同樣的指令。")
    assert read(c, a["pending_id"]).json()["status"] == "pending"
    c.table.submit_transaction = submit
    with pytest.raises(Problem) as unknown:
        relay._decide(c.owner, "native-a", "cid-one", decision(code))
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    outcome = relay._decide(c.owner, "native-a", "cid-one", decision(code, activity_id="act-3"))
    assert (outcome.agent_id, outcome.conversation_id, outcome.request_id) == ("native-a", "cid-one", "rid-a")
    assert read(c, a["pending_id"]).json()["status"] == "allow"


def test_the_commands_cannot_express_a_rule_change():
    with pytest.raises(ValidationError):
        RelayDecision.model_validate({**decision("123456").model_dump(), "kind": "rules_change"})
    assert "rules_change" not in RelayDecision.model_fields
    assert set(RelayDecision.model_fields["choice"].annotation.__args__) == {"allow_once", "deny"}
