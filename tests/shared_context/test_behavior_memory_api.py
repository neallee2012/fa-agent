"""Authenticated native-memory composition using the actual SDK/local transport."""
import base64
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from conftest import A, TENANT
from shared_context import behavior_wire as wire
from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_runtime import BehaviorRuntime
from shared_context.memory_projection import NativeMemoryProjection, ProjectionSetup
from shared_context.source_materials import SourceScope
from test_adapters import ENDPOINT
from test_behavior_api import BASE, METRICS, RuntimeHarness
from test_behavior_delivery import NOW
from test_m3_auth import jwk_client, signing_key
from test_memory_projection import Transport, USAGE, client


@pytest.fixture
def h(auth, jwk_client, signing_key):
    value = RuntimeHarness(auth, jwk_client, signing_key)
    ref = value.source()
    draft = wire.ClaimDraft(request_id="review-memory", version=1, text="The accepted metrics source has been reviewed.",
                            supporting=(ref,), effective_from=NOW)
    preview = value.post("/claims/claim-one/preview", draft)
    assert preview.status_code == 200
    assert value.post("/claims/claim-one/review", wire.ClaimReview(
        **draft.model_dump(), decision_id="review-memory-decision",
        **{key: preview.json()[key] for key in ("candidate_sha256", "evidence_sha256", "policy_version")},
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
    )).status_code == 200
    assert value.post("/claims/claim-one/commit", wire.Command(request_id="review-memory")).status_code == 200
    value.memory_transport = Transport([])
    value.memory_client = client(value.memory_transport)
    setup = ProjectionSetup(
        control_binding=value.store._binding, binding_id=value.binding.binding_id,
        endpoint=ENDPOINT, store="existing-memory", projection_namespace="fa-reviewed-v1-api",
        evidence_sha256="e" * 64, scope=SourceScope(tenant_id=TENANT, team_id=A.team, visibility="team"),
        subject=value.binding.subject, claim_ids=("claim-one",), versions=value.binding.context_versions,
        max_polls=3, max_searches=2, max_input_tokens=4096, max_query_tokens=512,
        max_input_bytes=16384, max_response_bytes=32768,
    )
    projection = NativeMemoryProjection(value.memory, value.memory_client, setup)
    value.dep = replace(value.dep, memory_projections={value.binding.binding_id: projection})
    value.runtime = BehaviorRuntime(value.dep, (value.binding,), clock=lambda: value.time,
                                    mode="offline_contract", controller_id="internal-controller")
    value.client = TestClient(create_behavior_app(value.runtime, value.verifier))
    value.reference = ref
    yield value
    value.memory_client.close()


def test_native_memory_miss_falls_back_to_exact_reviewed_originals_and_rechecks_withdrawal(h):
    plan = h.post("/memory/plan", wire.Command(request_id="projection-plan"))
    assert plan.status_code == 200, plan.text
    pid = plan.json()["result"]["projection_id"]
    scope = plan.json()["result"]["native_scope"]
    item = {"memory_id": "native-item", "updated_at": "2026-09-15T14:00:00Z", "scope": scope,
            "content": "Unrelated extracted summary without the required fact.", "kind": "user_profile"}
    h.memory_transport.responses.append((202, {"update_id": "native-update", "status": "queued"}, {}))
    command = wire.MemoryProjectionCommand(request_id="native-projection", projection_id=pid)
    assert h.post("/memory/start", command).status_code == 200
    assert h.post("/memory/start", command).status_code == 200
    assert len(h.memory_transport.requests) == 1
    h.memory_transport.responses.append((200, {
        "update_id": "native-update", "status": "completed",
        "result": {"memory_operations": [{"kind": "create", "memory_item": item}], "usage": USAGE},
    }, {}))
    assert h.post("/memory/poll", command).status_code == 200
    h.memory_transport.responses.append((200, {
        "search_id": "native-search", "memories": [{"memory_item": item}], "usage": USAGE,
    }, {}))
    query = wire.MemoryProjectionQuery(request_id="context-native", projection_id=pid, query="required metrics")
    result = h.post("/memory/context", query)
    assert result.status_code == 200, result.text
    assert "Unrelated extracted summary" not in result.text
    assert METRICS in [base64.b64decode(value) for value in result.json()["originals"].values()]
    assert result.json()["degraded"] is True
    assert h.post("/sources/metrics/withdraw", wire.SourceWithdrawal(
        request_id="withdraw-projection", withdrawal_id="withdraw-source",
        reference=h.reference, expected_access_revision=1,
    )).status_code == 200
    before = len(h.memory_transport.requests)
    denied = h.post("/memory/context", query)
    assert denied.status_code == 403
    assert len(h.memory_transport.requests) == before


