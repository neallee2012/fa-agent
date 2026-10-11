"""OFFLINE retained-result contracts. No provider, executor, or model is invoked."""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared_context import behavior_wire as wire
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_delivery import VerifiedWorkResult, WorkResultRequest, _digest, _material
from shared_context.behavior_inbox import InboxFetch
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_settlement import (
    AuthorizeSettlement, RetainedPayloadProtection, RetainedResultSettlement, RevokeSettlement,
    SettlementAcknowledgment, SettlementFetch, SettlementRegistration, SettlementUse, settlement_key,
)
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_behavior_api import CTX, RuntimeHarness
from test_behavior_store import code
from test_m3_auth import CLIENT_ID, jwk_client, signing_key


def write(store, key, data, *, material=None):
    prior = store.read_snapshot((key,))[key]
    if prior is not None and material is None and prior.payload["material"] is not None:
        from shared_context.behavior_store import InlineMaterial
        material = InlineMaterial.model_validate_json(json.dumps(prior.payload["material"]))
    request = ControlRequest(request_id=uuid4().hex, request_digest=_digest(data), read_set=(key,))
    return store.transact(request, lambda _: ControlChanges(writes=(ControlWrite(
        row_id=key, mode="create" if prior is None else "replace", data=data, material=material,
    ),), result={"offline_fixture": True}))


def terminal(runtime, binding, state="cancelled"):
    """Seed the already-completed recovery boundary, never run recovery/execution."""
    store = runtime.store
    job_key = worker_key("job", binding.job_id)
    job = store.read_snapshot((job_key,))[job_key].payload["data"]
    write(store, job_key, {**job, "state": state, "completion_credit": 0,
                          "cancel_credit": 0, "stop_credit": 0, "result_forwarded": True})
    result = VerifiedWorkResult(
        evidence_id="retained-result", verifier_version=runtime.dep.delivery.contract.verifier_version,
        work_id=binding.work.work_id, definition_digest=_digest(binding.work.model_dump(mode="json")),
        state=state, passed_checks=binding.work.required_checks if state == "succeeded" else (),
        unresolved_operations=() if state == "succeeded" else ("a" * 64,),
        content=b'{"model_outcome":"unknown","budget_refunded":false}',
        proof=b"OFFLINE retained terminal result fixture, not native evidence",
    )
    write(store, worker_key("work-result", result.evidence_id), result.model_dump(mode="json"),
          material=_material(result.model_dump(mode="json")))
    runtime.dep.delivery.record_work_result(
        WorkResultRequest(obligation_id=binding.obligation_id, evidence_id=result.evidence_id), binding.owner,
    )
    return result


class OfflineProtection:
    """Explicit synthetic observation; cannot be mistaken for acquisition."""

    def __init__(self, now, mode="offline_contract"):
        self.observed_at, self.mode, self.proof, self.calls, self.deny = now, mode, None, [], False

    def verify(self, evidence_id, plan):
        self.calls.append(plan.sha256)
        if self.deny:
            raise ValueError("OFFLINE observation revoked")
        if self.proof is None:
            self.proof = RetainedPayloadProtection(
                evidence_id=evidence_id, verifier_version="offline-retention-v1", evidence_mode=self.mode,
                configuration_sha256=plan.configuration_sha256, control_binding=plan.control_binding,
                plan_sha256=plan.sha256, authentication_version="behavior-jwt-v1",
                retained_materials=plan.retained_materials,
                coverage="exact-inline-result-and-inbox-exchange-receipts", receiver_scope="terminal-result-only",
                observed_at=self.observed_at, expires_at=self.observed_at + timedelta(minutes=4),
                proof=b"OFFLINE exact inline result and receiver observation; not native proof",
            )
        return self.proof


def human(binding):
    return BehaviorPrincipal(binding.tenant_id, binding.human_oid, binding.team_id, "human", None,
                             frozenset({"read", "approve", "acknowledge"}))


def configured(h, observer=None, *, register=True, settlement_id="one-settlement"):
    # This stand-in pins the original RuntimeHarness configuration, not a native deployment.
    config = SimpleNamespace(
        sha256="d" * 64, delivery=h.delivery.contract, workers=h.workers.contract,
        authentication_version=h.inbox.registration.authentication_version,
        table=SimpleNamespace(scope=h.store._scope), bindings=(h.binding,),
        identity=SimpleNamespace(human_oid=h.binding.human_oid, tenant_id=h.binding.tenant_id,
                                 team_id=h.binding.team_id, admin_client_id=CLIENT_ID, audience=h.verifier.auth.audience),
    )
    observer = observer or OfflineProtection(h.time)
    registration = SettlementRegistration(
        settlement_id=settlement_id, binding_id=h.binding.binding_id, configuration_sha256=config.sha256,
        expires_at=h.time + timedelta(minutes=10), protection_evidence_id="exact-retained-protection",
        protection_verifier_version="offline-retention-v1", protection_verifier=observer,
    )
    service = RetainedResultSettlement(config, h.runtime, registration)
    if register:
        h.inbox.register_retained_results(service)
    return service, observer


