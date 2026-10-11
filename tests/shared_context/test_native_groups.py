"""SP-22 group state through its public store Interface; Table I/O is an explicit boundary substitute."""

import pytest

from conftest import InMemoryRepository
from shared_context.errors import Problem


def test_open_group_preserves_one_root_members_and_original_human_question():
    from shared_context.native_groups import NativeGroupStore
    from shared_context.native_group_wire import NativeGroupOpen

    store = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team")
    command = NativeGroupOpen(
        request_id="original-group-opening", orchestrator_agent_id="agent-a",
        member_agent_ids=("agent-a", "agent-b", "agent-c"), content="Compare the three public proposals.",
    )
    group = store.open(command)
    assert group.orchestrator_agent_id == "agent-a"
    assert group.member_agent_ids == ("agent-a", "agent-b", "agent-c")
    assert group.round == 1 and group.state == "open"
    assert group.posts == () and group.awaiting_final
    assert store.read(group.group_id) == group
    assert store.open(command) == group
    with pytest.raises(Problem) as changed:
        store.open(command.model_copy(update={"content": "Replace the original human question."}))
    assert changed.value.body["error"]["code"] == "NATIVE_GROUP_CONFLICT"
    assert store.read(group.group_id) == group


def test_group_opening_must_fit_the_original_native_text_contract_before_any_write():
    from shared_context.native_group_wire import NativeGroupOpen

    with pytest.raises(ValueError):
        NativeGroupOpen(request_id="quoted-bound", orchestrator_agent_id="agent-a",
                        member_agent_ids=("agent-a", "agent-b"), content="x" + "\n" * 4096)


def test_member_limit_and_orchestrator_final_slot_are_enforced_before_publication():
    from shared_context.native_groups import NativeGroupStore
    from shared_context.native_group_wire import NativeGroupOpen, NativeGroupPost

    store = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(
        request_id="bounded-group", orchestrator_agent_id="agent-a",
        member_agent_ids=("agent-a", "agent-b", "agent-c"), content="Compare the public alternatives.",
    ))
    for number in range(1, 4):
        command = NativeGroupPost(request_id=f"b-{number}", round=1, kind="result", content=f"B point {number}")
        posted = store.post(group.group_id, "agent-b", command)
        assert store.post(group.group_id, "agent-b", command) == posted
    with pytest.raises(Problem) as fourth:
        store.post(group.group_id, "agent-b", NativeGroupPost(
            request_id="b-4", round=1, kind="result", content="A fourth B point."))
    assert fourth.value.body["error"]["code"] == "NATIVE_GROUP_ALLOWANCE"
    for number in range(1, 3):
        store.post(group.group_id, "agent-a", NativeGroupPost(
            request_id=f"a-{number}", round=1, kind="request", content=f"A question {number}"))
    with pytest.raises(Problem) as reserved:
        store.post(group.group_id, "agent-a", NativeGroupPost(
            request_id="a-3", round=1, kind="fyi", content="Do not consume the reserved final slot."))
    assert reserved.value.body["error"]["code"] == "NATIVE_GROUP_ALLOWANCE"
    final = store.post(group.group_id, "agent-a", NativeGroupPost(
        request_id="a-final", round=1, kind="final", content="The saved group conclusion."))
    view = store.read(group.group_id)
    assert len(view.posts) == 6 and view.posts[-1] == final
    assert view.state == "closed" and not view.awaiting_final


def test_rounds_do_not_reset_total_posts_and_the_tenth_slot_remains_a_final():
    from shared_context.native_groups import NativeGroupStore
    from shared_context.native_group_wire import NativeGroupAdvance, NativeGroupOpen, NativeGroupPost

    store = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(
        request_id="three-round-group", orchestrator_agent_id="agent-a",
        member_agent_ids=("agent-a", "agent-b", "agent-c"), content="Keep all original round limits.",
    ))
    for current_round in range(1, 4):
        for number in range(1, 4):
            store.post(group.group_id, "agent-b", NativeGroupPost(
                request_id=f"b-{current_round}-{number}", round=current_round, kind="result", content="One public point."))
        if current_round < 3:
            command = NativeGroupAdvance(request_id=f"next-{current_round}", expected_round=current_round)
            receipt = store.advance(group.group_id, "agent-a", command)
            assert receipt.to_round == current_round + 1
            assert store.advance(group.group_id, "agent-a", command) == receipt
    assert len(store.read(group.group_id).posts) == 9
    with pytest.raises(Problem) as fourth:
        store.advance(group.group_id, "agent-a", NativeGroupAdvance(request_id="next-3", expected_round=3))
    assert fourth.value.body["error"]["code"] == "NATIVE_GROUP_ALLOWANCE"
    with pytest.raises(Problem) as tenth:
        store.post(group.group_id, "agent-c", NativeGroupPost(
            request_id="extra-fyi", round=3, kind="fyi", content="Do not take the final slot."))
    assert tenth.value.body["error"]["code"] == "NATIVE_GROUP_ALLOWANCE"
    final = NativeGroupPost(request_id="only-final", round=3, kind="final", content="Original saved conclusion.")
    store.post(group.group_id, "agent-a", final)
    view = store.read(group.group_id)
    assert len(view.posts) == 10 and view.round == 3 and view.state == "closed"
    with pytest.raises(Problem) as eleventh:
        store.post(group.group_id, "agent-a", NativeGroupPost(
            request_id="another-final", round=3, kind="final", content="Do not create an eleventh post."))
    assert eleventh.value.body["error"]["code"] == "NATIVE_GROUP_CLOSED"
    assert store.read(group.group_id) == view


