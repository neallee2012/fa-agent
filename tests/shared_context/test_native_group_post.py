"""NativeGroupStore.post refusals, reservations, deferred finals and write recovery through the public store."""

import pytest

from conftest import InMemoryRepository
from shared_context.behavior_native_turn_wire import NativeResultReference, NativeTurnAddress
from shared_context.errors import Problem
from shared_context.native_group_wire import NativeGroupDelivery, NativeGroupOpen, NativeGroupPost
from shared_context.native_groups import NativeGroupStore, group_post_id
from shared_context.records import Record

MESSAGE = "d" * 64
MEMBER = ("NATIVE_GROUP_MEMBER_REQUIRED", 403)
RESERVE = (409, "NATIVE_GROUP_CONFLICT", "one original request must reserve one original result")
SETTLE = (409, "NATIVE_GROUP_CONFLICT", "the result must settle its original reserved message")
SPEAKER = (409, "NATIVE_GROUP_CONFLICT", "a saved result must retain its original speaking Agent")
EXHAUSTED = (409, "NATIVE_GROUP_ALLOWANCE", "the original publication allowance is exhausted")
NO_SLOT = (409, "NATIVE_GROUP_ALLOWANCE", "the recipient has no result publication slot")
AWAITS = (409, "NATIVE_GROUP_PENDING", "the saved final awaits the original member result publications")
STILL = (409, "NATIVE_GROUP_PENDING", "the saved final still awaits original member results")


def setup(members=("agent-a", "agent-b", "agent-c")):
    repository = InMemoryRepository()
    store = NativeGroupStore(repository, tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(request_id="post-pins", orchestrator_agent_id="agent-a",
                                       member_agent_ids=members, content="Pin the original publication rules."))
    return repository, store, group.group_id


def refusal(call):
    with pytest.raises(Problem) as caught:
        call()
    return caught.value.status, caught.value.body["error"]["code"], caught.value.body["error"]["message"]


def post(request_id, kind="result", round=1, **changes):
    return NativeGroupPost(request_id=request_id, round=round, kind=kind, content=f"Public {request_id}.", **changes)


def turn(agent="agent-a"):
    return NativeTurnAddress(agent_id=agent, conversation_id="cid", request_id="rid")


def result(number=1):
    return NativeResultReference(turn_number=number, turn_key=f"turn:{number}", result_digest="a" * 64,
                                 text_sha256="c" * 64, text_bytes=4, text_parts=1, checkpoint_sha256="b" * 64)


def delivery(message_id=MESSAGE, sender="agent-a", recipient="agent-b", **changes):
    return NativeGroupDelivery(message_id=message_id, sender_agent_id=sender, recipient_agent_id=recipient,
                               **{"round": 1, **changes})


def reserve(store, group_id, recipient="agent-b", sender="agent-a", message_id=MESSAGE, request_id="ask"):
    return store.post(group_id, sender, post(request_id, kind="request", recipient_agent_id=recipient),
                      delivery=delivery(message_id, sender, recipient))


@pytest.mark.parametrize("speaker,command,expected", [
    ("agent-x", post("outsider"), (403, "NATIVE_GROUP_MEMBER_REQUIRED", "only an original group member may publish")),
    ("agent-b", post("addressee", recipient_agent_id="agent-x"),
     (403, "NATIVE_GROUP_MEMBER_REQUIRED", "the published addressee must be a group member")),
    ("agent-b", post("half-turn", turn=turn("agent-b")), SPEAKER),
    ("agent-b", post("half-result", result=result()), SPEAKER),
    ("agent-b", post("other-turn", turn=turn("agent-c"), result=result()), SPEAKER),
    ("agent-b", post("blocked-other", kind="blocked", turn=turn("agent-c")), SPEAKER),
    ("agent-b", post("later", round=2), (409, "NATIVE_GROUP_ROUND_CHANGED", "read the original group's current round")),
    ("agent-b", post("member-final", kind="final"),
     (403, "NATIVE_GROUP_ORCHESTRATOR_REQUIRED", "only the original orchestrator may conclude")),
])
def test_each_refusal_keeps_its_status_code_and_message_and_writes_nothing(speaker, command, expected):
    _, store, group_id = setup()
    before = store.read(group_id)
    assert refusal(lambda: store.post(group_id, speaker, command)) == expected
    assert store.read(group_id) == before


