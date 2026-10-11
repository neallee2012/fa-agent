"""A signed human decline is not an operation grant, cancellation or outcome."""

import copy
import json
from types import SimpleNamespace

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_operations import AuthorizationRequest, _Operation, control_key
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn
from test_skill_guidance_authority import (
    approval_command, configure_read_operation, operation_path, prepare_operation, protected_rows,
)
from test_work_policy_lifecycle import enabled


@pytest.fixture
def rejection_case(assistant, signing_key, jwk_client, monkeypatch):
    def no_process(*args, **kwargs):
        raise AssertionError("The rejection API tests must not start an SDK process")

    monkeypatch.setattr("subprocess.Popen", no_process)
    effects = configure_read_operation(assistant, signing_key, jwk_client)
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    turn = open_turn(h, control, human, "operation-rejection")
    prepared, preview = prepare_operation(turn)
    command = {
        "schema_version": "fa-operation-rejection-v1",
        "request_id": "human-reject", "decision_id": "human-rejection-decision",
        "description_digest": prepared["description_digest"],
        "adapter_contract_digest": prepared["adapter_contract_digest"],
    }
    try:
        yield SimpleNamespace(h=h, turn=turn, prepared=prepared, preview=preview,
                              command=command, effects=effects)
    finally:
        turn.client.close()


def reject(case, command=None, identity="human"):
    return case.h.client.post(operation_path(case.turn) + "/reject",
        json=case.command if command is None else command, headers=case.h.headers(identity))


def op_row(case):
    key = control_key("operation", case.turn.binding.operation_id)
    return case.h.store.read_snapshot((key,))[key]


def test_signed_human_rejection_retains_material_without_grant_effect_or_refund(rejection_case):
    c = rejection_case
    before, original = protected_rows(c.turn), op_row(c)
    response = reject(c)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["rejected"] is True
    assert response.json()["result"]["decision_id"] == c.command["decision_id"]
    row = op_row(c)
    assert row.payload["material"] == original.payload["material"]
    data = row.payload["data"]
    rejection = data["human_rejection"]
    assert rejection["schema_version"] == c.command["schema_version"]
    assert rejection["operation_id"] == c.turn.binding.operation_id
    assert rejection["decision_id"] == c.command["decision_id"]
    assert rejection["rejected_by_oid"] == c.turn.binding.human_oid
    assert rejection["description_digest"] == c.command["description_digest"]
    assert rejection["adapter_contract_digest"] == c.command["adapter_contract_digest"]
    assert data["effect_state"] == "unknown" and data["business_state"] == "pending"
    assert data["current_attempt_id"] is None and data["successful_result"] is None
    after = protected_rows(c.turn)
    assert all(after[key] == value for key, value in before.items() if value != original)
    assert c.effects == [] and c.h.endpoint.requests == []
    preview = c.h.client.get(operation_path(c.turn), headers=c.h.headers("agent"))
    assert preview.status_code == 200
    public = preview.json()
    assert public["content"] == c.preview["content"] and public["effect_state"] == "unknown"
    assert public["human_rejection"]["decision_id"] == c.command["decision_id"]
    assert c.turn.binding.human_oid not in json.dumps(public["human_rejection"])


@pytest.mark.parametrize("identity", ["agent", "read-only", "old-m3"])
def test_only_the_current_human_with_approval_scope_can_decline(rejection_case, identity):
    c = rejection_case
    before = protected_rows(c.turn)
    guard = c.h.store._guard()
    response = reject(c, identity=identity)
    assert response.status_code == 403
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert c.effects == []


