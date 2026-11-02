"""Contract-only peer-message tests; no entrypoint, SDK, or cloud integration."""

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib

import pytest
from pydantic import ValidationError

from shared_context.behavior_agent_messages import (
    AgentAssignment, AgentDirectory, AgentMessage, AgentMessageLedger, AgentRoute, RootEnvelope, RootSpec, TeamReference,
    _QueueClaim, _ReceiverQueue, _RootState, decide_wake, message_key, message_state_key, queue_key, root_key,
    root_state_key,
)
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_operations import _parse, _row, _updated, _write
from shared_context.behavior_store import ControlChanges, ControlRequest, InlineMaterial, MAX_INLINE_BYTES
from shared_context.behavior_work_policy import CostLedger, work_policy_key
from shared_context.errors import Problem
from shared_context.source_materials import canonical
from test_behavior_store import rewrite_key, store


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def route(name):
    return AgentRoute(
        agent_id=name, tenant_id="tenant-one", team_id="team-one", principal_oid=name + "-oid",
        actor_id=name, policy_id=name + "-policy", template_id=name + "-template",
        template_sha256="a" * 64, target_id=name + "-target", conversation_id=name + "-conversation",
        hosted_session_id=name + "-hosted", sdk_session_id=name + "-sdk",
        memory_scope_id=name + "-memory", cwd_scope_id=name + "-cwd", credential_binding_id=name + "-credential",
    )


def principal(name):
    return BehaviorPrincipal("tenant-one", name + "-oid", "team-one", "agent", name,
                             frozenset({"read", "submit", "consume", "execute"}))


@pytest.fixture
def mailbox(store):
    adapter, client = store
    directory = AgentDirectory(agents=(route("agent-a"), route("agent-b")))
    cost_id = work_policy_key("cost", adapter._scope.authority_domain)
    cost = CostLedger(
        authority_domain=adapter._scope.authority_domain, cost_scope_sha256="a" * 64,
        observed_high_water_microusd=10_000_000, legacy_unresolved_reserve_microusd=2_000_000,
        legacy_liability_sha256="b" * 64, latest_observed_at=NOW, latest_observation_sha256="c" * 64,
    )
    adapter.transact(ControlRequest(request_id="existing-root-ledger", request_digest="d" * 64),
                     lambda _: ControlChanges(writes=(_write(cost_id, cost, "create"),),
                                              result={"existing_ledger": True}))
    spec = RootSpec(root_request_id="human-root-request", origin_agent_id="agent-a",
                    origin_work_id="original-a-work", origin_attempt_id="original-a-attempt",
                    root_budget_id="shared-root-budget", human_obligation_id="original-human-obligation",
                    deadline=NOW + timedelta(hours=1), max_messages=8, max_hops=2,
                    allowed_refs=(TeamReference(tenant_id="tenant-one", team_id="team-one",
                        resource_id="approved-team-source", version="v1", sha256="e" * 64),))
    ledger = AgentMessageLedger(adapter, directory, clock=lambda: NOW)
    root = ledger.register_root(spec)
    source = AgentAssignment(root_request_id=spec.root_request_id, agent_id="agent-a",
                             work_id=spec.origin_work_id, attempt_id=spec.origin_attempt_id)
    return adapter, client, directory, ledger, root, source


@pytest.mark.parametrize(("kind", "busy", "duplicate", "allowance", "action"), [
    ("request", False, False, True, "wake"),
    ("handoff", True, False, True, "queued"),
    ("result", False, False, True, "wake"),
    ("blocked", True, False, True, "queued"),
    ("blocked", False, False, False, "needs_attention"),
    ("receipt", False, False, True, "silence"),
    ("fyi", False, False, True, "silence"),
    ("result", False, True, True, "silence"),
])
def test_wake_policy_is_pure_and_never_settles_the_human_root(kind, busy, duplicate, allowance, action):
    value = decide_wake(kind, receiver_busy=busy, duplicate=duplicate, allowance_available=allowance)
    assert value.action == action
    assert value.reason
    assert value.settles_human_obligation is False
    assert value.model_dispatch_authorized is False