def test_blocked_post_may_keep_its_own_turn_without_a_result_and_a_full_result_publishes():
    _, store, group_id = setup()
    blocked = store.post(group_id, "agent-b", post("blocked", kind="blocked", turn=turn("agent-b")))
    assert (blocked.kind, blocked.turn, blocked.result) == ("blocked", turn("agent-b"), None)
    saved = store.post(group_id, "agent-c", post("full", turn=turn("agent-c"), result=result(2)))
    assert saved.result == result(2) and store.read(group_id).posts == (blocked, saved)


def test_membership_is_checked_before_the_closed_state_and_an_exact_replay_survives_closure():
    _, store, group_id = setup()
    first = store.post(group_id, "agent-b", post("before-close"))
    store.post(group_id, "agent-a", post("close", kind="final"))
    assert refusal(lambda: store.post(group_id, "agent-x", post("late")))[:2] == (403, "NATIVE_GROUP_MEMBER_REQUIRED")
    assert store.post(group_id, "agent-b", post("before-close")) == first
    assert refusal(lambda: store.post(group_id, "agent-b", post("after-close"))) == (
        409, "NATIVE_GROUP_CLOSED", "the original group is already closed")
    assert refusal(lambda: store.post(group_id, "agent-b", post("before-close", round=2))) == (
        409, "NATIVE_GROUP_CONFLICT", "the original post ID names different material")


def test_reservation_saves_one_awaiting_delivery_and_its_reference():
    _, store, group_id = setup()
    asked = reserve(store, group_id)
    view = store.read(group_id)
    assert asked.post_id == group_post_id(group_id, "agent-a", "ask")
    assert view.posts == (asked,) and view.deliveries == (delivery(),)
    assert view.state == "open" and view.awaiting_final


@pytest.mark.parametrize("speaker,command,reserved", [
    ("agent-c", post("ask", kind="request", recipient_agent_id="agent-b"), delivery()),
    ("agent-a", post("ask", kind="request", recipient_agent_id="agent-b"), delivery(round=2)),
    ("agent-a", post("ask", kind="request", recipient_agent_id="agent-c"), delivery()),
    ("agent-a", post("ask", kind="request", recipient_agent_id="agent-a"), delivery(recipient="agent-a")),
    ("agent-a", post("ask", kind="request", recipient_agent_id="agent-b"), delivery(status="completed")),
    ("agent-a", post("ask", kind="fyi", recipient_agent_id="agent-b"), delivery()),
    ("agent-a", post("ask", kind="result", recipient_agent_id="agent-b"), delivery()),
])
def test_a_reservation_must_match_its_original_request(speaker, command, reserved):
    _, store, group_id = setup()
    assert refusal(lambda: store.post(group_id, speaker, command, delivery=reserved)) == RESERVE
    assert store.read(group_id).posts == ()


def test_handoff_may_reserve_but_one_message_id_reserves_only_once():
    _, store, group_id = setup()
    handed = store.post(group_id, "agent-a", post("hand", kind="handoff", recipient_agent_id="agent-b"),
                        delivery=delivery())
    assert store.read(group_id).deliveries == (delivery(),) and handed.kind == "handoff"
    assert refusal(lambda: reserve(store, group_id, recipient="agent-c")) == RESERVE


@pytest.mark.parametrize("speaker,command,settles", [
    ("agent-b", post("answer"), "e" * 64),
    ("agent-c", post("answer"), MESSAGE),
    ("agent-b", post("answer", kind="fyi"), MESSAGE),
    ("agent-b", post("answer", kind="request"), MESSAGE),
])
def test_a_settling_post_must_answer_its_awaiting_reservation(speaker, command, settles):
    _, store, group_id = setup()
    reserve(store, group_id)
    assert refusal(lambda: store.post(group_id, speaker, command, settles=settles)) == SETTLE
    assert store.read(group_id).deliveries == (delivery(),)


@pytest.mark.parametrize("kind,status", [("result", "completed"), ("blocked", "blocked")])
def test_a_settling_result_or_block_marks_its_reservation(kind, status):
    _, store, group_id = setup()
    asked = reserve(store, group_id)
    answer = store.post(group_id, "agent-b", post("answer", kind=kind), settles=MESSAGE)
    view = store.read(group_id)
    assert view.posts == (asked, answer) and view.deliveries == (delivery(status=status),)
    assert view.state == "open" and view.awaiting_final
    assert refusal(lambda: store.post(group_id, "agent-b", post("again"), settles=MESSAGE)) == SETTLE