@pytest.fixture
def retained(auth, jwk_client, signing_key):
    h = RuntimeHarness(auth, jwk_client, signing_key, native_wire=False)
    h.submit()
    terminal(h.runtime, h.binding)
    h.mutate(control_key("budget", "root"), remaining_attempts=993, remaining_units=9993)
    h.mutate(control_key("delivery-quota", "root", "root"), scans_remaining=981)
    for key, value in (
        (control_key("original-model-intent", "model"), {"consumed": True, "outcome": "unknown"}),
        (control_key("original-approval", "operator"), {"decision": "cancel", "retained": True}),
        (control_key("original-native-grant", "job-one"), {"remaining_uses": 1, "expired": True}),
    ):
        write(h.store, key, value, material=_material(value))
    h.time += timedelta(hours=2)
    service, observer = configured(h)
    return h, service, observer, human(h.binding)


def approve(service, principal):
    plan = service.prepare(principal)
    return service.authorize(principal, AuthorizeSettlement(
        plan_sha256=plan.sha256, decision_id="explicit-human-settlement", decision="settle_retained_result",
    ))


def fetch_command(grant, request_id="fetch-one"):
    return SettlementFetch(authorization_id=grant.authorization_id, request_id=request_id)


def ack_command(grant, received, request_id="ack-one"):
    return SettlementAcknowledgment(
        authorization_id=grant.authorization_id, request_id=request_id,
        exchange_id=received.exchange_id, acknowledgment_token=received.acknowledgment_token,
        binding=received.binding,
    )


def data(h, kind, *ids):
    key = control_key(kind, *ids)
    return h.store.read_snapshot((key,))[key].payload["data"]


def test_cancelled_result_requires_independent_human_grant_and_real_exchange_ack(retained):
    h, service, observer, principal = retained
    original = copy.deepcopy(h.transport.backing.rows)
    with pytest.raises(Problem):
        h.runtime.fetch(h.binding.binding_id, wire.Command(request_id="old-pull"), principal)
    plan = service.prepare(principal)
    assert plan.terminal_state == "cancelled" and plan.delivery.route == h.binding.route
    assert h.executor.starts == [] and data(h, "delivery-obligation", h.binding.obligation_id)["fulfillment"] == "pending"
    with pytest.raises(Problem):
        service.fetch(principal, SettlementFetch(authorization_id="not-approved", request_id="no"))
    grant = service.authorize(principal, AuthorizeSettlement(
        plan_sha256=plan.sha256, decision_id="human-approved", decision="settle_retained_result",
    ))
    with pytest.raises(Problem) as unknown:
        service.reconcile(principal, SettlementUse(authorization_id=grant.authorization_id))
    assert code(unknown) == "INBOX_DELIVERY_UNKNOWN"
    received = service.fetch(principal, fetch_command(grant))
    assert received.send_outcome == "unknown"
    assert received.binding == grant.plan.delivery
    assert hashlib.sha256(received.payload).hexdigest() == grant.plan.delivery.payload_digest
    payload = json.loads(received.payload)
    import base64
    assert base64.urlsafe_b64decode(payload["content"]) == b'{"model_outcome":"unknown","budget_refunded":false}'
    assert payload["work_state"] == "cancelled" and payload["unresolved_operations"] == ["a" * 64]
    assert payload["message_kind"] == "failure"
    assert data(h, "delivery-obligation", h.binding.obligation_id)["fulfillment"] == "pending"
    receipt = service.acknowledge(principal, ack_command(grant, received))
    assert receipt.payload["result"]["confirmed_levels"] == ["client_received"]
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    assert service.acknowledge(principal, ack_command(grant, received)) == receipt
    assert service.reconcile(principal, SettlementUse(authorization_id=grant.authorization_id)) == receipt
    assert data(h, "delivery-outbox", received.binding.delivery_id)["receipt_count"] == 1
    assert h.executor.starts == []
    mutable_kinds = {"guard", "delivery_obligation", "delivery_pending_index", "delivery_quota"}
    for key, entity in original.items():
        payload = json.loads(entity["Payload"])
        if payload.get("kind") != "guard" and payload.get("data", {}).get("kind") not in mutable_kinds:
            assert h.transport.backing.rows[key] == entity, key
    assert data(h, "budget", "root")["remaining_attempts"] == 993
    assert data(h, "budget", "root")["remaining_units"] == 9993
    assert data(h, "delivery-quota", "root", "root")["scans_remaining"] == 981
    old = h.store.read_snapshot((runtime_key("policy", h.binding.binding_id),))[runtime_key("policy", h.binding.binding_id)]
    assert old.payload["data"]["valid_until"] == h.policy.model_dump(mode="json")["valid_until"]
    with pytest.raises(Problem):
        h.runtime.acknowledge(h.binding.binding_id, wire.PullAcknowledgment(
            request_id="old-ack", exchange_id=received.exchange_id, acknowledgment_token=received.acknowledgment_token,
            binding=wire.DeliveryBinding.model_validate_json(received.binding.model_dump_json()),
        ), principal)


