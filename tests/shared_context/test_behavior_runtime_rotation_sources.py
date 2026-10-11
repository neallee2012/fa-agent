"""Retired inline source/review/claim history from real offline producers."""
import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_runtime_rotation_history import validate_retirement
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_store import InlineMaterial
from shared_context.errors import Problem
from shared_context.records import Record
from shared_context.memory_governance import ClaimCandidate, MemoryGovernance
from shared_context.source_materials import AuthenticatedSourceContext, SourceInput, SourceMaterials
from shared_context.behavior_runtime_transition import _payload_hash
from shared_context.source_reviews import DurableHumanReviewVerifier, HumanReviewCommand, HumanReviewPolicy
from test_behavior_api import METRICS
from test_behavior_runtime_rotation import approved, rotated, write
from test_behavior_server import NOW
from test_behavior_server import TOKENIZER_TEST_CACHE
from test_behavior_store import Credential, WireTransport, json_response
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def source_history(rotated, request):
    h = rotated
    binding, policy = h.old.bindings[0], h.old.policies[0].policy
    context = AuthenticatedSourceContext(
        binding.tenant_id, binding.team_id, h.principal.source_actor_id, binding.ingress_id, policy.allowed_purposes,
    )
    clock = [NOW]
    sources = SourceMaterials(h.store, clock=lambda: clock[0])
    plan = sources.plan(context, SourceInput(
        source_id=h.old.assistant.profiles[0].source.source_id, version=1,
        visibility=policy.source_visibility, purpose=policy.purpose, schema_id="fa-artifact-metrics-v1",
        media_type="application/json",
        expires_at=None if getattr(request, "param", None) == "no-source-expiry" else NOW + timedelta(minutes=45),
    ), METRICS)
    sources.commit(context, plan)
    reviewer = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version=h.old.review_policy_version),
                                          clock=lambda: clock[0])
    memory = MemoryGovernance(sources, clock=lambda: clock[0], token_counter=len, review_verifier=reviewer)
    candidate = ClaimCandidate(
        claim_id=h.old.assistant.profiles[0].claim_id, version=1, subject=binding.subject,
        visibility=policy.source_visibility, purpose=policy.purpose, kind="note",
        text="The accepted metrics source has been reviewed.", supporting=(plan.reference,),
        effective_from=NOW, expires_at=NOW + timedelta(minutes=30),
    )
    preview = reviewer.preview(h.principal, context, candidate)
    review = reviewer.plan_decision(h.principal, context, HumanReviewCommand(
        review_id="retained-human-review", candidate=candidate, candidate_sha256=preview.candidate_sha256,
        evidence_sha256=preview.evidence_sha256, policy_version=preview.policy_version, decision="approve",
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        reason="Offline explicit human review.",
    ))
    reviewer.commit_decision(h.principal, context, review)
    claim = memory.plan_claim(context, candidate)
    memory.commit_claim(context, claim)
    clock[0] = h.h.time
    return SimpleNamespace(h=h, sources=sources, reviewer=reviewer, memory=memory, source_plan=plan,
                           review_plan=review, claim_plan=claim, context=context, clock=clock)


@pytest.mark.parametrize("source_history", ["expiring", "no-source-expiry"], indirect=True)
def test_post_cutover_source_head_review_claim_and_cursor_are_retained_without_renewal(source_history):
    s, h = source_history, source_history.h
    before = h.store.read_inventory()
    assert s.source_plan.reference.key in before.rows
    assert s.source_plan.reference.head_key in before.rows
    assert s.claim_plan.reference.key in before.rows
    if s.source_plan.source.expires_at is not None:
        with pytest.raises(Problem, match="expired"):
            s.sources.read_exact(s.context, s.source_plan.reference)
    else:
        assert s.sources.read_exact(s.context, s.source_plan.reference).original == METRICS
    with pytest.raises(Problem, match="expired"):
        s.memory.get_current(s.context, scope=s.claim_plan.reference.scope,
                             subject=s.claim_plan.reference.subject, claim_id=s.claim_plan.reference.claim_id)
    _, liability = validate_retirement(before, h.old)
    assert liability["model_outcome"] == "unknown"
    plan, auth = approved(h)
    h.service.apply(h.principal, plan, auth)
    h.service.verify(h.principal, plan, auth)
    after = h.store.read_inventory()
    for key in (s.source_plan.reference.key, s.source_plan.reference.head_key, s.claim_plan.reference.key,
                s.claim_plan.review.decision_id):
        assert after.rows[key] == before.rows[key]
    assert all(after.receipts[key] == row for key, row in before.receipts.items())
    assert installed_origin(MigrationStore(h.table, h.store._scope), h.new) == h.original