def test_a_pending_reservation_holds_the_recipients_slot_until_it_settles():
    _, store, group_id = setup()
    store.post(group_id, "agent-b", post("b-1"))
    store.post(group_id, "agent-b", post("b-2"))
    reserve(store, group_id)
    assert refusal(lambda: store.post(group_id, "agent-b", post("b-3"))) == EXHAUSTED
    store.post(group_id, "agent-b", post("b-3"), settles=MESSAGE)
    assert len(store.read(group_id).posts) == 4


def test_a_reservation_needs_a_free_slot_for_its_recipient():
    _, store, group_id = setup()
    store.post(group_id, "agent-b", post("b-1"))
    store.post(group_id, "agent-b", post("b-2"))
    reserve(store, group_id)
    _, store, group_id = setup()
    for number in range(1, 4):
        store.post(group_id, "agent-b", post(f"b-{number}"))
    assert refusal(lambda: reserve(store, group_id)) == NO_SLOT


def test_the_orchestrator_as_recipient_keeps_its_final_slot():
    _, store, group_id = setup()
    store.post(group_id, "agent-a", post("a-1", kind="fyi"))
    reserve(store, group_id, recipient="agent-a", sender="agent-c")
    _, store, group_id = setup()
    store.post(group_id, "agent-a", post("a-1", kind="fyi"))
    store.post(group_id, "agent-a", post("a-2", kind="fyi"))
    assert refusal(lambda: reserve(store, group_id, recipient="agent-a", sender="agent-c")) == NO_SLOT


def test_pending_reservations_count_toward_the_group_total():
    _, store, group_id = setup(("agent-a", "agent-b", "agent-c", "agent-d"))
    for agent, count in (("agent-b", 3), ("agent-c", 3), ("agent-d", 1)):
        for number in range(count):
            store.post(group_id, agent, post(f"{agent}-{number}"))
    reserve(store, group_id, recipient="agent-d")
    assert refusal(lambda: store.post(group_id, "agent-a", post("extra", kind="fyi"))) == EXHAUSTED


def test_a_final_waiting_for_results_needs_a_saved_turn_and_result():
    _, store, group_id = setup()
    reserve(store, group_id)
    assert refusal(lambda: store.post(group_id, "agent-a", post("final", kind="final"))) == (
        409, "NATIVE_GROUP_ORIGINAL_RESULT_REQUIRED", "only a verified saved final can wait for results")


def test_a_deferred_final_waits_then_closes_the_group_after_the_last_result():
    _, store, group_id = setup()
    asked = reserve(store, group_id)
    final = post("final-1", kind="final", turn=turn(), result=result(1))
    assert refusal(lambda: store.post(group_id, "agent-a", final)) == AWAITS
    waiting = store.read(group_id)
    assert waiting.posts == (asked,) and waiting.state == "open" and waiting.deliveries == (delivery(),)
    assert refusal(lambda: store.post(group_id, "agent-a", final)) == STILL
    newer = post("final-2", kind="final", turn=turn(), result=result(2))
    assert refusal(lambda: store.post(group_id, "agent-a", newer)) == AWAITS
    older = post("final-3", kind="final", turn=turn(), result=result(1))
    assert refusal(lambda: store.post(group_id, "agent-a", older)) == STILL
    assert store.read(group_id) == waiting
    answer = store.post(group_id, "agent-b", post("answer"), settles=MESSAGE)
    closed = store.read(group_id)
    assert closed.posts[:2] == (asked, answer) and len(closed.posts) == 3
    concluded = closed.posts[2]
    assert concluded.post_id == group_post_id(group_id, "agent-a", "final-2") and concluded.result == result(2)
    assert closed.state == "closed" and not closed.awaiting_final
    assert closed.deliveries == (delivery(status="completed"),)


def test_a_deferred_final_stays_waiting_while_another_reservation_is_pending():
    _, store, group_id = setup()
    reserve(store, group_id)
    reserve(store, group_id, recipient="agent-c", message_id="f" * 64, request_id="ask-c")
    assert refusal(lambda: store.post(
        group_id, "agent-a", post("final", kind="final", turn=turn(), result=result()))) == AWAITS
    store.post(group_id, "agent-b", post("answer-b"), settles=MESSAGE)
    view = store.read(group_id)
    assert view.state == "open" and len(view.posts) == 3
    store.post(group_id, "agent-c", post("answer-c", kind="blocked"), settles="f" * 64)
    view = store.read(group_id)
    assert view.state == "closed" and view.posts[-1].kind == "final" and len(view.posts) == 5


