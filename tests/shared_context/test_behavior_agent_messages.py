"""Contract-only peer-message tests; no entrypoint, SDK, or cloud integration."""

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from shared_context.behavior_agent_messages import (
    AgentAssignment, AgentDirectory, AgentMessage, AgentMessageLedger, AgentRoute, RootSpec, TeamReference, decide_wake,
)
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_operations import _write
from shared_context.behavior_store import ControlChanges, ControlRequest
from shared_context.behavior_work_policy import CostLedger, work_policy_key
from shared_context.errors import Problem
from test_behavior_store import store


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
