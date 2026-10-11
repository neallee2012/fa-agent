"""Real azure-ai-projects 2.3 serialization/local transport, not native Azure PASS."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import ServiceResponseError
from pydantic import ValidationError

from test_adapters import Credential, ENDPOINT, WireResponse, WireTransport
from test_behavior_store import FakeTable, SCOPE
from test_memory_governance import VERSIONS, candidate
from test_source_materials import CONTEXT, NOW, OTHER, admitted, code, input_source, source_service
from test_source_reviews import (
    HUMAN, HUMAN_CONTEXT, POLICY, approved, claimed, decision, reviews, withdrawal,
)
from shared_context.behavior_store import BehaviorStore
from shared_context.errors import Problem
from shared_context.memory_governance import MemoryGovernance
from shared_context.memory_projection import (
    NativeMemoryProjection, NativeProjectionPlan, NativeReceipt, ProjectionContext,
    ProjectionQuery, ProjectionSearch, ProjectionSetup, ProjectionState, _key,
)
from shared_context.source_materials import SourceMaterials, control_row, digest
from shared_context.source_reviews import DurableHumanReviewVerifier

QUERY = ProjectionQuery(query="dashboard color")
USAGE = {"embedding_tokens": 2, "input_tokens": 11, "output_tokens": 3, "total_tokens": 16,
         "input_tokens_details": {"cached_tokens": 0},
         "output_tokens_details": {"reasoning_tokens": 0}}


class Transport(WireTransport):
    def send(self, request, **kwargs):
        self.requests.append(request)
        action = self.responses.pop(0)
        if callable(action):
            action = action(request)
        if isinstance(action, Exception):
            raise action
        return WireResponse(request, *action)


def setup(memory, **changes):
    return ProjectionSetup(**{
        "control_binding": memory.store._binding, "binding_id": "runtime-one",
        "endpoint": ENDPOINT, "store": "existing-reviewed-memory",
        "projection_namespace": "fa-reviewed-v1-local-contract",
        "evidence_sha256": "e" * 64, "scope": CONTEXT.scope("team"),
        "subject": "human-one", "claim_ids": ("dashboard-color",), "versions": VERSIONS,
        "max_polls": 3, "max_searches": 2, "max_input_tokens": 4096,
        "max_query_tokens": 512, "max_input_bytes": 16384, "max_response_bytes": 32768, **changes,
    })


def client(transport):
    # Deliberately nonzero defaults: the adapter must override retries per call.
    return AIProjectClient(endpoint=ENDPOINT, credential=Credential(), transport=transport, api_version="v1",
                           retry_total=3, retry_backoff_factor=0, credential_scopes=["https://ai.azure.com/.default"])


@pytest.fixture
def projection_service(reviews):
    sources, table, source, verifier, memory, proposed = reviews
    claim = claimed(verifier, memory, proposed)
    transport = Transport([])
    service = NativeMemoryProjection(memory, client(transport), setup(memory))
    planned = service.plan(CONTEXT)
    pid = planned.payload["result"]["projection_id"]
    return SimpleNamespace(service=service, memory=memory, sources=sources, table=table, source=source,
                           verifier=verifier, proposed=proposed, claim=claim, transport=transport, pid=pid,
                           scope=planned.payload["result"]["native_scope"])


def item(h, text=None, **changes):
    return {
        "memory_id": "native-memory-1", "updated_at": "2026-09-15T14:00:00Z", "scope": h.scope,
        "content": h.claim.candidate.text if text is None else text, "kind": "user_profile", **changes,
    }


def complete_payload(h, text=None, *, operation_kind="create", **changes):
    return {"update_id": "native-update-1", "status": "completed",
            "result": {"memory_operations": [{"kind": operation_kind, "memory_item": item(h, text, **changes)}],
                       "usage": dict(USAGE)}}


def queued(h):
    h.transport.responses.append((202, {"update_id": "native-update-1", "status": "queued"}, {}))
    return h.service.start(CONTEXT, h.pid)


def completed(h, text=None, **changes):
    queued(h)
    raw = complete_payload(h, text, **changes)
    h.transport.responses.append((200, raw, {}))
    h.service.poll(CONTEXT, h.pid)
    return raw


def searched(h, text=None, **changes):
    h.transport.responses.append((200, {"search_id": "native-search-1",
                                       "memories": [{"memory_item": item(h, text, **changes)}], "usage": USAGE}, {}))
    return h.service.search(CONTEXT, h.pid, QUERY)


def restart(h):
    sources = SourceMaterials(BehaviorStore(FakeTable(h.table.backing), SCOPE))
    reviewer = DurableHumanReviewVerifier(sources, POLICY, clock=lambda: NOW)
    memory = MemoryGovernance(sources, clock=lambda: NOW, token_counter=len, review_verifier=reviewer)
    return NativeMemoryProjection(memory, client(h.transport), h.service.setup)


def state(h):
    return ProjectionState.model_validate_json(json.dumps(h.service.reconcile(CONTEXT, h.pid).payload))


def context(h, query=None):
    return ProjectionContext.model_validate_json(json.dumps(h.service.assemble(CONTEXT, h.pid, query).payload))


def update_posts(h):
    return [request for request in h.transport.requests if ":update_memories?" in request.url]


def test_original_plan_guarded_before_one_native_post_and_duplicate_replay(projection_service):
    h = projection_service
    before = len(h.table.submissions)
    assert h.service.plan(CONTEXT).payload["result"]["projection_id"] == h.pid
    assert len(h.table.submissions) == before
    pk = _key("plan", h.pid)
    plan = control_row(h.service.read_snapshot((pk,)), pk, NativeProjectionPlan)
    assert plan.document.claims == (h.claim.reference,)
    assert plan.document.sources == (h.source.reference,)
    assert plan.document.covered_sequence == 1
    assert plan.document.source_access_revisions[h.source.reference.key] == 1
    assert "cobalt" in plan.document.input_text
    assert plan.document.source_fingerprint == h.sources.evidence_digest(
        h.memory._source_set(CONTEXT, h.claim.candidate.supporting, CONTEXT.scope("team")))

    def accepted(request):
        assert state(h).start_consumed and state(h).phase == "outcome_unknown"
        assert control_row(h.service.read_snapshot((pk,)), pk, NativeProjectionPlan) == plan
        return 202, {"update_id": "native-update-1", "status": "queued"}, {}

    h.transport.responses.append(accepted)
    assert h.service.start(CONTEXT, h.pid).payload["phase"] == "accepted"
    assert h.service.start(CONTEXT, h.pid).payload["phase"] == "accepted"
    h.service = restart(h)
    assert h.service.start(CONTEXT, h.pid).payload["phase"] == "accepted"
    assert len(update_posts(h)) == 1
    wire = update_posts(h)[0]
    body = json.loads(wire.body)
    assert body["scope"] == h.scope and body["scope"].startswith("frp1-")
    assert not body["scope"].startswith("sc1-")
    assert body["items"] == [{"role": "user", "type": "message",
                              "content": [{"type": "input_text", "text": plan.document.input_text}]}]
    assert body["update_delay"] == 0 and body.get("previous_update_id") is None
    assert wire.headers["Foundry-Features"] == "MemoryStores=V1Preview"
    assert h.service.reconcile(CONTEXT, h.pid).payload["completion_receipt_key"] is None


@pytest.mark.parametrize("operation_kind", ["create", "created", "update", "updated"])
def test_native_completed_operation_preserves_receipt_and_candidate_lineage(projection_service, operation_kind):
    h = projection_service
    queued(h)
    raw = complete_payload(h, operation_kind=operation_kind, updated_at=1789526587)
    h.transport.responses.append((200, raw, {}))
    assert h.service.poll(CONTEXT, h.pid).payload["phase"] == "completed"
    h.service = restart(h)
    current = state(h)
    assert current.phase == "completed" and current.completion_receipt_key is not None
    receipt = control_row(h.service.read_snapshot((current.completion_receipt_key,)),
                          current.completion_receipt_key, NativeReceipt)
    assert receipt.native == raw and receipt.native_sha256 == digest(raw)
    assert not receipt.sanitized
    assert receipt.native["result"]["memory_operations"][0]["kind"] == operation_kind
    assert receipt.native["result"]["memory_operations"][0]["memory_item"]["updated_at"] == 1789526587
    h.transport.responses.append((200, {"search_id": "native-search-1",
        "memories": [{"memory_item": item(h, updated_at=1789526587)}], "usage": USAGE, "error": None}, {}))
    result = ProjectionSearch.model_validate_json(json.dumps(h.service.search(CONTEXT, h.pid, QUERY).payload))
    assert len(result.candidates) == 1 and not result.degraded and not result.omissions
    hit = result.candidates[0]
    assert hit.memory_item.scope == h.scope and hit.memory_item.content == h.claim.candidate.text
    assert hit.claim == h.claim.reference and hit.supporting == h.claim.candidate.supporting
    assert h.source.reference in hit.supporting
    assert hit.completion_receipt_key == current.completion_receipt_key
    assert hit.trust == "retrieval-candidate-not-authority"
    assembled = context(h, QUERY)
    assert assembled.summary == h.claim.candidate.text and not assembled.degraded
    assert assembled.originals[h.source.reference.key] == h.source.original
    assert len(h.transport.requests) == 3


@pytest.mark.parametrize("operation_kind", ["upsert", "deleted", "UPDATED"])
def test_unknown_native_operation_never_completes_or_allows_search(projection_service, operation_kind):
    h = projection_service
    queued(h)
    h.transport.responses.append((200, complete_payload(h, operation_kind=operation_kind), {}))
    with pytest.raises(Problem) as invalid:
        h.service.poll(CONTEXT, h.pid)
    assert code(invalid) == "MEMORY_NATIVE_SCHEMA"
    h.service = restart(h)
    current = state(h)
    assert current.phase == "accepted" and current.completion_receipt_key is None
    assert current.last_issue == "native-schema-invalid" and current.polls_consumed == 1
    with pytest.raises(Problem) as incomplete:
        h.service.search(CONTEXT, h.pid, QUERY)
    assert code(incomplete) == "MEMORY_NATIVE_NOT_COMPLETE"
    assert len(h.transport.requests) == 2


def test_unknown_search_context_returns_explicit_exact_fallback_without_reissuing(projection_service):
    h = projection_service
    completed(h)
    h.transport.responses.append(ServiceResponseError("lost search response"))
    with pytest.raises(Problem) as missing:
        h.service.search(CONTEXT, h.pid, QUERY)
    assert code(missing) == "MEMORY_SEARCH_UNKNOWN"
    before = len(h.transport.requests)
    result = context(h, QUERY)
    assert result.degraded and result.summary is None
    assert any(b"cobalt" in original for original in result.originals.values())
    assert any("native-search-unknown" in reason for reason in result.manifest.omissions)
    assert len(h.transport.requests) == before


def test_lost_native_accepted_response_restart_stays_unknown_without_id_or_second_post(projection_service):
    h = projection_service
    h.transport.responses.append(ServiceResponseError("accepted but response lost; Bearer synthetic-secret"))
    with pytest.raises(Problem) as error:
        h.service.start(CONTEXT, h.pid)
    assert code(error) == "MEMORY_NATIVE_UNKNOWN"
    h.service = restart(h)
    result = state(h)
    assert result.phase == "outcome_unknown" and result.update_id is None and result.start_consumed
    assert h.service.start(CONTEXT, h.pid).payload["phase"] == "outcome_unknown"
    with pytest.raises(Problem):
        h.service.poll(CONTEXT, h.pid)
    assert len(update_posts(h)) == 1
    assert "synthetic-secret" not in str(h.table.backing.rows)


def test_control_consume_ack_loss_never_calls_native_after_restart(projection_service):
    h = projection_service
    h.table.failure = "after"
    with pytest.raises(Problem) as error:
        h.service.start(CONTEXT, h.pid)
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    h.service = restart(h)
    assert state(h).phase == "outcome_unknown"
    assert h.service.start(CONTEXT, h.pid).payload["update_id"] is None
    assert h.transport.requests == []


@pytest.mark.parametrize("failure", ["before", "after"])
def test_native_ack_persistence_failure_reconciles_only_retained_id(projection_service, failure):
    h = projection_service

    def accepted(request):
        h.table.failure = failure
        return 202, {"update_id": "native-update-1", "status": "queued"}, {}

    h.transport.responses.append(accepted)
    with pytest.raises(Problem) as unknown:
        h.service.start(CONTEXT, h.pid)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    h.service = restart(h)
    result = h.service.start(CONTEXT, h.pid).payload
    assert result["phase"] == ("accepted" if failure == "after" else "outcome_unknown")
    assert result["update_id"] == ("native-update-1" if failure == "after" else None)
    assert len(update_posts(h)) == 1


@pytest.mark.parametrize("raw", [
    {"status": "queued"}, {"update_id": "../other?secret=x", "status": "queued"},
    {"update_id": "native-update-1", "status": "completed", "result": {"memory_operations": [], "usage": USAGE}},
    {"update_id": "native-update-1", "status": "invented"},
])
def test_invalid_accepted_receipt_never_fabricates_or_resubmits(projection_service, raw):
    h = projection_service
    h.transport.responses.append((202, raw, {}))
    with pytest.raises(Problem) as error:
        h.service.start(CONTEXT, h.pid)
    assert code(error) == "MEMORY_NATIVE_SCHEMA"
    assert state(h).update_id is None and state(h).phase == "outcome_unknown"
    h.service.start(CONTEXT, h.pid)
    assert len(update_posts(h)) == 1


def test_http_failure_overrides_sdk_retry_defaults_and_no_secret_error_storage(projection_service):
    h = projection_service
    h.transport.responses.append((503, {"error": {"code": "Synthetic", "message": "arbitrary-secret-value"}}, {}))
    with pytest.raises(Problem):
        h.service.start(CONTEXT, h.pid)
    assert len(h.transport.requests) == 1
    assert state(h).phase == "outcome_unknown"
    assert "arbitrary-secret-value" not in str(h.table.backing.rows)


def test_redirect_never_follows_changed_endpoint(projection_service):
    h = projection_service
    h.transport.responses.append((307, {}, {"Location": "https://untrusted.invalid/steal"}))
    with pytest.raises(Problem):
        h.service.start(CONTEXT, h.pid)
    assert len(h.transport.requests) == 1


def test_poll_budget_survives_restart_and_failed_reads_consume_slots(projection_service):
    h = projection_service
    queued(h)
    h.transport.responses.append(ServiceResponseError("read failed"))
    with pytest.raises(Problem):
        h.service.poll(CONTEXT, h.pid)
    assert state(h).polls_consumed == 1
    h.service = restart(h)
    for _ in range(2):
        h.transport.responses.append((200, {"update_id": "native-update-1", "status": "in_progress"}, {}))
        assert h.service.poll(CONTEXT, h.pid).payload["phase"] == "accepted"
    with pytest.raises(Problem) as exhausted:
        h.service.poll(CONTEXT, h.pid)
    assert code(exhausted) == "MEMORY_POLL_BUDGET_EXHAUSTED"
    assert len(h.transport.requests) == 4 and state(h).polls_consumed == 3


def test_mismatched_polled_update_id_consumes_budget_but_preserves_original_id(projection_service):
    h = projection_service
    queued(h)
    h.transport.responses.append((200, {"update_id": "different-id", "status": "in_progress"}, {}))
    with pytest.raises(Problem) as error:
        h.service.poll(CONTEXT, h.pid)
    assert code(error) == "MEMORY_NATIVE_SCHEMA"
    assert state(h).update_id == "native-update-1" and state(h).polls_consumed == 1
    assert "different-id" not in str(h.table.backing.rows)


@pytest.mark.parametrize("status", ["failed", "superseded"])
def test_failed_native_preserves_sanitized_status_and_explicit_exact_fallback(projection_service, status):
    h = projection_service
    queued(h)
    h.transport.responses.append((200, {
        "update_id": "native-update-1", "status": status,
        "error": {"code": "secret-error-code", "message": "arbitrary-provider-secret"},
    }, {}))
    with pytest.raises(Problem) as error:
        h.service.poll(CONTEXT, h.pid)
    assert code(error) == "MEMORY_NATIVE_FAILED" and state(h).phase == status
    with pytest.raises(Problem):
        h.service.search(CONTEXT, h.pid, QUERY)
    result = context(h)
    assert result.degraded and result.manifest.exact_source_fallback and result.summary is None
    assert "native-" + status in " ".join(result.manifest.omissions)
    assert b"cobalt" in result.originals[h.source.reference.key]
    assert "arbitrary-provider-secret" not in str(h.table.backing.rows)
    assert "secret-error-code" not in str(h.table.backing.rows)


def test_exact_native_receipt_lineage_search_and_context_are_not_authority(projection_service):
    h = projection_service
    original = completed(h)
    key = state(h).completion_receipt_key
    receipt = control_row(h.service.read_snapshot((key,)), key, NativeReceipt)
    assert receipt.native == original and receipt.native_sha256 == digest(original)
    assert not receipt.sanitized and receipt.projection_id == h.pid
    searched_result = ProjectionSearch.model_validate_json(json.dumps(searched(h).payload))
    hit = searched_result.candidates[0]
    assert hit.claim == h.claim.reference and hit.supporting == h.claim.candidate.supporting
    assert hit.completion_receipt_key == key and hit.trust == "retrieval-candidate-not-authority"
    assert searched_result.usage.total_tokens == 16
    result = context(h, QUERY)
    assert result.summary == h.claim.candidate.text
    assert not result.degraded and not result.manifest.exact_source_fallback
    assert result.originals[h.source.reference.key] == h.source.original
    assert result.manifest.claims == (h.claim.reference,)
    assert result.manifest.sections[-1].kind == "projection"
    assert result.trust == "retrieval-candidate-not-authority"
    assert len(h.transport.requests) == 3  # cached search did not send another POST
    assert h.transport.requests[1].url == ENDPOINT + "/memory_stores/existing-reviewed-memory/updates/native-update-1?api-version=v1"
    assert json.loads(h.transport.requests[2].body)["scope"] == h.scope


def test_native_extraction_losing_cobalt_never_loses_exact_original_or_claim(projection_service):
    h = projection_service
    text = "A dashboard color preference exists."
    original = completed(h, text)
    searched(h, text)
    result = context(h, QUERY)
    assert result.summary is None and result.degraded and result.manifest.exact_source_fallback
    assert "cobalt" in result.claims[0].candidate.text
    assert b"cobalt" in result.originals[h.source.reference.key]
    assert any("native-required-fact-omitted" in omission for omission in result.manifest.omissions)
    key = state(h).completion_receipt_key
    assert control_row(h.service.read_snapshot((key,)), key, NativeReceipt).native == original


def test_bounded_success_receipt_is_lossless_not_safe_text_truncated(projection_service):
    h = projection_service
    text = "An unreviewed description: " + "a" * 6000
    original = completed(h, text)
    key = state(h).completion_receipt_key
    receipt = control_row(h.service.read_snapshot((key,)), key, NativeReceipt)
    assert receipt.native == original
    assert receipt.native["result"]["memory_operations"][0]["memory_item"]["content"] == text
    searched(h, text)
    assert context(h, QUERY).summary is None


@pytest.mark.parametrize("prior_kind", [None, "create", "updated"])
def test_deleted_or_ambiguous_native_item_cannot_supply_lineage(projection_service, prior_kind):
    h = projection_service
    queued(h)
    raw = complete_payload(h, operation_kind="delete")
    if prior_kind is not None:
        raw["result"]["memory_operations"].insert(0, {"kind": prior_kind, "memory_item": item(h)})
    h.transport.responses.append((200, raw, {}))
    assert h.service.poll(CONTEXT, h.pid).payload["phase"] == "completed"
    h.service = restart(h)
    search = ProjectionSearch.model_validate_json(json.dumps(searched(h).payload))
    assert not search.candidates and search.degraded
    result = context(h, QUERY)
    assert result.summary is None and result.degraded
    assert "native-receipt-lineage-mismatch" in " ".join(result.manifest.omissions)


@pytest.mark.parametrize("operation_kind", ["create", "updated"])
@pytest.mark.parametrize("change,reason", [
    ({"scope": "some-other-scope"}, "native-scope-mismatch"),
    ({"memory_id": "unreceipted-item"}, "native-receipt-lineage-mismatch"),
    ({"updated_at": "2026-09-15T14:01:00Z"}, "native-receipt-lineage-mismatch"),
])
def test_similarity_or_exact_text_without_original_receipt_relation_is_excluded(
    projection_service, operation_kind, change, reason,
):
    h = projection_service
    completed(h, operation_kind=operation_kind)
    result = ProjectionSearch.model_validate_json(json.dumps(searched(h, **change).payload))
    assert not result.candidates and result.degraded
    assert reason in " ".join(result.omissions)
    assert context(h, QUERY).summary is None


def test_native_procedural_item_cannot_publish_policy_even_when_text_matches(projection_service):
    h = projection_service
    completed(h, kind="procedural")
    searched(h, kind="procedural")
    result = context(h, QUERY)
    assert result.summary is None and "native-procedural-data" in " ".join(result.manifest.omissions)


@pytest.mark.parametrize("operation_kind", ["create", "updated"])
@pytest.mark.parametrize("moment", ["prepared", "accepted", "before-search", "during-search"])
def test_withdrawal_never_revives_source_and_late_completion_still_preserves_receipt(
    projection_service, operation_kind, moment,
):
    h = projection_service

    def withdraw():
        h.sources.withdraw_source(HUMAN, HUMAN_CONTEXT, withdrawal(h.source))

    if moment == "prepared":
        withdraw()
        with pytest.raises(Problem):
            h.service.start(CONTEXT, h.pid)
        assert h.transport.requests == []
    elif moment == "accepted":
        queued(h)
        withdraw()
        h.transport.responses.append((200, complete_payload(h, operation_kind=operation_kind), {}))
        assert h.service.poll(CONTEXT, h.pid).payload["phase"] == "completed"
    else:
        completed(h, operation_kind=operation_kind)
        if moment == "before-search":
            withdraw()
        else:
            def during_search(request):
                withdraw()
                return 200, {"search_id": "native-search-1", "memories": [{"memory_item": item(h)}], "usage": USAGE}, {}
            h.transport.responses.append(during_search)
        with pytest.raises(Problem):
            h.service.search(CONTEXT, h.pid, QUERY)
    with pytest.raises(Problem) as revoked:
        context(h)
    assert code(revoked) == "SOURCE_WITHDRAWN"
    assert len(update_posts(h)) <= 1


def test_claim_revocation_at_same_guard_prevents_send(projection_service):
    h = projection_service
    revoked = h.verifier.plan_decision(
        HUMAN, HUMAN_CONTEXT, decision(h.verifier, h.proposed, decision="revoke", review_id="revoke-one"))
    h.table.before_submit = lambda: h.verifier.commit_decision(HUMAN, HUMAN_CONTEXT, revoked)
    with pytest.raises(Problem) as conflict:
        h.service.start(CONTEXT, h.pid)
    assert code(conflict) == "BEHAVIOR_CONFLICT"
    assert h.transport.requests == []
    assert state(h).phase == "planned"


def test_correction_after_start_consumption_is_checked_at_transport_gate(projection_service, monkeypatch):
    h = projection_service
    original = h.service._reserve

    def reserve(*args, **kwargs):
        won = original(*args, **kwargs)
        admitted(h.sources, b"New exact source is amber.", version=2)
        return won

    monkeypatch.setattr(h.service, "_reserve", reserve)
    with pytest.raises(Problem):
        h.service.start(CONTEXT, h.pid)
    assert state(h).phase == "outcome_unknown"
    assert h.transport.requests == []
    with pytest.raises(Problem):
        context(h)


def test_corrected_claim_uses_current_exact_fallback_not_stale_native(projection_service):
    h = projection_service
    completed(h)
    searched(h)
    replacement = h.proposed.model_copy(update={
        "version": 2, "supersedes": h.claim.reference.sha256,
        "text": "The reviewed dashboard appearance preference is cobalt.",
    })
    new = claimed(h.verifier, h.memory, replacement)
    with pytest.raises(Problem) as stale:
        h.service.search(CONTEXT, h.pid, QUERY)
    assert code(stale) == "MEMORY_PROJECTION_STALE"
    result = context(h, QUERY)
    assert result.summary is None and result.manifest.claims == (new.reference,)
    assert any("native-projection-stale" in omission for omission in result.manifest.omissions)
    assert len(h.transport.requests) == 3


def test_private_projection_wrong_actor_cannot_read_plan_send_search_or_assemble(source_service):
    sources, _ = source_service
    source = sources.plan(HUMAN_CONTEXT, input_source(visibility="private"), b"private cobalt preference")
    sources.commit(HUMAN_CONTEXT, source)
    verifier = DurableHumanReviewVerifier(sources, POLICY, clock=lambda: NOW)
    memory = MemoryGovernance(sources, clock=lambda: NOW, token_counter=len, review_verifier=verifier)
    proposed = candidate(source.reference, visibility="private")
    approved(verifier, proposed)
    claim = memory.plan_claim(HUMAN_CONTEXT, proposed)
    memory.commit_claim(HUMAN_CONTEXT, claim)
    transport = Transport([])
    service = NativeMemoryProjection(memory, client(transport), setup(memory, scope=HUMAN_CONTEXT.scope("private")))
    pid = service.plan(HUMAN_CONTEXT).payload["result"]["projection_id"]
    for operation in (lambda: service.reconcile(OTHER, pid), lambda: service.start(OTHER, pid),
                      lambda: service.search(OTHER, pid, QUERY), lambda: service.assemble(OTHER, pid)):
        with pytest.raises(Problem) as denied:
            operation()
        assert code(denied) == "SOURCE_SCOPE_DENIED"
    assert transport.requests == []


def test_unknown_token_counts_and_input_query_context_budgets_fail_explicitly(projection_service):
    h = projection_service
    h.memory._count = lambda _: None
    with pytest.raises(Problem) as unknown:
        h.service.plan(CONTEXT)
    assert code(unknown) == "MEMORY_TOKEN_COUNT_UNKNOWN"
    h.memory._count = len
    small = NativeMemoryProjection(h.memory, client(h.transport), setup(h.memory, max_input_tokens=1))
    with pytest.raises(Problem) as budget:
        small.plan(CONTEXT)
    assert code(budget) == "MEMORY_PROJECTION_INPUT_BUDGET"
    completed(h)
    with pytest.raises(Problem) as query:
        h.service.search(CONTEXT, h.pid, ProjectionQuery(query="x" * 513))
    assert code(query) == "MEMORY_QUERY_BUDGET"
    h.memory._count = lambda _: None
    with pytest.raises(Problem) as unknown_context:
        context(h)
    assert code(unknown_context) == "MEMORY_TOKEN_COUNT_UNKNOWN"
    h.memory._count = len
    h.memory._max_tokens = 1
    with pytest.raises(Problem) as required:
        context(h)
    assert code(required) == "MEMORY_REQUIRED_CONTEXT_TOO_LARGE"
    assert len(h.transport.requests) == 2


@pytest.mark.parametrize("mutation", ["unknown-tokens", "oversize", "secret", "unknown-field"])
def test_invalid_success_is_not_truncated_or_saved_as_completion(projection_service, mutation):
    h = projection_service
    queued(h)
    raw = complete_payload(h)
    if mutation == "unknown-tokens":
        raw["result"]["usage"]["total_tokens"] = None
    elif mutation == "oversize":
        raw["result"]["memory_operations"][0]["memory_item"]["content"] = "x" * 40000
    elif mutation == "secret":
        raw["result"]["memory_operations"][0]["memory_item"]["content"] = "Bearer unique-provider-secret"
    else:
        raw["result"]["client_secret"] = "unique-provider-secret"
    h.transport.responses.append((200, raw, {}))
    with pytest.raises(Problem) as invalid:
        h.service.poll(CONTEXT, h.pid)
    assert code(invalid) == "MEMORY_NATIVE_SCHEMA"
    assert state(h).phase == "accepted" and state(h).completion_receipt_key is None
    assert "unique-provider-secret" not in str(h.table.backing.rows)


def test_search_ack_loss_is_durable_and_duplicate_query_never_second_post(projection_service):
    h = projection_service
    completed(h)
    h.transport.responses.append(ServiceResponseError("search ack lost; private-error"))
    with pytest.raises(Problem) as error:
        h.service.search(CONTEXT, h.pid, QUERY)
    assert code(error) == "MEMORY_SEARCH_UNKNOWN"
    h.service = restart(h)
    with pytest.raises(Problem):
        h.service.search(CONTEXT, h.pid, QUERY)
    assert state(h).searches_consumed == 1 and len(h.transport.requests) == 3
    assert "private-error" not in str(h.table.backing.rows)
    result = context(h)
    assert result.summary is None and result.degraded and result.originals


def test_finite_search_budget_and_unknown_native_search_usage(projection_service):
    h = projection_service
    completed(h)
    for i in range(2):
        h.transport.responses.append((200, {"search_id": "s", "memories": [], "usage": {}}, {}))
        with pytest.raises(Problem) as unknown:
            h.service.search(CONTEXT, h.pid, ProjectionQuery(query=f"query {i}"))
        assert code(unknown) == "MEMORY_SEARCH_UNKNOWN"
    with pytest.raises(Problem) as exhausted:
        h.service.search(CONTEXT, h.pid, ProjectionQuery(query="query 3"))
    assert code(exhausted) == "MEMORY_SEARCH_BUDGET_EXHAUSTED"
    assert state(h).searches_consumed == 2 and len(h.transport.requests) == 4


def test_empty_native_extraction_is_explicit_degraded_not_empty_memory_success(projection_service):
    h = projection_service
    queued(h)
    raw = complete_payload(h)
    raw["result"]["memory_operations"] = []
    h.transport.responses.append((200, raw, {}))
    h.service.poll(CONTEXT, h.pid)
    h.transport.responses.append((200, {"search_id": "s", "memories": [], "usage": USAGE}, {}))
    result = context(h, QUERY)
    assert result.degraded and result.manifest.exact_source_fallback and result.claims and result.originals
    assert "native-required-fact-omitted" in " ".join(result.manifest.omissions)


def test_binding_namespace_and_strict_request_schemas(projection_service):
    h = projection_service
    for key in ("scope", "owner", "approved", "update_id", "projection_namespace"):
        with pytest.raises(ValidationError):
            ProjectionQuery(query="color", **{key: "injected"})
    for changes in ({"max_polls": None}, {"max_polls": True}, {"max_polls": 0},
                    {"projection_namespace": "old-m4-store"}, {"max_searches": 0}):
        with pytest.raises(ValidationError):
            setup(h.memory, **changes)
    with pytest.raises(Problem):
        NativeMemoryProjection(h.memory, client(h.transport), setup(h.memory, control_binding="f" * 64))
    other = NativeMemoryProjection(h.memory, client(h.transport), setup(h.memory, binding_id="runtime-two"))
    other_plan = other.plan(CONTEXT)
    assert other_plan.payload["result"]["native_scope"] != h.scope
    with pytest.raises(Problem) as wrong_binding:
        other.reconcile(CONTEXT, h.pid)
    assert code(wrong_binding) == "MEMORY_PROJECTION_BINDING"
    assert h.transport.requests == []