@pytest.mark.parametrize("mutation", [
    "head-actor", "head-hash", "head-access", "head-sequence", "source-expiry", "source-bytes",
    "source-scope", "source-request", "cursor", "orphan-cursor", "claim-text", "claim-expiry",
    "claim-reviewer", "claim-material", "claim-request", "subject-sequence", "subject-head",
    "review-identity", "review-evidence", "review-material", "review-policy", "review-naive-time",
])
def test_tampered_typed_source_claim_history_denies_without_repair(source_history, mutation):
    s, h = source_history, source_history.h
    source, claim = s.source_plan.reference, s.claim_plan.reference
    if mutation.startswith("head-"):
        key = source.head_key
    elif mutation.startswith("source-"):
        key = source.key
    elif mutation in {"cursor", "orphan-cursor"}:
        key = control_key("source-cursor", source.scope.key)
    elif mutation.startswith("claim-"):
        key = claim.key
    elif mutation.startswith("subject-"):
        key = control_key("memory-subject", claim.scope.key, claim.subject)
    else:
        key = s.claim_plan.review.decision_id
    record = h.store.read_snapshot((key,))[key]
    data = copy.deepcopy(record.payload["data"])
    material = None if record.payload["material"] is None else InlineMaterial.model_validate_json(
        json.dumps(record.payload["material"]))
    if mutation == "head-actor":
        data["source_actor"] = "agent-b"
    elif mutation == "head-hash":
        data["reference"]["sha256"] = "e" * 64
    elif mutation == "head-access":
        data["access_revision"] += 1
    elif mutation == "head-sequence":
        data["sequence"] += 1
    elif mutation == "source-expiry":
        data["expires_at"] = (NOW + timedelta(hours=5)).isoformat()
    elif mutation == "source-bytes":
        material = InlineMaterial(data=b"substitution", sha256=hashlib.sha256(b"substitution").hexdigest())
    elif mutation == "source-scope":
        data["reference"]["scope"]["team_id"] = "other-team"
    elif mutation == "source-request":
        data["request"]["read_set"] = []
    elif mutation == "cursor":
        data["sequence"] = 0
    elif mutation == "orphan-cursor":
        key = "unregistered-source-cursor"
    elif mutation == "claim-text":
        data["candidate"]["text"] = "unreviewed assertion"
    elif mutation == "claim-expiry":
        data["candidate"]["expires_at"] = (NOW + timedelta(hours=5)).isoformat()
    elif mutation == "claim-reviewer":
        data["review"]["reviewer_id"] = h.old.bindings[0].agent_oid
    elif mutation == "claim-material":
        from shared_context.behavior_delivery import _material
        material = _material(data)
    elif mutation == "claim-request":
        data["request"]["read_set"] = []
    elif mutation == "subject-sequence":
        data["sequence"] += 1
    elif mutation == "subject-head":
        data["heads"][0]["reference"]["sha256"] = "e" * 64
    elif mutation == "review-identity":
        data["human_oid"] = h.old.bindings[0].agent_oid
    elif mutation == "review-evidence":
        data["evidence_sha256"] = "e" * 64
    elif mutation == "review-policy":
        data["policy_version"] = "another-review-policy"
    elif mutation == "review-naive-time":
        from shared_context.behavior_delivery import _material
        body = json.loads(material.data)
        body["reviewed_at"] = "2026-09-16T01:00:00"
        material = _material(body)
    else:
        material = None
    if record.payload["material"] is not None and (
        None if material is None else material.model_dump(mode="json")
    ) != record.payload["material"]:
        before = h.store.read_inventory()
        with pytest.raises(Problem, match="inline material cannot"):
            write(h.store, key, data, material=material)
        payload = {**record.payload, "data": data, "material": None if material is None else material.model_dump(mode="json")}
        corrupted = replace(before, rows={**before.rows, key: Record(payload, record.etag)},
                            entity_sha256={**before.entity_sha256, "behavior:v1:row:" + key: _payload_hash(payload)})
        with pytest.raises(Problem):
            validate_retirement(corrupted, h.old)
        assert h.store.read_inventory().entity_sha256 == before.entity_sha256
        return
    write(h.store, key, data, material=material)
    before = h.store.read_inventory()
    count = len(h.table.submissions)
    with pytest.raises((Problem, ValueError)):
        validate_retirement(before, h.old)
    assert h.store.read_inventory().entity_sha256 == before.entity_sha256
    assert len(h.table.submissions) == count


