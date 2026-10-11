"""Pending delivery capacity at the original admission/receipt seams; no model or SDK."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_delivery import (
    DeliveryBinding, MessageRequest, PendingIndexRecord, ReceiptRequest, ScanRequest,
    VerifiedDeliveryReceipt, VerifiedWorkResult, WorkResultRequest,
)
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import _json
from shared_context.behavior_work_policy import WorkPreflightQuery
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_work_policy_submit_compatibility import post, route


@pytest.fixture
def runtime_path():
    return Path(__file__)


def test_new_admission_crosses_legacy_pending_row_limit_without_ack_or_losing_backlog(assistant, monkeypatch):
    monkeypatch.setenv("FA_DELIVERY_PENDING_INDEX_WRITER", "false")
    h, control, _, starts = route(assistant)
    index_key = control_key("delivery-index", control.policy.template.owner.root_budget_id)
    original_entity = h.store._entity
    rejected = []

    def entity(key, value):
        try:
            return original_entity(key, value)
        except Problem as error:
            if error.status == 413:
                rejected.append((getattr(value, "row_id", None), len(_json(value)), error.body["error"]["code"]))
            raise

    monkeypatch.setattr(h.store, "_entity", entity)
    for number in range(1, 134):
        if number == 133:
            before = h.store.read_snapshot((index_key,))[index_key]
            monkeypatch.setenv("FA_DELIVERY_PENDING_INDEX_WRITER", "true")
        command = InstructionSubmit(request_id=f"pending-capacity-{number:03}", content="Retain this original delivery.")
        if number >= 131:
            preflight = post(h, "preflight", WorkPreflightQuery(
                read_id=f"before-{number}", request_id=command.request_id,
                submit_body_sha256=hashlib.sha256(command.model_dump_json().encode()).hexdigest(),
                content=command.content,
            ))
            assert preflight.status_code == 200 and not preflight.json()["already_admitted"], preflight.text
        response = post(h, "submit", command)
        if number >= 131 or response.status_code != 200:
            retained = h.store.read_snapshot((index_key,))[index_key]
            index = PendingIndexRecord.model_validate_json(json.dumps(retained.payload["data"]))
            print({"admission": number, "status": response.status_code, "pending_retained": len(index.entries),
                   "rejected": rejected}, flush=True)
        assert response.status_code == 200, (number, response.text, rejected)
    assert len(starts) == 133 and not h.endpoint.requests
    assert h.store.read_snapshot((index_key,))[index_key] == before


def test_new_pending_members_keep_legacy_bytes_and_only_exact_receipt_fulfills(assistant, monkeypatch):
    monkeypatch.setenv("FA_DELIVERY_PENDING_INDEX_WRITER", "true")
    h, control, _, _ = route(assistant)
    core = h.delivery
    root = control.policy.template.owner.root_budget_id
    index_key = control_key("delivery-index", root)
    before = h.store.read_snapshot((index_key,))[index_key]
    tasks = []
    for number in range(2):
        accepted = post(h, "submit", InstructionSubmit(request_id=f"paged-{number}", content="Retain until received."))
        assert accepted.status_code == 200, accepted.text
        tasks.append(h.runtime.bindings[accepted.json()["binding_id"]])
    first, second = tasks
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
