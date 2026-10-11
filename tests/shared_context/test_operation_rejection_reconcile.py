"""Original human rejection receipts are read without consuming or resubmitting."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from shared_context.behavior_runtime import runtime_key
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_operation_rejection import op_row, rejection_case, reject
from test_skill_guidance_authority import operation_path, protected_rows


def test_current_read_authority_can_reconcile_the_exact_original_rejection_without_writes(rejection_case):
    c = rejection_case
    accepted = reject(c)
    assert accepted.status_code == 200
    before, guard, batches = protected_rows(c.turn), c.h.store._guard(), len(c.h.transport.parts)
    with patch.object(c.h.store, "transact", side_effect=AssertionError("reconciliation must only read")):
        response = c.h.client.post(operation_path(c.turn) + "/rejection-reconcile",
            json=c.command, headers=c.h.headers("read-only"))
    assert response.status_code == 200
    assert response.json() == accepted.json()
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert len(c.h.transport.parts) == batches and c.effects == [] and c.h.endpoint.requests == []


def reconcile(case, *, command=None, identity="read-only"):
    return case.h.client.post(operation_path(case.turn) + "/rejection-reconcile",
        json=case.command if command is None else command, headers=case.h.headers(identity))


@pytest.mark.parametrize("stage", ["not-submitted", "plan", "consume", "decision"])
def test_reconciliation_never_advances_an_unconfirmed_rejection(rejection_case, monkeypatch, stage):
    c = rejection_case
    original = c.h.store._submit
    plan_key = c.h.runtime._plan_key(c.turn.binding, "operation-reject", c.command["request_id"])
    target = {
        "plan": runtime_key("plan-reserve", plan_key),
        "consume": runtime_key("consume", plan_key),
        "decision": runtime_key("operation-reject", c.turn.binding.binding_id, c.command["decision_id"]),
    }.get(stage)
    lost = []

    def lose_ack(operations, request):
        if request is not None and request.request_id == target and not lost:
            lost.append(True)
            c.h.transport.after_apply = "lost"
        try:
            return original(operations, request)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lose_ack)
    if stage != "not-submitted":
        response = reject(c)
        assert response.status_code == 503 and lost == [True]
    before, guard, batches = protected_rows(c.turn), c.h.store._guard(), len(c.h.transport.parts)
    with patch.object(c.h.store, "transact", side_effect=AssertionError("lookup cannot consume")) as writes:
        first, second = reconcile(c), reconcile(c)
        writes.assert_not_called()
    if stage == "decision":
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json() and first.json()["result"]["rejected"] is True
        assert first.json()["result"]["decision_id"] == c.command["decision_id"]
    else:
        assert first.status_code == second.status_code == 503
        assert first.json()["error"]["code"] == second.json()["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert len(c.h.transport.parts) == batches and c.effects == []
    assert ("human_rejection" in op_row(c).payload["data"]) == (stage == "decision")


@pytest.mark.parametrize("change,status", [
    ({"decision_id": "different-decision"}, 409),
    ({"description_digest": "0" * 64}, 409),
    ({"adapter_contract_digest": "0" * 64}, 409),
    ({"request_id": "different-request"}, 503),
    ({"schema_version": "future"}, 422),
    ({"rejected_by_oid": "not-client-authority"}, 422),
])
def test_lookup_requires_the_original_request_material(rejection_case, change, status):
    c = rejection_case
    assert reject(c).status_code == 200
    before, guard = protected_rows(c.turn), c.h.store._guard()
    response = reconcile(c, command={**c.command, **change})
    assert response.status_code == status
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard and c.effects == []


@pytest.mark.parametrize("identity", ["agent", "old-m3"])
def test_only_the_current_assigned_human_can_read_the_original_decision(rejection_case, identity):
    c = rejection_case
    assert reject(c).status_code == 200
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert reconcile(c, identity=identity).status_code == 403
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


def test_reconciliation_requires_current_read_permission(rejection_case):
    c = rejection_case
    assert reject(c).status_code == 200
    response = c.h.client.post("/operator/v1/standing-work-policy/revoke",
        headers=c.h.headers(), json={"decision_id": "revoke-original-reader"})
    assert response.status_code == 200
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert reconcile(c).status_code == 403
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


def test_revocation_during_lookup_does_not_return_a_stale_receipt(rejection_case, monkeypatch):
    c = rejection_case
    assert reject(c).status_code == 200
    original, raced = c.h.store._read_physical, []
    receipt_key = "behavior:v1:receipt:" + runtime_key(
        "operation-reject", c.turn.binding.binding_id, c.command["decision_id"])

    def revoke_after_read(row_key, model):
        result = original(row_key, model)
        if row_key == receipt_key and not raced:
            raced.append(True)
            with ThreadPoolExecutor(max_workers=1) as executor:
                response = executor.submit(c.h.client.post, "/operator/v1/standing-work-policy/revoke",
                    headers=c.h.headers(), json={"decision_id": "revoke-during-receipt-read"}).result(timeout=10)
            assert response.status_code == 200
        return result

    monkeypatch.setattr(c.h.store, "_read_physical", revoke_after_read)
    before = op_row(c)
    response = reconcile(c)
    assert raced == [True] and response.status_code == 412
    assert response.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert op_row(c) == before and c.effects == []
