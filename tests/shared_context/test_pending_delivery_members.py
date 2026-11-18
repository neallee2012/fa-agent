"""Pending delivery capacity at the original admission/receipt seams; no model or SDK."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_delivery import (
    DeliveryBinding, MessageRequest, PendingIndexRecord, ReceiptRequest, ScanRequest,
    VerifiedDeliveryReceipt, VerifiedWorkResult, WorkResultRequest,
)
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import _json
from shared_context.errors import Problem
from shared_context.source_materials import digest
from legacy_submit_rows_helpers import load_store
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_work_policy_submit_compatibility import route


PAGED = ("9f994b00fdb80614adc6b559bd45954381dedda573aa51844180247535368bd0",
         "cfdd8b2c843c3d00c18f838596031679beb3a9a1d41a0fdbfb6aadedf95ee717")


def test_new_pending_members_keep_legacy_bytes_and_only_exact_receipt_fulfills(assistant_api, monkeypatch):
    monkeypatch.setenv("FA_DELIVERY_PENDING_INDEX_WRITER", "true")
    h, control, _, _ = route(assistant_api)
    core = h.delivery
    root = control.policy.template.owner.root_budget_id
    index_key = control_key("delivery-index", root)
    before = h.store.read_snapshot((index_key,))[index_key]
    # The two instructions "paged-0" and "paged-1" the retiring submit admitted with the pending writer on: the whole
    # stored table, then a restart's refresh installs their bindings.
    load_store(h.transport.backing, "pending-members")
    control.refresh()
    first, second = (h.runtime.bindings[binding] for binding in PAGED)
    h.time += timedelta(seconds=core.contract.review_interval_seconds)
    scan = core.scan_due(ScanRequest(scan_id="first-paged-scan"), first.owner)
    assert [entry["obligation_id"] for entry in scan.payload["result"]["due"]] == [first.obligation_id]
    outcome = VerifiedWorkResult(evidence_id="synthetic-completion", verifier_version=core.contract.verifier_version,
        work_id=first.work.work_id, definition_digest=digest(first.work.model_dump(mode="json")),
        state="succeeded", passed_checks=first.work.required_checks, content=b"Original saved public answer.",
        proof=b"Offline verifier input; not a model or native execution.")
    core.registration = replace(core.registration, verify_work_result=lambda _: outcome)
    core.record_work_result(WorkResultRequest(obligation_id=first.obligation_id, evidence_id=outcome.evidence_id), first.owner)
    prepared = core.prepare_message(MessageRequest(obligation_id=first.obligation_id,
        delivery_id="first-paged-answer", expected_version=0, message_kind="final"), first.owner)
    assert core.scan_due(ScanRequest(scan_id="saved-not-acked"), first.owner).payload["result"]["due"]
    proof = VerifiedDeliveryReceipt(
        evidence_id="synthetic-received", verifier_version=core.contract.verifier_version,
        binding=DeliveryBinding.model_validate_json(json.dumps(prepared.payload["result"]["binding"])),
        send_outcome="emitted", confirmed_levels=("client_received",), dispatch_authorization_ref="original-dispatch",
        proof=b"Offline exact receipt verifier; not a human receipt.")
    core.registration = replace(core.registration, verify_receipt=lambda _: proof)
    command = ReceiptRequest(obligation_id=first.obligation_id, delivery_id="first-paged-answer",
                             evidence_id=proof.evidence_id)
    receipt = core.record_receipt(command, first.owner)
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    guard = h.store._guard()
    assert core.record_receipt(command, first.owner) == receipt and h.store._guard() == guard
    assert not core.scan_due(ScanRequest(scan_id="after-exact-ack"), first.owner).payload["result"]["due"]
    assert core.scan_due(ScanRequest(scan_id="other-original-still-pending"), second.owner).payload["result"]["due"]
    assert h.store.read_snapshot((index_key,))[index_key] == before
    assert not h.endpoint.requests