def test_agent_source_and_root_are_derived_before_atomic_queue_receipt(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    money = adapter.read_snapshot((root.cost_ledger_id,))[root.cost_ledger_id]
    command = AgentMessage(message_id="original-message", recipient_agent_id="agent-b",
                           kind="request", content="Compare the approved Team fragments.")
    accepted = ledger.send(principal("agent-a"), source, command)
    assert accepted.message.source_kind == "agent"
    assert accepted.message.sender_agent_id == "agent-a"
    assert accepted.message.sender_oid == principal("agent-a").oid
    assert accepted.message.from_work_id == "original-a-work"
    assert accepted.message.root_request_id == root.spec.root_request_id
    assert accepted.message.root_budget_id == root.spec.root_budget_id
    assert accepted.message.recipient_agent_id == "agent-b"
    assert accepted.status == "queued"
    assert accepted.decision.action == "wake" and not accepted.decision.model_dispatch_authorized
    assert ledger.pending(principal("agent-b")) == (accepted.message.message_key,)
    assert adapter.read_snapshot((root.cost_ledger_id,))[root.cost_ledger_id] == money

    before = copy.deepcopy(client.backing.rows)
    restarted = AgentMessageLedger(adapter, directory, clock=lambda: NOW)
    assert restarted.send(principal("agent-a"), source, command) == accepted
    assert restarted.read(principal("agent-b"), accepted.message.message_key) == accepted
    assert client.backing.rows == before


def test_one_peer_claim_and_reply_keep_the_original_root_work_and_budget(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request = ledger.send(principal("agent-a"), source, AgentMessage(
        message_id="original-message", recipient_agent_id="agent-b", kind="handoff", content="Approved Team brief."))
    claim = ledger.claim(principal("agent-b"), request.message.message_key,
                         request_id="claim-original", consumer_id="receiver-worker", epoch=1)
    assert claim.fresh and claim.assignment.agent_id == "agent-b"
    assert claim.assignment.work_id == request.message.to_work_id
    assert claim.assignment.root_request_id == source.root_request_id
    assert not claim.model_dispatch_authorized

    replay = AgentMessageLedger(adapter, directory, clock=lambda: NOW).claim(
        principal("agent-b"), request.message.message_key,
        request_id="claim-original", consumer_id="receiver-worker", epoch=1)
    assert not replay.fresh and replay.assignment == claim.assignment
    reply = ledger.send(principal("agent-b"), claim.assignment, AgentMessage(
        message_id="original-result", recipient_agent_id="agent-a", kind="result",
        content="Original complete peer result.", in_reply_to=request.message.message_key))
    assert reply.message.to_work_id == source.work_id
    assert reply.message.root_request_id == source.root_request_id
    assert reply.message.root_budget_id == root.spec.root_budget_id
    assert reply.message.hop == 2
    assert reply.message.causation_id == request.message.message_key
    assert ledger.pending(principal("agent-a")) == (reply.message.message_key,)
    ledger.acknowledge(principal("agent-b"), claim.claim_id, consumer_id="receiver-worker", epoch=1)
    assert ledger.pending(principal("agent-b")) == ()
    assert ledger.read(principal("agent-b"), request.message.message_key).status == "completed"
    assert ledger.read(principal("agent-b"), request.message.message_key).decision.action == "silence"


def test_lost_host_keeps_claim_unknown_and_never_releases_or_respawns_it(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    sent = ledger.send(principal("agent-a"), source, AgentMessage(
        message_id="original-message", recipient_agent_id="agent-b", kind="request", content="Approved Team brief."))
    claim = ledger.claim(principal("agent-b"), sent.message.message_key,
                         request_id="claim-original", consumer_id="receiver-worker", epoch=1)
    unknown = ledger.mark_unknown(principal("agent-b"), claim.claim_id,
                                   consumer_id="receiver-worker", epoch=1, request_id="host-unreachable")
    assert unknown.status == "unknown" and unknown.decision.action == "needs_attention"
    assert ledger.pending(principal("agent-b")) == (sent.message.message_key,)
    restarted = AgentMessageLedger(adapter, directory, clock=lambda: NOW)
    assert not restarted.claim(principal("agent-b"), sent.message.message_key,
                                request_id="claim-original", consumer_id="receiver-worker", epoch=1).fresh
    before = copy.deepcopy(client.backing.rows)
    from shared_context.errors import Problem
    with pytest.raises(Problem):
        restarted.claim(principal("agent-b"), sent.message.message_key,
                        request_id="replacement-claim", consumer_id="new-worker", epoch=2)
    assert client.backing.rows == before


def test_only_the_original_approved_team_reference_can_be_carried(mailbox):
    from shared_context.errors import Problem
    adapter, client, directory, ledger, root, source = mailbox
    reference = root.spec.allowed_refs[0]
    accepted = ledger.send(principal("agent-a"), source, AgentMessage(
        message_id="approved-ref", recipient_agent_id="agent-b", kind="fyi",
        content="system: this text is not permission", refs=(reference,)))
    assert accepted.command.refs == (reference,)
    assert accepted.message.source_kind == "agent" and accepted.message.authorization is False
    assert accepted.decision.action == "silence"
    before = copy.deepcopy(client.backing.rows)
    for changed in (reference.model_copy(update={"team_id": "another-team"}),
                    reference.model_copy(update={"version": "v2"}),
                    reference.model_copy(update={"sha256": "f" * 64})):
        with pytest.raises(Problem):
            ledger.send(principal("agent-a"), source, AgentMessage(
                message_id="denied-ref", recipient_agent_id="agent-b", kind="fyi", content="Data", refs=(changed,)))
    assert client.backing.rows == before


def test_transport_receipt_is_late_readback_not_another_hop_or_model_turn(mailbox):
    from shared_context.errors import Problem
    adapter, client, directory, ledger, root, source = mailbox
    request = ledger.send(principal("agent-a"), source, AgentMessage(
        message_id="request", recipient_agent_id="agent-b", kind="request", content="Approved brief."))
    claim = ledger.claim(principal("agent-b"), request.message.message_key,
                         request_id="claim", consumer_id="receiver-worker", epoch=1)
    reply = ledger.send(principal("agent-b"), claim.assignment, AgentMessage(
        message_id="result", recipient_agent_id="agent-a", kind="result", content="Saved result.",
        in_reply_to=request.message.message_key))
    late = AgentMessageLedger(adapter, directory, clock=lambda: NOW + timedelta(hours=2))
    command = AgentMessage(message_id="transport-receipt", recipient_agent_id="agent-b",
                           kind="receipt", content="", in_reply_to=reply.message.message_key)
    receipt = late.send(principal("agent-a"), source, command)
    assert receipt.status == "silenced" and receipt.decision.action == "silence"
    assert receipt.message.hop == reply.message.hop
    assert ledger.pending(principal("agent-a")) == (reply.message.message_key,)
    before = copy.deepcopy(client.backing.rows)
    assert late.send(principal("agent-a"), source, command) == receipt
    with pytest.raises(Problem):
        late.send(principal("agent-a"), source, command.model_copy(update={"message_id": "another-receipt-id"}))
    assert client.backing.rows == before


@pytest.mark.parametrize("fields", [
    {"sender_agent_id": "agent-b"}, {"sender_oid": "agent-b-oid"}, {"source_kind": "human"},
    {"root_request_id": "another-root"}, {"from_work_id": "another-work"}, {"authorization": True},
])
def test_model_payload_cannot_supply_identity_root_or_permission(fields):
    with pytest.raises(ValidationError):
        AgentMessage.model_validate({"message_id": "original", "recipient_agent_id": "agent-b",
                                     "kind": "request", "content": "Data", **fields})


@pytest.mark.parametrize("changes", [
    {"kind": "human", "actor_id": None}, {"oid": "unknown-agent"}, {"tenant_id": "another-tenant"},
    {"team": "another-team"}, {"actor_id": "agent-b"}, {"actions": frozenset({"read"})},
])
def test_nonmatching_credential_cannot_enqueue_or_spend_a_root(mailbox, changes):
    adapter, client, directory, ledger, root, source = mailbox
    before = copy.deepcopy(client.backing.rows)
    with pytest.raises(Problem):
        ledger.send(replace(principal("agent-a"), **changes), source, AgentMessage(
            message_id="forged", recipient_agent_id="agent-b", kind="request", content="Data"))
    assert client.backing.rows == before


@pytest.mark.parametrize("field", [
    "principal_oid", "conversation_id", "hosted_session_id", "sdk_session_id",
    "memory_scope_id", "cwd_scope_id", "credential_binding_id", "policy_id", "template_id",
])
def test_peer_private_scopes_and_agent_owned_configuration_cannot_be_shared(field):
    first, second = route("agent-a"), route("agent-b")
    with pytest.raises(ValidationError):
        AgentDirectory(agents=(first, second.model_copy(update={field: getattr(first, field)})))


def test_same_message_id_with_changed_body_is_rejected_and_identical_fyi_ids_are_not_conflated(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    first = AgentMessage(message_id="fyi-1", recipient_agent_id="agent-b", kind="fyi", content="Same public data")
    one = ledger.send(principal("agent-a"), source, first)
    two = ledger.send(principal("agent-a"), source, first.model_copy(update={"message_id": "fyi-2"}))
    assert one.message.message_key != two.message.message_key
    assert ledger.pending(principal("agent-b")) == ()
    before = copy.deepcopy(client.backing.rows)
    with pytest.raises(Problem) as changed:
        ledger.send(principal("agent-a"), source, first.model_copy(update={"content": "Different payload"}))
    assert changed.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT"
    assert client.backing.rows == before


def test_commit_response_loss_returns_original_message_without_requeue_or_spending_again(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    command = AgentMessage(message_id="original", recipient_agent_id="agent-b", kind="request", content="Approved brief.")
    client.failure = "after"
    try:
        with pytest.raises(Problem) as lost:
            ledger.send(principal("agent-a"), source, command)
    finally:
        client.failure = None
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    before = copy.deepcopy(client.backing.rows)
    replay = AgentMessageLedger(adapter, directory, clock=lambda: NOW).send(principal("agent-a"), source, command)
    assert ledger.pending(principal("agent-b")) == (replay.message.message_key,)
    assert client.backing.rows == before


def test_two_consumers_cannot_claim_the_same_original_message(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    sent = ledger.send(principal("agent-a"), source, AgentMessage(
        message_id="original", recipient_agent_id="agent-b", kind="request", content="Approved brief."))
    other = AgentMessageLedger(adapter, directory, clock=lambda: NOW)
    winner = []
    client.before_submit = lambda: winner.append(other.claim(principal("agent-b"), sent.message.message_key,
        request_id="second", consumer_id="second-consumer", epoch=2))
    with pytest.raises(Problem) as conflict:
        ledger.claim(principal("agent-b"), sent.message.message_key,
                     request_id="first", consumer_id="first-consumer", epoch=1)
    assert conflict.value.status == 412
    assert len(winner) == 1 and winner[0].fresh
    before = copy.deepcopy(client.backing.rows)
    with pytest.raises(Problem):
        ledger.acknowledge(principal("agent-b"), winner[0].claim_id, consumer_id="first-consumer", epoch=1)
    assert client.backing.rows == before


def test_busy_receiver_does_not_claim_and_blocked_response_is_not_silenced(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    sent = ledger.send(principal("agent-a"), source, AgentMessage(
        message_id="original", recipient_agent_id="agent-b", kind="request", content="Approved brief."),
        receiver_busy=True)
    assert sent.decision.action == "queued"
    before = copy.deepcopy(client.backing.rows)
    with pytest.raises(Problem):
        ledger.claim(principal("agent-b"), sent.message.message_key,
                     request_id="claim", consumer_id="receiver", epoch=1, receiver_busy=True)
    assert client.backing.rows == before
    claim = ledger.claim(principal("agent-b"), sent.message.message_key,
                         request_id="claim", consumer_id="receiver", epoch=1)
    blocked = ledger.send(principal("agent-b"), claim.assignment, AgentMessage(
        message_id="blocked", recipient_agent_id="agent-a", kind="blocked", content="Required source is unavailable.",
        in_reply_to=sent.message.message_key))
    assert blocked.status == "queued" and blocked.decision.action == "wake"
    assert not blocked.decision.settles_human_obligation


def test_late_original_result_is_preserved_after_unknown_without_a_replacement_consumer(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    sent = ledger.send(principal("agent-a"), source, AgentMessage(
        message_id="original", recipient_agent_id="agent-b", kind="request", content="Approved brief."))
    claim = ledger.claim(principal("agent-b"), sent.message.message_key,
                         request_id="claim", consumer_id="original-worker", epoch=1)
    ledger.mark_unknown(principal("agent-b"), claim.claim_id,
                        consumer_id="original-worker", epoch=1, request_id="original-unknown")
    late = AgentMessageLedger(adapter, directory, clock=lambda: NOW + timedelta(hours=2))
    result = late.send(principal("agent-b"), claim.assignment, AgentMessage(
        message_id="late-result", recipient_agent_id="agent-a", kind="result",
        content="Complete original result.", in_reply_to=sent.message.message_key))
    assert result.status == "needs_attention"
    assert result.command.content == "Complete original result."
    assert late.pending(principal("agent-a")) == (result.message.message_key,)
    confirmed = late.acknowledge(principal("agent-b"), claim.claim_id,
                                 consumer_id="original-worker", epoch=1)
    assert confirmed.status == "completed"
    assert late.pending(principal("agent-b")) == ()
    assert not confirmed.decision.settles_human_obligation


def test_root_allowance_reserves_the_one_original_result_instead_of_losing_it_to_fyi(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    spec = root.spec.model_copy(update={"root_request_id": "small-root", "origin_work_id": "small-work",
                                       "max_messages": 2})
    another = ledger.register_root(spec)
    assert another.cost_ledger_id == root.cost_ledger_id
    actor = source.model_copy(update={"root_request_id": spec.root_request_id, "work_id": spec.origin_work_id})
    request = ledger.send(principal("agent-a"), actor, AgentMessage(
        message_id="request", recipient_agent_id="agent-b", kind="request", content="Approved brief."))
    before = copy.deepcopy(client.backing.rows)
    with pytest.raises(Problem) as denied:
        ledger.send(principal("agent-a"), actor, AgentMessage(
            message_id="fyi", recipient_agent_id="agent-b", kind="fyi", content="Do not consume the reserved reply."))
    assert denied.value.body["error"]["code"] == "AGENT_MESSAGE_ALLOWANCE"
    assert client.backing.rows == before
    claim = ledger.claim(principal("agent-b"), request.message.message_key,
                         request_id="claim", consumer_id="worker", epoch=1)
    result = ledger.send(principal("agent-b"), claim.assignment, AgentMessage(
        message_id="result", recipient_agent_id="agent-a", kind="result",
        content="Original result.", in_reply_to=request.message.message_key))
    assert result.message.root_budget_id == root.spec.root_budget_id
    assert result.status == "queued"


def test_lost_claim_and_consumption_responses_reconcile_without_reclaiming(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    sent = ledger.send(principal("agent-a"), source, AgentMessage(
        message_id="original", recipient_agent_id="agent-b", kind="request", content="Approved brief."))
    client.failure = "after"
    try:
        with pytest.raises(Problem):
            ledger.claim(principal("agent-b"), sent.message.message_key,
                         request_id="claim", consumer_id="worker", epoch=1)
    finally:
        client.failure = None
    before = copy.deepcopy(client.backing.rows)
    restored = AgentMessageLedger(adapter, directory, clock=lambda: NOW)
    claim = restored.claim(principal("agent-b"), sent.message.message_key,
                           request_id="claim", consumer_id="worker", epoch=1)
    assert not claim.fresh and client.backing.rows == before
    client.failure = "after"
    try:
        with pytest.raises(Problem):
            restored.acknowledge(principal("agent-b"), claim.claim_id, consumer_id="worker", epoch=1)
    finally:
        client.failure = None
    before = copy.deepcopy(client.backing.rows)
    assert restored.acknowledge(principal("agent-b"), claim.claim_id, consumer_id="worker", epoch=1).status == "completed"
    assert restored.pending(principal("agent-b")) == ()
    assert client.backing.rows == before


@pytest.mark.parametrize("change", ["root", "work", "attempt", "agent"])
def test_assignment_cannot_select_another_root_or_work(mailbox, change):
    adapter, client, directory, ledger, root, source = mailbox
    fields = {"root": {"root_request_id": "unknown-root"}, "work": {"work_id": "another-work"},
              "attempt": {"attempt_id": "another-attempt"}, "agent": {"agent_id": "agent-b"}}[change]
    before = copy.deepcopy(client.backing.rows)
    with pytest.raises(Problem):
        ledger.send(principal("agent-a"), source.model_copy(update=fields), AgentMessage(
            message_id="wrong-scope", recipient_agent_id="agent-b", kind="request", content="Data"))
    assert client.backing.rows == before


def test_message_size_refusal_never_truncates_or_saves_a_partial_acceptance(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    before = copy.deepcopy(client.backing.rows)
    with pytest.raises(Problem) as denied:
        ledger.send(principal("agent-a"), source, AgentMessage(
            message_id="large", recipient_agent_id="agent-b", kind="request", content="x" * 32768))
    assert denied.value.body["error"]["code"] == "AGENT_MESSAGE_LIMIT"
    assert client.backing.rows == before


# --- send_recorded: one violated rule at a time, then the order the rules run in ---

LATE = NOW + timedelta(hours=2)


def _rejection(call):
    with pytest.raises(Problem) as denied:
        call()
    return denied.value.status, denied.value.body["error"]["code"], denied.value.body["error"]["message"]


def _rows(client):
    """The stored bytes, without the ETags a concurrent rewrite changes."""
    return {key: bytes(entity["Payload"]) for key, entity in client.backing.rows.items()}


def _message(identifier="next", recipient="agent-b", kind="fyi", content="Data", **fields):
    return AgentMessage(message_id=identifier, recipient_agent_id=recipient, kind=kind, content=content, **fields)


def _receipt(reply):
    return AgentMessage(message_id="transport-receipt", recipient_agent_id=reply.message.sender_agent_id,
                        kind="receipt", content="", in_reply_to=reply.message.message_key)


def _result(request, identifier="result", kind="result"):
    return _message(identifier, "agent-a", kind, "Saved result.", in_reply_to=request.message.message_key)


def _second_root(ledger, root, **fields):
    spec = root.spec.model_copy(update={"root_request_id": "second-root", **fields})
    ledger.register_root(spec)
    return AgentAssignment(root_request_id=spec.root_request_id, agent_id=spec.origin_agent_id,
                           work_id=spec.origin_work_id, attempt_id=spec.origin_attempt_id)


def _peer_work(mailbox):
    """A's original request to B, claimed by B."""
    adapter, client, directory, ledger, root, source = mailbox
    request = ledger.send(principal("agent-a"), source, _message("original", kind="request", content="Approved brief."))
    return request, ledger.claim(principal("agent-b"), request.message.message_key,
                                 request_id="claim", consumer_id="worker", epoch=1)


def _create_row(adapter, key, value):
    """Another writer that bypasses the ledger, so the row has no message receipt."""
    adapter.transact(ControlRequest(request_id="forged-" + key[:32], request_digest="f" * 64, read_set=(key,)),
                     lambda _: ControlChanges(writes=(_write(key, value, "create"),), result={"forged": True}))


def _change_row(adapter, key, model, **updates):
    """Another writer that bypasses the ledger and keeps the row's retained material."""
    def change(rows):
        kept = rows[key].payload.get("material")
        kept = None if kept is None else _parse(kept, InlineMaterial)
        return ControlChanges(writes=(_write(key, _updated(_row(rows, key, model), **updates), "replace", kept),),
                              result={"forged": True})
    adapter.transact(ControlRequest(request_id="forged-" + key[:32], request_digest="f" * 64, read_set=(key,)), change)


def _forged_row(key):
    return _RootState(root_request_id="forged-root",
                      request=ControlRequest(request_id="forged-original", request_digest="f" * 64, read_set=(key,)))


def _fill_queue(adapter, agent, size):
    _create_row(adapter, queue_key(agent), _ReceiverQueue(
        agent_id=agent, pending=tuple(f"waiting-{index}" for index in range(size)),
        request=ControlRequest(request_id="forged-queue", request_digest="f" * 64)))


def _racing(before_first_read=None, times=()):
    """A ledger clock: the pre-commit wake read is its first, so before_first_read lands after every preflight read."""
    reads = []

    def clock():
        turn = len(reads)
        reads.append(turn)
        if turn == 0 and before_first_read is not None:
            before_first_read()
        return times[turn] if turn < len(times) else NOW
    clock.reads = reads
    return clock


def _ledger_with(mailbox, clock):
    adapter, client, directory, ledger, root, source = mailbox
    return AgentMessageLedger(adapter, directory, clock=clock)


def _lose_commit(client, source):
    """A competing writer touches the counter just before the batch lands, so that batch gets a 412."""
    return lambda: rewrite_key(client.backing, (root_state_key(source.root_request_id),))


def test_a_new_message_is_fresh_and_its_replay_returns_the_original_as_not_fresh(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    first = ledger.send_recorded(principal("agent-a"), source, _message())
    replay = ledger.send_recorded(principal("agent-a"), source, _message())
    assert first.fresh is True and replay.fresh is False
    assert replay.view == first.view


def test_a_reference_outside_the_root_is_refused_with_its_own_reason(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    changed = root.spec.allowed_refs[0].model_copy(update={"version": "v2"})
    before = _rows(client)
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(refs=(changed,)))) == (
        403, "AGENT_MESSAGE_SHARE_DENIED", "only the root's exact approved Team references may be carried")
    assert _rows(client) == before


def test_an_approved_reference_of_another_team_is_still_refused(mailbox, monkeypatch):
    adapter, client, directory, ledger, root, source = mailbox
    foreign = root.spec.allowed_refs[0].model_copy(update={"team_id": "another-team"})
    forged = root.model_copy(update={"spec": root.spec.model_copy(update={"allowed_refs": (foreign,)})})
    monkeypatch.setattr(ledger, "_root", lambda _: forged)
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(refs=(foreign,)))) == (
        403, "AGENT_MESSAGE_SHARE_DENIED", "only the root's exact approved Team references may be carried")


@pytest.mark.parametrize(("sender", "recipient"), [("agent-a", "agent-a"), ("agent-b", "agent-a")])
def test_the_sender_must_be_its_assignment_and_not_its_own_recipient(mailbox, sender, recipient):
    adapter, client, directory, ledger, root, source = mailbox
    assert _rejection(lambda: ledger.send(principal(sender), source, _message(recipient=recipient))) == (
        403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "sender identity must come from its original assignment")


def test_a_reply_to_a_message_nobody_can_read_is_unknown(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(in_reply_to="missing"))) == (
        503, "AGENT_MESSAGE_UNKNOWN", "only the original message receipt may be reconciled")


@pytest.mark.parametrize(("name", "kind", "fields"), [
    ("agent-a", "result", {}), ("agent-a", "blocked", {}), ("agent-a", "receipt", {}),
    ("agent-b", "request", {"agent_id": "agent-b"}), ("agent-b", "fyi", {"agent_id": "agent-b"}),
    ("agent-a", "request", {"work_id": "another-work"}), ("agent-a", "fyi", {"attempt_id": "another-attempt"}),
])
def test_only_the_origin_starts_without_a_prior_message_and_only_with_new_work(mailbox, name, kind, fields):
    adapter, client, directory, ledger, root, source = mailbox
    recipient = "agent-a" if name == "agent-b" else "agent-b"
    content = "" if kind == "receipt" else "Data"
    before = _rows(client)
    assert _rejection(lambda: ledger.send(principal(name), source.model_copy(update=fields),
                                          _message(recipient=recipient, kind=kind, content=content))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "a reply or receipt needs its original message")
    assert _rows(client) == before


def test_a_reply_cannot_cross_into_another_root(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    reply = ledger.send(principal("agent-b"), claim.assignment, _result(request))
    other = _second_root(ledger, root)
    assert _rejection(lambda: ledger.send(principal("agent-a"), other, _message(
        in_reply_to=reply.message.message_key))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "reply must match the original root, peers and work")


def test_a_sender_cannot_reply_to_its_own_message(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, _ = _peer_work(mailbox)
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(
        in_reply_to=request.message.message_key))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "reply must match the original root, peers and work")


@pytest.mark.parametrize("field", ["sender_agent_id", "recipient_agent_id"])
def test_a_reply_needs_both_peers_of_the_original_message(mailbox, monkeypatch, field):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    forged = request.model_copy(update={"message": request.message.model_copy(update={field: "agent-c"})})
    monkeypatch.setattr(ledger, "read", lambda *_: forged)
    assert _rejection(lambda: ledger.send(principal("agent-b"), claim.assignment, _result(request))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "reply must match the original root, peers and work")


def test_a_reply_must_come_from_the_work_the_message_went_to(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    moved = claim.assignment.model_copy(update={"work_id": "another-work"})
    assert _rejection(lambda: ledger.send(principal("agent-b"), moved, _result(request))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "reply must match the original root, peers and work")


@pytest.mark.parametrize("kind", ["request", "handoff"])
def test_a_reply_cannot_open_new_work(mailbox, kind):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    assert _rejection(lambda: ledger.send(principal("agent-b"), claim.assignment, _result(request, kind=kind))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "reply must match the original root, peers and work")


def test_a_receipt_cannot_answer_a_receipt(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    reply = ledger.send(principal("agent-b"), claim.assignment, _result(request))
    receipt = ledger.send(principal("agent-a"), source, _receipt(reply))
    assert receipt.status == "silenced"
    assert _rejection(lambda: ledger.send(principal("agent-b"), claim.assignment, _receipt(receipt))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "reply must match the original root, peers and work")


def test_a_peer_reply_needs_the_claim_it_was_given(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    other = claim.assignment.model_copy(update={"attempt_id": "another-attempt"})
    assert _rejection(lambda: ledger.send(principal("agent-b"), other, _result(request))) == (
        403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "only the original claimed peer work may reply")


def test_a_peer_reply_before_any_claim_is_unknown_after_the_reply_rules(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request = ledger.send(principal("agent-a"), source, _message("original", kind="request", content="Brief."))
    unclaimed = AgentAssignment(root_request_id=source.root_request_id, agent_id="agent-b",
                                work_id=request.message.to_work_id, attempt_id="not-claimed")
    assert _rejection(lambda: ledger.send(principal("agent-b"), unclaimed, _result(request))) == (
        503, "AGENT_MESSAGE_UNKNOWN", "only the original message receipt may be reconciled")
    assert _rejection(lambda: ledger.send(principal("agent-b"), unclaimed, _result(request, kind="request"))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "reply must match the original root, peers and work")


def test_the_origin_cannot_close_its_own_work(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    reply = ledger.send(principal("agent-b"), claim.assignment, _result(request))
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(
        kind="result", in_reply_to=reply.message.message_key))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "only the assigned peer returns the original result")


def test_the_origin_cannot_close_even_a_request_that_reached_it(mailbox, monkeypatch):
    adapter, client, directory, ledger, root, source = mailbox
    request = ledger.send(principal("agent-a"), source, _message("original", kind="request", content="Brief."))
    forged = request.model_copy(update={"message": request.message.model_copy(update={
        "sender_agent_id": "agent-b", "recipient_agent_id": "agent-a", "to_work_id": source.work_id})})
    monkeypatch.setattr(ledger, "read", lambda *_: forged)
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(
        kind="result", in_reply_to=request.message.message_key))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "only the assigned peer returns the original result")


def test_a_peer_cannot_close_a_message_that_was_not_new_work(mailbox, monkeypatch):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    forged = request.model_copy(update={"message": request.message.model_copy(update={"message_kind": "fyi"})})
    monkeypatch.setattr(ledger, "read", lambda *_: forged)
    assert _rejection(lambda: ledger.send(principal("agent-b"), claim.assignment, _result(request))) == (
        403, "AGENT_MESSAGE_REPLY_DENIED", "only the assigned peer returns the original result")


def test_message_size_limit_is_exact(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    overhead = len(canonical(_message("sized", content="x").model_dump(mode="json"))) - 1
    fits = _message("sized", content="x" * (MAX_INLINE_BYTES - overhead))
    assert len(canonical(fits.model_dump(mode="json"))) == MAX_INLINE_BYTES
    assert ledger.send(principal("agent-a"), source, fits).command == fits
    over = _message("sized", content="x" * (MAX_INLINE_BYTES - overhead + 1))
    assert len(canonical(over.model_dump(mode="json"))) == MAX_INLINE_BYTES + 1
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, over)) == (
        413, "AGENT_MESSAGE_LIMIT", "complete message exceeds the original inline envelope")


def test_every_reply_rule_runs_before_the_size_limit(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    big = "x" * (MAX_INLINE_BYTES + 1)
    request, claim = _peer_work(mailbox)
    reply = ledger.send(principal("agent-b"), claim.assignment, _result(request))
    assert _rejection(lambda: ledger.send(principal("agent-b"), claim.assignment, _message(
        recipient="agent-a", content=big)))[2] == "a reply or receipt needs its original message"
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(
        content=big, in_reply_to=request.message.message_key)))[2] == (
        "reply must match the original root, peers and work")
    other = claim.assignment.model_copy(update={"attempt_id": "another-attempt"})
    assert _rejection(lambda: ledger.send(principal("agent-b"), other, _message(
        recipient="agent-a", content=big, in_reply_to=request.message.message_key)))[2] == (
        "only the original claimed peer work may reply")
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(
        kind="result", content=big, in_reply_to=reply.message.message_key)))[2] == (
        "only the assigned peer returns the original result")


def test_the_share_and_assignment_rules_run_in_order_before_the_prior_is_read(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    changed = root.spec.allowed_refs[0].model_copy(update={"version": "v2"})
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(
        recipient="agent-a", refs=(changed,))))[1] == "AGENT_MESSAGE_SHARE_DENIED"
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(
        recipient="agent-a", in_reply_to="missing")))[1] == "AGENT_MESSAGE_ASSIGNMENT_DENIED"


def test_an_oversized_message_never_reads_the_clock(mailbox):
    clock = _racing()
    race = _ledger_with(mailbox, clock)
    assert _rejection(lambda: race.send(principal("agent-a"), mailbox[5], _message(
        content="x" * (MAX_INLINE_BYTES + 1))))[1] == "AGENT_MESSAGE_LIMIT"
    assert clock.reads == []


def test_a_reply_past_the_root_hop_limit_is_refused(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    reply = ledger.send(principal("agent-b"), claim.assignment, _result(request))
    assert reply.message.hop == root.spec.max_hops
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message(
        in_reply_to=reply.message.message_key))) == (
        429, "AGENT_MESSAGE_ALLOWANCE", "the original root message allowance is exhausted")


def test_the_root_allowance_ends_exactly_at_its_deadline(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    race = _ledger_with(mailbox, _racing(times=(root.spec.deadline,)))
    sent = race.send(principal("agent-b"), claim.assignment, _result(request))
    assert sent.status == "needs_attention" and sent.decision.reason == "root_allowance_unavailable"


def test_a_changed_root_is_refused_at_commit_even_past_the_deadline(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    changed = root.spec.model_copy(update={"max_messages": 9})
    race = _ledger_with(mailbox, _racing(lambda: _change_row(
        adapter, root_key(root.spec.root_request_id), RootEnvelope, spec=changed), times=(NOW, LATE)))
    assert _rejection(lambda: race.send(principal("agent-a"), source, _message())) == (
        403, "AGENT_MESSAGE_ROOT_EXPIRED", "original root authority or deadline changed")


def test_the_deadline_is_read_again_at_commit_before_an_existing_row(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    identifier = message_key(source.root_request_id, "agent-a", "next")
    _create_row(adapter, identifier, _forged_row(identifier))
    race = _ledger_with(mailbox, _racing(times=(NOW, LATE)))
    assert _rejection(lambda: race.send(principal("agent-a"), source, _message())) == (
        403, "AGENT_MESSAGE_ROOT_EXPIRED", "original root deadline has expired")


@pytest.mark.parametrize("key", [message_key, lambda *parts: message_state_key(message_key(*parts))])
def test_a_row_without_its_receipt_is_not_overwritten_or_replayed(mailbox, key):
    adapter, client, directory, ledger, root, source = mailbox
    identifier = key(source.root_request_id, "agent-a", "next")
    _create_row(adapter, identifier, _forged_row(identifier))
    before = _rows(client)
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message())) == (
        409, "AGENT_MESSAGE_ORIGINAL_REQUIRED", "read the original message and receipt")
    assert _rows(client) == before


def test_a_second_result_cannot_replace_the_first(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    ledger.send(principal("agent-b"), claim.assignment, _result(request))
    before = _rows(client)
    assert _rejection(lambda: ledger.send(principal("agent-b"), claim.assignment, _result(request, "again"))) == (
        409, "AGENT_MESSAGE_RESULT_EXISTS", "the original peer result cannot be replaced")
    assert _rows(client) == before


def test_an_existing_row_is_reported_before_the_retained_result(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    ledger.send(principal("agent-b"), claim.assignment, _result(request))
    identifier = message_key(source.root_request_id, "agent-b", "again")
    _create_row(adapter, identifier, _forged_row(identifier))
    assert _rejection(lambda: ledger.send(principal("agent-b"), claim.assignment, _result(request, "again")))[1] == (
        "AGENT_MESSAGE_ORIGINAL_REQUIRED")


def test_a_result_must_answer_the_work_the_root_retained(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    race = _ledger_with(mailbox, _racing(lambda: _change_row(
        adapter, root_state_key(source.root_request_id), _RootState, handoff_message_id="another-handoff")))
    assert _rejection(lambda: race.send(principal("agent-b"), claim.assignment, _result(request))) == (
        409, "AGENT_MESSAGE_RESULT_EXISTS", "the original peer result cannot be replaced")


def test_a_claim_that_changed_after_the_preflight_is_refused_at_commit(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    moved = claim.assignment.model_copy(update={"attempt_id": "taken-over"})
    _fill_queue(adapter, "agent-a", 32)
    race = _ledger_with(mailbox, _racing(lambda: _change_row(adapter, claim.claim_id, _QueueClaim, assignment=moved)))
    assert _rejection(lambda: race.send(principal("agent-b"), claim.assignment, _result(request))) == (
        409, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "original peer assignment changed")


def test_the_retained_result_is_reported_before_a_changed_claim(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    ledger.send(principal("agent-b"), claim.assignment, _result(request))
    moved = claim.assignment.model_copy(update={"attempt_id": "taken-over"})
    race = _ledger_with(mailbox, _racing(lambda: _change_row(adapter, claim.claim_id, _QueueClaim, assignment=moved)))
    assert _rejection(lambda: race.send(principal("agent-b"), claim.assignment, _result(request, "again")))[1] == (
        "AGENT_MESSAGE_RESULT_EXISTS")


def test_the_receiver_queue_keeps_exactly_32_messages(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    _fill_queue(adapter, "agent-b", 31)
    sent = ledger.send(principal("agent-a"), source, _message("last", kind="request", content="Brief."))
    assert ledger.pending(principal("agent-b"))[-1] == sent.message.message_key
    other = _second_root(ledger, root)
    before = _rows(client)
    assert _rejection(lambda: ledger.send(principal("agent-a"), other, _message(
        "full", kind="request", content="Brief."))) == (
        429, "AGENT_MESSAGE_QUEUE_FULL", "the bounded original receiver queue is full")
    assert _rows(client) == before


def test_a_full_queue_does_not_stop_a_silenced_message(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    _fill_queue(adapter, "agent-b", 32)
    assert ledger.send(principal("agent-a"), source, _message()).status == "silenced"


def test_a_lost_race_retries_the_same_request_and_stays_fresh(mailbox, caplog):
    adapter, client, directory, ledger, root, source = mailbox
    client.before_submit = _lose_commit(client, source)
    with caplog.at_level("WARNING", logger="shared_context.behavior_agent_messages"):
        sent = ledger.send_recorded(principal("agent-a"), source, _message())
    assert sent.fresh is True and sent.view.status == "silenced"
    assert [record.getMessage() for record in caplog.records] == [
        "AGENT_MESSAGE_COMMIT_CONFLICT attempt=1 maximum=3"]
    assert client.submissions[-1] and [item[1] for item in client.submissions[-2]] == [
        item[1] for item in client.submissions[-1]]


def test_three_lost_races_raise_the_conflict_and_write_nothing(mailbox, caplog):
    adapter, client, directory, ledger, root, source = mailbox
    lose = _lose_commit(client, source)

    def again():
        lose()
        client.before_submit = again
    client.before_submit = again
    before, submitted = _rows(client), len(client.submissions)
    with caplog.at_level("WARNING", logger="shared_context.behavior_agent_messages"):
        assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message()))[:2] == (
            412, "BEHAVIOR_CONFLICT")
    assert len(client.submissions) - submitted == 3
    assert [record.getMessage() for record in caplog.records] == [
        "AGENT_MESSAGE_COMMIT_CONFLICT attempt=1 maximum=3", "AGENT_MESSAGE_COMMIT_CONFLICT attempt=2 maximum=3"]
    assert _rows(client) == before


def test_a_race_lost_to_the_same_request_returns_the_winner_as_not_fresh(mailbox, caplog):
    adapter, client, directory, ledger, root, source = mailbox
    other, winner = _ledger_with(mailbox, lambda: NOW), []
    client.before_submit = lambda: winner.append(other.send_recorded(principal("agent-a"), source, _message()))
    with caplog.at_level("WARNING", logger="shared_context.behavior_agent_messages"):
        sent = ledger.send_recorded(principal("agent-a"), source, _message())
    assert winner[0].fresh is True and sent.fresh is False
    assert sent.view == winner[0].view
    assert caplog.records == []


def test_a_winner_landing_between_two_attempts_is_not_counted_as_ours(mailbox, monkeypatch):
    adapter, client, directory, ledger, root, source = mailbox
    other, winner, lost = _ledger_with(mailbox, lambda: NOW), [], []
    lose, find = _lose_commit(client, source), adapter._receipt
    client.before_submit = lambda: (lose(), lost.append(True))

    def receipt(request):
        found = find(request)
        if lost:
            lost.clear()
            winner.append(other.send_recorded(principal("agent-a"), source, _message()))
        return found
    monkeypatch.setattr(adapter, "_receipt", receipt)
    sent = ledger.send_recorded(principal("agent-a"), source, _message())
    assert winner[0].fresh is True and sent.fresh is False
    assert sent.view == winner[0].view


def test_a_race_lost_to_the_same_id_with_other_input_is_refused(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    other = _ledger_with(mailbox, lambda: NOW)
    client.before_submit = lambda: other.send(principal("agent-a"), source, _message(content="Other data"))
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message()))[:2] == (
        409, "BEHAVIOR_REQUEST_CONFLICT")


@pytest.mark.parametrize(("status", "code"), [(409, "BEHAVIOR_CONFLICT"), (412, "ANOTHER_REFUSAL")])
def test_only_a_412_behavior_conflict_is_retried(mailbox, monkeypatch, status, code):
    adapter, client, directory, ledger, root, source = mailbox
    calls = []

    def refuse(*_):
        calls.append(1)
        raise Problem(status, code, "synthetic refusal")
    monkeypatch.setattr(adapter, "transact", refuse)
    assert _rejection(lambda: ledger.send(principal("agent-a"), source, _message()))[:2] == (status, code)
    assert len(calls) == 1


def test_each_attempt_reads_its_own_clock_and_the_second_can_expire(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    clock = _racing(times=(NOW, NOW, LATE))
    race = _ledger_with(mailbox, clock)
    client.before_submit = _lose_commit(client, source)
    assert _rejection(lambda: race.send(principal("agent-a"), source, _message())) == (
        403, "AGENT_MESSAGE_ROOT_EXPIRED", "original root deadline has expired")
    assert len(clock.reads) == 3


def test_the_wake_decision_is_made_once_before_the_first_attempt(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    request, claim = _peer_work(mailbox)
    clock = _racing(times=(LATE, NOW, NOW))
    race = _ledger_with(mailbox, clock)
    client.before_submit = _lose_commit(client, source)
    sent = race.send(principal("agent-b"), claim.assignment, _result(request))
    assert sent.status == "needs_attention" and sent.decision.reason == "root_allowance_unavailable"
    assert len(clock.reads) == 3


def _last_batch(client):
    """Every byte the last batch stored, in batch order."""
    value = [(item[0], item[1]["RowKey"], bytes(item[1]["Payload"]).hex()) for item in client.submissions[-1]]
    return hashlib.sha256(repr(value).encode()).hexdigest()


# Taken on 43b88ec5, before send_recorded was split. A changed hash only says some stored byte differs;
# the plain counter assertions in the test below say which field.
GOLDEN_BATCHES = {
    "wake": ("queued", "94d8a6df793c7e5e9ca0a2d98068c17584ecb47665c2472645ea1a64609ca8b4"),
    "silenced": ("silenced", "42fe0e37f6778620796feea3c2d418816d3fb3960430e15e7677ba5f8a478bcc"),
    "reply": ("queued", "a25e0d25c329157e1650713908cc986fab727f99a3e03fb8e59bf520f0ba2a0e"),
    "receipt": ("silenced", "2d58318350d2042de36d6a4528db8ef28ad6fb2bbe8ba1f5d5096ad10c3d547a"),
    "busy": ("queued", "d366786f3f028b483bdfa76a8b8d6e7aa342ed2586a3df6ed6eb01b80d5be127"),
    "late": ("needs_attention", "8595aec1686b9aafe37826b4c496ed4eb4793908382222a2f56d3caef0b90bf4"),
}


def test_stored_bytes_of_every_kind_of_send_stay_the_same(mailbox):
    adapter, client, directory, ledger, root, source = mailbox
    late = _ledger_with(mailbox, lambda: LATE)
    a, b = principal("agent-a"), principal("agent-b")
    batches = {}

    def step(name, submission):
        batches[name] = (submission.view.status, _last_batch(client))
        return submission.view
    sent = step("wake", ledger.send_recorded(a, source, _message(
        "original", kind="request", content="Approved brief.", refs=root.spec.allowed_refs)))
    step("silenced", ledger.send_recorded(a, source, _message("note")))
    claim = ledger.claim(b, sent.message.message_key, request_id="claim", consumer_id="worker", epoch=1)
    reply = step("reply", ledger.send_recorded(b, claim.assignment, _result(sent)))
    receipt = late.send_recorded(a, source, _receipt(reply))
    step("receipt", receipt)
    ledger.acknowledge(b, claim.claim_id, consumer_id="worker", epoch=1)
    other = _second_root(ledger, root, origin_work_id="second-work")
    second = step("busy", ledger.send_recorded(a, other, _message(
        "second", kind="handoff", content="Second brief."), receiver_busy=True))
    claim = ledger.claim(b, second.message.message_key, request_id="second-claim", consumer_id="worker", epoch=1)
    step("late", late.send_recorded(b, claim.assignment, _result(second, "late", "blocked")))
    first, closed = (ledger._verified(root_state_key(rid), _RootState)[0] for rid in (root.spec.root_request_id, "second-root"))
    mine = lambda sender, message_id: message_key(root.spec.root_request_id, sender, message_id)
    assert (first.message_count, first.receipt_count) == (3, 1)
    assert first.handoff_message_id == mine("agent-a", "original")
    assert first.response_message_id == mine("agent-b", "result")
    assert first.message_keys == (mine("agent-a", "original"), mine("agent-a", "note"), mine("agent-b", "result"),
                                  receipt.view.message.message_key)
    theirs = lambda sender, message_id: message_key("second-root", sender, message_id)
    assert (closed.message_count, closed.receipt_count) == (2, 0)
    assert closed.handoff_message_id == theirs("agent-a", "second")
    assert closed.response_message_id == theirs("agent-b", "late")  # a blocked reply closes the root too
    assert closed.message_keys == (theirs("agent-a", "second"), theirs("agent-b", "late"))
    assert batches == GOLDEN_BATCHES
