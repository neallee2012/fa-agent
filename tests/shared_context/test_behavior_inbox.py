"""Real pull-channel code over an explicitly OFFLINE fake Table authority.

These tests do not prove JWT/native deployment readiness, UI rendering, human
reading, Azure retention, or Foundry execution.
"""

import json
import multiprocessing
import os
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError

from shared_context.behavior_delivery import ChannelContract, DeliveryBinding, DeliveryConsumer
from shared_context.behavior_inbox import (
    INBOX_CHANNEL_ID, INBOX_VERSION, InboxAcknowledgment, InboxFetch, InboxRegistration,
    PullInbox, VerifiedInboxAccess,
)
from shared_context.behavior_store import BehaviorStore
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT, NOW, ROUTE, Harness
from test_behavior_store import FakeTable, SCOPE, code


class InboxHarness(Harness):
    def __init__(self):
        super().__init__(channel=ChannelContract(
            channel_id=INBOX_CHANNEL_ID, version=INBOX_VERSION,
            supported_levels=("client_received",), max_confirmation_level="client_received",
        ))
        self.access = {}
        self.inbox = self.rebuild(self.store)

    def rebuild(self, store):
        inbox = PullInbox(store, InboxRegistration("offline-auth-v1", lambda key: self.lookup(self.access, key)))
        self.registration = replace(self.registration, verify_receipt=inbox.verify_receipt, channel_adapter=inbox)
        self.core = self.broker(store, self.registration)
        inbox.attach(self.core)
        return inbox

    def command(self, *args, **kwargs):
        command = super().command(*args, **kwargs)
        command = command.model_copy(update={"route": ROUTE.model_copy(update={"channel_id": INBOX_CHANNEL_ID})})
        self.attest(command, kwargs.get("context", CONTEXT), kwargs.get("source", "human"))
        return command

    def prepare(self, *, budget=20):
        ob, _, _ = self.admit()
        if budget:
            self.work_result(ob)
        else:
            self.mutate("budget", "root", remaining_attempts=0, remaining_units=0)
        result = DeliveryConsumer(self.core).consume(ob, CONTEXT, "delivery-worker")
        return ob, result["binding"]["delivery_id"]

    def allow(self, ob, delivery, action="fetch", handle="access", **updates):
        binding = DeliveryBinding.model_validate_json(json.dumps(self.data("delivery-outbox", delivery)["binding"]))
        self.access[handle] = VerifiedInboxAccess(
            evidence_id=handle, principal_id=CONTEXT.principal_id,
            authentication_version="offline-auth-v1", binding=binding, action=action,
            policy_revision=1, observed_at=NOW, expires_at=NOW + timedelta(minutes=5),
            proof=b"explicit offline authenticated-client fixture", **updates,
        )
        return InboxFetch(obligation_id=ob, delivery_id=delivery, access_evidence_id=handle)

    def ack(self, response, ob, delivery, handle="ack-access", **updates):
        self.allow(ob, delivery, "acknowledge", handle)
        return InboxAcknowledgment(
            obligation_id=ob, delivery_id=delivery, access_evidence_id=handle,
            exchange_id=response.exchange_id, acknowledgment_token=response.acknowledgment_token,
            binding=response.binding, **updates,
        )


def test_pull_returns_exact_saved_bytes_and_only_authenticated_ack_fulfills():
    h = InboxHarness()
    ob, delivery = h.prepare()
    response = h.inbox.fetch(h.allow(ob, delivery), CONTEXT)
    assert response.payload == h.material("delivery-outbox", delivery).data
    assert response.binding.payload_digest == h.material("delivery-outbox", delivery).sha256
    assert response.send_outcome == "unknown"
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"
    assert h.data("delivery-obligation", ob)["delivery_state"] == "delivery_unknown"
    assert h.data("delivery-outbox", delivery)["send_outcome"] == "unknown"
    receipt = h.inbox.acknowledge(h.ack(response, ob, delivery), CONTEXT)
    assert receipt.payload["result"]["confirmed_levels"] == ["client_received"]
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    assert receipt.payload["result"]["actual_environment"] == "NOT_RUN"
    assert h.data("delivery-work-state", ob)["state"] == "succeeded"


