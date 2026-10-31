"""An offline assistant runtime with a real SDK-protocol executor, and the human source review it needs."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from conftest import A, AUDIENCE, FakeMemory, InMemoryRepository
from shared_context import behavior_assistant_wire as aw, behavior_wire as wire
from shared_context.auth import AuthSettings
from shared_context.behavior_assistant import AssistantAuthority, AssistantProfile, AssistantRuntime, EXECUTOR_VERSION, PROTECTION_VERSION
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import ExecutorAdapter, ExecutorEvidence
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_auth import ApprovalSettings
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.models import CreateWork
from shared_context.service import ContextService
from shared_context.source_materials import digest
from test_behavior_server import NOW
from test_m3_auth import CLIENT_ID
import test_behavior_api as api_fixture
import test_m3_auth as auth_fixture

TENANT = "00000000-0000-0000-0000-000000000013"
HUMAN = "00000000-0000-0000-0000-00000000001b"


class SdkProtocolExecutor:
    def __init__(self, h):
        self.h, self.starts, self.results = h, [], {}
        self.mode = "success"
        self.error = None
        self.before_save = lambda: None
        self.answer_override = None
        self.model_requests = []

    def adapter(self):
        def start(request):
            try:
                return self.start(request)
            except Exception as error:
                self.error = error
                raise
        return ExecutorAdapter(start=start, query=self.query, stop=lambda _: None, verify=self.results.__getitem__)

    def start(self, start):
        self.starts.append(start)
        h = self.h
        assignment, ticket = h.assistant.issue(start)
        if self.mode == "unknown":
            evidence_id = control_key("offline-sdk-terminal", start.binding.start_request_id)
            self.results[evidence_id] = ExecutorEvidence(
                evidence_id=evidence_id, binding=start.binding, sequence=1, state="unknown",
                proof=b"OFFLINE missing provider response; no idle proof",
            )
            return evidence_id
        principal = BehaviorPrincipal(TENANT, A.oid, A.team, "agent", A.actor, frozenset({"read", "execute", "consume"}))
        session = h.assistant.session(assignment.session_id, ticket, principal)
        h.assistant.acquire(session, wire.Command(request_id="acquire"))
        result = None
        if self.mode == "success":
            context = h.assistant.context(session, wire.Command(request_id="context"), principal)
            body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": json.dumps({
                "item_count": context.item_count, "completed_count": context.completed_count,
            })}]}
            request_digest = digest(body)
            h.assistant.reserve_model(session, aw.Reservation(request_id=request_digest, request_sha256=request_digest), principal)
            h.assistant.consume_model(session, aw.ModelRequest(request_id=request_digest, body=body), principal)
            self.model_requests.append(request_digest)
            self.before_save()
            h.assistant.save(session, aw.Save(request_id="save", answer=aw.Answer(
                item_count=context.item_count, completed_count=context.completed_count,
                remaining_count=(context.item_count - context.completed_count
                                 if self.answer_override is None else self.answer_override))), principal)
            h.assistant.finish(session, wire.Command(request_id="finish"), principal)
            result = h.assistant.result(start.binding)
        else:
            if self.mode == "failed-after-model":
                h.assistant.context(session, wire.Command(request_id="context"), principal)
                body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "fixed metrics"}]}
                request_digest = digest(body)
                h.assistant.reserve_model(session, aw.Reservation(request_id=request_digest, request_sha256=request_digest), principal)
                h.assistant.consume_model(session, aw.ModelRequest(request_id=request_digest, body=body), principal)
            stop = wire.AssistantSdkStop(pid=123, exit_code=1, graceful=True)
            report = SimpleNamespace(outcome="failed", sdk_stop=stop, child_pid=123, exit_code=1,
                                     child_exited=True, result=None, checkpoint=None)
            result, _, _ = h.assistant.native_report_plan(SimpleNamespace(start=start), report, principal)
        evidence_id = control_key("offline-sdk-terminal", start.binding.start_request_id)
        self.results[evidence_id] = ExecutorEvidence(
            evidence_id=evidence_id, binding=start.binding, sequence=1, native_execution_id="offline-sdk-execution",
            state="succeeded" if self.mode == "success" else "failed", result=result, entire_tree_idle=True, all_inflight_starts_fenced=True,
            proof=b"OFFLINE SDK protocol tool execution; not native process evidence",
        )
        return evidence_id

    def query(self, binding):
        return control_key("offline-sdk-terminal", binding.start_request_id)


@pytest.fixture
def sdk_assistant(monkeypatch, jwk_client, signing_key):
    for module in (api_fixture, auth_fixture):
        monkeypatch.setattr(module, "TENANT", TENANT)
        monkeypatch.setattr(module, "HUMAN_OID", HUMAN)
    worker_type, delivery_type = api_fixture.WorkerContract, api_fixture.RuntimeContract
    monkeypatch.setattr(api_fixture, "WorkerContract", lambda **data: worker_type(**{
        **data, "capacity": 1, "executor_version": EXECUTOR_VERSION, "protection_version": PROTECTION_VERSION,
    }))
    monkeypatch.setattr(api_fixture, "RuntimeContract", lambda **data: delivery_type(**{
        **data, "protection_version": PROTECTION_VERSION,
    }))
    auth = AuthSettings(TENANT, AUDIENCE, (f"https://sts.windows.net/{TENANT}/",), {A.oid: A})
    h = api_fixture.RuntimeHarness(auth, jwk_client, signing_key, native_wire=False, work_checks=aw.CHECKS)
    h.table = h.transport
    ref = h.source()
    draft = wire.ClaimDraft(request_id="review", version=1, text="The accepted metrics source has been reviewed.",
                            supporting=(ref,), effective_from=h.time)
    preview = h.post("/claims/claim-one/preview", draft).json()
    reviewed = h.post("/claims/claim-one/review", wire.ClaimReview(
        **draft.model_dump(), decision_id="decision", candidate_sha256=preview["candidate_sha256"],
        evidence_sha256=preview["evidence_sha256"], policy_version=preview["policy_version"],
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
    ))
    assert reviewed.status_code == 200, reviewed.text
    assert h.post("/claims/claim-one/commit", wire.Command(request_id="review")).status_code == 200
    context = ContextService(InMemoryRepository(), FakeMemory(), auth)
    context.create(A, A.team, "legacy-source", CreateWork(goal="Metrics result owner"))
    h.artifacts = ArtifactService(context, InMemoryArtifactStore(TENANT), ApprovalSettings(HUMAN, CLIENT_ID, A.team))
    profile = AssistantProfile(binding_id=h.binding.binding_id, source=ref, claim_id="claim-one",
                               legacy_source_work="legacy-source", artifact_id="old-report", artifact_version="a" * 32,
                               policy_version="assistant-own-result-v1")
    h.runtime = AssistantRuntime(h.dep, (h.binding,), profiles=(profile,), clock=lambda: h.time,
                                 mode="offline_contract", controller_id="internal-controller")
    h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
    h.sdk = SdkProtocolExecutor(h)
    h.workers.adapter = h.sdk.adapter()
    h.parent = SimpleNamespace(runtime=h.runtime, config=SimpleNamespace(assistant=SimpleNamespace(
        provider_endpoint="https://offline.example.org/openai/v1/chat/completions")))
    return h


def review_source(store, config, *, review_id="offline-reviewed-pins", claim_id=None,
                  text="The fixed metrics source is reviewed."):
    from shared_context.source_materials import SourceMaterials, SourceInput, AuthenticatedSourceContext
    from shared_context.source_reviews import DurableHumanReviewVerifier, HumanReviewPolicy, HumanReviewCommand
    from shared_context.memory_governance import MemoryGovernance, ClaimCandidate
    from test_behavior_api import METRICS
    b, policy, profile = config.bindings[0], config.policies[0].policy, config.assistant.profiles[0]
    identity = config.identity
    principal = BehaviorPrincipal(identity.tenant_id, identity.human_oid, identity.team_id, "human", None,
                                  frozenset({"read", "approve"}))
    context = AuthenticatedSourceContext(b.tenant_id, b.team_id, principal.source_actor_id, b.ingress_id, policy.allowed_purposes)
    sources = SourceMaterials(store, clock=lambda: NOW)
    plan = sources.plan(context, SourceInput(source_id=profile.source.source_id, version=profile.source.version,
        visibility=policy.source_visibility, purpose=policy.purpose, schema_id="fa-artifact-metrics-v1",
        media_type="application/json"), METRICS)
    assert plan.reference.sha256 == profile.source.sha256
    sources.commit(context, plan)
    reviewer = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version=config.review_policy_version),
                                         clock=lambda: NOW)
    candidate = ClaimCandidate(claim_id=claim_id or profile.claim_id, version=1, subject=b.subject,
        visibility=policy.source_visibility, purpose=policy.purpose, kind="note",
        text=text, supporting=(plan.reference,),
        effective_from=NOW, expires_at=NOW + timedelta(minutes=2))
    preview = reviewer.preview(principal, context, candidate)
    command = HumanReviewCommand(review_id=review_id, candidate=candidate,
        candidate_sha256=preview.candidate_sha256, evidence_sha256=preview.evidence_sha256,
        policy_version=preview.policy_version, decision="approve",
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        reason="Offline explicit designated human review.")
    reviewer.commit_decision(principal, context, reviewer.plan_decision(principal, context, command))
    memory = MemoryGovernance(sources, clock=lambda: NOW, token_counter=len, review_verifier=reviewer)
    memory.commit_claim(context, memory.plan_claim(context, candidate))