@pytest.mark.parametrize("changes,status", [
    ({"description_digest": "0" * 64}, 403),
    ({"adapter_contract_digest": "0" * 64}, 403),
    ({"schema_version": "future"}, 422),
    ({"rejected_by_oid": "fake-human"}, 422),
    ({"rejected_at": "2026-09-26T00:00:00Z"}, 422),
    ({"approved": True}, 422),
    ({"reason": "PRIVATE_REJECTION_CANARY"}, 422),
])
def test_rejection_pins_current_material_and_rejects_claimed_authority(rejection_case, changes, status):
    c = rejection_case
    before = protected_rows(c.turn)
    response = reject(c, {**c.command, **changes})
    assert response.status_code == status
    assert "PRIVATE_REJECTION_CANARY" not in response.text
    assert protected_rows(c.turn) == before and c.effects == []


def test_original_rejection_replay_is_readback_not_a_second_decision(rejection_case):
    c = rejection_case
    saved = reject(c)
    assert saved.status_code == 200
    count = len(c.h.transport.parts)
    rows = protected_rows(c.turn)
    assert reject(c).json() == saved.json()
    assert len(c.h.transport.parts) == count and protected_rows(c.turn) == rows
    changed = reject(c, {**c.command, "description_digest": "0" * 64})
    assert changed.status_code == 409
    another = reject(c, {**c.command, "request_id": "second-reject", "decision_id": "second-decision"})
    assert another.status_code == 409
    assert protected_rows(c.turn) == rows and c.effects == []


def test_rejection_blocks_new_approval_and_core_authorization_without_model_authority(rejection_case):
    c = rejection_case
    assert reject(c).status_code == 200
    before = protected_rows(c.turn)
    body = approval_command(c.turn, c.prepared)
    approved = c.h.client.post(operation_path(c.turn) + "/approve",
        content=body.model_dump_json(), headers=c.h.headers())
    assert approved.status_code == 409
    executed = c.h.client.post(operation_path(c.turn) + "/execute",
        content=wire.Command(request_id="attempt-after-rejection").model_dump_json(), headers=c.h.headers("agent"))
    assert executed.status_code == 409
    direct = AuthorizationRequest(operation_id=c.turn.binding.operation_id,
        attempt_id="another-model-attempt", grant_id=c.turn.binding.grant_id)
    with pytest.raises(Problem) as denied:
        c.h.runtime.dep.broker.authorize(direct, c.turn.session.execution.owner)
    assert denied.value.body["error"]["code"] == "OPERATION_HUMAN_REJECTED"
    assert protected_rows(c.turn) == before and c.effects == []


@pytest.mark.parametrize("stage", ["approved", "applied"])
def test_rejection_does_not_replace_existing_approval_or_effect_history(rejection_case, stage):
    c = rejection_case
    body = approval_command(c.turn, c.prepared)
    approved = c.h.client.post(operation_path(c.turn) + "/approve",
        content=body.model_dump_json(), headers=c.h.headers())
    assert approved.status_code == 200
    if stage == "applied":
        response = c.turn.client.operation()
        assert response.effect_state == "applied"
        assert c.effects == ["credential", "transport"]
    before, effects = protected_rows(c.turn), list(c.effects)
    response = reject(c)
    assert response.status_code == 409
    assert protected_rows(c.turn) == before and c.effects == effects
    assert "human_rejection" not in op_row(c).payload["data"]


@pytest.mark.parametrize("stage", ["consume", "decision"])
def test_unknown_rejection_ack_preserves_original_request_and_never_reinvokes(rejection_case, monkeypatch, stage):
    c = rejection_case
    binding = c.turn.binding
    target = (runtime_key("consume", c.h.runtime._plan_key(binding, "operation-reject", c.command["request_id"]))
              if stage == "consume" else runtime_key("operation-reject", binding.binding_id, c.command["decision_id"]))
    original = c.h.store._submit
    lost = []

    def lose_ack(operations, request):
        fail = request is not None and request.request_id == target and not lost
        if fail:
            lost.append(True)
            c.h.transport.after_apply = "lost"
        try:
            return original(operations, request)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lose_ack)
    response = reject(c)
    assert response.status_code == 503 and response.json()["error"]["reconcile_original"]
    assert len(lost) == 1
    count, rows = len(c.h.transport.parts), protected_rows(c.turn)
    repeat = reject(c)
    assert repeat.status_code == (503 if stage == "consume" else 200)
    assert len(c.h.transport.parts) == count and protected_rows(c.turn) == rows
    assert ("human_rejection" in op_row(c).payload["data"]) == (stage == "decision")
    assert c.effects == []


