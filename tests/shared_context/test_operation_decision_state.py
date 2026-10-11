"""Current stored decision facts are not an execution or rejection permit."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from unittest.mock import patch

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_operations import control_key
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_operation_rejection import approved_revocation, lose_consume_ack, rejection_case, reject
from test_skill_guidance_authority import approval_command, operation_path, protected_rows


def observe(case, identity="read-only"):
    return case.h.client.get(operation_path(case.turn) + "/decision-state", headers=case.h.headers(identity))


def test_prepared_and_granted_operations_with_unknown_effect_have_different_read_only_facts(rejection_case):
    c = rejection_case
    before, guard = protected_rows(c.turn), c.h.store._guard()
    with patch.object(c.h.store, "transact", side_effect=AssertionError("observation must not write")) as writes:
        prepared = observe(c)
        writes.assert_not_called()
    assert prepared.status_code == 200
    assert prepared.json()["schema_version"] == "fa-operation-decision-state-v1"
    assert prepared.json()["purpose"] == "observation-only-not-operation-permission"
    assert prepared.json()["binding_id"] == c.turn.binding.binding_id
    assert prepared.json()["operation_id"] == c.turn.binding.operation_id
    assert prepared.json()["description_digest"] == c.prepared["description_digest"]
    assert prepared.json()["grant"] is None and prepared.json()["rejection"] is None
    assert prepared.json()["effect_state"] == "unknown" and prepared.json()["business_state"] == "pending"
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    command = approval_command(c.turn, c.prepared)
    assert c.h.client.post(operation_path(c.turn) + "/approve",
        headers=c.h.headers(), content=command.model_dump_json()).status_code == 200
    before, guard = protected_rows(c.turn), c.h.store._guard()
    with patch.object(c.h.store, "transact", side_effect=AssertionError("observation must not authorize")) as writes:
        granted = observe(c)
        writes.assert_not_called()
    assert granted.status_code == 200
    assert granted.json()["effect_state"] == "unknown"
    assert granted.json()["grant"]["approval_id"] == command.decision_id
    assert granted.json()["grant"]["revoked"] is False
    assert granted.json()["grant"]["remaining_uses"] == granted.json()["grant"]["remaining_units"] == 1
    assert granted.json()["grant"]["expires_at"] == command.model_dump(mode="json")["expires_at"]
    assert "can_execute" not in granted.json() and "can_reject" not in granted.json()
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard and c.effects == []


@pytest.mark.parametrize("state", ["rejected", "revoked", "expired", "applied"])
def test_snapshot_keeps_real_decision_and_allowance_states_separate(rejection_case, state):
    c = rejection_case
    approval = None
    if state == "rejected":
        assert reject(c).status_code == 200
    else:
        approval = approval_command(c.turn, c.prepared)
        if state == "expired":
            approval = approval.model_copy(update={"expires_at": c.h.time + timedelta(seconds=2)})
        assert c.h.client.post(operation_path(c.turn) + "/approve",
            headers=c.h.headers(), content=approval.model_dump_json()).status_code == 200
        if state == "revoked":
            revoked = wire.OperationRevocation(request_id="current-view-revoke", decision_id="current-view-revoke",
                description_digest=approval.description_digest, expected_revocation_revision=0)
            assert c.h.client.post(operation_path(c.turn) + "/revoke",
                headers=c.h.headers(), content=revoked.model_dump_json()).status_code == 200
        elif state == "expired":
            c.h.time += timedelta(seconds=3)
        elif state == "applied":
            assert c.turn.client.operation().effect_state == "applied"
            assert c.effects == ["credential", "transport"]
    before, guard, effects = protected_rows(c.turn), c.h.store._guard(), list(c.effects)
    result = observe(c)
    assert result.status_code == 200
    view = wire.OperationDecisionState.model_validate_json(result.content)
    assert view.observed_at == c.h.time
    if state == "rejected":
        assert view.grant is None and view.rejection.decision_id == c.command["decision_id"]
        assert view.rejection.rejected_at == c.h.time
        assert view.effect_state == "unknown" and view.current_attempt_id is None
    else:
        assert view.rejection is None and view.grant.approval_id == approval.decision_id
        assert view.grant.revoked is (state == "revoked")
        assert view.grant.revocation_revision == (1 if state == "revoked" else 0)
        assert view.grant.remaining_uses == view.grant.remaining_units == (0 if state == "applied" else 1)
        if state == "expired":
            assert view.grant.expires_at < view.observed_at
        if state == "applied":
            assert view.effect_state == "applied"
            assert view.current_attempt_id == c.turn.binding.attempt_id
    encoded = json.dumps(result.json())
    assert c.turn.binding.human_oid not in encoded and c.turn.binding.agent_oid not in encoded
    assert not {"subjects", "principal_id", "root_budget_id", "rejected_by_oid", "content", "can_execute",
                "can_reject"}.intersection(result.json())
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard and c.effects == effects


def test_absent_grant_does_not_resolve_an_original_unknown_approval(rejection_case, monkeypatch):
    c = rejection_case
    command = approval_command(c.turn, c.prepared)
    lose_consume_ack(c, "operation-approve", command.request_id, monkeypatch)
    assert c.h.client.post(operation_path(c.turn) + "/approve",
        headers=c.h.headers(), content=command.model_dump_json()).status_code == 503
    before, guard = protected_rows(c.turn), c.h.store._guard()
    response = observe(c)
    assert response.status_code == 200
    assert response.json()["grant"] is None and response.json()["effect_state"] == "unknown"
    original = c.h.client.post(operation_path(c.turn) + "/approval-reconcile",
        headers=c.h.headers("read-only"), content=command.model_dump_json())
    assert original.status_code == 503 and original.json()["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard and c.effects == []


@pytest.mark.parametrize("identity", ["agent", "old-m3"])
def test_observation_is_only_for_current_configured_human_readers(rejection_case, identity):
    c = rejection_case
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert observe(c, identity).status_code == 403
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


def test_current_read_revocation_denies_observation(rejection_case):
    c = rejection_case
    assert c.h.client.post("/operator/v1/standing-work-policy/revoke",
        headers=c.h.headers(), json={"decision_id": "stop-current-view-read"}).status_code == 200
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert observe(c).status_code == 403
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


def test_grant_changed_during_observation_never_returns_a_mixed_snapshot(rejection_case, monkeypatch):
    c = rejection_case
    revocation = approved_revocation(c)
    row_key = "behavior:v1:row:" + control_key("grant", c.turn.binding.grant_id)
    native, raced = c.h.store._read_physical, []

    def revoke_after_read(identity, model):
        value = native(identity, model)
        if identity == row_key and not raced:
            raced.append(True)
            with ThreadPoolExecutor(max_workers=1) as executor:
                response = executor.submit(c.h.client.post, operation_path(c.turn) + "/revoke",
                    headers=c.h.headers(), content=revocation.model_dump_json()).result(timeout=10)
            assert response.status_code == 200
        return value

    monkeypatch.setattr(c.h.store, "_read_physical", revoke_after_read)
    response = observe(c)
    assert raced == [True] and response.status_code == 412
    assert response.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert observe(c).json()["grant"]["revoked"] is True and c.effects == []


@pytest.mark.parametrize("change", [
    {"grant_id": "other-grant"}, {"root_budget_id": "other-budget"},
    {"description_digest": "0" * 64}, {"adapter_contract_digest": "0" * 64},
    {"policy_id": "other-policy"}, {"subjects": [{"principal_id": "other", "conversation_id": "other"}]},
])
def test_corrupt_grant_bindings_are_not_published_as_current_facts(rejection_case, change):
    c = rejection_case
    approved_revocation(c)
    c.h.mutate(control_key("grant", c.turn.binding.grant_id), **change)
    before, guard = protected_rows(c.turn), c.h.store._guard()
    principal = c.h.verifier(c.h.client.build_request("GET", operation_path(c.turn),
                                                     headers=c.h.headers("read-only")))
    with pytest.raises(Problem) as denied:
        c.h.runtime.operation_decision_state(c.turn.binding.binding_id, principal)
    assert denied.value.status == 403
    assert denied.value.body["error"]["code"] == "BEHAVIOR_OPERATION_VIEW_DENIED"
    result = observe(c)
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "BEHAVIOR_ACCESS_DENIED"
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


@pytest.mark.parametrize("arguments", [
    {"params": {"grant_id": "another"}}, {"content": b'{"approve":true}'},
])
def test_observation_has_no_input_for_selecting_or_granting_authority(rejection_case, arguments):
    c = rejection_case
    before, guard = protected_rows(c.turn), c.h.store._guard()
    result = c.h.client.request("GET", operation_path(c.turn) + "/decision-state",
                               headers=c.h.headers("read-only"), **arguments)
    assert result.status_code == 422
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


def test_existing_previews_and_original_receipts_are_unchanged(rejection_case):
    c = rejection_case
    original = reject(c)
    assert original.status_code == 200
    preview = c.h.client.get(operation_path(c.turn), headers=c.h.headers()).json()
    before, guard, batches = protected_rows(c.turn), c.h.store._guard(), len(c.h.transport.parts)
    assert observe(c).status_code == observe(c).status_code == 200
    assert c.h.client.get(operation_path(c.turn), headers=c.h.headers()).json() == preview
    receipt = c.h.client.post(operation_path(c.turn) + "/rejection-reconcile",
        headers=c.h.headers("read-only"), json=c.command)
    assert receipt.status_code == 200 and receipt.json() == original.json()
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert len(c.h.transport.parts) == batches and c.effects == []
