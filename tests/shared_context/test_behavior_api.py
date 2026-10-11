"""OFFLINE: signed JWT + real modules/Table SDK over an explicitly local transport.

The deterministic executor below is a local fixture, NOT Hosted evidence.
No Entra grants, native readiness, Azure calls or model requests are made.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import anyio
import pytest
from fastapi.testclient import TestClient

from conftest import A, TENANT
from shared_context import behavior_wire as wire
from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.behavior_delivery import (
    ChannelContract, DeliveryCore, PendingIndexRecord, QuotaRecord, ReadinessEvidence,
    Route, RuntimeContract, RuntimeRegistration, VerifiedWorkResult, WorkDefinition,
)
from shared_context.behavior_effects import GOAL_REVIEW, WorkGoalUpdate, WorkStateEffectAdapter
from shared_context.behavior_inbox import INBOX_CHANNEL_ID, INBOX_VERSION, InboxRegistration, PullInbox
from shared_context.behavior_operations import (
    BudgetRecord, ExecutionContext, OwnerRecord, PolicyRecord, ToolBrokerCore, control_key,
)
from shared_context.behavior_runtime import (
    BehaviorRuntime, RuntimeBinding, RuntimeDependencies, RuntimeEvidenceAuthority, RuntimePolicy, runtime_key,
)
from shared_context.behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import ExecutorAdapter, ExecutorEvidence, WorkerContract, WorkerCore
from shared_context.errors import Problem
from shared_context.memory_governance import ContextVersions, MemoryGovernance
from shared_context.source_materials import SourceMaterials
from shared_context.source_reviews import DurableHumanReviewVerifier, HumanReviewPolicy
from test_behavior_delivery import NOW
from test_behavior_store import FakeTable, SCOPE, wire_store
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token

BASE = "/behavior/v1/bindings/binding-one"
METRICS = b'{ "schema_version": "fa-artifact-metrics-v1", "item_count": 3, "completed_count": 2 }\n'
CTX = ExecutionContext(
    owner_record_id="owner", principal_id=A.oid, epoch=1, conversation_id="conversation",
    policy_id="policy", root_budget_id="root",
)


class LocalExecutor:
    def __init__(self):
        self.starts, self.results = [], {}
        self.verifier_version = "runtime-verifier-v1"
        self.fail_after_apply = False

    def adapter(self):
        return ExecutorAdapter(self.start, self.query, self.stop, lambda key: self.results[key])

    def start(self, request):
        self.starts.append(request)
        body = wire.ExecutorJobInput.model_validate_json(request.input)
        result = wire.ExecutorJobResult(
            job_id=body.job_id, work_id=body.work_id, definition_digest=body.definition_digest,
            passed_checks=body.required_checks,
        )
        evidence_id = control_key("offline-completed", request.binding.start_request_id)
        self.results[evidence_id] = ExecutorEvidence(
            evidence_id=evidence_id, binding=request.binding, sequence=1,
            native_execution_id="offline-execution", state="succeeded",
            entire_tree_idle=True, all_inflight_starts_fenced=True,
            result=VerifiedWorkResult(
                evidence_id=control_key("offline-work-result", evidence_id), verifier_version=self.verifier_version,
                work_id=result.work_id, definition_digest=result.definition_digest, state="succeeded",
                passed_checks=result.passed_checks, content=result.result_text.encode(),
                proof=b"OFFLINE deterministic executor result, not native evidence",
            ),
            proof=b"OFFLINE no-overlap executor fixture",
        )
        if self.fail_after_apply:
            raise TimeoutError("SYNTHETIC_EXECUTOR_SECRET")
        return evidence_id

    def query(self, binding):
        return control_key("offline-completed", binding.start_request_id)

    def stop(self, request):
        raise Problem(403, "OFFLINE_STOP_DISABLED", "stop is outside this fixture")


class RuntimeHarness:
    def __init__(self, auth, key_client, signing, *, native_wire=True, work_checks=("complete",)):
        self.time, self.serial, self.signing = NOW, 0, signing
        if native_wire:
            self.store, self.transport = wire_store()
        else:
            self.transport = FakeTable()
            self.store = BehaviorStore(self.transport, SCOPE)
        guard = self.store.initialize()
        self.evidence = RuntimeEvidenceAuthority(self.store, clock=lambda: self.time, authentication_version="behavior-jwt-v1")
        self.inbox = PullInbox(self.store, InboxRegistration("behavior-jwt-v1", self.evidence.verify_access))
        self.executor = LocalExecutor()
        self.workers = WorkerCore(
            self.store, WorkerContract(
                workspace_id="workspace", control_binding=guard.payload["binding"],
                executor_id="offline-executor", executor_version="v1", protection_version="inline-v1",
            ), adapter=self.executor.adapter(), clock=lambda: self.time, mode="offline_contract",
        )
        self.config = RuntimeContract(
            control_binding=guard.payload["binding"], team_id=A.team, entry_id="behavior-api",
            protection_version="inline-v1", consumer_version="consumer-v1", timer_version="timer-v1",
            verifier_version=self.executor.verifier_version,
            channel=ChannelContract(channel_id=INBOX_CHANNEL_ID, version=INBOX_VERSION,
                                    supported_levels=("client_received",), max_confirmation_level="client_received"),
        )
        registration = RuntimeRegistration(
            contract=self.config, mode="offline_contract", verify_readiness=self.readiness,
            verify_ingress=self.evidence.verify_ingress, verify_work_result=self.workers.verify_work_result,
            verify_receipt=self.inbox.verify_receipt, channel_adapter=self.inbox,
        )
        self.delivery = DeliveryCore(self.store, clock=lambda: self.time, registration=registration)
        self.inbox.attach(self.delivery)
        self.sources = SourceMaterials(self.store)
        self.reviewer = DurableHumanReviewVerifier(self.sources, HumanReviewPolicy(policy_version="human-review-v1"), clock=lambda: self.time)
        self.memory = MemoryGovernance(self.sources, clock=lambda: self.time, token_counter=len, review_verifier=self.reviewer)
        self.references = {}
        self.adapter = WorkStateEffectAdapter(self.delivery, allowed_work_ids=("work-one",), references=self.references)
        self.broker = ToolBrokerCore(
            self.store, clock=lambda: self.time, adapter=self.adapter.registration, execution_adapter=self.adapter,
        )
        self.binding = RuntimeBinding(
            binding_id="binding-one", tenant_id=TENANT, team_id=A.team, human_oid=HUMAN_OID,
            agent_oid=A.oid, owner=CTX, ingress_id="authenticated-api",
            source_ids=("metrics", "dependency"), claim_ids=("claim-one",), subject="metrics-subject",
            event_id="event-one", root_request_id="root-request", job_id="job-one",
            work=WorkDefinition(work_id="work-one", goal="Run the deterministic lifecycle proof.",
                                criteria_version="criteria-v1", result_kind="answer", required_checks=work_checks),
            route=Route(tenant_id=TENANT, account_id="account", recipient_id=HUMAN_OID,
                        channel_id=INBOX_CHANNEL_ID, thread_id="thread-one"),
            operation_id="op", attempt_id="attempt", grant_id="grant", allowed_input_refs=("goal",),
            context_versions=ContextVersions(work_revision=1, release_version="r1", catalog_version="c1", memory_schema_version="m1"),
        )
        self.dep = RuntimeDependencies(self.store, self.sources, self.memory, self.broker, self.delivery,
                                       self.inbox, self.workers, self.evidence, self.reviewer)
        self.runtime = BehaviorRuntime(self.dep, (self.binding,), clock=lambda: self.time, mode="offline_contract",
                                       controller_id="internal-controller")
        rows = [
            (control_key("owner", "owner"), OwnerRecord(**CTX.model_dump(), active=True, operation_ids=("op",))),
            (control_key("policy", "policy"), PolicyRecord(policy_id="policy", revision=1, active=True)),
            (control_key("budget", "root"), BudgetRecord(root_budget_id="root", active=True, remaining_attempts=20, remaining_units=40)),
            (control_key("delivery-index", "root"), PendingIndexRecord(
                configuration_digest=self.delivery.configuration_digest(), root_budget_id="root",
            )),
        ]
        for kind, value in (("team", A.team), ("entry", self.config.entry_id), ("root", "root")):
            rows.append((control_key("delivery-quota", kind, value), QuotaRecord(
                configuration_digest=self.delivery.configuration_digest(), scope_kind=kind, scope_id=value,
                pending_limit=32, root_request_id="root-request" if kind == "root" else None,
                available_control_units=1000 if kind == "root" else 0, scans_remaining=64 if kind == "root" else 0,
            )))
        self.seed(rows)
        self.workers.initialize()
        self.policy = RuntimePolicy(
            binding_digest=self.runtime.binding_digest(self.binding), operation_policy_revision=1, active=True,
            source_visibility="team", purpose="metrics", allowed_purposes=("metrics",), valid_until=NOW + timedelta(hours=1),
        )
        self.runtime.bootstrap({"binding-one": self.policy})
        self.verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), key_client)
        self.app = create_behavior_app(self.runtime, self.verifier)
        self.client = TestClient(self.app)

    def readiness(self):
        return ReadinessEvidence(
            evidence_id="offline-readiness", configuration_digest=self.delivery.configuration_digest(),
            control_binding=self.config.control_binding, protection_version=self.config.protection_version,
            consumer_version=self.config.consumer_version, timer_version=self.config.timer_version,
            coverage=("atomic_control", "inline_retention", "durable_consumer", "timer_redrive", "bounded_scan", "ownership_recovery"),
            observed_at=NOW, expires_at=NOW + timedelta(hours=1), proof=b"OFFLINE fixture only",
        )

    def seed(self, rows):
        self.serial += 1
        self.store.transact(ControlRequest(request_id=f"seed-{self.serial}", request_digest="a" * 64),
                            lambda _: ControlChanges(writes=tuple(ControlWrite(row_id=key, mode="create",
                            data=value.model_dump(mode="json")) for key, value in rows), result={"offline": True}))

    def mutate(self, key, **updates):
        self.serial += 1
        request = ControlRequest(request_id=f"mutate-{self.serial}", request_digest="b" * 64, read_set=(key,))

        def apply(rows):
            from shared_context.behavior_store import InlineMaterial
            row = rows[key].payload
            return ControlChanges(writes=(ControlWrite(
                row_id=key, mode="replace", data={**row["data"], **updates},
                material=None if row["material"] is None else InlineMaterial.model_validate_json(json.dumps(row["material"])),
            ),), result={"offline": True})

        return self.store.transact(request, apply)

    def headers(self, identity="human"):
        if identity == "agent":
            encoded = token(self.signing, {"oid": A.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",))
        else:
            scope = "Artifacts.Approve" if identity == "old-m3" else "Behavior.Read" + (
                " Behavior.Approve" if identity == "human" else "")
            encoded = token(self.signing, {"scp": scope})
        return {"Authorization": "Bearer " + encoded, "Content-Type": "application/json"}

    def post(self, path, command, identity="human"):
        raw = command.model_dump_json() if isinstance(command, wire.WireModel) else json.dumps(command)
        return self.client.post(BASE + path, content=raw, headers=self.headers(identity))

    def source(self):
        command = wire.SourcePrepare(request_id="source-one", version=1, original=METRICS)
        prepared = self.post("/sources/metrics/prepare", command)
        assert prepared.status_code == 200, prepared.text
        committed = self.post("/sources/metrics/commit", wire.Command(request_id="source-one"))
        assert committed.status_code == 200, committed.text
        return wire.ExactSourceRef(source_id="metrics", version=1, sha256=hashlib.sha256(METRICS).hexdigest(), byte_length=len(METRICS))

    def submit(self):
        response = self.post("/work/submit", wire.SubmitWork(request_id="submit-one"))
        assert response.status_code == 200, response.text
        return response


@pytest.fixture
def h(auth, jwk_client, signing_key):
    return RuntimeHarness(auth, jwk_client, signing_key)


def test_full_authenticated_submit_worker_result_exact_pull_and_ack(h):
    assert h.submit().json()["state"] == "queued"
    result = h.runtime.controller.step("binding-one")
    assert result.state == "succeeded" and result.result_forwarded
    assert len(h.executor.starts) == 1
    pull = h.post("/delivery/pull", wire.Command(request_id="pull-one"))
    assert pull.status_code == 200, pull.text
    binding = wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(pull.headers["x-behavior-binding"]))
    assert hashlib.sha256(pull.content).hexdigest() == binding.payload_digest
    acknowledgment = wire.PullAcknowledgment(
        request_id="ack-one", exchange_id=pull.headers["x-behavior-exchange"],
        acknowledgment_token=pull.headers["x-behavior-ack-token"], binding=binding,
    )
    ack = h.post("/delivery/acknowledge", acknowledgment)
    assert ack.status_code == 200, ack.text
    assert ack.json()["result"]["fulfillment"] == "fulfilled"
    assert ack.json()["result"]["delivery_state"] == "delivered"
    assert ack.json()["result"]["required_confirmation_level"] == "client_received"
    assert ack.json()["result"]["confirmed_levels"] == ["client_received"]
    assert ack.json()["result"]["channel_version"] == binding.channel_version
    assert ack.json()["result"]["fulfillment_receipt_id"]
    assert h.post("/delivery/acknowledge", acknowledgment).json() == ack.json()
    assert h.post("/work/submit", wire.SubmitWork(request_id="submit-one")).json()["obligation_fulfilled"]
    assert len(h.executor.starts) == 1


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_delivery_response_projects_state_without_rewriting_original_receipt(state):
    from shared_context.behavior_api import _response
    from test_behavior_delivery import Harness, CONTEXT
    from shared_context.behavior_delivery import DeliveryConsumer

    h = Harness()
    ob, _, _ = h.admit()
    h.work_result(ob, state)
    h.message(ob, "final" if state == "succeeded" else "failure")
    receipt = h.receipt(ob)
    original = json.dumps(receipt.payload, sort_keys=True)
    writes = h.submissions()
    response = json.loads(_response(receipt).body)["result"]
    assert response["delivery_state"] == "delivered"
    assert response["fulfillment"] == "fulfilled"
    assert response["required_confirmation_level"] in response["confirmed_levels"]
    assert response["fulfillment_receipt_id"] == "receipt-one"
    assert response["channel_version"] == "channel-v1"
    assert json.dumps(receipt.payload, sort_keys=True) == original
    assert h.data("delivery-obligation", ob)["delivery_state"] == "fulfilled"
    assert DeliveryConsumer(h.core).consume(ob, CONTEXT, "offline-consumer")["status"] == "fulfilled"
    assert h.submissions() == writes


@pytest.mark.parametrize("field,value", [
    ("fulfillment", "pending"), ("fulfillment_receipt_id", None), ("channel_version", ""),
    ("required_confirmation_level", "human_read"), ("confirmed_levels", []),
    ("confirmed_levels", ["client_received", "client_received"]),
    ("confirmed_levels", ["client_received", "invented_level"]), ("evidence_digest", "not-a-digest"),
])
def test_delivered_response_rejects_incomplete_or_inconsistent_receipt(field, value):
    from shared_context.behavior_api import _response
    from shared_context.records import Record
    from test_behavior_delivery import final_ready

    h, ob = final_ready()
    receipt = h.receipt(ob)
    invalid = Record({**receipt.payload, "result": {**receipt.payload["result"], field: value}}, receipt.etag)
    with pytest.raises(Problem) as error:
        _response(invalid)
    assert error.value.body["error"]["code"] == "BEHAVIOR_DELIVERY_RECEIPT_INVALID"


@pytest.mark.parametrize("field", [
    "obligation_id", "fulfillment", "fulfillment_receipt_id", "channel_version",
    "required_confirmation_level", "confirmed_levels", "evidence_digest",
])
def test_delivered_response_requires_each_confirmation_field(field):
    from shared_context.behavior_api import _response
    from shared_context.records import Record
    from test_behavior_delivery import final_ready

    h, ob = final_ready()
    receipt = h.receipt(ob)
    result = {name: value for name, value in receipt.payload["result"].items() if name != field}
    with pytest.raises(Problem) as error:
        _response(Record({**receipt.payload, "result": result}, receipt.etag))
    assert error.value.body["error"]["code"] == "BEHAVIOR_DELIVERY_RECEIPT_INVALID"


@pytest.mark.parametrize("outcome", ["suppressed", "unknown", "failed"])
def test_unconfirmed_delivery_response_never_claims_delivered(outcome):
    from shared_context.behavior_api import _response
    from test_behavior_delivery import final_ready

    h, ob = final_ready()
    receipt = h.receipt(ob, outcome=outcome)
    result = json.loads(_response(receipt).body)["result"]
    assert result == receipt.payload["result"]
    assert result["fulfillment"] == "pending" and result["delivery_state"] != "delivered"


def test_sources_snapshot_review_claim_current_and_exact_context_use_real_modules(h):
    ref = h.source()
    read = h.client.get(BASE + "/sources/metrics/versions/1", headers=h.headers())
    assert read.status_code == 200 and read.content == METRICS
    capture = h.post("/snapshots/snapshot-one/capture", wire.SnapshotCapture(request_id="snapshot", roots=(ref,)))
    assert capture.status_code == 200, capture.text
    restored = h.post("/snapshots/snapshot-one/verify", wire.Command(request_id="snapshot"))
    assert restored.status_code == 200, restored.text
    assert METRICS in [base64.b64decode(value) for value in restored.json()["originals"].values()]
    draft = wire.ClaimDraft(
        request_id="review", version=1, text="The accepted metrics source has been reviewed.",
        supporting=(ref,), effective_from=NOW,
    )
    preview = h.post("/claims/claim-one/preview", draft)
    assert preview.status_code == 200, preview.text
    body = wire.ClaimReview(
        **draft.model_dump(), decision_id="decision",
        candidate_sha256=preview.json()["candidate_sha256"], evidence_sha256=preview.json()["evidence_sha256"],
        policy_version=preview.json()["policy_version"],
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
    )
    reviewed = h.post("/claims/claim-one/review", body)
    assert reviewed.status_code == 200, reviewed.text
    committed = h.post("/claims/claim-one/commit", wire.Command(request_id="review"))
    assert committed.status_code == 200, committed.text
    current = h.client.get(BASE + "/claims/claim-one", headers=h.headers())
    assert current.status_code == 200, current.text
    assert current.json()["review"]["reviewer_kind"] == "human"
    assert current.json()["review"]["reviewer_id"] == HUMAN_OID
    assembled = h.post("/context", wire.ContextRequest(request_id="context", required_claim_ids=("claim-one",)))
    assert assembled.status_code == 200, assembled.text
    assert METRICS in [base64.b64decode(value) for value in assembled.json()["originals"].values()]
    withdrawal = wire.SourceWithdrawal(request_id="withdraw", withdrawal_id="withdraw-one", reference=ref, expected_access_revision=1)
    assert h.post("/sources/metrics/withdraw", withdrawal).status_code == 200
    assert h.client.get(BASE + "/sources/metrics/versions/1", headers=h.headers()).status_code == 403
    assert h.client.get(BASE + "/claims/claim-one", headers=h.headers()).status_code == 403
    assert h.post("/context", wire.ContextRequest(request_id="context-two", required_claim_ids=("claim-one",))).status_code == 403
    assert h.post("/snapshots/snapshot-one/verify", wire.Command(request_id="snapshot")).status_code == 403


def test_exact_human_approval_does_not_allow_rewriting_an_enqueued_work(h):
    h.submit()
    grant_key = control_key("grant", "grant")
    assert h.store.read_snapshot((grant_key,))[grant_key] is None
    inspected = h.adapter.inspect("work-one", CTX)
    h.references["goal"] = WorkGoalUpdate(
        work_id="work-one", obligation_id=h.binding.obligation_id, expected_etag=inspected.etag,
        expected_definition_digest=inspected.definition_digest, goal=GOAL_REVIEW,
    )
    prepared = h.post("/operation/prepare", wire.OperationPrepare(request_id="prepare", input_ref="goal"), "agent")
    assert prepared.status_code == 200, prepared.text
    result = prepared.json()["result"]
    preview = h.client.get(BASE + "/operation", headers=h.headers())
    assert preview.status_code == 200
    assert preview.json()["content"] == result["content"]
    assert preview.json()["execution_window_contract"] == "fa-table-work-effect-1"
    assert preview.json()["strict_service_commit_time_expiry_supported"] is False
    approval = wire.OperationApproval(
        request_id="approval", decision_id="decision", description_digest=result["description_digest"],
        adapter_contract_digest=result["adapter_contract_digest"], expires_at=NOW + timedelta(minutes=2),
    )
    assert h.post("/operation/approve", approval, "agent").status_code == 403
    assert h.post("/operation/approve", approval, "read-only-human").status_code == 403
    granted = h.post("/operation/approve", approval)
    assert granted.status_code == 200, granted.text
    assert h.post("/operation/approve", approval).json() == granted.json()
    executed = h.post("/operation/execute", wire.Command(request_id="execute"), "agent")
    assert executed.status_code == 409, executed.text
    assert executed.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert executed.json()["error"]["reconcile_original"] is False
    assert h.post("/operation/execute", wire.Command(request_id="execute"), "agent").json() == executed.json()
    assert h.adapter.inspect("work-one", CTX) == inspected
    assert h.store.read_snapshot((grant_key,))[grant_key].payload["data"]["remaining_uses"] == 1
    assert h.runtime.controller.step("binding-one").state == "succeeded"


@pytest.mark.parametrize("identity,path", [
    ("old-m3", "/work/submit"), ("human", "/jobs/job-one/consume"),
    ("agent", "/delivery/acknowledge"), ("read-only-human", "/operation/approve"),
])
def test_route_grants_are_not_inferred_from_json_or_old_m3_consent(h, identity, path):
    count = len(h.transport.requests)
    result = h.post(path, {"request_id": "forged", "approved": True, "actor": "human", "context": CTX.model_dump()}, identity)
    assert result.status_code == (404 if path.endswith("/consume") else 403)
    assert "Bearer" not in result.text
    assert len(h.executor.starts) == 0


@pytest.mark.parametrize("raw", [
    b'{"request_id":"one","request_id":"two"}',
    b'{"request_id":"one","approved":true}',
    b'{"request_id":"one","context":{"principal_id":"SYNTHETIC_SECRET"}}',
    b'{"request_id":"one","root_budget_id":"other"}',
    b'{"request_id":"one","clock":"2099-01-01"}',
])
def test_duplicate_unknown_or_authority_fields_rejected_without_echoing_raw_input(h, raw):
    response = h.client.post(BASE + "/work/submit", content=raw, headers=h.headers())
    assert response.status_code == 422
    assert "SYNTHETIC_SECRET" not in response.text


def test_authentication_precedes_stream_reads_even_for_oversized_body(h):
    received, output = [], []

    async def run():
        async def receive():
            received.append(True)
            return {"type": "http.request", "body": b"x" * 100000, "more_body": False}

        async def send(message):
            output.append(message)

        await h.app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "https", "path": BASE + "/work/submit",
            "raw_path": (BASE + "/work/submit").encode(), "query_string": b"",
            "headers": [(b"content-type", b"application/json"), (b"content-length", b"100000")],
            "client": ("local", 1), "server": ("local", 443), "root_path": "",
        }, receive, send)

    anyio.run(run)
    assert received == [] and output[0]["status"] == 401


def test_current_policy_revocation_blocks_normal_and_replayed_access(h):
    h.source()
    h.mutate(control_key("policy", "policy"), active=False)
    assert h.client.get(BASE + "/sources/metrics/versions/1", headers=h.headers()).status_code == 403
    assert h.post("/sources/metrics/commit", wire.Command(request_id="source-one")).status_code == 403
    assert h.post("/work/submit", wire.SubmitWork(request_id="new")).status_code == 403


def test_unknown_source_commit_reconciles_original_without_resubmission(h):
    command = wire.SourcePrepare(request_id="uncertain", version=1, original=METRICS)
    assert h.post("/sources/metrics/prepare", command).status_code == 200
    # Force the consume marker's commit response to be lost: the source must NOT commit.
    h.transport.after_apply = "lost"
    response = h.post("/sources/metrics/commit", wire.Command(request_id="uncertain"))
    assert response.status_code == 503 and response.json()["error"]["reconcile_original"]
    h.transport.after_apply = None
    count = len(h.transport.parts)
    repeat = h.post("/sources/metrics/commit", wire.Command(request_id="uncertain"))
    assert repeat.status_code == 503
    reconciled = h.post("/sources/metrics/reconcile", wire.Command(request_id="uncertain"))
    assert reconciled.status_code == 503 and len(h.transport.parts) == count


def test_applied_source_response_loss_reads_original_receipt_and_exact_bytes(h):
    command = wire.SourcePrepare(request_id="applied", version=1, original=METRICS)
    prepared = h.post("/sources/metrics/prepare", command)
    assert prepared.status_code == 200
    request_id = prepared.json()["control_request_id"]
    submit = h.store._submit

    def lose_source(operations, request):
        if request is not None and request.request_id == request_id:
            h.transport.after_apply = "lost"
        try:
            return submit(operations, request)
        finally:
            h.transport.after_apply = None

    h.store._submit = lose_source
    failed = h.post("/sources/metrics/commit", wire.Command(request_id="applied"))
    assert failed.status_code == 503 and failed.json()["error"]["reconcile_original"]
    count = len(h.transport.parts)
    result = h.post("/sources/metrics/commit", wire.Command(request_id="applied"))
    assert result.status_code == 200 and len(h.transport.parts) == count
    assert h.client.get(BASE + "/sources/metrics/versions/1", headers=h.headers()).content == METRICS


def test_admission_plan_and_authenticated_evidence_are_atomic_across_lost_ack(h):
    h.transport.after_apply = "lost"
    result = h.post("/work/submit", wire.SubmitWork(request_id="original"))
    assert result.status_code == 503
    h.transport.after_apply = None
    # The captured admission/evidence time must survive a restart/retry.
    h.time += timedelta(seconds=1)
    result = h.post("/work/submit", wire.SubmitWork(request_id="original"))
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "queued" and len(h.executor.starts) == 0


@pytest.mark.parametrize("original", [
    b'{"schema_version":"SECRET_MARKER","schema_version":"fa-artifact-metrics-v1","item_count":1,"completed_count":0}',
    b'{"schema_version":"fa-artifact-metrics-v1","item_count":1,"completed_count":0,"token":"SECRET_MARKER"}',
    b"Bearer SECRET_MARKER",
])
def test_unsafe_original_never_enters_plan_checkpoint_or_normal_output(h, original):
    result = h.post("/sources/metrics/prepare", wire.SourcePrepare(request_id="unsafe", version=1, original=original))
    assert result.status_code == 422 and "SECRET_MARKER" not in result.text
    for entity in h.transport.backing.rows.values():
        assert b"SECRET_MARKER" not in entity["Payload"]


def test_authenticated_body_bounds_duplicate_headers_and_wrong_binding_are_explicit(h):
    response = h.client.post(BASE + "/work/submit", content=b"x" * (24 * 1024 + 1), headers=h.headers())
    assert response.status_code == 413
    headers = list(h.headers().items())
    headers.append(("Authorization", h.headers()["Authorization"]))
    assert h.client.post(BASE + "/work/submit", content="{}", headers=headers).status_code == 401
    assert h.client.post(BASE.replace("binding-one", "other") + "/work/submit",
                         content=wire.SubmitWork(request_id="x").model_dump_json(), headers=h.headers()).status_code == 403
    assert h.client.get(BASE + "/jobs/other-job", headers=h.headers()).status_code == 403
    assert h.client.post(BASE + "/work/submit?approved=true", content="{}", headers=h.headers()).status_code == 422


def test_human_can_revoke_only_exact_current_grant_without_releasing_reservations(h):
    h.submit()
    version = h.adapter.inspect("work-one", CTX)
    h.references["goal"] = WorkGoalUpdate(work_id="work-one", obligation_id=h.binding.obligation_id,
                                         expected_etag=version.etag, expected_definition_digest=version.definition_digest,
                                         goal=GOAL_REVIEW)
    prepared = h.post("/operation/prepare", wire.OperationPrepare(request_id="p", input_ref="goal"), "agent").json()["result"]
    approval = wire.OperationApproval(request_id="a", decision_id="a", description_digest=prepared["description_digest"],
                                      adapter_contract_digest=prepared["adapter_contract_digest"], expires_at=NOW + timedelta(minutes=1))
    assert h.post("/operation/approve", approval).status_code == 200
    revoke = wire.OperationRevocation(request_id="r", decision_id="r", description_digest=approval.description_digest,
                                     expected_revocation_revision=0)
    assert h.post("/operation/revoke", revoke, "agent").status_code == 403
    accepted = h.post("/operation/revoke", revoke)
    assert accepted.status_code == 200
    assert h.post("/operation/revoke", revoke).json() == accepted.json()
    assert h.post("/operation/execute", wire.Command(request_id="x"), "agent").status_code == 403
    assert h.adapter.inspect("work-one", CTX).definition_digest == version.definition_digest


def test_metrics_http_adapter_is_usable_and_replays_only_completed_evidence(h):
    import httpx
    from shared_context.behavior_dispatch import ArtifactReadSpec, ArtifactRoute, ContextArtifactAdapter, ContextArtifactSettings
    from test_behavior_dispatch import Chunks

    route = ArtifactRoute(team=A.team, artifact_id="metrics", version_id="1" * 32)
    spec = ArtifactReadSpec(**route.model_dump(), if_match='"0xABC"', sha256=hashlib.sha256(METRICS).hexdigest(), bytes=len(METRICS))
    calls = []

    def read(request):
        calls.append(request)
        assert request.method == "GET" and request.headers["if-match"] == spec.if_match
        return httpx.Response(200, headers={
            "content-type": "application/octet-stream", "etag": spec.if_match, "x-artifact-sha256": spec.sha256,
        }, stream=Chunks([METRICS]))

    adapter = ContextArtifactAdapter(
        ContextArtifactSettings(api_origin="https://context.example", principal_id=A.oid, routes=(route,)),
        references={"goal": spec}, token=lambda: "SYNTHETIC_CREDENTIAL",
        clock=lambda: h.time, transport=httpx.MockTransport(read),
    )
    broker = ToolBrokerCore(h.store, clock=lambda: h.time, adapter=adapter.registration, execution_adapter=adapter)
    h.runtime = BehaviorRuntime(replace(h.dep, broker=broker), (h.binding,), clock=lambda: h.time, mode="offline_contract")
    h.client = TestClient(create_behavior_app(h.runtime, h.verifier))
    prepared = h.post("/operation/prepare", wire.OperationPrepare(request_id="p", input_ref="goal"), "agent").json()["result"]
    approval = wire.OperationApproval(
        request_id="a", decision_id="a", description_digest=prepared["description_digest"],
        adapter_contract_digest=prepared["adapter_contract_digest"], expires_at=NOW + timedelta(minutes=1),
    )
    assert h.post("/operation/approve", approval).status_code == 200
    command = wire.Command(request_id="execute")
    first = h.post("/operation/execute", command, "agent")
    assert first.status_code == 200 and first.json()["result"]["safe_result"]["data"]["item_count"] == 3
    assert h.post("/operation/execute", command, "agent").json() == first.json()
    assert h.post("/operation/reconcile", command, "agent").json() == first.json()
    assert len(calls) == 1


def test_worker_start_response_loss_reconciles_same_job_without_second_start(h):
    h.submit()
    h.executor.fail_after_apply = True
    with pytest.raises(Problem):
        h.runtime.controller.step("binding-one")
    h.executor.fail_after_apply = False
    resumed = h.runtime.controller.step("binding-one")
    assert resumed.state == "succeeded" and len(h.executor.starts) == 1


def test_missing_native_prerequisites_cannot_be_laundered_from_offline_fixtures(h):
    with pytest.raises(Problem):
        BehaviorRuntime(h.dep, (h.binding,), clock=lambda: NOW, mode="native")
    closed = replace(h.dep, claim_reviewer=None)
    runtime = BehaviorRuntime(closed, (h.binding,), clock=lambda: NOW, mode="offline_contract")
    client = TestClient(create_behavior_app(runtime, h.verifier))
    result = client.post(BASE + "/claims/claim-one/preview", headers=h.headers(),
                        content=wire.ClaimDraft(request_id="x", version=1, text="The accepted metrics source has been reviewed.",
                        supporting=(wire.ExactSourceRef(source_id="metrics", version=1, sha256="a" * 64, byte_length=1),),
                        effective_from=NOW).model_dump_json())
    assert result.status_code == 503