def test_a_final_without_pending_reservations_closes_at_once_after_a_settled_one():
    _, store, group_id = setup()
    reserve(store, group_id)
    store.post(group_id, "agent-b", post("answer"), settles=MESSAGE)
    final = store.post(group_id, "agent-a", post("final", kind="final"))
    view = store.read(group_id)
    assert view.posts[-1] == final and view.state == "closed"


def _fail(repository, monkeypatch, method, status, *, write=False, change=None):
    original = getattr(repository, method)

    def failing(*args):
        if write:
            if change is not None:
                change(args[2])
            original(*args)
        raise Problem(status, "UPSTREAM", "OFFLINE injected failure")

    monkeypatch.setattr(repository, method, failing)


@pytest.mark.parametrize("status,write,expected", [
    (400, False, (400, "UPSTREAM", "OFFLINE injected failure")),
    (503, False, (503, "NATIVE_GROUP_UNKNOWN", "the original post content remains unconfirmed")),
    (409, False, (503, "NATIVE_GROUP_UNKNOWN", "the original post content remains unconfirmed")),
    (412, False, (503, "NATIVE_GROUP_UNKNOWN", "the original post content remains unconfirmed")),
])
def test_an_unconfirmed_content_write_refuses_without_publishing(monkeypatch, status, write, expected):
    repository, store, group_id = setup()
    _fail(repository, monkeypatch, "create", status, write=write)
    assert refusal(lambda: store.post(group_id, "agent-b", post("lost"))) == expected
    assert store.read(group_id).posts == ()


def test_a_lost_content_ack_still_publishes_the_original_reference(monkeypatch):
    repository, store, group_id = setup()
    _fail(repository, monkeypatch, "create", 503, write=True)
    saved = store.post(group_id, "agent-b", post("lost-ack"))
    assert store.read(group_id).posts == (saved,)
    assert store.read_post(group_id, saved.post_id).content == "Public lost-ack."


def test_existing_content_under_the_same_post_id_must_be_the_same_original():
    repository, store, group_id = setup()
    post_id = group_post_id(group_id, "agent-b", "taken")
    repository.create(store.partition_key, "post:" + post_id, {
        "kind": "fa-native-group-post-v1", "group_id": group_id, "speaker_agent_id": "agent-b",
        "command": post("taken").model_copy(update={"content": "Other text."}).model_dump(mode="json")})
    assert refusal(lambda: store.post(group_id, "agent-b", post("taken"))) == (
        409, "NATIVE_GROUP_CONFLICT", "the original post ID names different material")
    repository.data[store.partition_key, "post:" + post_id] = Record({"kind": "unknown"}, 'W/"bad"')
    assert refusal(lambda: store.post(group_id, "agent-b", post("taken"))) == (
        409, "NATIVE_GROUP_STATE_INVALID", "the original published post is invalid")
    assert store.read(group_id).posts == ()


def test_a_content_confirmation_read_failure_other_than_missing_is_reraised(monkeypatch):
    repository, store, group_id = setup()
    _fail(repository, monkeypatch, "create", 503)
    get = repository.get

    def broken(*args):
        if args[1].startswith("post:"):
            raise Problem(500, "READ_BROKEN", "OFFLINE read failure")
        return get(*args)

    monkeypatch.setattr(repository, "get", broken)
    assert refusal(lambda: store.post(group_id, "agent-b", post("lost"))) == (500, "READ_BROKEN", "OFFLINE read failure")


@pytest.mark.parametrize("status,expected", [
    (400, (400, "UPSTREAM", "OFFLINE injected failure")),
    (503, (503, "NATIVE_GROUP_UNKNOWN", "the original post publication is unconfirmed; read the same post ID")),
    (500, (503, "NATIVE_GROUP_UNKNOWN", "the original post publication is unconfirmed; read the same post ID")),
    (412, (409, "NATIVE_GROUP_UNKNOWN", "the original post publication is unconfirmed; read the same post ID")),
    (409, (409, "NATIVE_GROUP_UNKNOWN", "the original post publication is unconfirmed; read the same post ID")),
])
def test_an_unconfirmed_root_publication_maps_its_status(monkeypatch, status, expected):
    repository, store, group_id = setup()
    _fail(repository, monkeypatch, "replace", status)
    assert refusal(lambda: store.post(group_id, "agent-b", post("lost"))) == expected
    assert store.read(group_id).posts == ()


