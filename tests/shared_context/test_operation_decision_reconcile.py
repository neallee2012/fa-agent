"""Approval/revocation receipt lookup is history, never fresh operation authority."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import runtime_key
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_operation_rejection import approved_revocation, lose_consume_ack, rejection_case
from test_skill_guidance_authority import approval_command, operation_path, protected_rows


@pytest.mark.parametrize("decision", ["approval", "revocation"])
def test_current_reader_recovers_original_decisions_without_reactivating_a_revoked_grant(rejection_case, decision):
    c = rejection_case
    approval = approval_command(c.turn, c.prepared)
    accepted = c.h.client.post(operation_path(c.turn) + "/approve",
        headers=c.h.headers(), content=approval.model_dump_json())
    assert accepted.status_code == 200
    revocation = wire.OperationRevocation(request_id="original-revocation", decision_id="original-revocation-decision",
        description_digest=approval.description_digest, expected_revocation_revision=0)
    revoked = c.h.client.post(operation_path(c.turn) + "/revoke",
        headers=c.h.headers(), content=revocation.model_dump_json())
    assert revoked.status_code == 200
    command, original = (approval, accepted) if decision == "approval" else (revocation, revoked)
    before, guard, batches = protected_rows(c.turn), c.h.store._guard(), len(c.h.transport.parts)
    grant_key = control_key("grant", c.turn.binding.grant_id)
    assert before[grant_key].payload["data"]["revoked"] is True
    with patch.object(c.h.store, "transact", side_effect=AssertionError("lookup cannot create authority")) as writes:
        response = c.h.client.post(operation_path(c.turn) + "/" + decision + "-reconcile",
            headers=c.h.headers("read-only"), content=command.model_dump_json())
        writes.assert_not_called()
    assert response.status_code == 200
    assert response.json() == original.json()
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert len(c.h.transport.parts) == batches and c.effects == []


def decision_command(case, decision):
    return (approval_command(case.turn, case.prepared) if decision == "approval" else approved_revocation(case))


def action_for(decision):
    return "approve" if decision == "approval" else "revoke"


def lookup(case, decision, command, identity="read-only"):
    return case.h.client.post(operation_path(case.turn) + "/" + decision + "-reconcile",
        headers=case.h.headers(identity), content=command.model_dump_json())


@pytest.mark.parametrize("decision", ["approval", "revocation"])
@pytest.mark.parametrize("stage", ["not-submitted", "plan", "consume", "decision"])
def test_unknown_decision_lookup_never_completes_or_consumes_the_original_request(
        rejection_case, monkeypatch, decision, stage):
    c = rejection_case
    command = decision_command(c, decision)
    kind = "operation-" + action_for(decision)
    plan_key = c.h.runtime._plan_key(c.turn.binding, kind, command.request_id)
    target = {
        "plan": runtime_key("plan-reserve", plan_key),
        "consume": runtime_key("consume", plan_key),
        "decision": runtime_key(kind, c.turn.binding.binding_id, command.decision_id),
    }.get(stage)
    original, lost = c.h.store._submit, []

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
        response = c.h.client.post(operation_path(c.turn) + "/" + action_for(decision),
            headers=c.h.headers(), content=command.model_dump_json())
        assert response.status_code == 503 and lost == [True]
    before, guard, batches = protected_rows(c.turn), c.h.store._guard(), len(c.h.transport.parts)
    with patch.object(c.h.store, "transact", side_effect=AssertionError("only read original receipts")) as writes:
        first, second = lookup(c, decision, command), lookup(c, decision, command)
        writes.assert_not_called()
    if stage == "decision":
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["control_request_id"] == target
    else:
        assert first.status_code == second.status_code == 503
        assert first.json()["error"]["code"] == second.json()["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert len(c.h.transport.parts) == batches and c.effects == []


@pytest.mark.parametrize("decision", ["approval", "revocation"])
def test_a_later_explicit_decision_does_not_confirm_the_original_unknown(rejection_case, monkeypatch, decision):
    c = rejection_case
    command = decision_command(c, decision)
    action = action_for(decision)
    lose_consume_ack(c, "operation-" + action, command.request_id, monkeypatch)
    path = operation_path(c.turn) + "/" + action
    assert c.h.client.post(path, headers=c.h.headers(), content=command.model_dump_json()).status_code == 503
    later = command.model_copy(update={"request_id": "new-explicit-request", "decision_id": "new-explicit-decision"})
    assert c.h.client.post(path, headers=c.h.headers(), content=later.model_dump_json()).status_code == 200
    before, guard = protected_rows(c.turn), c.h.store._guard()
    original = lookup(c, decision, command)
    confirmed = lookup(c, decision, later)
    assert original.status_code == 503 and original.json()["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    assert confirmed.status_code == 200
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard and c.effects == []


@pytest.mark.parametrize("decision", ["approval", "revocation"])
@pytest.mark.parametrize("field", ["request_id", "decision_id", "description_digest"])
def test_receipt_lookup_remains_bound_to_the_original_typed_material(rejection_case, decision, field):
    c = rejection_case
    command = decision_command(c, decision)
    assert c.h.client.post(operation_path(c.turn) + "/" + action_for(decision),
        headers=c.h.headers(), content=command.model_dump_json()).status_code == 200
    changed = command.model_copy(update={field: "0" * 64 if field == "description_digest" else "other-id"})
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert lookup(c, decision, changed).status_code == (503 if field == "request_id" else 409)
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


@pytest.mark.parametrize("decision", ["approval", "revocation"])
def test_lookup_does_not_drop_original_expiry_or_revocation_revision(rejection_case, decision):
    c = rejection_case
    command = decision_command(c, decision)
    assert c.h.client.post(operation_path(c.turn) + "/" + action_for(decision),
        headers=c.h.headers(), content=command.model_dump_json()).status_code == 200
    change = ({"expires_at": command.expires_at + timedelta(seconds=1)} if decision == "approval" else
              {"expected_revocation_revision": 1})
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert lookup(c, decision, command.model_copy(update=change)).status_code == 409
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


@pytest.mark.parametrize("decision", ["approval", "revocation"])
@pytest.mark.parametrize("identity", ["agent", "old-m3"])
def test_decision_receipts_are_only_read_by_the_current_configured_human(rejection_case, decision, identity):
    c = rejection_case
    command = decision_command(c, decision)
    assert c.h.client.post(operation_path(c.turn) + "/" + action_for(decision),
        headers=c.h.headers(), content=command.model_dump_json()).status_code == 200
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert lookup(c, decision, command, identity).status_code == 403
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


def test_an_expired_approval_receipt_is_history_not_a_new_grant(rejection_case):
    c = rejection_case
    command = approval_command(c.turn, c.prepared).model_copy(update={"expires_at": c.h.time + timedelta(seconds=2)})
    accepted = c.h.client.post(operation_path(c.turn) + "/approve",
        headers=c.h.headers(), content=command.model_dump_json())
    assert accepted.status_code == 200
    c.h.time += timedelta(seconds=3)
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert lookup(c, "approval", command).json() == accepted.json()
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    grant = before[control_key("grant", c.turn.binding.grant_id)].payload["data"]
    assert grant["expires_at"] == command.model_dump(mode="json")["expires_at"]


@pytest.mark.parametrize("decision", ["approval", "revocation"])
def test_current_read_revocation_still_fences_a_saved_decision(rejection_case, decision):
    c = rejection_case
    command = decision_command(c, decision)
    assert c.h.client.post(operation_path(c.turn) + "/" + action_for(decision),
        headers=c.h.headers(), content=command.model_dump_json()).status_code == 200
    revoked = c.h.client.post("/operator/v1/standing-work-policy/revoke",
        headers=c.h.headers(), json={"decision_id": "revoke-current-receipt-reader"})
    assert revoked.status_code == 200
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert lookup(c, decision, command).status_code == 403
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


@pytest.mark.parametrize("decision", ["approval", "revocation"])
def test_a_concurrent_authority_change_cannot_publish_a_stale_decision_receipt(
        rejection_case, monkeypatch, decision):
    c = rejection_case
    command = decision_command(c, decision)
    action = action_for(decision)
    assert c.h.client.post(operation_path(c.turn) + "/" + action,
        headers=c.h.headers(), content=command.model_dump_json()).status_code == 200
    receipt_key = "behavior:v1:receipt:" + runtime_key(
        "operation-" + action, c.turn.binding.binding_id, command.decision_id)
    original, raced = c.h.store._read_physical, []

    def revoke_after_read(row_key, model):
        value = original(row_key, model)
        if row_key == receipt_key and not raced:
            raced.append(True)
            with ThreadPoolExecutor(max_workers=1) as executor:
                response = executor.submit(c.h.client.post, "/operator/v1/standing-work-policy/revoke",
                    headers=c.h.headers(), json={"decision_id": "revoke-during-decision-read"}).result(timeout=10)
            assert response.status_code == 200
        return value

    monkeypatch.setattr(c.h.store, "_read_physical", revoke_after_read)
    before = protected_rows(c.turn)
    response = lookup(c, decision, command)
    assert raced == [True] and response.status_code == 412
    assert response.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert protected_rows(c.turn) == before and c.effects == []