@pytest.mark.parametrize("first", ["approve", "reject"])
def test_approval_and_rejection_cas_cannot_both_win(rejection_case, monkeypatch, first):
    c = rejection_case
    approval = approval_command(c.turn, c.prepared)
    binding = c.turn.binding
    deferred = "reject" if first == "approve" else "approve"
    decision_id = c.command["decision_id"] if deferred == "reject" else approval.decision_id
    target = runtime_key("operation-" + deferred, binding.binding_id, decision_id)
    original, interleaving = c.h.store._submit, []

    def submit(operations, request):
        if request is not None and request.request_id == target and not interleaving:
            interleaving.append(True)
            response = (reject(c) if first == "reject" else
                c.h.client.post(operation_path(c.turn) + "/approve",
                    content=approval.model_dump_json(), headers=c.h.headers()))
            assert response.status_code == 200
        return original(operations, request)

    monkeypatch.setattr(c.h.store, "_submit", submit)
    response = (reject(c) if deferred == "reject" else
        c.h.client.post(operation_path(c.turn) + "/approve",
            content=approval.model_dump_json(), headers=c.h.headers()))
    assert response.status_code == 412
    assert len(interleaving) == 1
    grant_key = control_key("grant", binding.grant_id)
    granted = c.h.store.read_snapshot((grant_key,))[grant_key] is not None
    declined = "human_rejection" in op_row(c).payload["data"]
    assert granted != declined and declined == (first == "reject")
    assert c.effects == []


def test_old_operation_bytes_omit_new_decision_fields(rejection_case):
    c = rejection_case
    before = op_row(c).payload["data"]
    assert "human_rejection" not in before
    assert _Operation.model_validate_json(json.dumps(before)).model_dump(mode="json") == before


@pytest.mark.parametrize("change", [
    {"operation_id": "another-operation"}, {"description_digest": "0" * 64},
    {"adapter_contract_digest": "0" * 64}, {"schema_version": "future"},
])
def test_saved_rejection_must_match_its_original_operation(rejection_case, change):
    c = rejection_case
    assert reject(c).status_code == 200
    payload = copy.deepcopy(op_row(c).payload["data"])
    payload["human_rejection"].update(change)
    with pytest.raises(ValueError):
        _Operation.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("new_decision", [False, True])
def test_new_human_decision_does_not_replace_an_unknown_original_request(
        rejection_case, monkeypatch, new_decision):
    c = rejection_case
    target = runtime_key("consume", c.h.runtime._plan_key(
        c.turn.binding, "operation-reject", c.command["request_id"]))
    submit = c.h.store._submit
    lost = []

    def lose_consume(operations, request):
        if request is not None and request.request_id == target and not lost:
            lost.append(True)
            c.h.transport.after_apply = "lost"
        try:
            return submit(operations, request)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lose_consume)
    initial = reject(c)
    assert initial.status_code == 503 and initial.json()["error"]["reconcile_original"]
    original_plan = c.h.runtime._existing(
        c.turn.binding, "operation-reject",
        c.h.verifier(c.h.client.build_request("GET", "/", headers=c.h.headers())),
        wire.OperationRejection.model_validate(c.command),
    )
    original_consume = c.h.store.read_snapshot((target,))[target]
    another = {**c.command, "request_id": "new-explicit-human-request"}
    if new_decision:
        another["decision_id"] = "new-explicit-human-decision"
    response = reject(c, another)
    assert response.status_code == (200 if new_decision else 409)
    assert c.h.store.read_snapshot((target,))[target] == original_consume
    assert c.h.runtime._existing(c.turn.binding, "operation-reject",
        c.h.verifier(c.h.client.build_request("GET", "/", headers=c.h.headers())),
        wire.OperationRejection.model_validate(c.command)) == original_plan
    repeated = reject(c)
    assert repeated.status_code == 503 and repeated.json()["error"]["reconcile_original"]
    assert ("human_rejection" in op_row(c).payload["data"]) is new_decision
    assert c.effects == []