@pytest.mark.parametrize("receipt", ["source", "review", "claim"])
def test_original_source_review_claim_receipts_cannot_be_missing(source_history, receipt):
    s = source_history
    inventory = s.h.store.read_inventory()
    request = {"source": s.source_plan.request, "review": s.review_plan.request, "claim": s.claim_plan.request}[receipt]
    receipts = {key: value for key, value in inventory.receipts.items() if key != request.request_id}
    with pytest.raises(Problem):
        validate_retirement(replace(inventory, receipts=receipts), s.h.old)


class QueryTransport(WireTransport):
    """Actual Table SDK serialization, exclusively local HTTP responses."""
    def send(self, request, **kwargs):
        if request.method == "GET" and urlsplit(request.url).path.rstrip("()") == "/Behavior":
            self.requests.append(request)
            assert parse_qs(urlsplit(request.url).query)["$filter"] == ["PartitionKey eq 'control-one'"]
            items = [{**entity, "Payload": base64.b64encode(entity["Payload"]).decode(),
                      "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"]}
                     for (table, partition, _), entity in sorted(self.backing.rows.items())
                     if table == "Behavior" and partition == "control-one"]
            return json_response(request, 200, {"value": items}, {})
        return super().send(request, **kwargs)


@pytest.fixture
def runtime_rotated(monkeypatch, request):
    import test_behavior_runtime_transition as prior
    original = prior.synthetic_configuration
    def configured():
        config = original()
        return config.model_copy(update={"tokenizer": config.tokenizer.model_copy(
            update={"cache_directory": str(TOKENIZER_TEST_CACHE)})})
    # Bind the real installed offline tokenizer path BEFORE either cutover.
    monkeypatch.setattr(prior, "synthetic_configuration", configured)
    return request.getfixturevalue("rotated")