def test_default_and_unregistered_seam_stay_closed(retained):
    h, service, _, principal = retained
    disabled = RetainedResultSettlement(service.config, h.runtime)
    with pytest.raises(Problem) as denied:
        disabled.prepare(principal)
    assert code(denied) == "SETTLEMENT_DISABLED"
    other, _ = configured(h, register=False)
    grant = approve(service, principal)
    with pytest.raises(Problem) as denied:
        other.fetch(principal, fetch_command(grant))
    assert code(denied) == "SETTLEMENT_DISABLED"
    with pytest.raises(Problem):
        h.inbox.fetch_retained(InboxFetch(obligation_id=h.binding.obligation_id,
                                         delivery_id=grant.plan.delivery.delivery_id, access_evidence_id="fake"),
                               CTX, object())
    with pytest.raises(Problem):
        h.inbox.register_retained_results(other)


@pytest.mark.parametrize("field,value", [
    ("kind", "agent"), ("oid", "wrong-human"), ("tenant_id", "wrong-tenant"),
    ("team", "wrong-team"), ("actor_id", "pretend-human"), ("actions", frozenset({"read"})),
])
def test_only_configured_approving_human_can_authorize(retained, field, value):
    _, service, _, principal = retained
    with pytest.raises(Problem):
        approve(service, replace(principal, **{field: value}))


@pytest.mark.parametrize("action", ["fetch", "acknowledge", "reconcile", "authorize"])
@pytest.mark.parametrize("invalid", ["expiry", "revocation", "protection"])
def test_expired_revoked_or_unverified_authorization_never_replays(retained, action, invalid):
    h, service, observer, principal = retained
    grant = approve(service, principal)
    received = service.fetch(principal, fetch_command(grant))
    acknowledged = ack_command(grant, received)
    if invalid == "expiry":
        h.time = grant.expires_at
    elif invalid == "revocation":
        service.revoke(principal, RevokeSettlement(authorization_id=grant.authorization_id, decision_id="revoke"))
    else:
        observer.deny = True
    calls = {
        "fetch": lambda: service.fetch(principal, fetch_command(grant)),
        "acknowledge": lambda: service.acknowledge(principal, acknowledged),
        "reconcile": lambda: service.reconcile(principal, SettlementUse(authorization_id=grant.authorization_id)),
        "authorize": lambda: service.authorize(principal, grant.decision),
    }
    with pytest.raises(Problem):
        calls[action]()
    assert data(h, "delivery-obligation", h.binding.obligation_id)["fulfillment"] == "pending"


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "recipient_id", "channel_id", "thread_id"])
def test_exact_original_route_changes_fail(retained, field):
    h, service, _, principal = retained
    grant = approve(service, principal)
    ob = data(h, "delivery-obligation", h.binding.obligation_id)
    h.mutate(control_key("delivery-obligation", h.binding.obligation_id), route={**ob["route"], field: "wrong"})
    with pytest.raises(Problem):
        service.fetch(principal, fetch_command(grant))


@pytest.mark.parametrize("target,updates", [
    ("policy", {"active": False}), ("policy", {"revision": 2}),
    ("runtime-policy", {"valid_until": "2099-01-01T00:00:00Z"}), ("runtime-policy", {"active": False}),
    ("owner", {"epoch": 2}), ("job", {"state": "running"}), ("job", {"result_forwarded": False}),
    ("job", {"generation": 3}),
])
def test_scope_policy_epoch_and_terminal_job_are_current_predicates(retained, target, updates):
    h, service, _, principal = retained
    grant = approve(service, principal)
    keys = {"policy": control_key("policy", "policy"), "runtime-policy": runtime_key("policy", h.binding.binding_id),
            "owner": control_key("owner", "owner"), "job": worker_key("job", h.binding.job_id)}
    h.mutate(keys[target], **updates)
    with pytest.raises(Problem):
        service.fetch(principal, fetch_command(grant))