def test_lost_publication_ack_returns_one_original_post_and_its_verified_text(monkeypatch):
    from shared_context.native_groups import NativeGroupStore
    from shared_context.native_group_wire import NativeGroupOpen, NativeGroupPost

    repository = InMemoryRepository()
    store = NativeGroupStore(repository, tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(
        request_id="lost-publication-ack", orchestrator_agent_id="agent-a",
        member_agent_ids=("agent-a", "agent-b"), content="Keep the original published text.",
    ))
    original_replace, writes = repository.replace, []

    def lost(*args):
        writes.append(args)
        original_replace(*args)
        raise Problem(503, "UPSTREAM_UNKNOWN", "OFFLINE root CAS ACK lost")

    monkeypatch.setattr(repository, "replace", lost)
    command = NativeGroupPost(
        request_id="original-b-post", round=1, kind="result", content="Original public result.\nNo replacement.")
    first = store.post(group.group_id, "agent-b", command)
    assert store.post(group.group_id, "agent-b", command) == first
    assert len(writes) == 1 and len(store.read(group.group_id).posts) == 1
    body = store.read_post(group.group_id, first.post_id)
    assert body.reference == first and body.content == command.content
    with pytest.raises(Problem) as changed:
        store.post(group.group_id, "agent-b", command.model_copy(update={"content": "Different result."}))
    assert changed.value.body["error"]["code"] == "NATIVE_GROUP_CONFLICT"
    assert store.read_post(group.group_id, first.post_id) == body


@pytest.mark.parametrize("kind,reason", [("receipt", "transport_receipt"), ("fyi", "fyi_no_reply_requested")])
def test_fyi_and_receipt_silence_does_not_publish_consume_or_clear_the_human_final(kind, reason):
    from shared_context.native_groups import NativeGroupStore
    from shared_context.native_group_wire import NativeGroupOpen, NativeGroupSignal

    store = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(
        request_id="human-still-waits", orchestrator_agent_id="agent-a",
        member_agent_ids=("agent-a", "agent-b"), content="I still need an actual group answer.",
    ))
    signal = NativeGroupSignal(
        event_id="b" * 64, recipient_agent_id="agent-a", kind=kind, payload_sha256="c" * 64)
    observed = store.route(group.group_id, "agent-b", signal)
    assert observed.action == "silence" and observed.reason == reason
    assert not observed.start_work
    assert store.route(group.group_id, "agent-b", signal) == observed
    assert store.read(group.group_id) == group
    assert group.awaiting_final and not group.posts
    with pytest.raises(Problem) as changed:
        store.route(group.group_id, "agent-b", signal.model_copy(update={"payload_sha256": "d" * 64}))
    assert changed.value.body["error"]["code"] == "NATIVE_GROUP_CONFLICT"