@pytest.mark.parametrize("level", ["persisted", "provider_accepted", "ui_rendered", "human_read"])
def test_client_cannot_claim_an_unsupported_confirmation_level(level):
    h = InboxHarness()
    ob, delivery = h.prepare()
    response = h.inbox.fetch(h.allow(ob, delivery), CONTEXT)
    with pytest.raises(ValidationError):
        h.ack(response, ob, delivery, confirmation_level=level)
    with pytest.raises(ValidationError):
        InboxFetch(obligation_id=ob, delivery_id=delivery, access_evidence_id="access", delivered=True)
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "recipient_id", "channel_id", "thread_id"])
def test_pull_rejects_wrong_exact_destination_binding(field):
    h = InboxHarness()
    ob, delivery = h.prepare()
    command = h.allow(ob, delivery)
    access = h.access["access"]
    h.access["access"] = access.model_copy(update={"binding": access.binding.model_copy(update={
        "route": access.binding.route.model_copy(update={field: "wrong"}),
    })})
    with pytest.raises(Problem):
        h.inbox.fetch(command, CONTEXT)
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"


def test_current_read_policy_is_checked_even_on_fetch_receipt_replay():
    h = InboxHarness()
    ob, delivery = h.prepare()
    command = h.allow(ob, delivery)
    h.inbox.fetch(command, CONTEXT)
    h.mutate("policy", "policy", active=False)
    with pytest.raises(Problem) as denied:
        h.inbox.fetch(command, CONTEXT)
    assert code(denied) == "INBOX_POLICY_DENIED"


def test_revocation_winning_fetch_gate_prevents_releasing_bytes():
    h = InboxHarness()
    ob, delivery = h.prepare()
    command = h.allow(ob, delivery)
    h.client.before_submit = lambda: h.mutate("policy", "policy", active=False)
    with pytest.raises(Problem) as denied:
        h.inbox.fetch(command, CONTEXT)
    assert code(denied) == "BEHAVIOR_CONFLICT"
    assert h.data("delivery-outbox", delivery)["send_outcome"] is None


def test_ack_is_historical_receipt_not_a_new_send_after_policy_revocation():
    h = InboxHarness()
    ob, delivery = h.prepare()
    response = h.inbox.fetch(h.allow(ob, delivery), CONTEXT)
    h.mutate("policy", "policy", active=False)
    receipt = h.inbox.acknowledge(h.ack(response, ob, delivery), CONTEXT)
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    assert h.data("delivery-outbox", delivery)["receipt_count"] == 1


@pytest.mark.parametrize("part", ["digest", "version", "token", "exchange", "identity"])
def test_client_ack_cannot_fabricate_a_delivery_receipt(part):
    h = InboxHarness()
    ob, delivery = h.prepare()
    response = h.inbox.fetch(h.allow(ob, delivery), CONTEXT)
    command = h.ack(response, ob, delivery)
    if part == "identity":
        h.access["ack-access"] = h.access["ack-access"].model_copy(update={"principal_id": "not-recipient"})
    elif part in {"digest", "version"}:
        command = command.model_copy(update={"binding": command.binding.model_copy(update={
            "payload_digest" if part == "digest" else "version": "0" * 64 if part == "digest" else 2,
        })})
    else:
        command = command.model_copy(update={"acknowledgment_token" if part == "token" else "exchange_id": "wrong"})
    with pytest.raises(Problem):
        h.inbox.acknowledge(command, CONTEXT)
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"