def lose_consume_ack(case, kind, request_id, monkeypatch):
    target = runtime_key("consume", case.h.runtime._plan_key(case.turn.binding, kind, request_id))
    original = case.h.store._submit
    lost = []

    def submit(operations, request):
        if request is not None and request.request_id == target and not lost:
            lost.append(True)
            case.h.transport.after_apply = "lost"
        try:
            return original(operations, request)
        finally:
            case.h.transport.after_apply = None

    monkeypatch.setattr(case.h.store, "_submit", submit)


def lose_rejection_consume_ack(case, monkeypatch):
    lose_consume_ack(case, "operation-reject", case.command["request_id"], monkeypatch)


def same_decision_approval(case):
    original = approval_command(case.turn, case.prepared)
    return wire.OperationApproval.model_validate({
        **original.model_dump(), "request_id": "approve-after-intent",
        "decision_id": case.command["decision_id"],
    })


@pytest.mark.parametrize("reuse_decision", [True, False])
def test_approval_cannot_borrow_an_unknown_rejection_decision_id(rejection_case, monkeypatch, reuse_decision):
    c = rejection_case
    lose_rejection_consume_ack(c, monkeypatch)
    assert reject(c).status_code == 503
    before = protected_rows(c.turn)
    approval = same_decision_approval(c) if reuse_decision else approval_command(c.turn, c.prepared)
    response = c.h.client.post(operation_path(c.turn) + "/approve",
        content=approval.model_dump_json(), headers=c.h.headers())
    assert response.status_code == (409 if reuse_decision else 200)
    if reuse_decision:
        assert protected_rows(c.turn) == before
    else:
        grant_key = control_key("grant", c.turn.binding.grant_id)
        assert c.h.store.read_snapshot((grant_key,))[grant_key].payload["data"]["approval_id"] == approval.decision_id
    original = reject(c)
    assert original.status_code == 503 and original.json()["error"]["reconcile_original"]
    assert c.effects == []


def test_approval_rechecks_rejection_intent_in_its_commit_read_set(rejection_case, monkeypatch):
    c = rejection_case
    lose_rejection_consume_ack(c, monkeypatch)
    approval = same_decision_approval(c)
    target = runtime_key("operation-approve", c.turn.binding.binding_id, approval.decision_id)
    transact, injected = c.h.store.transact, []

    def interleave(request, decide, **kwargs):
        if request.request_id == target and not injected:
            assert runtime_key("human-operation-rejection-intent",
                c.turn.binding.binding_id, approval.decision_id) in request.read_set
            injected.append(True)
            assert reject(c).status_code == 503
        return transact(request, decide, **kwargs)

    monkeypatch.setattr(c.h.store, "transact", interleave)
    response = c.h.client.post(operation_path(c.turn) + "/approve",
        content=approval.model_dump_json(), headers=c.h.headers())
    assert response.status_code == 409
    grant_key = control_key("grant", c.turn.binding.grant_id)
    assert c.h.store.read_snapshot((grant_key,))[grant_key] is None
    assert len(injected) == 1 and c.effects == []
    assert reject(c).status_code == 503