@pytest.mark.parametrize("field,value", [
    ("binding", None), ("exchange_id", "wrong-exchange"), ("acknowledgment_token", "wrong-token"),
    ("authorization_id", "wrong-grant"),
])
def test_wrong_ack_fails_without_fulfilling(retained, field, value):
    h, service, _, principal = retained
    grant = approve(service, principal)
    received = service.fetch(principal, fetch_command(grant))
    if field == "binding":
        value = received.binding.model_copy(update={"payload_digest": "0" * 64})
    with pytest.raises(Problem):
        service.acknowledge(principal, ack_command(grant, received).model_copy(update={field: value}))
    assert data(h, "delivery-obligation", h.binding.obligation_id)["fulfillment"] == "pending"


def test_retained_payload_and_result_data_cannot_diverge_from_immutable_material(retained):
    h, service, _, principal = retained
    grant = approve(service, principal)
    key = control_key("delivery-work-state", h.binding.obligation_id)
    work = h.store.read_snapshot((key,))[key].payload["data"]
    h.mutate(key, result={**work["result"], "unresolved_operations": []})
    with pytest.raises(Problem):
        service.fetch(principal, fetch_command(grant))


@pytest.mark.parametrize("part", ["payload_digest", "version", "message_kind"])
def test_outbox_mutation_fails(retained, part):
    h, service, _, principal = retained
    grant = approve(service, principal)
    key = control_key("delivery-outbox", grant.plan.delivery.delivery_id)
    outbox = h.store.read_snapshot((key,))[key].payload["data"]
    updates = ({"message_kind": "final"} if part == "message_kind" else
               {"binding": {**outbox["binding"], part: "0" * 64 if part == "payload_digest" else 8}})
    h.mutate(key, **updates)
    with pytest.raises(Problem):
        service.fetch(principal, fetch_command(grant))


def test_no_duplicate_grant_expiry_extension_or_unbounded_fetch_lane(retained):
    h, service, _, principal = retained
    grant = approve(service, principal)
    h.time += timedelta(seconds=5)
    assert service.authorize(principal, grant.decision) == grant
    with pytest.raises(Problem):
        service.authorize(principal, grant.decision.model_copy(update={"decision_id": "replacement"}))
    for index in range(8):
        received = service.fetch(principal, fetch_command(grant, f"fetch-{index}"))
    count = len(h.transport.backing.rows)
    with pytest.raises(Problem) as full:
        service.fetch(principal, fetch_command(grant, "overflow"))
    assert code(full) == "INBOX_FETCH_LIMIT" and len(h.transport.backing.rows) == count
    service.acknowledge(principal, ack_command(grant, received))


@pytest.mark.parametrize("action", ["fetch", "acknowledge"])
def test_revocation_winning_atomic_gate_blocks_delivery(retained, action):
    h, service, _, principal = retained
    grant = approve(service, principal)
    received = service.fetch(principal, fetch_command(grant)) if action == "acknowledge" else None
    h.transport.before_submit = lambda: service.revoke(
        principal, RevokeSettlement(authorization_id=grant.authorization_id, decision_id="revoke-before-commit"),
    )
    with pytest.raises(Problem) as denied:
        if action == "fetch":
            service.fetch(principal, fetch_command(grant))
        else:
            service.acknowledge(principal, ack_command(grant, received))
    assert code(denied) == "BEHAVIOR_CONFLICT"
    assert data(h, "delivery-obligation", h.binding.obligation_id)["fulfillment"] == "pending"


def test_fetch_and_ack_commit_response_loss_reconcile_same_ids_without_work(retained):
    h, service, _, principal = retained
    grant = approve(service, principal)
    h.transport.failure = "after"
    with pytest.raises(Problem) as unknown:
        service.fetch(principal, fetch_command(grant))
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    h.transport.failure = None
    received = service.fetch(principal, fetch_command(grant))
    assert data(h, "inbox-exchange", received.binding.delivery_id)["fetch_count"] == 1
    h.transport.failure = "after"
    with pytest.raises(Problem) as unknown:
        service.acknowledge(principal, ack_command(grant, received))
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    h.transport.failure = None
    # New service object uses only persisted authorization/plan/exchange evidence.
    from shared_context.behavior_inbox import PullInbox
    h.inbox = PullInbox(h.store, h.inbox.registration)
    h.delivery.registration = replace(h.delivery.registration, channel_adapter=h.inbox, verify_receipt=h.inbox.verify_receipt)
    h.inbox.attach(h.delivery)
    h.runtime.dep = replace(h.runtime.dep, inbox=h.inbox)
    rebuilt = RetainedResultSettlement(service.config, h.runtime, service.registration)
    h.inbox.register_retained_results(rebuilt)
    receipt = rebuilt.reconcile(principal, SettlementUse(authorization_id=grant.authorization_id))
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    assert data(h, "inbox-exchange", received.binding.delivery_id)["fetch_count"] == 1
    assert data(h, "delivery-outbox", received.binding.delivery_id)["receipt_count"] == 1
    assert h.executor.starts == []