def test_a_root_publication_that_saved_a_different_reference_is_refused(monkeypatch):
    repository, store, group_id = setup()

    def change(payload):
        payload["view"]["posts"][-1]["content_sha256"] = "e" * 64

    _fail(repository, monkeypatch, "replace", 503, write=True, change=change)
    assert refusal(lambda: store.post(group_id, "agent-b", post("lost"))) == (
        409, "NATIVE_GROUP_STATE_INVALID", "the published post reference differs")


@pytest.mark.parametrize("status,write,expected", [
    (400, False, (400, "UPSTREAM", "OFFLINE injected failure")),
    (503, False, (503, "NATIVE_GROUP_UNKNOWN", "the original deferred final is unconfirmed")),
    (412, False, (409, "NATIVE_GROUP_UNKNOWN", "the original deferred final is unconfirmed")),
    (503, True, AWAITS),
    (412, True, AWAITS),
])
def test_an_unconfirmed_deferred_final_maps_its_status(monkeypatch, status, write, expected):
    repository, store, group_id = setup()
    reserve(store, group_id)
    _fail(repository, monkeypatch, "replace", status, write=write)
    final = post("final", kind="final", turn=turn(), result=result())
    assert refusal(lambda: store.post(group_id, "agent-a", final)) == expected
    monkeypatch.undo()
    retry = STILL if write else AWAITS
    assert refusal(lambda: store.post(group_id, "agent-a", final)) == retry


def test_a_post_for_an_earlier_round_is_refused_after_the_round_advances():
    from shared_context.native_group_wire import NativeGroupAdvance

    _, store, group_id = setup()
    store.advance(group_id, "agent-a", NativeGroupAdvance(request_id="next", expected_round=1))
    assert refusal(lambda: store.post(group_id, "agent-b", post("stale"))) == (
        409, "NATIVE_GROUP_ROUND_CHANGED", "read the original group's current round")


def test_a_deferred_final_from_the_same_saved_turn_replaces_the_waiting_one():
    _, store, group_id = setup()
    reserve(store, group_id)
    for request_id in ("final-1", "final-2"):
        final = post(request_id, kind="final", turn=turn(), result=result(1))
        assert refusal(lambda: store.post(group_id, "agent-a", final)) == AWAITS
    store.post(group_id, "agent-b", post("answer"), settles=MESSAGE)
    assert store.read(group_id).posts[-1].post_id == group_post_id(group_id, "agent-a", "final-2")


def test_a_lost_root_ack_rechecks_the_original_content_before_returning(monkeypatch):
    repository, store, group_id = setup()
    post_id = group_post_id(group_id, "agent-b", "lost")

    def change(_payload):
        stored = repository.data[store.partition_key, "post:" + post_id]
        stored.payload["command"]["content"] = "Replaced text."

    _fail(repository, monkeypatch, "replace", 503, write=True, change=change)
    assert refusal(lambda: store.post(group_id, "agent-b", post("lost"))) == (
        409, "NATIVE_GROUP_CONFLICT", "the original post ID names different material")


def test_a_lost_deferred_write_is_confirmed_by_a_final_that_was_already_published(monkeypatch):
    repository, store, group_id = setup()
    reserve(store, group_id)

    def change(payload):
        final, payload["deferred_final"] = payload["deferred_final"], None
        payload["view"].update(state="closed", awaiting_final=False, posts=[*payload["view"]["posts"], final])
        payload["view"]["deliveries"][0]["status"] = "completed"

    _fail(repository, monkeypatch, "replace", 412, write=True, change=change)
    final = post("final", kind="final", turn=turn(), result=result())
    assert refusal(lambda: store.post(group_id, "agent-a", final)) == AWAITS
    assert store.read(group_id).state == "closed"


def test_only_a_settling_post_publishes_a_waiting_final():
    repository, store, group_id = setup()
    reserve(store, group_id)
    final = post("final", kind="final", turn=turn(), result=result())
    assert refusal(lambda: store.post(group_id, "agent-a", final)) == AWAITS
    # A saved root may hold a waiting final with no awaiting delivery; a plain post must not close it.
    stored = repository.data[store.partition_key, "group:" + group_id]
    stored.payload["view"]["deliveries"][0]["status"] = "completed"
    store.post(group_id, "agent-c", post("note", kind="fyi"))
    view = store.read(group_id)
    assert view.state == "open" and [item.kind for item in view.posts] == ["request", "fyi"]