def test_saved_approval_receipt_survives_a_later_rejection_intent(rejection_case, monkeypatch):
    c = rejection_case
    approval = same_decision_approval(c)
    save_plan, approved = c.h.runtime._save_plan, []

    def interleave(binding, kind, principal, command, context, data, request, **kwargs):
        if kind == "operation-reject" and not approved:
            response = c.h.client.post(operation_path(c.turn) + "/approve",
                content=approval.model_dump_json(), headers=c.h.headers())
            assert response.status_code == 200
            approved.append(response.json())
        return save_plan(binding, kind, principal, command, context, data, request, **kwargs)

    monkeypatch.setattr(c.h.runtime, "_save_plan", interleave)
    assert reject(c).status_code == 409
    intent = runtime_key("human-operation-rejection-intent", c.turn.binding.binding_id, approval.decision_id)
    assert c.h.store.read_snapshot((intent,))[intent] is not None
    before, batches = protected_rows(c.turn), len(c.h.transport.parts)
    replay = c.h.client.post(operation_path(c.turn) + "/approve",
        content=approval.model_dump_json(), headers=c.h.headers())
    assert replay.status_code == 200 and replay.json() == approved[0]
    assert protected_rows(c.turn) == before and len(c.h.transport.parts) == batches
    assert c.effects == []


@pytest.mark.parametrize("same_decision", [True, False])
def test_unknown_approval_stays_unknown_after_a_later_human_rejection(
        rejection_case, monkeypatch, same_decision):
    c = rejection_case
    approval = same_decision_approval(c) if same_decision else approval_command(c.turn, c.prepared)
    lose_consume_ack(c, "operation-approve", approval.request_id, monkeypatch)
    original = c.h.client.post(operation_path(c.turn) + "/approve",
        content=approval.model_dump_json(), headers=c.h.headers())
    assert original.status_code == 503 and original.json()["error"]["reconcile_original"]
    assert reject(c).status_code == 200
    before, batches = protected_rows(c.turn), len(c.h.transport.parts)
    repeated = c.h.client.post(operation_path(c.turn) + "/approve",
        content=approval.model_dump_json(), headers=c.h.headers())
    assert repeated.status_code == 503 and repeated.json()["error"]["reconcile_original"]
    assert protected_rows(c.turn) == before and len(c.h.transport.parts) == batches
    assert c.effects == []


@pytest.mark.parametrize("same_decision", [True, False])
def test_unknown_approval_decision_stays_bound_to_its_original_request(
        rejection_case, monkeypatch, same_decision):
    c = rejection_case
    approval = approval_command(c.turn, c.prepared)
    lose_consume_ack(c, "operation-approve", approval.request_id, monkeypatch)
    path = operation_path(c.turn) + "/approve"
    first = c.h.client.post(path, content=approval.model_dump_json(), headers=c.h.headers())
    assert first.status_code == 503 and first.json()["error"]["reconcile_original"]
    before, batches = protected_rows(c.turn), len(c.h.transport.parts)
    changed = approval.model_copy(update={
        "request_id": "another-approval-request",
        "decision_id": approval.decision_id if same_decision else "another-human-approval",
    })
    result = c.h.client.post(path, content=changed.model_dump_json(), headers=c.h.headers())
    assert result.status_code == (409 if same_decision else 200), result.text
    if same_decision:
        assert protected_rows(c.turn) == before and len(c.h.transport.parts) == batches
    else:
        grant_key = control_key("grant", c.turn.binding.grant_id)
        assert c.h.store.read_snapshot((grant_key,))[grant_key].payload["data"]["approval_id"] == changed.decision_id
    original = c.h.client.post(path, content=approval.model_dump_json(), headers=c.h.headers())
    assert original.status_code == 503 and original.json()["error"]["reconcile_original"]
    assert c.effects == []


