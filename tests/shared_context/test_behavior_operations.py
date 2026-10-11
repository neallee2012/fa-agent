"""OFFLINE only: synthetic grants/evidence, real core, SDK-shaped Table fake.

The native-SDK cases use a local fake HTTP transport, not Azure. No fixture
assertion establishes human approval, destination terminality or Hosted PASS.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import ResourceNotFoundError
from pydantic import ValidationError

from shared_context.behavior_operations import (
    AdapterContract, AttemptBinding, AuthorizationRequest, BudgetRecord,
    ContentDependency, EffectPart, ExecutionContext, GrantRecord, GrantSubject,
    IdempotencyEvidence, OperationDescription, OutcomeRequest, OwnerRecord,
    PolicyRecord, ReceiptResult, RegisteredAdapter, ResolvedResource, ToolBrokerCore,
    ToolIntent, VerifiedOutcome, canonical_material, control_key,
)
from shared_context.behavior_store import (
    GUARD_KEY, BehaviorStore, ControlChanges, ControlRequest, ControlWrite, InlineMaterial,
)
from shared_context.errors import Problem
from test_behavior_store import FakeTable, SCOPE, code, wire_store

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
CONTEXT = ExecutionContext(
    owner_record_id="owner-one", principal_id="person-one", epoch=1,
    conversation_id="conversation-one", policy_id="policy", root_budget_id="root",
)
OTHER = CONTEXT.model_copy(update={
    "owner_record_id": "owner-two", "principal_id": "person-two", "conversation_id": "conversation-two",
})
RESOURCE = ResolvedResource(resource_id="synthetic-resource", version="v1")
DESCRIPTION = OperationDescription(
    adapter_id="synthetic-tool", adapter_version="1", kind="request", method="POST",
    target="synthetic-resource", content=b"actual request A", resources=(RESOURCE,),
)


class Harness:
    """Offline server-registration/test data, NEVER a production adapter."""

    def __init__(self, *, wire=False, terminal=False, idempotent=False):
        if wire:
            self.store, self.client = wire_store()
        else:
            self.client = FakeTable()
            self.store = BehaviorStore(self.client, SCOPE)
        self.store.initialize()
        self.time, self.seed_number = NOW, 0
        self.aliases, self.resolve_calls = {"current": DESCRIPTION}, []
        self.outcomes, self.idempotency = {}, {}
        contract = AdapterContract(
            adapter_id="synthetic-tool", adapter_version="1", allowed_kinds=("request", "script", "plan"),
            allowed_methods=("POST",), allowed_targets=("synthetic-resource",),
            reservation_units=2, evidence_source="synthetic-destination", outcome_contract_version="outcome-1",
            terminality_contract_version="terminal-1" if terminal else None,
            idempotency_contract_version="idempotent-1" if idempotent else None,
        )
        self.adapter = RegisteredAdapter(
            contract=contract, resolve=self.resolve, verify_outcome=self.verified_outcome,
            verify_idempotency=self.verified_idempotency,
        )
        self.core = self.broker(self.store)
        self.put(
            ("owner", CONTEXT.owner_record_id, OwnerRecord(**CONTEXT.model_dump(), active=True, operation_ids=("op-a",))),
            ("owner", OTHER.owner_record_id, OwnerRecord(**OTHER.model_dump(), active=True, operation_ids=("op-b",))),
            ("policy", "policy", PolicyRecord(policy_id="policy", revision=1, active=True)),
            ("budget", "root", BudgetRecord(root_budget_id="root", active=True, remaining_attempts=64, remaining_units=128)),
        )

    def broker(self, store):
        return ToolBrokerCore(store, clock=lambda: self.time, adapter=self.adapter)

    def resolve(self, ref):
        self.resolve_calls.append(ref)
        if ref not in self.aliases:
            raise Problem(404, "SYNTHETIC_REFERENCE_MISSING", "offline alias is missing")
        return self.aliases[ref]

    def verified_outcome(self, ref):
        if ref not in self.outcomes:
            raise Problem(403, "SYNTHETIC_EVIDENCE_UNVERIFIED", "no offline verified outcome fixture")
        return self.outcomes[ref]

    def verified_idempotency(self, ref):
        if ref not in self.idempotency:
            raise Problem(403, "SYNTHETIC_EVIDENCE_UNVERIFIED", "no offline verified idempotency fixture")
        return self.idempotency[ref]

    def put(self, *rows):
        self.seed_number += 1
        name = f"synthetic-seed-{self.seed_number}"
        request = ControlRequest(request_id=name, request_digest=hashlib.sha256(name.encode()).hexdigest())
        self.store.transact(request, lambda _: ControlChanges(
            writes=tuple(ControlWrite(row_id=control_key(kind, key), mode="create", data=value.model_dump(mode="json"))
                         for kind, key, value in rows),
            result={"synthetic_only": True},
        ))

    def raw(self, kind, *ids):
        entity = self.client.backing.get("Behavior", SCOPE.partition_key, "behavior:v1:row:" + control_key(kind, *ids))
        return json.loads(entity["Payload"])

    def data(self, kind, *ids):
        return self.raw(kind, *ids)["data"]

    def material(self, kind, *ids):
        return InlineMaterial.model_validate_json(json.dumps(self.raw(kind, *ids)["material"]))

    def mutate(self, kind, identifier, **updates):
        self.seed_number += 1
        key = control_key(kind, identifier)
        name = f"synthetic-update-{self.seed_number}"
        request = ControlRequest(request_id=name, request_digest=hashlib.sha256(name.encode()).hexdigest(), read_set=(key,))

        def decide(snapshot):
            row = snapshot[key].payload
            return ControlChanges(writes=(ControlWrite(
                row_id=key, mode="replace", data={**row["data"], **updates},
                material=None if row["material"] is None else InlineMaterial.model_validate_json(json.dumps(row["material"])),
            ),), result={"synthetic_only": True})

        return self.store.transact(request, decide)

    def prepare(self, op="op-a", context=CONTEXT):
        return self.core.prepare(ToolIntent(operation_id=op, input_ref="current"), context)

    def approve(self, grant="grant", uses=64, units=128):
        prepared = self.data("operation", "op-a")
        self.put(("grant", grant, GrantRecord(
            grant_id=grant, approval_source="human", approval_id="synthetic-approval",
            subjects=tuple(GrantSubject(principal_id=c.principal_id, conversation_id=c.conversation_id) for c in (CONTEXT, OTHER)),
            root_budget_id="root", description_digest=prepared["description_digest"],
            adapter_contract_digest=prepared["adapter_contract_digest"], policy_id="policy",
            policy_revision=1, revocation_revision=0, revoked=False,
            not_before=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(hours=1),
            remaining_uses=uses, remaining_units=units,
        )))

    def authorize(self, *, op="op-a", attempt="a", previous=None, evidence=None, context=CONTEXT):
        return self.core.authorize(AuthorizationRequest(
            operation_id=op, attempt_id=attempt, grant_id="grant",
            previous_attempt_id=previous, idempotency_evidence_id=evidence,
        ), context)

    def binding(self, attempt="a", op="op-a"):
        return AttemptBinding.model_validate_json(json.dumps(self.data("attempt", op, attempt)["binding"]))

    def observe(self, observation, *, evidence_id="proof-one", attempt="a", sequence=1, business="unknown", parts=()):
        terminal = observation in {"terminal_rejection", "terminal_withdrawal", "effect_fenced"}
        self.outcomes[evidence_id] = VerifiedOutcome(
            evidence_id=evidence_id, source="synthetic-destination", contract_version="outcome-1",
            binding=self.binding(attempt), observation=observation, sequence=sequence,
            observed_at=self.time, proof=b"synthetic verified destination receipt",
            terminality_contract_version="terminal-1" if terminal else None,
            terminality_scope="entire_attempt_all_in_flight_deliveries" if terminal else None,
            terminality_reference="synthetic-terminal-receipt" if terminal else None,
            business_result=business, parts=parts,
            result=ReceiptResult(receipt_id="synthetic-result", resource=RESOURCE, status="completed") if business == "succeeded" else None,
        )
        return self.core.record_outcome(OutcomeRequest(operation_id="op-a", attempt_id=attempt, evidence_id=evidence_id), CONTEXT)

    def idem(self, attempt="a", *, ref="idempotency-proof", expires=None):
        self.idempotency[ref] = IdempotencyEvidence(
            evidence_id=ref, source="synthetic-destination", contract_version="idempotent-1",
            binding=self.binding(attempt), scope="whole_operation_all_overlapping_deliveries",
            not_before=NOW, expires_at=expires or NOW + timedelta(minutes=10),
            proof=b"synthetic whole-operation destination contract",
        )
        return ref

    def submissions(self):
        if isinstance(self.client, FakeTable):
            return len(self.client.submissions)
        return len([request for request in self.client.requests if request.method == "POST"])


def ready(**options):
    harness = Harness(**options)
    harness.prepare()
    harness.approve()
    return harness


def saved_description(receipt):
    material = InlineMaterial.model_validate_json(json.dumps(receipt.payload["result"]["content"]))
    return OperationDescription.model_validate_json(material.data)


def test_no_adapter_or_grant_can_be_supplied_by_tool_intent():
    h = Harness()
    disabled = ToolBrokerCore(h.store, clock=lambda: NOW)
    with pytest.raises(Problem) as error:
        disabled.prepare(ToolIntent(operation_id="op-a", input_ref="current"), CONTEXT)
    assert code(error) == "OPERATION_CAPABILITY_DISABLED"
    for field in ("approved", "authorized_now", "grant", "execute", "terminal"):
        with pytest.raises(ValidationError):
            ToolIntent(operation_id="op-a", input_ref="current", **{field: True})
    h.prepare()
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.authorize()
    assert code(error) == "OPERATION_RECORD_MISSING"
    assert h.submissions() == count


@pytest.mark.parametrize("wire", [False, True])
def test_authorization_is_one_batch_with_saved_bytes_and_all_predicates(wire):
    h = ready(wire=wire)
    count = h.submissions()
    guard = h.client.backing.get("Behavior", SCOPE.partition_key, GUARD_KEY).metadata["etag"]
    result = h.authorize()
    assert h.submissions() == count + 1
    assert saved_description(result) == DESCRIPTION
    attempt = result.payload["result"]["attempt"]
    assert attempt["context"] == CONTEXT.model_dump(mode="json")
    assert attempt["policy_revision"] == 1 and attempt["grant_revocation_revision"] == 0
    assert result.payload["result"]["execution_enabled"] is False
    assert h.data("grant", "grant")["remaining_uses"] == 63
    assert h.data("budget", "root")["remaining_attempts"] == 63
    assert h.data("budget", "root")["remaining_units"] == 126
    if wire:
        batch = h.client.parts[-1]
        assert len(batch) == 6 and batch[0][2]["If-Match"] == guard
    else:
        batch = h.client.submissions[-1]
        assert len(batch) == 6 and batch[0][2]["etag"] == guard
        assert {entry[1]["RowKey"] for entry in batch[2:]} == {
            "behavior:v1:row:" + control_key(kind, *ids) for kind, ids in [
                ("operation", ("op-a",)), ("attempt", ("op-a", "a")),
                ("grant", ("grant",)), ("budget", ("root",)),
            ]
        }
        assert {"behavior:v1:row:" + control_key(kind, key) for kind, key in [
            ("owner", "owner-one"), ("policy", "policy"), ("grant", "grant"), ("budget", "root"),
        ]} <= {row for _, row in h.client.reads}


@pytest.mark.parametrize("kind", ["request", "script", "plan"])
def test_alias_replacement_after_approval_and_after_last_gate_never_changes_saved_content(kind):
    h = Harness()
    original = DESCRIPTION.model_copy(update={"kind": kind})
    h.aliases["current"] = original
    prepared = h.prepare()
    h.approve()
    h.aliases["current"] = original.model_copy(update={"content": b"replacement B"})
    result = h.authorize()
    h.aliases["current"] = original.model_copy(update={"content": b"replacement C after gate"})
    assert saved_description(prepared) == original
    assert saved_description(result) == original
    assert h.resolve_calls == ["current"]
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.core.execute("op-a", "a")
    assert code(error) == "OPERATION_EXECUTION_DISABLED"
    assert h.submissions() == count and h.resolve_calls == ["current"]


def test_same_operation_id_cannot_bind_new_content_and_prepare_is_not_approval():
    h = ready()
    original = h.material("operation", "op-a")
    h.aliases["current"] = DESCRIPTION.model_copy(update={"content": b"new B"})
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.prepare()
    assert code(error) == "OPERATION_ID_CONFLICT"
    assert h.submissions() == count and h.material("operation", "op-a") == original
    h.mutate("grant", "grant", revoked=True, revocation_revision=1)
    with pytest.raises(Problem) as error:
        h.authorize()
    assert code(error) == "OPERATION_GRANT_DENIED"


def test_model_cannot_mint_a_new_operation_id_to_retry_an_unknown_action():
    h = ready()
    h.authorize()
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.prepare("new-id-to-retry")
    assert code(error) == "OPERATION_OWNER_DENIED"
    assert h.submissions() == count
    assert h.data("operation", "op-a")["unsettled_attempt_ids"] == ["a"]
    assert h.data("grant", "grant")["remaining_uses"] == 63


def test_canonicalization_binds_dependencies_resource_versions_and_adapter_version():
    one = ContentDependency(resource_id="one", version="v1", content=b"dependency one")
    two = ContentDependency(resource_id="two", version="v2", content=b"dependency two")
    first = DESCRIPTION.model_copy(update={"dependencies": (one, two)})
    assert canonical_material(first) == canonical_material(first.model_copy(update={"dependencies": (two, one)}))
    for updates in (
        {"content": b"different"}, {"method": "DELETE"}, {"target": "another-target"},
        {"adapter_version": "2"}, {"resources": (RESOURCE.model_copy(update={"version": "v2"}),)},
        {"dependencies": (one.model_copy(update={"content": b"changed"}), two)},
    ):
        assert canonical_material(first).sha256 != canonical_material(first.model_copy(update=updates)).sha256
    with pytest.raises(Problem):
        canonical_material(DESCRIPTION.model_copy(update={"canonicalization_version": "unknown"}))
    for version in ("current", "latest", "unknown", "HEAD"):
        with pytest.raises(ValidationError):
            ResolvedResource(resource_id="target", version=version)
        contract = Harness().adapter.contract
        with pytest.raises(ValidationError):
            AdapterContract.model_validate({**contract.model_dump(), "adapter_version": version})


@pytest.mark.parametrize("updates", [
    {"method": "DELETE"}, {"target": "unregistered-target"}, {"adapter_version": "999"},
])
def test_unregistered_method_target_or_version_is_rejected_before_persistence(updates):
    h = Harness()
    h.aliases["current"] = DESCRIPTION.model_copy(update=updates)
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.prepare()
    assert code(error) == "OPERATION_CONTENT_DENIED"
    assert h.submissions() == count


@pytest.mark.parametrize("kind,key,updates,expected", [
    ("owner", "owner-one", {"epoch": 2}, "OPERATION_OWNER_DENIED"),
    ("owner", "owner-one", {"active": False}, "OPERATION_OWNER_DENIED"),
    ("policy", "policy", {"revision": 2}, "OPERATION_GRANT_DENIED"),
    ("policy", "policy", {"active": False}, "OPERATION_GRANT_DENIED"),
    ("grant", "grant", {"revoked": True}, "OPERATION_GRANT_DENIED"),
    ("grant", "grant", {"subjects": []}, "OPERATION_RECORD_INVALID"),
    ("grant", "grant", {"description_digest": "0" * 64}, "OPERATION_GRANT_DENIED"),
    ("grant", "grant", {"approval_source": "agent"}, "OPERATION_RECORD_INVALID"),
    ("grant", "grant", {"remaining_uses": 0}, "OPERATION_BUDGET_DENIED"),
    ("grant", "grant", {"remaining_units": 1}, "OPERATION_BUDGET_DENIED"),
    ("budget", "root", {"remaining_attempts": 0}, "OPERATION_BUDGET_DENIED"),
    ("budget", "root", {"remaining_units": 1}, "OPERATION_BUDGET_DENIED"),
])
def test_current_trusted_predicates_are_required(kind, key, updates, expected):
    h = ready()
    h.mutate(kind, key, **updates)
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.authorize()
    assert code(error) == expected and h.submissions() == count
    assert h.data("operation", "op-a")["current_attempt_id"] is None


@pytest.mark.parametrize("quota", ["grant", "root-budget"])
def test_two_conversations_compete_for_one_shared_atomic_reservation(quota):
    h = ready()
    h.prepare("op-b", OTHER)
    if quota == "grant":
        h.mutate("grant", "grant", remaining_uses=1)
    else:
        h.mutate("budget", "root", remaining_attempts=1, remaining_units=2)
    other_client = FakeTable(h.client.backing)
    other_core = h.broker(BehaviorStore(other_client, SCOPE))
    winners = []
    h.client.before_submit = lambda: winners.append(other_core.authorize(
        AuthorizationRequest(operation_id="op-b", attempt_id="b", grant_id="grant"), OTHER,
    ))
    with pytest.raises(Problem) as error:
        h.authorize()
    assert code(error) == "BEHAVIOR_CONFLICT"
    assert len(winners) == 1
    assert h.data("operation", "op-a")["current_attempt_id"] is None
    assert h.data("operation", "op-b")["current_attempt_id"] == "b"
    assert h.data("grant", "grant")["remaining_uses"] == (0 if quota == "grant" else 63)
    assert h.data("budget", "root")["remaining_attempts"] == (0 if quota == "root-budget" else 63)
    with pytest.raises(ResourceNotFoundError):
        h.raw("attempt", "op-a", "a")


@pytest.mark.parametrize("kind,key,updates", [
    ("grant", "grant", {"revoked": True, "revocation_revision": 1}),
    ("policy", "policy", {"revision": 2}),
    ("owner", "owner-one", {"epoch": 2}),
])
def test_predicate_change_between_decision_and_commit_conflicts_without_retry(kind, key, updates):
    h = ready()
    h.client.before_submit = lambda: h.mutate(kind, key, **updates)
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.authorize()
    assert code(error) == "BEHAVIOR_CONFLICT"
    assert h.submissions() == count + 2  # One competing writer and one rejected batch.
    assert h.data("operation", "op-a")["current_attempt_id"] is None


def test_replay_returns_accounting_history_not_fresh_permission_or_a_second_reservation():
    h = ready()
    first = h.authorize()
    h.mutate("grant", "grant", revoked=True)
    h.time = NOW + timedelta(days=1)
    count = h.submissions()
    replay = h.authorize()
    assert replay == first and h.submissions() == count
    assert replay.payload["result"]["execution_enabled"] is False
    assert h.data("grant", "grant")["remaining_uses"] == 63
    with pytest.raises(Problem) as error:
        h.core.execute("op-a", "a")
    assert code(error) == "OPERATION_EXECUTION_DISABLED"


@pytest.mark.parametrize("clock", [NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=8)))])
def test_clock_must_be_trusted_utc(clock):
    h = ready()
    h.time = clock
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.authorize()
    assert code(error) == "OPERATION_CLOCK_INVALID" and h.submissions() == count


@pytest.mark.parametrize("offset", [timedelta(minutes=-2), timedelta(hours=1)])
def test_grant_time_is_checked_at_current_gate(offset):
    h = ready()
    h.time = NOW + offset
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.authorize()
    assert code(error) == "OPERATION_GRANT_DENIED" and h.submissions() == count


@pytest.mark.parametrize("after", [NOW + timedelta(hours=1), NOW - timedelta(seconds=1)])
def test_commit_crossing_expiry_or_clock_rollback_returns_no_dispatch_success_and_holds_reservation(after):
    h = ready()
    h.client.before_submit = lambda: setattr(h, "time", after)
    with pytest.raises(Problem) as error:
        h.authorize()
    assert code(error) == "OPERATION_AUTHORIZATION_WINDOW_CLOSED"
    assert h.data("grant", "grant")["remaining_uses"] == 63
    assert h.data("operation", "op-a")["unsettled_attempt_ids"] == ["a"]
    assert h.data("attempt", "op-a", "a")["effect_state"] == "unknown"


@pytest.mark.parametrize("observation", [
    "negative_query", "cancel_accepted", "local_lease_expired", "connection_closed", "caller_stopped", "unknown",
])
def test_delayed_a_negative_observation_never_starts_b_or_releases_budget(observation):
    h = ready()
    h.authorize()
    h.observe(observation, business="failed")
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.authorize(attempt="b", previous="a")
    assert code(error) == "OPERATION_RETRY_UNSAFE" and h.submissions() == count
    assert h.data("attempt", "op-a", "a")["effect_state"] == "unknown"
    assert h.data("operation", "op-a")["business_state"] == "pending"
    assert h.data("grant", "grant")["remaining_uses"] == 63
    assert h.data("budget", "root")["remaining_units"] == 126
    with pytest.raises(ResourceNotFoundError):
        h.raw("attempt", "op-a", "b")
    h.observe("applied", evidence_id="late-a", sequence=2, business="succeeded")
    assert h.data("operation", "op-a")["effect_state"] == "applied"
    assert h.data("operation", "op-a")["business_state"] == "succeeded"
    assert h.data("budget", "root")["remaining_units"] == 126


@pytest.mark.parametrize("terminal", ["terminal_rejection", "terminal_withdrawal", "effect_fenced"])
def test_terminal_non_application_requires_explicit_verified_destination_contract(terminal):
    h = ready(terminal=True)
    h.authorize()
    h.observe(terminal)
    retry = h.authorize(attempt="b", previous="a")
    assert retry.payload["result"]["attempt"]["basis"] == "terminal_not_applied"
    assert h.binding("a").idempotency_key == h.binding("b").idempotency_key
    assert h.data("budget", "root")["remaining_attempts"] == 62
    assert h.data("budget", "root")["remaining_units"] == 124


def test_query_only_registration_cannot_claim_terminal_non_application():
    h = ready()
    h.authorize()
    with pytest.raises(Problem) as error:
        h.observe("terminal_rejection")
    assert code(error) == "OPERATION_EVIDENCE_DENIED"
    assert h.data("attempt", "op-a", "a")["effect_state"] == "unknown"
    assert h.data("grant", "grant")["remaining_uses"] == 63


def test_partial_or_local_terminal_scope_cannot_be_verified_as_destination_finality():
    h = ready(terminal=True)
    h.authorize()
    h.observe("effect_fenced")
    proof = h.outcomes["proof-one"]
    for updates in (
        {"terminality_scope": None}, {"terminality_scope": "local_epoch_only"},
        {"terminality_reference": None}, {"observation": "negative_query"},
    ):
        with pytest.raises(ValidationError):
            VerifiedOutcome.model_validate({**proof.model_dump(), **updates})


def test_valid_whole_unit_idempotency_does_not_require_terminal_a():
    h = ready(idempotent=True)
    h.authorize()
    h.observe("negative_query")
    ref = h.idem()
    result = h.authorize(attempt="b", previous="a", evidence=ref)
    assert result.payload["result"]["attempt"]["basis"] == "whole_unit_idempotency"
    assert h.data("attempt", "op-a", "a")["effect_state"] == "unknown"
    assert h.binding("a").idempotency_key == h.binding("b").idempotency_key
    assert h.data("operation", "op-a")["unsettled_attempt_ids"] == ["a", "b"]
    saved_proof = IdempotencyEvidence.model_validate_json(h.material("attempt", "op-a", "b").data)
    assert saved_proof == h.idempotency[ref]


@pytest.mark.parametrize("change", ["expired", "not-yet-valid", "wrong-binding", "wrong-source", "wrong-contract", "partial-scope", "not-installed"])
def test_invalid_idempotency_never_permits_b(change):
    h = ready(idempotent=change != "not-installed")
    h.authorize()
    ref = h.idem()
    updates = {
        "expired": {"expires_at": NOW},
        "not-yet-valid": {"not_before": NOW + timedelta(minutes=1)},
        "wrong-binding": {"binding": h.binding().model_copy(update={"description_digest": "0" * 64})},
        "wrong-source": {"source": "untrusted"},
        "wrong-contract": {"contract_version": "unknown"},
        "partial-scope": {"scope": "one-step-only"},
        "not-installed": {},
    }[change]
    if change == "expired":
        h.time = NOW + timedelta(minutes=10)  # Exact expiry is excluded.
    else:
        h.idempotency[ref] = h.idempotency[ref].model_copy(update=updates)
    count = h.submissions()
    with pytest.raises(Problem):
        h.authorize(attempt="b", previous="a", evidence=ref)
    assert h.submissions() == count
    assert h.data("grant", "grant")["remaining_uses"] == 63


@pytest.mark.parametrize("branch", ["terminal", "idempotent"])
def test_safe_retry_branch_still_requires_current_grant_and_budget(branch):
    h = ready(terminal=True, idempotent=True)
    h.authorize()
    ref = None
    if branch == "terminal":
        h.observe("effect_fenced")
    else:
        ref = h.idem()
    h.mutate("grant", "grant", revoked=True)
    with pytest.raises(Problem) as error:
        h.authorize(attempt="b", previous="a", evidence=ref)
    assert code(error) == "OPERATION_GRANT_DENIED"
    assert h.data("budget", "root")["remaining_attempts"] == 63


def test_idempotency_expiring_during_commit_leaves_b_reserved_but_never_dispatchable():
    h = ready(idempotent=True)
    h.authorize()
    ref = h.idem(expires=NOW + timedelta(seconds=1))
    h.client.before_submit = lambda: setattr(h, "time", NOW + timedelta(seconds=1))
    with pytest.raises(Problem) as error:
        h.authorize(attempt="b", previous="a", evidence=ref)
    assert code(error) == "OPERATION_AUTHORIZATION_WINDOW_CLOSED"
    assert h.data("operation", "op-a")["unsettled_attempt_ids"] == ["a", "b"]
    assert h.data("budget", "root")["remaining_attempts"] == 62


def test_terminal_b_does_not_erase_still_delayed_a_after_idempotent_overlap():
    h = ready(terminal=True, idempotent=True)
    h.authorize()
    h.authorize(attempt="b", previous="a", evidence=h.idem())
    h.observe("terminal_rejection", attempt="b", evidence_id="terminal-b")
    assert h.data("operation", "op-a")["effect_state"] == "unknown"
    assert h.data("operation", "op-a")["unsettled_attempt_ids"] == ["a"]
    with pytest.raises(Problem) as error:
        h.authorize(attempt="c", previous="b")
    assert code(error) == "OPERATION_RETRY_UNSAFE"
    h.observe("applied", attempt="a", evidence_id="late-a", business="succeeded")
    assert h.data("operation", "op-a")["business_state"] == "succeeded"
    with pytest.raises(Problem) as error:
        h.authorize(attempt="c", previous="b")
    assert code(error) == "OPERATION_RETRY_UNSAFE"


def test_partial_is_not_succeeded_and_never_retries_whole_unit_even_with_idempotency():
    h = ready(idempotent=True)
    h.authorize()
    parts = (EffectPart(unit_id="one", effect="applied"), EffectPart(unit_id="two", effect="unknown"))
    h.observe("partial", parts=parts, business="failed")
    ref = h.idem()
    with pytest.raises(Problem) as error:
        h.authorize(attempt="b", previous="a", evidence=ref)
    assert code(error) == "OPERATION_RETRY_UNSAFE"
    assert h.data("operation", "op-a")["business_state"] == "pending"
    assert h.data("operation", "op-a")["effect_state"] == "partial"
    assert h.data("budget", "root")["remaining_units"] == 126
    proof = VerifiedOutcome.model_validate_json(h.material("outcome", "op-a", "a", "proof-one").data)
    assert proof.parts == parts and proof.proof == b"synthetic verified destination receipt"
    with pytest.raises(ValidationError):
        VerifiedOutcome.model_validate({**proof.model_dump(), "business_result": "succeeded"})


def test_unsettled_overlapping_attempt_prevents_business_success_until_accounted_for():
    h = ready(terminal=True, idempotent=True)
    h.authorize()
    h.authorize(attempt="b", previous="a", evidence=h.idem())
    h.observe("applied", attempt="b", evidence_id="completed-b", business="succeeded")
    assert h.data("operation", "op-a")["business_state"] == "pending"
    h.observe("terminal_withdrawal", attempt="a", evidence_id="terminal-a")
    assert h.data("operation", "op-a")["business_state"] == "succeeded"
    assert h.data("operation", "op-a")["successful_result"]["receipt_id"] == "synthetic-result"


def test_outcome_requires_registered_verifier_and_exact_binding():
    h = ready()
    h.authorize()
    with pytest.raises(Problem) as error:
        h.core.record_outcome(OutcomeRequest(operation_id="op-a", attempt_id="a", evidence_id="invented"), CONTEXT)
    assert code(error) == "SYNTHETIC_EVIDENCE_UNVERIFIED"
    h.observe("negative_query")
    old = h.outcomes["proof-one"]
    h.outcomes["wrong"] = old.model_copy(update={
        "evidence_id": "wrong", "sequence": 2,
        "binding": old.binding.model_copy(update={"target": "wrong-resource"}),
    })
    with pytest.raises(Problem) as error:
        h.core.record_outcome(OutcomeRequest(operation_id="op-a", attempt_id="a", evidence_id="wrong"), CONTEXT)
    assert code(error) == "OPERATION_EVIDENCE_DENIED"
    no_verifier = ToolBrokerCore(h.store, clock=lambda: NOW, adapter=replace(h.adapter, verify_outcome=None))
    with pytest.raises(Problem) as error:
        no_verifier.record_outcome(OutcomeRequest(operation_id="op-a", attempt_id="a", evidence_id="proof-one"), CONTEXT)
    assert code(error) == "OPERATION_EVIDENCE_DISABLED"


def test_success_receipt_cannot_substitute_a_different_artifact_version():
    h = ready()
    h.authorize()
    h.observe("negative_query")
    prior = h.outcomes["proof-one"]
    h.outcomes["wrong-version"] = prior.model_copy(update={
        "evidence_id": "wrong-version", "sequence": 2, "observation": "applied",
        "business_result": "succeeded",
        "result": ReceiptResult(
            receipt_id="wrong-result", resource=RESOURCE.model_copy(update={"version": "v2"}), status="completed",
        ),
    })
    with pytest.raises(Problem) as error:
        h.core.record_outcome(
            OutcomeRequest(operation_id="op-a", attempt_id="a", evidence_id="wrong-version"), CONTEXT,
        )
    assert code(error) == "OPERATION_EVIDENCE_DENIED"
    assert h.data("operation", "op-a")["business_state"] == "pending"
    assert h.data("budget", "root")["remaining_units"] == 126


@pytest.mark.parametrize("observation,effect", [
    ("applied", "unknown"), ("applied", "not_applied"),
    ("terminal_rejection", "applied"), ("terminal_rejection", "unknown"),
])
def test_partial_evidence_cannot_be_wrapped_in_a_whole_attempt_terminal_label(observation, effect):
    h = ready(terminal=True)
    h.authorize()
    h.observe("negative_query")
    prior = h.outcomes["proof-one"]
    fields = {"observation": observation, "parts": (EffectPart(unit_id="one", effect=effect),)}
    if observation == "terminal_rejection":
        fields.update(
            terminality_contract_version="terminal-1",
            terminality_scope="entire_attempt_all_in_flight_deliveries",
            terminality_reference="terminal",
        )
    with pytest.raises(ValidationError):
        VerifiedOutcome.model_validate({**prior.model_dump(), **fields})


def test_revoking_new_effects_does_not_block_recording_an_in_flight_outcome():
    h = ready()
    h.authorize()
    h.mutate("grant", "grant", revoked=True, revocation_revision=1)
    h.observe("applied", business="succeeded")
    assert h.data("operation", "op-a")["business_state"] == "succeeded"
    assert h.data("grant", "grant")["remaining_uses"] == 63


def test_replayed_or_contradictory_evidence_cannot_rewrite_known_effect():
    h = ready()
    h.authorize()
    first = h.observe("applied", business="succeeded")
    command = OutcomeRequest(operation_id="op-a", attempt_id="a", evidence_id="proof-one")
    count = h.submissions()
    assert h.core.record_outcome(command, CONTEXT) == first
    assert h.submissions() == count
    h.outcomes["proof-one"] = h.outcomes["proof-one"].model_copy(update={"proof": b"changed evidence"})
    with pytest.raises(Problem) as error:
        h.core.record_outcome(command, CONTEXT)
    assert code(error) == "OPERATION_EVIDENCE_CONFLICT"
    with pytest.raises(Problem) as error:
        h.observe("negative_query", evidence_id="later-negative", sequence=2)
    assert code(error) == "OPERATION_EVIDENCE_CONFLICT"
    assert h.data("operation", "op-a")["business_state"] == "succeeded"


@pytest.mark.parametrize("failure", ["before", "after"])
def test_authorization_commit_unknown_is_not_retried_or_success_shaped(failure):
    h = ready()
    command = AuthorizationRequest(operation_id="op-a", attempt_id="a", grant_id="grant")
    request = h.core.reconciliation_request(command, CONTEXT)
    h.client.failure = failure
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.core.authorize(command, CONTEXT)
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN" and h.submissions() == count + 1
    h.client.failure = None
    if failure == "after":
        receipt = h.store.reconcile(request)
        assert receipt.payload["result"]["execution_enabled"] is False
        assert saved_description(receipt) == DESCRIPTION
        assert h.data("budget", "root")["remaining_units"] == 126
    else:
        with pytest.raises(Problem) as missing:
            h.store.reconcile(request)
        assert code(missing) == "BEHAVIOR_COMMIT_UNKNOWN"
        assert h.data("budget", "root")["remaining_units"] == 128
    assert h.submissions() == count + 1
    assert h.material("operation", "op-a") == canonical_material(DESCRIPTION)


def test_unknown_prepare_reconciles_saved_a_without_re_resolving_mutable_alias():
    h = Harness()
    intent = ToolIntent(operation_id="op-a", input_ref="current")
    request = h.core.reconciliation_request(intent, CONTEXT)
    h.client.failure = "after"
    with pytest.raises(Problem) as error:
        h.core.prepare(intent, CONTEXT)
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    h.aliases["current"] = DESCRIPTION.model_copy(update={"content": b"replacement B"})
    count = h.submissions()
    receipt = h.store.reconcile(request)
    assert receipt.payload["result"]["description_digest"] == canonical_material(DESCRIPTION).sha256
    assert h.material("operation", "op-a") == canonical_material(DESCRIPTION)
    assert h.resolve_calls == ["current"] and h.submissions() == count


def test_unknown_outcome_commit_retains_input_result_and_evidence_without_refund():
    h = ready()
    h.authorize()
    command = OutcomeRequest(operation_id="op-a", attempt_id="a", evidence_id="proof-one")
    request = h.core.reconciliation_request(command, CONTEXT)
    h.client.failure = "after"
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.observe("applied", business="succeeded")
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    receipt = h.store.reconcile(request)
    assert receipt.payload["result"]["operation_business_state"] == "succeeded"
    saved = VerifiedOutcome.model_validate_json(h.material("outcome", "op-a", "a", "proof-one").data)
    assert saved == h.outcomes["proof-one"]
    assert saved.result.receipt_id == "synthetic-result"
    assert h.material("operation", "op-a") == canonical_material(DESCRIPTION)
    assert h.data("budget", "root")["remaining_units"] == 126
    assert h.submissions() == count + 1


@pytest.mark.parametrize("failure", ["lost", "incomplete"])
def test_real_sdk_unknown_authorization_ack_only_reconciles_without_another_post(failure):
    h = ready(wire=True)
    command = AuthorizationRequest(operation_id="op-a", attempt_id="a", grant_id="grant")
    request = h.core.reconciliation_request(command, CONTEXT)
    count = h.submissions()
    h.client.after_apply = failure
    with pytest.raises(Problem) as error:
        h.core.authorize(command, CONTEXT)
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert h.submissions() == count + 1
    receipt = h.store.reconcile(request)
    assert saved_description(receipt) == DESCRIPTION
    assert h.data("budget", "root")["remaining_units"] == 126
    assert h.submissions() == count + 1


def test_unsettled_attempt_capacity_is_bounded_before_another_reservation():
    h = ready(idempotent=True)
    h.authorize(attempt="a0")
    for index in range(1, 16):
        previous = f"a{index - 1}"
        h.authorize(attempt=f"a{index}", previous=previous, evidence=h.idem(previous, ref=f"proof{index}"))
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.authorize(attempt="a16", previous="a15", evidence=h.idem("a15", ref="proof16"))
    assert code(error) == "OPERATION_ATTEMPT_LIMIT" and h.submissions() == count
    assert len(h.data("operation", "op-a")["unsettled_attempt_ids"]) == 16
    assert h.data("budget", "root")["remaining_attempts"] == 48


def test_oversized_canonical_material_is_rejected_before_control_persistence():
    h = Harness()
    h.aliases["current"] = DESCRIPTION.model_copy(update={
        "content": b"x" * 24576,
        "dependencies": (ContentDependency(resource_id="dep", version="1", content=b"x" * 16384),),
    })
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.prepare()
    assert code(error) == "OPERATION_CONTENT_LIMIT" and h.submissions() == count