@pytest.fixture
def runtime_history(runtime_rotated, jwk_client):
    import httpx
    from shared_context.behavior_server import build_server
    from shared_context import behavior_wire as wire
    from shared_context.source_materials import SourcePlan
    from shared_context.memory_governance import ClaimPlan

    h = runtime_rotated
    transport = QueryTransport()
    transport.backing = h.table.backing
    clock = [NOW]
    def forbidden(*_args, **_kwargs):
        pytest.fail("retirement tests must not invoke Blob/Foundry/model endpoints")
    no_io = WireTransport()
    no_io.send = forbidden
    with build_server(h.old, credential=Credential(), table_transport=transport, source_transport=no_io,
                      backup_transport=no_io, assistant_transport=httpx.MockTransport(forbidden),
                      native_transports={target.executor_id: httpx.MockTransport(forbidden) for target in h.old.active_targets},
                      key_client=jwk_client, clock=lambda: clock[0]) as composition:
        runtime = composition.runtime
        runtime.dep.sources._clock = lambda: clock[0]
        installed_origin(runtime.store, h.old)
        principal = replace(h.principal, actions=h.principal.actions | {"submit"})
        b, profile = h.old.bindings[0], h.old.assistant.profiles[0]
        command = wire.SourcePrepare(request_id="real-source-command", version=1, original=METRICS,
                                     expires_at=NOW + timedelta(minutes=45))
        runtime.prepare_source(b.binding_id, profile.source.source_id, command, principal)
        runtime.commit_source(b.binding_id, profile.source.source_id, wire.Command(request_id=command.request_id), principal)
        source = SourcePlan.model_validate_json(json.dumps(runtime._load(
            b, "source-" + profile.source.source_id, command.request_id, principal).data["source_plan"]))
        draft = wire.ClaimDraft(request_id="real-claim-command", version=1, text="The accepted metrics source has been reviewed.",
                               supporting=(profile.source,), effective_from=NOW, expires_at=NOW + timedelta(minutes=30))
        preview = runtime.preview_claim(b.binding_id, profile.claim_id, draft, principal)
        review = wire.ClaimReview(**draft.model_dump(), decision_id="real-human-review",
                                 candidate_sha256=preview.candidate_sha256, evidence_sha256=preview.evidence_sha256,
                                 policy_version=preview.policy_version,
                                 reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"))
        runtime.review_claim(b.binding_id, profile.claim_id, review, principal)
        runtime.commit_claim(b.binding_id, profile.claim_id, wire.Command(request_id=review.request_id), principal)
        claim = ClaimPlan.model_validate_json(json.dumps(runtime._load(
            b, "claim-" + profile.claim_id, review.request_id, principal).data["claim_plan"]))
        clock[0] = h.h.time
        from shared_context.behavior_runtime_rotation import RuntimeRotation
        h.store = MigrationStore(composition.clients[0], h.old.table.scope)
        h.service = RuntimeRotation(h.store, h.old, h.new, h.paths, native_grants=h.service.grants,
                                    clock=h.service.clock, allow_apply=True)
        yield SimpleNamespace(h=h, runtime=runtime, source_plan=source, claim_plan=claim,
                              principal=principal, transport=transport, clock=clock)


def test_real_runtime_source_review_claim_commands_survive_expired_policy(runtime_history):
    s, h = runtime_history, runtime_history.h
    before = h.store.read_inventory()
    with pytest.raises(Problem):
        s.runtime.current_claim(h.old.bindings[0].binding_id, s.claim_plan.reference.claim_id, s.principal)
    plan, auth = approved(h)
    h.service.apply(h.principal, plan, auth)
    h.service.verify(h.principal, plan, auth)
    after = h.store.read_inventory()
    preserved = {s.source_plan.reference.key, s.source_plan.reference.head_key, s.claim_plan.reference.key,
                 s.claim_plan.review.decision_id}
    for key, row in before.rows.items():
        if "source_plan" in row.payload["data"].get("data", {}) or "claim_plan" in row.payload["data"].get("data", {}):
            preserved.add(key)
    assert all(after.rows[key] == before.rows[key] for key in preserved)
    assert all(after.receipts[key] == row for key, row in before.receipts.items())
    assert plan.liability.model_outcome == "unknown"


@pytest.mark.parametrize("change", ["input", "read-set", "material", "index"])
def test_source_command_plan_integrity_is_not_replaced_by_schema_only_classification(runtime_history, change):
    h, b = runtime_history.h, runtime_history.h.old.bindings[0]
    key = runtime_key("plan", b.binding_id, "source-" + h.old.assistant.profiles[0].source.source_id, "real-source-command")
    if change == "index":
        key = runtime_key("plan-index", b.binding_id)
    row = h.store.read_snapshot((key,))[key]
    data = copy.deepcopy(row.payload["data"])
    if change == "input":
        data["input_digest"] = "f" * 64
    elif change == "read-set":
        data["data"]["source_plan"]["request"]["read_set"] = []
    elif change == "index":
        data["keys"] = []
    material = None if row.payload["material"] is None else InlineMaterial.model_validate_json(json.dumps(row.payload["material"]))
    if change == "material":
        before = h.store.read_inventory()
        payload = {**row.payload, "material": None}
        corrupt = replace(before, rows={**before.rows, key: Record(payload, row.etag)},
                          entity_sha256={**before.entity_sha256, "behavior:v1:row:" + key: _payload_hash(payload)})
        with pytest.raises(Problem):
            validate_retirement(corrupt, h.old)
        return
    write(h.store, key, data, material=material)
    before = h.store.read_inventory()
    with pytest.raises(Problem):
        validate_retirement(before, h.old)
    assert h.store.read_inventory().entity_sha256 == before.entity_sha256