def test_approval_decision_reservation_rechecks_the_original_plan_index(rejection_case, monkeypatch):
    c = rejection_case
    first = approval_command(c.turn, c.prepared)
    winner = first.model_copy(update={"request_id": "interleaved-approval"})
    lose_consume_ack(c, "operation-approve", winner.request_id, monkeypatch)
    path = operation_path(c.turn) + "/approve"
    target = runtime_key("plan-reserve", c.h.runtime._plan_key(c.turn.binding, "operation-approve", first.request_id))
    transact, injected = c.h.store.transact, []
    before = protected_rows(c.turn)

    def interleave(request, decide, **kwargs):
        if request.request_id == target and not injected:
            injected.append(True)
            response = c.h.client.post(path, content=winner.model_dump_json(), headers=c.h.headers())
            assert response.status_code == 503 and response.json()["error"]["reconcile_original"]
        return transact(request, decide, **kwargs)

    monkeypatch.setattr(c.h.store, "transact", interleave)
    result = c.h.client.post(path, content=first.model_dump_json(), headers=c.h.headers())
    assert injected and result.status_code == 412, result.text
    assert protected_rows(c.turn) == before and c.effects == []
    original = c.h.client.post(path, content=winner.model_dump_json(), headers=c.h.headers())
    assert original.status_code == 503 and original.json()["error"]["reconcile_original"]


def approved_revocation(case):
    approval = approval_command(case.turn, case.prepared)
    response = case.h.client.post(operation_path(case.turn) + "/approve",
                                  content=approval.model_dump_json(), headers=case.h.headers())
    assert response.status_code == 200, response.text
    return wire.OperationRevocation(
        request_id="revoke-original", decision_id="revoke-original-decision",
        description_digest=approval.description_digest, expected_revocation_revision=0,
    )


@pytest.mark.parametrize("same_decision", [True, False])
def test_unknown_revocation_decision_stays_bound_to_its_original_request(
        rejection_case, monkeypatch, same_decision):
    c = rejection_case
    command = approved_revocation(c)
    lose_consume_ack(c, "operation-revoke", command.request_id, monkeypatch)
    path = operation_path(c.turn) + "/revoke"
    first = c.h.client.post(path, content=command.model_dump_json(), headers=c.h.headers())
    assert first.status_code == 503 and first.json()["error"]["reconcile_original"]
    before, batches = protected_rows(c.turn), len(c.h.transport.parts)
    changed = command.model_copy(update={
        "request_id": "another-revocation-request",
        "decision_id": command.decision_id if same_decision else "another-human-revocation",
    })
    result = c.h.client.post(path, content=changed.model_dump_json(), headers=c.h.headers())
    assert result.status_code == (409 if same_decision else 200), result.text
    if same_decision:
        assert protected_rows(c.turn) == before and len(c.h.transport.parts) == batches
    else:
        grant_key = control_key("grant", c.turn.binding.grant_id)
        grant = c.h.store.read_snapshot((grant_key,))[grant_key].payload["data"]
        assert grant["revoked"] is True and grant["revocation_revision"] == 1
    original = c.h.client.post(path, content=command.model_dump_json(), headers=c.h.headers())
    assert original.status_code == 503 and original.json()["error"]["reconcile_original"]
    assert c.effects == []


def test_revocation_decision_reservation_rechecks_the_original_plan_index(rejection_case, monkeypatch):
    c = rejection_case
    first = approved_revocation(c)
    winner = first.model_copy(update={"request_id": "interleaved-revocation"})
    lose_consume_ack(c, "operation-revoke", winner.request_id, monkeypatch)
    path = operation_path(c.turn) + "/revoke"
    target = runtime_key("plan-reserve", c.h.runtime._plan_key(c.turn.binding, "operation-revoke", first.request_id))
    transact, injected = c.h.store.transact, []
    before = protected_rows(c.turn)

    def interleave(request, decide, **kwargs):
        if request.request_id == target and not injected:
            injected.append(True)
            response = c.h.client.post(path, content=winner.model_dump_json(), headers=c.h.headers())
            assert response.status_code == 503 and response.json()["error"]["reconcile_original"]
        return transact(request, decide, **kwargs)

    monkeypatch.setattr(c.h.store, "transact", interleave)
    result = c.h.client.post(path, content=first.model_dump_json(), headers=c.h.headers())
    assert injected and result.status_code == 412, result.text
    assert protected_rows(c.turn) == before and c.effects == []
    original = c.h.client.post(path, content=winner.model_dump_json(), headers=c.h.headers())
    assert original.status_code == 503 and original.json()["error"]["reconcile_original"]
