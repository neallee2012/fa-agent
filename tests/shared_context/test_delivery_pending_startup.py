"""Startup readers for paged instruction pending members. Offline synthetic fixture only."""

import json

from shared_context import behavior_wire as wire
from shared_context.behavior_delivery_pending import (
    WRITER_ENV, PendingHead, PendingMember, PendingPage, head_key, member_key, page_key,
)
from shared_context.behavior_migration import _kind_models
from shared_context.behavior_operations import control_key
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401

PENDING_KINDS = {"delivery_pending_head_v1", "delivery_pending_page_v1", "delivery_pending_member_v1"}


def admission_writes(inventory, binding):
    receipt = inventory.receipts[control_key("delivery-request", "admit", binding.event_id)]
    return dict(receipt.payload["writes"])


def pending_rows(inventory):
    return {key for key, row in inventory.rows.items() if row.payload["data"].get("kind") in PENDING_KINDS}


def legacy_ids(inventory, root):
    return {entry["obligation_id"] for entry in
            inventory.rows[control_key("delivery-index", root)].payload["data"]["entries"]}


def typed_row(inventory, key, model):
    return model.model_validate_json(json.dumps(inventory.rows[key].payload["data"]))


def acknowledge(c, binding, request_id):
    runtime = c.h.runtime
    runtime.controller.step(binding.binding_id)
    got = runtime.fetch(binding.binding_id, wire.Command(request_id=request_id + "-fetch"), c.human)
    runtime.acknowledge(binding.binding_id, wire.PullAcknowledgment(
        request_id=request_id, exchange_id=got.exchange_id, acknowledgment_token=got.acknowledgment_token,
        binding=wire.DeliveryBinding.model_validate_json(got.binding.model_dump_json())), c.human)
    assert runtime.job_status(binding.binding_id, c.human).obligation_fulfilled


def test_pending_kinds_are_classified_by_startup_readers():
    kinds = _kind_models()
    assert (kinds["delivery_pending_head_v1"], kinds["delivery_pending_page_v1"],
            kinds["delivery_pending_member_v1"]) == (PendingHead, PendingPage, PendingMember)


def test_new_members_and_original_entries_survive_cold_start_and_ack(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    monkeypatch.delenv(WRITER_ENV, raising=False)
    original = saved_turn(c, "pending-original-entry")
    root = original.binding.owner.root_budget_id
    index = control_key("delivery-index", root)
    before = read_startup(c)
    assert index in admission_writes(before, original.binding)
    assert not pending_rows(before)

    monkeypatch.setenv(WRITER_ENV, "true")
    new = saved_turn(c, "pending-new-member")
    mkey = member_key(root, new.binding.obligation_id)
    pending = read_startup(c)
    assert index not in admission_writes(pending, new.binding)
    assert {head_key(root), page_key(root, 1), mkey} <= set(admission_writes(pending, new.binding))
    assert pending.rows[index] == before.rows[index]
    member = typed_row(pending, mkey, PendingMember)
    assert member.page_number == 1 and member.entry.obligation_id == new.binding.obligation_id
    assert typed_row(pending, head_key(root), PendingHead).pending_count == 1
    assert typed_row(pending, page_key(root, 1), PendingPage).obligation_ids == (new.binding.obligation_id,)

    # A reader image with the writer off still reads and acknowledges existing members.
    monkeypatch.setenv(WRITER_ENV, "false")
    acknowledge(c, new.binding, "pending-new-member-ack")
    fulfilled = read_startup(c)
    assert typed_row(fulfilled, mkey, PendingMember).entry is None
    assert typed_row(fulfilled, head_key(root), PendingHead).pending_count == 0
    assert fulfilled.rows[index] == before.rows[index]

    acknowledge(c, original.binding, "pending-original-entry-ack")
    shrunk = read_startup(c)
    assert original.binding.obligation_id not in legacy_ids(shrunk, root)
    assert shrunk.rows[head_key(root)] == fulfilled.rows[head_key(root)]
    assert shrunk.rows[mkey] == fulfilled.rows[mkey]
