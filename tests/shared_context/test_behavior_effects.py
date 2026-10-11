"""Concrete receiver + real Table SDK/local transport; actual_environment=NOT_RUN."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta

import pytest
from azure.core.exceptions import ResourceNotFoundError
from pydantic import ValidationError

from shared_context.behavior_delivery import VerifiedWorkResult, WorkResultRequest
from shared_context.behavior_effects import (
    GOAL_REVIEW, GOAL_VERIFY, WorkEffectDelivery, WorkEffectReceipt,
    WorkGoalUpdate, WorkStateEffectAdapter, effect_key,
)
from shared_context.behavior_operations import (
    AuthorizationRequest, GrantRecord, GrantSubject, OperationDescription,
    ToolBrokerCore, ToolIntent, canonical_material, control_key,
)
from shared_context.behavior_store import BehaviorStore
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT, NOW, Harness
from test_behavior_store import FakeTable, SCOPE, code


class EffectHarness(Harness):
    def __init__(self, *, wire=False):
        super().__init__(wire=wire)
        self.ob, self.admission, _ = self.admit()
        self.work_id = self.admission.work.work_id
        self.references = {}
        self.adapter = WorkStateEffectAdapter(
            self.core, allowed_work_ids=(self.work_id,), references=self.references,
        )
        self.effects = ToolBrokerCore(
            self.store, clock=lambda: self.time,
            adapter=self.adapter.registration, execution_adapter=self.adapter,
        )
        version = self.adapter.inspect(self.work_id, CONTEXT)
        self.spec = WorkGoalUpdate(
            work_id=self.work_id, obligation_id=self.ob, expected_etag=version.etag,
            expected_definition_digest=version.definition_digest, goal=GOAL_REVIEW,
        )
        self.references["current"] = self.spec
        self.effects.prepare(ToolIntent(operation_id="op-a", input_ref="current"), CONTEXT)
        prepared = self.data("operation", "op-a")
        self.put([(control_key("grant", "grant"), GrantRecord(
            grant_id="grant", approval_source="human", approval_id="trusted-human-approval",
            subjects=(GrantSubject(principal_id=CONTEXT.principal_id, conversation_id=CONTEXT.conversation_id),),
            root_budget_id=CONTEXT.root_budget_id, description_digest=prepared["description_digest"],
            adapter_contract_digest=prepared["adapter_contract_digest"], policy_id=CONTEXT.policy_id,
            policy_revision=1, revocation_revision=0, revoked=False,
            not_before=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=1),
            remaining_uses=1, remaining_units=1,
        ))])
        self.command = AuthorizationRequest(operation_id="op-a", attempt_id="a", grant_id="grant")

    @property
    def receiver(self):
        return self.effects.dispatcher.receiver

    def stage(self):
        self.effects.authorize(self.command, CONTEXT)
        self.effects.dispatcher._arm(self.command, CONTEXT)
        self.effects.dispatcher._consume(self.command, CONTEXT)
        return self.receiver._envelope("op-a", "a").command

    def execute(self):
        return self.effects.execute("op-a", "a", context=CONTEXT, grant_id="grant")

    def reconcile(self, context=CONTEXT):
        return self.effects.dispatcher.reconcile("op-a", "a", context)

    def work(self):
        return self.data("delivery-work-state", self.ob)

    def peer(self):
        store = BehaviorStore(FakeTable(self.client.backing), SCOPE)
        delivery = self.broker(store)
        adapter = WorkStateEffectAdapter(delivery, allowed_work_ids=(self.work_id,), references=self.references)
        return ToolBrokerCore(store, clock=lambda: self.time, adapter=adapter.registration, execution_adapter=adapter)


@pytest.mark.parametrize("wire", [False, True])
def test_actual_receiver_mutates_only_existing_v12_work_and_creates_receipt_in_same_egt(wire):
    h = EffectHarness(wire=wire)
    before, identity = copy.deepcopy(h.work()), copy.deepcopy(h.data("delivery-work", h.work_id))
    delivery = h.stage()
    count = h.submissions()
    receipt = h.receiver.receive(delivery, CONTEXT)
    assert h.submissions() == count + 1
    batch = h.client.parts[-1] if wire else h.client.submissions[-1]
    assert len(batch) == 5  # guard + transaction receipt + actual state + identity + receiver receipt
    if not wire:
        assert {item[1]["RowKey"] for item in batch[2:]} == {
            "behavior:v1:row:" + control_key("delivery-work-state", h.ob),
            "behavior:v1:row:" + control_key("delivery-work", h.work_id),
            "behavior:v1:row:" + effect_key("receipt", "op-a", "a"),
        }
        assert batch[2][2]["etag"] == h.spec.expected_etag
    else:
        assert batch[2][2]["If-Match"] == h.spec.expected_etag
    changed = h.work()
    assert changed["state"] == "queued" and changed["result"] is None
    assert changed["definition"] == {**before["definition"], "goal": GOAL_REVIEW}
    assert changed["definition_digest"] != before["definition_digest"]
    current_identity = h.data("delivery-work", h.work_id)
    assert current_identity == {**identity, "definition_digest": changed["definition_digest"]}
    assert current_identity["state_row_id"] == control_key("delivery-work-state", h.ob)
    proof = WorkEffectReceipt.model_validate_json(json.dumps(receipt.payload["result"]["receipt"]))
    assert proof.previous_definition_digest == before["definition_digest"]
    assert proof.applied_definition_digest == changed["definition_digest"]
    assert proof.prior_work_revision == hashlib.sha256(h.spec.expected_etag.encode()).hexdigest()
    assert h.data("grant", "grant")["remaining_uses"] == 0
    assert h.data("grant", "grant")["remaining_units"] == 0
    assert h.data("budget", "root")["remaining_attempts"] == 19
    assert h.data("budget", "root")["remaining_units"] == 39
    assert h.data("operation", "op-a")["effect_state"] == "unknown"
    assert h.data("dispatch", "op-a", "a")["phase"] == "consumed"
    assert h.data("delivery-obligation", h.ob)["fulfillment"] == "pending"
    assert h.material("work-effect-receipt", "op-a", "a").sha256 == receipt.payload["result"]["receiver_receipt_digest"]


def test_end_to_end_dispatch_acknowledges_operation_but_does_not_complete_work_or_delivery():
    h = EffectHarness()
    receipt = h.execute()
    assert h.work()["definition"]["goal"] == GOAL_REVIEW
    assert receipt.payload["result"]["operation_business_state"] == "succeeded"
    assert receipt.payload["result"]["work_state"] == "queued"
    assert receipt.payload["result"]["obligation_fulfilled"] is False
    assert h.data("dispatch", "op-a", "a")["phase"] == "settled"
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "resolved"
    assert h.data("operation", "op-a")["unsettled_attempt_ids"] == []
    count = h.submissions()
    with pytest.raises(Problem) as replay:
        h.execute()
    assert code(replay) == "OPERATION_DISPATCH_CONSUMED"
    assert h.submissions() == count
    assert h.reconcile() == receipt and h.submissions() == count


@pytest.mark.parametrize("kind,ids,updates,expected", [
    ("grant", ("grant",), {"revoked": True, "revocation_revision": 1}, "WORK_EFFECT_GRANT_DENIED"),
    ("grant", ("grant",), {"revocation_revision": 1}, "WORK_EFFECT_GRANT_DENIED"),
    ("policy", ("policy",), {"active": False}, "WORK_EFFECT_GRANT_DENIED"),
    ("policy", ("policy",), {"revision": 2}, "WORK_EFFECT_GRANT_DENIED"),
    ("owner", ("owner-one",), {"epoch": 2}, "OPERATION_OWNER_DENIED"),
    ("owner", ("owner-one",), {"active": False}, "OPERATION_OWNER_DENIED"),
    ("budget", ("root",), {"active": False}, "WORK_EFFECT_GRANT_DENIED"),
])
def test_receiver_rechecks_controls_after_dispatch_gate_before_effect(kind, ids, updates, expected):
    h = EffectHarness()
    delivery = h.stage()
    before = h.work()
    h.mutate(kind, *ids, **updates)
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.receiver.receive(delivery, CONTEXT)
    assert code(error) == expected
    assert h.submissions() == count and h.work() == before
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "pending_reconciliation"
    assert h.data("grant", "grant")["remaining_uses"] == 0
    with pytest.raises(ResourceNotFoundError):
        h.raw("work-effect-receipt", "op-a", "a")


@pytest.mark.parametrize("kind", ["grant-expiry", "dispatch-expiry", "clock-rollback", "naive-clock"])
def test_receiver_checks_trusted_utc_after_dispatch_before_effect(kind):
    h = EffectHarness()
    if kind == "grant-expiry":
        h.mutate("grant", "grant", expires_at=(NOW + timedelta(seconds=1)).isoformat())
    delivery = h.stage()
    before = h.work()
    h.time = {
        "grant-expiry": NOW + timedelta(seconds=1),
        "dispatch-expiry": NOW + timedelta(seconds=2),
        "clock-rollback": NOW - timedelta(seconds=1),
        "naive-clock": NOW.replace(tzinfo=None),
    }[kind]
    with pytest.raises(Problem) as error:
        h.receiver.receive(delivery, CONTEXT)
    assert code(error) in {"OPERATION_DISPATCH_WINDOW_CLOSED", "OPERATION_CLOCK_INVALID"}
    assert h.work() == before and h.data("grant", "grant")["remaining_units"] == 0


def test_expiry_during_receiver_serialization_is_rechecked_at_native_batch_hook():
    h = EffectHarness()
    delivery = h.stage()
    before = h.work()
    native = h.client.submit_transaction

    def delay(*args, **kwargs):
        h.time = NOW + timedelta(seconds=2)
        return native(*args, **kwargs)

    h.client.submit_transaction = delay
    with pytest.raises(Problem) as error:
        h.receiver.receive(delivery, CONTEXT)
    assert code(error) == "OPERATION_DISPATCH_WINDOW_CLOSED"
    assert h.work() == before
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "pending_reconciliation"


@pytest.mark.parametrize("predicate", ["revoke", "epoch", "work"])
def test_competing_control_write_at_effect_commit_fences_whole_batch(predicate):
    h = EffectHarness()
    delivery = h.stage()
    before = h.work()
    if predicate == "revoke":
        competing = lambda: h.mutate("grant", "grant", revoked=True, revocation_revision=1)
    elif predicate == "epoch":
        competing = lambda: h.mutate("owner", "owner-one", epoch=2)
    else:
        # Even an identical payload advances the native work ETag/authority guard.
        competing = lambda: h.mutate("delivery-work-state", h.ob, state="queued")
    h.client.before_submit = competing
    with pytest.raises(Problem) as error:
        h.receiver.receive(delivery, CONTEXT)
    assert code(error) == "BEHAVIOR_CONFLICT"
    assert h.work() == before
    with pytest.raises(ResourceNotFoundError):
        h.raw("work-effect-receipt", "op-a", "a")


def test_changed_work_revision_after_dispatch_is_not_a_preflight_only_condition():
    h = EffectHarness()
    delivery = h.stage()
    h.mutate("delivery-work-state", h.ob, state="queued")
    with pytest.raises(Problem) as error:
        h.receiver.receive(delivery, CONTEXT)
    assert code(error) == "WORK_EFFECT_REVISION_CONFLICT"
    assert h.work()["definition"]["goal"] != GOAL_REVIEW


@pytest.mark.parametrize("change", ["content", "version", "target", "method", "resource", "consume-id"])
def test_receiver_rejects_changed_material_or_identity_in_delivery(change):
    h = EffectHarness()
    delivery = h.stage()
    description = OperationDescription.model_validate_json(delivery.material.data)
    if change == "consume-id":
        forged = delivery.model_copy(update={"consume_request_id": "0" * 64})
    else:
        fields = {}
        if change == "content":
            fields["content"] = description.content.replace(GOAL_REVIEW.encode(), GOAL_VERIFY.encode())
        elif change == "version":
            fields["content"] = description.content.replace(h.spec.expected_etag.replace('"', '\\"').encode(), b'W/\\"fake-0\\"')
        elif change == "target":
            fields["target"] = "https://legacy.example/teams/team/works/old"
        elif change == "method":
            fields["method"] = "DELETE"
        else:
            fields["resources"] = (description.resources[0].model_copy(update={"version": "0" * 64}),)
        forged = delivery.model_copy(update={"material": canonical_material(description.model_copy(update=fields))})
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.receiver.receive(forged, CONTEXT)
    assert code(error) == "WORK_EFFECT_DELIVERY_DENIED"
    assert h.submissions() == count


def test_racing_duplicate_receivers_return_one_immutable_winner_receipt():
    h = EffectHarness()
    delivery = h.stage()
    peer = h.peer().dispatcher.receiver
    winners = []
    h.client.before_submit = lambda: winners.append(peer.receive(delivery, CONTEXT))
    original = h.receiver.receive(delivery, CONTEXT)
    assert len(winners) == 1 and original == winners[0]
    current_revision = h.adapter.inspect(h.work_id, CONTEXT).etag
    count = h.submissions()
    h.mutate("grant", "grant", revoked=True, revocation_revision=1)
    # Replay is historical even after revocation/expiry, not a fresh effect.
    h.time = NOW + timedelta(minutes=2)
    assert h.receiver.receive(delivery, CONTEXT) == original
    assert h.submissions() == count + 1
    assert h.adapter.inspect(h.work_id, CONTEXT).etag == current_revision
    assert h.data("budget", "root")["remaining_attempts"] == 19


def test_applied_but_sender_ack_lost_keeps_unknown_until_original_receipt_reconciliation():
    h = EffectHarness()
    calls = []

    def lost(command, context):
        calls.append(command)
        h.receiver.receive(command, context)
        raise TimeoutError("SYNTHETIC_SECRET_AFTER_APPLY")

    h.adapter.transport = lost
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "WORK_EFFECT_SEND_UNKNOWN"
    assert "SYNTHETIC_SECRET" not in str(error.value.body)
    assert h.work()["definition"]["goal"] == GOAL_REVIEW
    assert h.data("operation", "op-a")["effect_state"] == "unknown"
    assert h.data("dispatch", "op-a", "a")["phase"] == "consumed"
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "pending_reconciliation"
    with pytest.raises(Problem):
        h.execute()
    h.mutate("grant", "grant", revoked=True, revocation_revision=1)
    h.time = NOW + timedelta(minutes=2)
    result = h.reconcile()
    assert result.payload["result"]["effect_state"] == "applied"
    count = h.submissions()
    assert h.reconcile() == result and h.submissions() == count
    assert len(calls) == 1
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "resolved"


@pytest.mark.parametrize("failure", ["before", "after"])
def test_unknown_receiver_commit_reconciles_original_id_without_resend(failure):
    h = EffectHarness()
    delivery = h.stage()
    before = h.work()
    h.client.failure = failure
    with pytest.raises(Problem) as error:
        h.receiver.receive(delivery, CONTEXT)
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    h.client.failure = None
    if failure == "after":
        assert h.work()["definition"]["goal"] == GOAL_REVIEW
        assert h.reconcile().payload["result"]["effect_state"] == "applied"
    else:
        count = h.submissions()
        with pytest.raises(Problem) as unknown:
            h.reconcile()
        assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
        assert h.submissions() == count and h.work() == before
        assert h.data("dispatch-obligation", "op-a", "a")["state"] == "pending_reconciliation"
    assert h.data("grant", "grant")["remaining_uses"] == 0
    assert h.data("budget", "root")["remaining_units"] == 39


@pytest.mark.parametrize("failure", ["lost", "incomplete"])
def test_native_sdk_applied_lost_ack_retains_effect_receipt_and_original_reconciliation(failure):
    h = EffectHarness(wire=True)
    delivery = h.stage()
    h.client.after_apply = failure
    with pytest.raises(Problem) as error:
        h.receiver.receive(delivery, CONTEXT)
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert h.work()["definition"]["goal"] == GOAL_REVIEW
    h.client.after_apply = None
    assert h.reconcile().payload["result"]["effect_state"] == "applied"


def test_lost_sender_acknowledgement_commit_is_reconciled_without_reapplying_work():
    h = EffectHarness()
    delivery = h.stage()
    h.receiver.receive(delivery, CONTEXT)
    revision = h.adapter.inspect(h.work_id, CONTEXT).etag
    h.client.failure = "after"
    with pytest.raises(Problem) as error:
        h.reconcile()
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    h.client.failure = None
    count = h.submissions()
    assert h.reconcile().payload["result"]["effect_state"] == "applied"
    assert h.submissions() == count
    assert h.adapter.inspect(h.work_id, CONTEXT).etag == revision


def test_current_owner_can_reconcile_old_attempt_after_epoch_transfer_without_new_effect():
    h = EffectHarness()
    delivery = h.stage()
    h.receiver.receive(delivery, CONTEXT)
    h.mutate("owner", "owner-one", epoch=2)
    current = CONTEXT.model_copy(update={"epoch": 2})
    with pytest.raises(Problem):
        h.reconcile()
    assert h.reconcile(current).payload["result"]["effect_state"] == "applied"
    assert h.data("budget", "root")["remaining_attempts"] == 19


def test_accounting_reservation_without_consume_never_authorizes_receiver():
    h = EffectHarness()
    h.effects.authorize(h.command, CONTEXT)
    forged = WorkEffectDelivery(
        operation_id="op-a", attempt_id="a", consume_request_id=control_key("dispatch-consume", "op-a", "a"),
        material=h.material("operation", "op-a"),
    )
    with pytest.raises(Problem):
        h.receiver.receive(forged, CONTEXT)
    assert h.work()["definition"]["goal"] != GOAL_REVIEW


def test_unconfirmed_consume_never_invokes_receiver_on_receipt_replay():
    h = EffectHarness()
    h.effects.authorize(h.command, CONTEXT)
    h.effects.dispatcher._arm(h.command, CONTEXT)
    before = h.work()
    h.client.failure = "after"
    with pytest.raises(Problem) as error:
        h.effects.dispatcher._consume(h.command, CONTEXT)
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    h.client.failure = None
    with pytest.raises(Problem):
        h.execute()
    with pytest.raises(Problem) as unknown:
        h.reconcile()
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert h.work() == before


def test_prepare_a_then_alias_b_and_c_cannot_change_the_effect_content():
    h = EffectHarness()
    h.references["current"] = h.spec.model_copy(update={"goal": GOAL_VERIFY})
    delivery = h.stage()
    h.references["current"] = h.spec.model_copy(update={"work_id": "legacy"})
    h.receiver.receive(delivery, CONTEXT)
    assert h.work()["definition"]["goal"] == GOAL_REVIEW


def test_definition_revision_invalidates_old_result_evidence_without_weakening_checks():
    h = EffectHarness()
    old_digest = h.work()["definition_digest"]
    h.results["old-proof"] = VerifiedWorkResult(
        evidence_id="old-proof", verifier_version=h.config.verifier_version, work_id=h.work_id,
        definition_digest=old_digest, state="succeeded", passed_checks=("answer-checked",),
        content=b"old result", proof=b"old synthetic evidence",
    )
    h.execute()
    with pytest.raises(Problem) as error:
        h.core.record_work_result(WorkResultRequest(obligation_id=h.ob, evidence_id="old-proof"), CONTEXT)
    assert code(error) == "DELIVERY_WORK_EVIDENCE_DENIED"
    assert h.work()["definition"]["required_checks"] == ["answer-checked"]
    h.work_result(h.ob, evidence_id="new-proof")
    assert h.work()["state"] == "succeeded"


@pytest.mark.parametrize("field", ["actor", "context", "approved", "grant", "grant_id", "clock", "policy", "epoch"])
def test_receiver_command_cannot_supply_authority(field):
    h = EffectHarness()
    delivery = h.stage()
    with pytest.raises(ValidationError):
        WorkEffectDelivery(**delivery.model_dump(), **{field: True})


def test_terminal_or_unadmitted_work_is_not_an_alternate_mutation_authority():
    h = EffectHarness()
    delivery = h.stage()
    h.work_result(h.ob)
    with pytest.raises(Problem) as error:
        h.receiver.receive(delivery, CONTEXT)
    assert code(error) == "WORK_EFFECT_REVISION_CONFLICT"
    with pytest.raises(Problem):
        h.adapter.inspect("legacy-work", CONTEXT)
    assert h.adapter.contract.allowed_methods == ("PATCH",)
    assert h.adapter.contract.idempotency_contract_version is None
    assert h.adapter.contract.terminality_contract_version is None
    for goal in ("Bearer SYNTHETIC_TOKEN", "arbitrary free text"):
        with pytest.raises(ValidationError):
            WorkGoalUpdate(**{**h.spec.model_dump(), "goal": goal})


def test_another_authority_cannot_install_receiver_for_this_work():
    h = EffectHarness()
    other = BehaviorStore(FakeTable(), SCOPE.model_copy(update={"authority_domain": "another-authority"}))
    with pytest.raises(Problem) as error:
        ToolBrokerCore(other, clock=lambda: NOW, adapter=h.adapter.registration, execution_adapter=h.adapter)
    assert code(error) == "WORK_EFFECT_AUTHORITY_MISMATCH"