@pytest.mark.parametrize("extra", ["clock", "principal", "endpoint", "provider_receipt", "protection", "skip_readiness"])
def test_public_commands_forbid_authority_injection(extra):
    with pytest.raises(ValidationError):
        SettlementFetch.model_validate({"authorization_id": "x", "request_id": "y", extra: "untrusted"})
    with pytest.raises(ValidationError):
        AuthorizeSettlement(plan_sha256="a" * 64, decision_id="user-not-available", decision="user not available")


@pytest.mark.parametrize("state", ["succeeded", "failed"])
def test_other_verified_terminal_results_use_existing_final_preparation(auth, jwk_client, signing_key, state):
    h = RuntimeHarness(auth, jwk_client, signing_key, native_wire=False)
    h.submit()
    result = terminal(h.runtime, h.binding, state)
    h.time += timedelta(hours=2)
    service, _ = configured(h)
    principal = human(h.binding)
    grant = approve(service, principal)
    received = service.fetch(principal, fetch_command(grant))
    assert json.loads(received.payload)["work_state"] == state
    assert grant.plan.result_evidence_id == result.evidence_id
    assert json.loads(received.payload)["message_kind"] == ("final" if state == "succeeded" else "failure")
    service.acknowledge(principal, ack_command(grant, received))
    assert h.executor.starts == []


def test_queued_work_does_not_become_an_eligible_failure_when_old_policy_expires(auth, jwk_client, signing_key):
    h = RuntimeHarness(auth, jwk_client, signing_key, native_wire=False)
    h.submit()
    h.time += timedelta(hours=2)
    service, _ = configured(h)
    before = copy.deepcopy(h.transport.backing.rows)
    with pytest.raises(Problem) as denied:
        service.prepare(human(h.binding))
    assert code(denied) == "SETTLEMENT_RESULT_DENIED"
    assert h.transport.backing.rows == before
    assert data(h, "delivery-obligation", h.binding.obligation_id)["final_delivery_id"] is None


def test_authorization_response_loss_retains_original_decision_and_expiry(retained):
    h, service, _, principal = retained
    plan = service.prepare(principal)
    command = AuthorizeSettlement(plan_sha256=plan.sha256, decision_id="approve", decision="settle_retained_result")
    h.transport.failure = "after"
    with pytest.raises(Problem) as unknown:
        service.authorize(principal, command)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    h.transport.failure = None
    key = settlement_key("authorization", service.registration.settlement_id)
    original = h.store.read_snapshot((key,))[key]
    h.time += timedelta(seconds=20)
    reconciled = service.authorize(principal, command)
    assert reconciled.model_dump(mode="json") == original.payload["data"]
    assert h.store.read_snapshot((key,))[key] == original


def test_expiry_during_fetch_commit_cannot_release_payload(retained):
    h, service, _, principal = retained
    grant = approve(service, principal)
    h.transport.before_submit = lambda: setattr(h, "time", grant.expires_at)
    with pytest.raises(Problem):
        service.fetch(principal, fetch_command(grant))
    assert data(h, "delivery-obligation", h.binding.obligation_id)["fulfillment"] == "pending"
    assert data(h, "inbox-exchange", grant.plan.delivery.delivery_id)["acknowledged"] is False


@pytest.mark.parametrize("field,value", [
    ("configuration_sha256", "0" * 64), ("control_binding", "0" * 64), ("plan_sha256", "0" * 64),
    ("authentication_version", "wrong-auth"), ("evidence_id", "wrong-evidence"),
])
def test_protection_witness_is_exact_and_not_merely_present(retained, field, value):
    _, service, observer, principal = retained
    grant = approve(service, principal)
    observer.proof = observer.proof.model_copy(update={field: value})
    with pytest.raises(Problem) as denied:
        service.fetch(principal, fetch_command(grant))
    assert code(denied) == "SETTLEMENT_PROTECTION_DENIED"
