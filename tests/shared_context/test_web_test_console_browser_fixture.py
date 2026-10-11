"""Explicit OFFLINE-only browser fixture. Never used by the product launcher."""
import argparse
import json
from datetime import timedelta
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient

from conftest import A, TENANT, auth, InMemoryRepository, FakeMemory
from test_m3_auth import signing_key, jwk_client, HUMAN_OID, CLIENT_ID
from test_behavior_api import RuntimeHarness
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import AssistantAuthority, AssistantProfile, AssistantRuntime, _LocalHttp
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_auth import ApprovalSettings
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.models import CreateWork
from shared_context.service import ContextService
from web_test_console.adapter import ConsoleConfig
from web_test_console.bridge import create_app


def assistant_harness(settings, keys, signing):
    h = RuntimeHarness(settings, keys, signing, work_checks=("answer_verified", "artifact_verified", "finish_verified"))
    source = h.source()
    candidate = wire.ClaimDraft(request_id="review", version=1, text="The accepted metrics source has been reviewed.",
                               supporting=(source,), effective_from=h.time)
    response = h.post("/claims/claim-one/preview", candidate)
    assert response.status_code == 200, response.text
    preview = response.json()
    review = wire.ClaimReview(**candidate.model_dump(), decision_id="decision",
        candidate_sha256=preview["candidate_sha256"], evidence_sha256=preview["evidence_sha256"],
        policy_version=preview["policy_version"],
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"))
    assert h.post("/claims/claim-one/review", review).status_code == 200
    assert h.post("/claims/claim-one/commit", wire.Command(request_id="review")).status_code == 200
    context = ContextService(InMemoryRepository(), FakeMemory(), settings)
    context.create(A, A.team, "legacy-source", CreateWork(goal="OFFLINE saved result"))
    artifacts = ArtifactService(context, InMemoryArtifactStore(settings.tenant_id), ApprovalSettings(HUMAN_OID, CLIENT_ID, A.team))
    profile = AssistantProfile(binding_id=h.binding.binding_id, source=source, claim_id="claim-one",
        legacy_source_work="legacy-source", artifact_id="offline-report", artifact_version="a"*32,
        policy_version="assistant-own-result-v1")
    h.runtime = AssistantRuntime(h.dep, (h.binding,), profiles=(profile,), clock=lambda: h.time,
                                 mode="offline_contract", controller_id="internal-controller")
    h.assistant = AssistantAuthority(h.runtime, artifacts, (A,))
    h.client.close()
    h.app = create_assistant_app(h.assistant, h.verifier)
    h.client = TestClient(h.app)
    return h


def configuration(h):
    return ConsoleConfig.model_validate_json(json.dumps({
        "schema_version": "fa-web-test-console-v1", "scope_id": "OFFLINE-browser-only",
        "backend_origin": "https://offline.example.org", "tenant_id": TENANT, "human_oid": HUMAN_OID,
        "client_id": CLIENT_ID, "audience": h.verifier.auth.audience,
        "not_before": h.time.isoformat(), "expires_at": (h.time+timedelta(minutes=30)).isoformat(),
        "approval_reference": "OFFLINE-NOT-NATIVE", "approval_sha256": "a"*64,
        "backend_configuration_sha256": "b"*64, "backend_core_sha256": "c"*64,
        "tasks": [{
            "task_id": "metrics", "label": "OFFLINE｜已審核 metrics → 原成果 → 收件",
            "description": "本機 fixture 專用。真原 producer／JWT／HTTP；SDK、模型與儲存為離線替身，非原生驗收。",
            "mode": "normal_pull", "submission": "assistant",
            "binding_id": h.binding.binding_id, "job_id": h.binding.job_id, "work_id": h.binding.work.work_id,
            "event_id": h.binding.event_id, "root_request_id": h.binding.root_request_id,
            "obligation_id": h.binding.obligation_id, "owner_record_id": h.binding.owner.owner_record_id,
            "route": h.binding.route.model_dump(mode="json"), "channel_version": h.config.channel.version,
            "submit_request_id": "offline-fixed-submit", "fetch_request_id": "offline-fixed-fetch",
            "ack_request_id": "offline-fixed-ack", "allowed_actions": ["status", "submit", "fetch", "acknowledge"]
        }]
    }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    key = signing_key.__wrapped__()
    h = assistant_harness(auth.__wrapped__(), jwk_client.__wrapped__(key), key)
    import test_teams_metrics
    test_teams_metrics.TENANT = TENANT
    executor = test_teams_metrics.SdkProtocolExecutor(h)
    h.workers.adapter = executor.adapter()
    local = _LocalHttp(h.client)
    trace = []
    def send(request):
        response = local.handle_request(request)
        trace.append({"method": request.method, "path": request.url.path, "status": response.status_code})
        if request.url.path.endswith("/assistant/submit") and response.status_code == 200:
            result = h.runtime.controller.step(h.binding.binding_id)  # Only this offline fixture owns a controller.
            if executor.error is not None:
                raise executor.error
            assert result.state == "succeeded"
        args.journal.with_suffix(".http-summary.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
        return response
    backend = httpx.Client(transport=httpx.MockTransport(send), follow_redirects=False)
    app = create_app(configuration(h), args.journal,
        token_provider=lambda: h.headers()["Authorization"].removeprefix("Bearer "),
        backend=backend, clock=lambda: h.time, port=args.port, environment="OFFLINE_TEST")
    print("OFFLINE_TEST only; real ASGI producers behind a no-network HTTPX transport.")
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False, server_header=False)
    finally:
        backend.close()
        h.client.close()


if __name__ == "__main__":
    main()