def test_native_projection_http_does_not_accept_caller_scope_or_old_m3_consent(h):
    denied = h.post("/memory/plan", wire.Command(request_id="wrong-scope"), identity="old-m3")
    assert denied.status_code == 403
    forbidden = h.post("/memory/plan", {"request_id": "spoof", "scope": "private:someone-else"})
    assert forbidden.status_code == 422
    assert h.memory_transport.requests == []


def test_completed_native_receipt_is_not_recall_without_lineage_and_never_replaces_missing_originals(h):
    planned = h.post("/memory/plan", wire.Command(request_id="state-vs-recall")).json()["result"]
    pid, scope = planned["projection_id"], planned["native_scope"]
    current = h.client.get(BASE + "/claims/claim-one", headers=h.headers()).json()
    item = {"memory_id": "receipted-item", "updated_at": "2026-09-15T14:00:00Z", "scope": scope,
            "content": current["candidate"]["text"], "kind": "chat_summary"}
    h.memory_transport.responses.append((202, {"update_id": "memory-update", "status": "queued"}, {}))
    command = wire.MemoryProjectionCommand(request_id="original-memory", projection_id=pid)
    accepted = h.post("/memory/start", command)
    assert accepted.status_code == 200 and accepted.json()["phase"] == "accepted"
    assert accepted.json()["completion_receipt_key"] is None
    native_receipt = {"update_id": "memory-update", "status": "completed",
                      "result": {"memory_operations": [{"kind": "created", "memory_item": item}], "usage": USAGE}}
    h.memory_transport.responses.append((200, native_receipt, {}))
    completed = h.post("/memory/poll", command)
    assert completed.status_code == 200 and completed.json()["phase"] == "completed"
    receipt_key = completed.json()["completion_receipt_key"]
    receipt = h.store.read_snapshot((receipt_key,))[receipt_key]
    h.memory_transport.responses.append((200, {"search_id": "search",
        "memories": [{"memory_item": {**item, "memory_id": "not-in-the-original-receipt"}}], "usage": USAGE}, {}))
    query = wire.MemoryProjectionQuery(request_id="recall-original", projection_id=pid, query="reviewed metrics")
    recalled = h.post("/memory/context", query)
    assert recalled.status_code == 200, recalled.text
    context = recalled.json()
    assert context["native_state"] == "completed" and context["completion_receipt_key"] == receipt_key
    assert context["summary"] is None and context["degraded"] is True
    assert context["manifest"]["exact_source_fallback"]
    assert any("lineage-mismatch" in reason for reason in context["manifest"]["omissions"])
    assert METRICS in [base64.b64decode(raw) for raw in context["originals"].values()]
    assert h.post("/memory/context", query).json() == context
    assert len(h.memory_transport.requests) == 3
    assert h.store.read_snapshot((receipt_key,))[receipt_key] == receipt
    assert h.post("/sources/metrics/withdraw", wire.SourceWithdrawal(
        request_id="withdraw-required-memory", withdrawal_id="withdraw-required",
        reference=h.reference, expected_access_revision=1)).status_code == 200
    refused = h.post("/memory/context", query)
    assert refused.status_code == 403 and current["candidate"]["text"] not in refused.text
    assert len(h.memory_transport.requests) == 3
    assert h.store.read_snapshot((receipt_key,))[receipt_key] == receipt