def test_competing_last_publication_slots_preserve_one_final_and_both_original_candidates(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from shared_context.native_groups import NativeGroupStore
    from shared_context.native_group_wire import NativeGroupOpen, NativeGroupPost

    repository = InMemoryRepository()
    store = NativeGroupStore(repository, tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(
        request_id="last-normal-slot", orchestrator_agent_id="agent-a",
        member_agent_ids=("agent-a", "agent-b", "agent-c", "agent-d"),
        content="Do not overshoot the shared group limit.",
    ))
    for agent, count in (("agent-a", 2), ("agent-b", 3), ("agent-c", 3)):
        for number in range(count):
            store.post(group.group_id, agent, NativeGroupPost(
                request_id=f"{agent}-{number}", round=1, kind="result", content="An original public point."))
    barrier, lock, seen = Barrier(2), Lock(), [0]
    get = repository.get

    def same_root(*args):
        result = get(*args)
        if args[1] == "group:" + group.group_id:
            with lock:
                seen[0] += 1
                together = seen[0] <= 2
            if together:
                barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(repository, "get", same_root)

    def publish(number):
        try:
            return store.post(group.group_id, "agent-d", NativeGroupPost(
                request_id=f"d-{number}", round=1, kind="result", content=f"Distinct candidate {number}."))
        except Problem as error:
            return error.body["error"]["code"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(publish, (1, 2)))
    assert outcomes.count("NATIVE_GROUP_UNKNOWN") == 1
    assert len(store.read(group.group_id).posts) == 9
    store.post(group.group_id, "agent-a", NativeGroupPost(
        request_id="concurrent-final", round=1, kind="final", content="The original group conclusion."))
    assert len(store.read(group.group_id).posts) == 10


def test_other_teams_and_nonmembers_cannot_read_publish_advance_or_close_a_group():
    from shared_context.native_groups import NativeGroupStore
    from shared_context.native_group_wire import NativeGroupAdvance, NativeGroupOpen, NativeGroupPost, NativeGroupSignal

    repository = InMemoryRepository()
    store = NativeGroupStore(repository, tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(
        request_id="same-human-id", orchestrator_agent_id="agent-a",
        member_agent_ids=("agent-a", "agent-b"), content="Keep the original Team.",
    ))
    other = NativeGroupStore(repository, tenant_id="tenant", team_id="another-team")
    with pytest.raises(Problem) as denied:
        other.read(group.group_id)
    assert denied.value.body["error"]["code"] == "NATIVE_GROUP_NOT_FOUND"
    for call in (
        lambda: store.post(group.group_id, "agent-c", NativeGroupPost(
            request_id="wrong-member", round=1, kind="result", content="Not a member.")),
        lambda: store.advance(group.group_id, "agent-b", NativeGroupAdvance(
            request_id="wrong-orchestrator", expected_round=1)),
        lambda: store.post(group.group_id, "agent-b", NativeGroupPost(
            request_id="wrong-final", round=1, kind="final", content="I cannot close this group.")),
        lambda: store.route(group.group_id, "agent-b", NativeGroupSignal(
            event_id="c" * 64, recipient_agent_id="agent-c", kind="fyi", payload_sha256="d" * 64)),
    ):
        with pytest.raises(Problem) as refused:
            call()
        assert refused.value.status == 403
    assert store.read(group.group_id) == group


@pytest.mark.parametrize("kind", ["request", "handoff"])
def test_a_group_member_cannot_request_work_from_the_orchestrator_it_owes_a_result(kind):
    import json
    from shared_context.behavior_native_turn_wire import NativeAgentReceive
    from shared_context.native_group_wire import NativeGroupOpen
    from shared_context.native_groups import NativeGroupStore
    from test_native_turn_wire import agent_receive

    store = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(request_id="no-leader-cycle", orchestrator_agent_id="leader",
        member_agent_ids=("leader", "sender-agent"), content="Keep the original return path."))
    data = agent_receive("b" * 64, recipient="leader").model_dump(mode="json")
    data["capture"]["template"]["settings"]["state_profile"] = "foundry-responses-v1"
    data["source"].update(message_kind=kind, hop=2, root={
        "agent_id": "leader", "conversation_id": "leader-cid", "request_id": group.group_id})
    message = NativeAgentReceive.model_validate_json(json.dumps(data))
    with pytest.raises(Problem) as refused:
        store.relay_request(message)
    assert refused.value.body["error"]["code"] == "NATIVE_GROUP_REPLY_REQUIRED"
    assert store.read(group.group_id) == group


def test_a_group_member_can_request_another_member_without_creating_an_orchestrator_cycle():
    import json
    from shared_context.behavior_native_turn_wire import NativeAgentReceive
    from shared_context.native_group_wire import NativeGroupOpen
    from shared_context.native_groups import NativeGroupStore
    from test_native_turn_wire import agent_receive

    store = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(request_id="member-to-member", orchestrator_agent_id="leader",
        member_agent_ids=("leader", "sender-agent", "other-member"), content="Keep useful member handoffs."))
    data = agent_receive("b" * 64, recipient="other-member").model_dump(mode="json")
    data["capture"]["template"]["settings"]["state_profile"] = "foundry-responses-v1"
    data["source"].update(hop=2, root={
        "agent_id": "leader", "conversation_id": "leader-cid", "request_id": group.group_id})
    message = NativeAgentReceive.model_validate_json(json.dumps(data))
    posted = store.relay_request(message)
    saved = store.read(group.group_id)
    assert saved.posts == (posted,) and posted.speaker_agent_id == "sender-agent"
    assert posted.recipient_agent_id == "other-member"
    assert saved.deliveries[0].status == "awaiting"


def test_published_reader_rejects_a_changed_original_addressee(monkeypatch):
    from copy import deepcopy
    from shared_context.native_groups import NativeGroupStore
    from shared_context.native_group_wire import NativeGroupOpen, NativeGroupPost
    from shared_context.records import Record

    repository = InMemoryRepository()
    store = NativeGroupStore(repository, tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(request_id="bound-addressee", orchestrator_agent_id="agent-a",
        member_agent_ids=("agent-a", "agent-b"), content="Retain the original publication scope."))
    posted = store.post(group.group_id, "agent-a", NativeGroupPost(
        request_id="public-fyi", round=1, kind="fyi", content="Public data.", recipient_agent_id="agent-b"))
    get = repository.get

    def changed(*args):
        record = get(*args)
        if args[1] == "post:" + posted.post_id:
            data = deepcopy(record.payload)
            data["command"]["recipient_agent_id"] = "agent-a"
            return Record(data, record.etag)
        return record

    monkeypatch.setattr(repository, "get", changed)
    with pytest.raises(Problem) as refused:
        store.read_post(group.group_id, posted.post_id)
    assert refused.value.body["error"]["code"] == "NATIVE_GROUP_STATE_INVALID"