def test_ack_commit_response_loss_is_recovered_from_authority_without_another_fetch():
    h = InboxHarness()
    ob, delivery = h.prepare()
    response = h.inbox.fetch(h.allow(ob, delivery), CONTEXT)
    command = h.ack(response, ob, delivery)
    h.client.failure = "after"
    with pytest.raises(Problem) as unknown:
        h.inbox.acknowledge(command, CONTEXT)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"
    rebuilt = h.rebuild(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    receipt = rebuilt.reconcile(delivery, CONTEXT)
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    assert h.data("delivery-outbox", delivery)["receipt_count"] == 1
    assert rebuilt.reconcile(delivery, CONTEXT) == receipt
    assert h.data("budget", "root")["remaining_attempts"] == 20


def test_fetch_commit_response_loss_retains_unknown_and_allows_only_authenticated_same_bytes():
    h = InboxHarness()
    ob, delivery = h.prepare()
    command = h.allow(ob, delivery)
    h.client.failure = "after"
    with pytest.raises(Problem):
        h.inbox.fetch(command, CONTEXT)
    rebuilt = h.rebuild(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    with pytest.raises(Problem) as unknown:
        rebuilt.reconcile(delivery, CONTEXT)
    assert code(unknown) == "INBOX_DELIVERY_UNKNOWN"
    response = rebuilt.fetch(command, CONTEXT)
    assert response.payload == h.material("delivery-outbox", delivery).data
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"


def test_late_ack_for_old_progress_cannot_fulfill_current_final():
    h = InboxHarness()
    ob, _, _ = h.admit()
    h.message(ob, "progress", "old")
    old = h.inbox.fetch(h.allow(ob, "old", handle="old-fetch"), CONTEXT)
    h.work_result(ob)
    h.message(ob, "final", "final")
    h.inbox.acknowledge(h.ack(old, ob, "old"), CONTEXT)
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"
    final = h.inbox.fetch(h.allow(ob, "final", handle="new-fetch"), CONTEXT)
    h.inbox.acknowledge(h.ack(final, ob, "final", handle="new-ack"), CONTEXT)
    assert h.data("delivery-obligation", ob)["fulfillment"] == "fulfilled"


def test_zero_model_budget_failure_can_be_received_and_fulfilled():
    h = InboxHarness()
    ob, delivery = h.prepare(budget=0)
    response = h.inbox.fetch(h.allow(ob, delivery), CONTEXT)
    payload = json.loads(response.payload)
    assert payload["message_kind"] == "failure" and payload["work_state"] == "queued"
    h.inbox.acknowledge(h.ack(response, ob, delivery), CONTEXT)
    assert h.data("delivery-obligation", ob)["fulfillment"] == "fulfilled"
    assert h.data("budget", "root")["remaining_attempts"] == 0


def test_full_pull_allowance_keeps_ack_lane_available():
    h = InboxHarness()
    ob, delivery = h.prepare()
    for number in range(8):
        response = h.inbox.fetch(h.allow(ob, delivery, handle=f"fetch-{number}"), CONTEXT)
    with pytest.raises(Problem) as full:
        h.inbox.fetch(h.allow(ob, delivery, handle="overflow"), CONTEXT)
    assert code(full) == "INBOX_FETCH_LIMIT"
    h.inbox.acknowledge(h.ack(response, ob, delivery), CONTEXT)
    assert h.data("delivery-obligation", ob)["fulfillment"] == "fulfilled"


def test_native_activation_rejects_callbacks_without_persisted_deployment_proof():
    h = InboxHarness()
    inbox = PullInbox(h.store, replace(
        h.inbox.registration, verify_deployment=lambda _: pytest.fail("no persisted proof to verify"),
    ))
    native = h.broker(h.store, replace(h.registration, mode="native", channel_adapter=inbox))
    inbox.attach(native)
    with pytest.raises(Problem) as disabled:
        native.admit(h.command(), CONTEXT)
    assert code(disabled) == "DELIVERY_NATIVE_DISABLED"


def process_ack(path, config_json, command_json, access_json):
    from shared_context.behavior_delivery import DeliveryCore, RuntimeContract, RuntimeRegistration
    from test_behavior_workers import SQLiteBacking

    client = FakeTable(SQLiteBacking(path))
    store = BehaviorStore(client, SCOPE)
    access = VerifiedInboxAccess.model_validate_json(access_json)
    inbox = PullInbox(store, InboxRegistration("offline-auth-v1", lambda _: access))
    core = DeliveryCore(store, clock=lambda: NOW, registration=RuntimeRegistration(
        RuntimeContract.model_validate_json(config_json), mode="offline_contract",
        verify_receipt=inbox.verify_receipt, channel_adapter=inbox,
    ))
    inbox.attach(core)
    client.failure = "after"
    try:
        inbox.acknowledge(InboxAcknowledgment.model_validate_json(command_json), CONTEXT)
    except Problem as exc:
        if exc.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN":
            os._exit(23)
        raise


def test_real_process_ack_loss_rebuilds_receipt_from_persisted_evidence(tmp_path):
    from shared_context.behavior_delivery import authority_snapshot
    from shared_context.behavior_operations import control_key
    from test_behavior_workers import SQLiteBacking, join

    h = InboxHarness()
    ob, delivery = h.prepare()
    response = h.inbox.fetch(h.allow(ob, delivery), CONTEXT)
    command = h.ack(response, ob, delivery)
    path = tmp_path / "inbox.db"
    backing = SQLiteBacking(path)
    backing.seed(h.client.backing)
    process = multiprocessing.get_context("spawn").Process(target=process_ack, args=(
        str(path), h.config.model_dump_json(), command.model_dump_json(), h.access["ack-access"].model_dump_json(),
    ))
    process.start()
    join(process)
    assert process.exitcode == 23
    store = BehaviorStore(FakeTable(backing), SCOPE)
    rebuilt = h.rebuild(store)
    receipt = rebuilt.reconcile(delivery, CONTEXT)
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    key = control_key("delivery-outbox", delivery)
    row = authority_snapshot(store, (key,))[key]
    assert row.payload["data"]["receipt_count"] == 1
    assert row.payload["data"]["confirmed_levels"] == ["client_received"]
    assert rebuilt.reconcile(delivery, CONTEXT) == receipt
