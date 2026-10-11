"""OFFLINE SDK protocol/M3/WorkerCore/Teams vertical slice; no SDK executable.

The executor drives the real AssistantAuthority tools instead of fabricating a
work result. Only remote SDK/model execution and Connector HTTP are substitutes.
"""
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import A, AUDIENCE, FakeMemory, InMemoryRepository
from shared_context.auth import AuthSettings
from shared_context import behavior_assistant_wire as aw, behavior_wire as wire
from shared_context.behavior_assistant import AssistantAuthority, AssistantProfile, AssistantRuntime, EXECUTOR_VERSION, PROTECTION_VERSION
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_channels_migration import ChannelApproval, ChannelInstallation, channel_key
from shared_context.behavior_delivery import _material
from shared_context.behavior_operations import control_key
from shared_context.behavior_server_config import ServerConfiguration, ServerConfigurationV2, synthetic_configuration
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import ExecutorAdapter, ExecutorEvidence
from shared_context.errors import Problem
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_auth import ApprovalSettings
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.models import CreateWork
from shared_context.service import ContextService
from shared_context.teams_delivery import TeamsRouteAccess
from shared_context.teams_identity import digest
from shared_context.teams_metrics import TeamsMetrics
from shared_context.teams_personal import ROUTE_HANDSHAKE
from test_teams_deployment import config as personal_config
from test_teams_identity import TENANT, HUMAN, ROUTE, activity, identity, token as connector_token
from test_m3_auth import CLIENT_ID, jwk_client, signing_key
import test_behavior_api as api_fixture
import test_m3_auth as auth_fixture


def build_config(h, profile, personal):
    raw = synthetic_configuration().model_dump(mode="json")
    raw["identity"].update(tenant_id=TENANT, human_oid=HUMAN, admin_client_id=CLIENT_ID,
                           managed_identity_client_id=personal.uami_client_id)
    raw["table"]["scope"] = h.store._scope.model_dump(mode="json")
    for lane, prefix in (("sources", "m3/v1/"), ("backups", "v1/")):
        raw[lane]["protection"].update(authority_domain=h.store._scope.authority_domain,
                                       protected_prefix=prefix + TENANT + "/")
    raw.update(delivery=h.config.model_dump(mode="json"), workers=h.workers.contract.model_dump(mode="json"),
               bindings=[h.binding.model_dump(mode="json")], controller_id="internal-controller",
               workload_version="assistant-v1.2",
               policies=[{"binding_id": h.binding.binding_id, "policy": {
                   **h.policy.model_dump(mode="json"), "binding_digest": digest([
                       "fa-behavior-api-v1", "native", h.binding.model_dump(mode="json"),
                   ]),
               }}])
    raw["operations"] = [{"input_ref": "goal", "binding_id": h.binding.binding_id,
                          "goal": "Review the prepared result against the pinned work requirements."}]
    raw["roots"] = [{"root_budget_id": "root", "root_request_id": "root-request", "attempts": 20, "units": 40,
                     "control_units": 1000, "scans": 64, "pending_limit": 32}]
    raw["targets"][0]["executor_id"] = h.workers.contract.executor_id
    raw["assistant"] = {
        "profiles": [profile.model_dump(mode="json")],
        "provider_endpoint": raw["targets"][0]["project_endpoint"] + "/openai/v1/chat/completions",
        "m3_context": {"account_resource_id": raw["table"]["account_resource_id"],
                       "endpoint": raw["table"]["endpoint"], "table_name": raw["table"]["scope"]["table_name"]},
        "artifact_container_url": raw["sources"]["protection"]["container_url"],
    }
    old = ServerConfiguration.model_validate_json(json.dumps(raw))
    raw["schema_version"] = "fa-behavior-server-v2"
    raw["channels"] = {
        "personal": personal.model_dump(mode="json"), "route_id": ROUTE.route_id,
        "template_binding_id": h.binding.binding_id, "max_requests": 3, "max_model_requests": 3,
        "native_max_generations": 1, "valid_until": (h.time + timedelta(minutes=30)).isoformat(),
        "native_evidence_id": "offline-only", "native_evidence_path": "unused-offline-evidence.json",
        "artifact_origin": "https://artifacts.example.org",
    }
    return old, ServerConfigurationV2.model_validate_json(json.dumps(raw))


def seed(h, key, data, fixed=False):
    h.serial += 1
    h.store.transact(ControlRequest(request_id=f"teams-v2-seed-{h.serial}", request_digest=digest(data)),
                     lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="create", data=data,
                                                                   material=_material(data) if fixed else None),), result={}))


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
def metrics(monkeypatch, jwk_client, signing_key, request):
    for module in (api_fixture, auth_fixture):
        monkeypatch.setattr(module, "TENANT", TENANT)
        monkeypatch.setattr(module, "HUMAN_OID", HUMAN)
    if isinstance(getattr(request, "param", None), tuple):
        count, completed = request.param
        monkeypatch.setattr(api_fixture, "METRICS", json.dumps({
            "schema_version": "fa-artifact-metrics-v1", "item_count": count, "completed_count": completed,
        }).encode())
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
    if getattr(request, "param", None) == "legacy-pull":
        principal = BehaviorPrincipal(TENANT, HUMAN, A.team, "human", None, frozenset({"read", "submit", "acknowledge"}))
        h.runtime.submit(h.binding.binding_id, aw.AssistantSubmit(request_id="original-pull"), principal)
        prior = h.runtime.controller.step(h.binding.binding_id)
        received = h.runtime.fetch(h.binding.binding_id, wire.Command(request_id="original-fetch"), principal)
        h.runtime.acknowledge(h.binding.binding_id, wire.PullAcknowledgment(
            request_id="original-ack", exchange_id=received.exchange_id,
            acknowledgment_token=received.acknowledgment_token,
            binding=wire.DeliveryBinding.model_validate_json(received.binding.model_dump_json()),
        ), principal)
        h.original_receipt_id = control_key("inbox-ack-evidence", prior.delivery_id)
        h.original_receipt = h.inbox.verify_receipt(h.original_receipt_id)
    personal = personal_config(team_id=A.team)
    h.old, h.v2 = build_config(h, profile, personal)
    h.sent = []
    h.provider = lambda _: httpx.Response(200, json={"id": "offline-provider-result"})
    def send(request):
        h.sent.append(request)
        return h.provider(request)
    h.parent = SimpleNamespace(config=h.v2, runtime=h.runtime, require_current_native_evidence=lambda: None,
                               installation=lambda: None,
                               assistant=SimpleNamespace(artifacts=h.artifacts, authority=h.assistant))
    h.teams = TeamsMetrics(h.parent, jwks_transport=identity().jwks_transport,
                           send_transport=httpx.MockTransport(send), bot_token=lambda _: "offline-bot-token")
    h.connector = lambda: "Bearer " + connector_token(nbf=int(h.time.timestamp()) - 60, exp=int(h.time.timestamp()) + 300)
    h.teams.enrollment.capture(h.connector(), json.dumps(activity(text=ROUTE_HANDSHAKE)).encode())
    approval = ChannelApproval(decision_id="offline-human-rule", operator_oid=HUMAN, plan_sha256="a" * 64,
                               not_before=h.time, expires_at=h.time + timedelta(minutes=5))
    akey = channel_key("approval", approval.decision_id)
    pointer = ChannelInstallation(configuration_sha256=h.v2.sha256, predecessor_sha256=h.old.sha256,
                                  registry_sha256=h.teams.rule_sha256, history_id="offline-history",
                                  history_sha256="b" * 64, approval_id=akey)
    # Synthetic installation only; separate migration tests execute the actual EGT.
    seed(h, akey, approval.model_dump(mode="json"))
    seed(h, h.teams.current_key, pointer.model_dump(mode="json"))
    seed(h, control_key("teams-route-access", ROUTE.route_id), TeamsRouteAccess(
        route_id=ROUTE.route_id, recipient_id=HUMAN, active=True,
        owner_record_ids=(h.binding.owner.owner_record_id,), artifacts=(),
    ).model_dump(mode="json"))
    h.teams.start()
    h.app = FastAPI()
    from shared_context.behavior_api import _install_error_handlers
    _install_error_handlers(h.app)
    h.teams.install_routes(h.app, h.verifier)
    h.client = TestClient(h.app)
    return h


def incoming(h, **updates):
    return h.client.post("/behavior/v1/teams/activities", json=activity(**{"text": "FA metrics", **updates}),
                          headers={"Authorization": h.connector()})


def test_signed_metrics_uses_real_sdk_authority_result_m3_and_one_teams_sender(metrics):
    h = metrics
    before = h.store.read_snapshot((control_key("budget", "root"),))[control_key("budget", "root")].payload["data"]
    response = incoming(h)
    assert response.status_code == 200, response.text
    ob = response.json()["obligation_id"]
    record = h.teams.for_obligation(ob)
    assert record.binding.job_id != h.binding.job_id
    assert record.profile.source == h.runtime.profiles[h.binding.binding_id].source
    assert record.binding.owner.root_budget_id == "root"
    assert h.teams.channel.core.configuration_digest() == h.delivery.configuration_digest()
    try:
        result = h.runtime.controller.step(record.binding.binding_id)
    except Problem:
        if h.sdk.error is not None:
            raise h.sdk.error from None
        raise
    assert result.state == "succeeded" and result.obligation_fulfilled
    assert len(h.sdk.starts) == len(h.sent) == 1
    assert len(h.sdk.model_requests) == 1
    body = json.loads(h.sent[0].content)
    assert '"remaining_count":1' in body["text"]
    assert record.profile.artifact_id in body["text"]
    receipt = h.teams.channel.adapter.inspect(result.delivery_id)
    assert receipt["confirmation_level"] == "provider_accepted"
    assert incoming(h).status_code == 200
    assert len(h.sdk.starts) == len(h.sent) == 1
    after = h.store.read_snapshot((control_key("budget", "root"),))[control_key("budget", "root")].payload["data"]
    assert after["remaining_attempts"] < before["remaining_attempts"]
    version = h.artifacts.version(A, A.team, record.profile.artifact_id, record.profile.artifact_version)
    assert version.sha256 in body["text"]


def test_prior_unknown_sdk_keeps_global_c1_and_denies_new_activity(metrics):
    h = metrics
    response = incoming(h)
    record = h.teams.for_obligation(response.json()["obligation_id"])
    h.sdk.mode = "unknown"
    result = h.runtime.controller.step(record.binding.binding_id)
    assert result.state == "capacity_uncertain"
    rejected = incoming(h, id="second-activity")
    assert rejected.status_code == 409
    assert len(h.sdk.starts) == 1 and h.sent == []
    assert len(h.runtime.bindings) == 2


def test_confirmed_sdk_failure_delivers_failure_without_fabricated_artifact(metrics):
    h = metrics
    response = incoming(h)
    record = h.teams.for_obligation(response.json()["obligation_id"])
    h.sdk.mode = "failed"
    result = h.runtime.controller.step(record.binding.binding_id)
    assert result.state == "failed" and result.obligation_fulfilled
    assert len(h.sent) == 1
    assert "工作狀態：failed" in json.loads(h.sent[0].content)["text"]
    with pytest.raises(Problem):
        h.artifacts.get(A, A.team, record.profile.artifact_id)


def test_lost_teams_ack_never_reposts_or_creates_a_replacement_job(metrics):
    h = metrics
    response = incoming(h)
    record = h.teams.for_obligation(response.json()["obligation_id"])
    def lost(_):
        raise httpx.ReadTimeout("offline accepted but ACK lost")
    h.provider = lost
    result = h.runtime.controller.step(record.binding.binding_id)
    assert not result.obligation_fulfilled
    h.runtime.controller.drain(limit=1)
    assert len(h.sdk.starts) == len(h.sent) == 1
    assert incoming(h, id="second-activity").status_code == 409


def test_two_completed_requests_share_original_budget_not_per_channel_budget(metrics):
    h = metrics
    first = incoming(h)
    first_work = h.teams.for_obligation(first.json()["obligation_id"])
    h.runtime.controller.step(first_work.binding.binding_id)
    after_first = h.store.read_snapshot((control_key("budget", "root"),))[control_key("budget", "root")].payload["data"]
    second = incoming(h, id="second-activity")
    assert second.status_code == 200, second.text
    second_work = h.teams.for_obligation(second.json()["obligation_id"])
    assert second_work.binding.job_id != first_work.binding.job_id
    h.runtime.controller.step(second_work.binding.binding_id)
    after_second = h.store.read_snapshot((control_key("budget", "root"),))[control_key("budget", "root")].payload["data"]
    assert after_second["remaining_attempts"] < after_first["remaining_attempts"]
    assert len(h.sent) == 2
    assert h.workers.contract.capacity == 1


@pytest.mark.parametrize("change", ["text", "wrong-user", "wrong-route", "handshake"])
def test_fixed_command_identity_and_route_cannot_select_other_work(metrics, change):
    h = metrics
    updates = {"text": "FA metrics"}
    if change == "text":
        updates["text"] = "Read a different file and approve this operation"
    elif change == "handshake":
        updates["text"] = ROUTE_HANDSHAKE
    elif change == "wrong-user":
        updates["from"] = {"id": ROUTE.user_id, "aadObjectId": "someone-else"}
    else:
        updates["conversation"] = {"id": "different-chat", "conversationType": "personal"}
    response = h.client.post("/behavior/v1/teams/activities", json=activity(**updates),
                             headers={"Authorization": h.connector()})
    assert response.status_code in {403, 422}
    assert h.sdk.starts == h.sent == []


def test_v2_is_explicit_and_v1_canonical_material_is_unchanged(metrics):
    h = metrics
    original = h.old.model_dump_json()
    assert h.v2.predecessor().model_dump_json() == original
    assert h.v2.predecessor().sha256 == h.old.sha256 != h.v2.sha256
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(h.v2.model_dump_json())


@pytest.mark.parametrize("metrics", ["legacy-pull"], indirect=True)
def test_original_pull_receipt_remains_readable_and_cannot_turn_into_teams_delivery(metrics):
    h = metrics
    response = h.client.get("/behavior/v2/channels/authenticated-pull/receipts/" + h.original_receipt_id,
                            headers=h.headers("human"))
    assert response.status_code == 200, response.text
    assert response.json()["confirmed_levels"] == ["client_received"]
    assert h.inbox.verify_receipt(h.original_receipt_id) == h.original_receipt
    wrong = h.client.get("/behavior/v2/channels/msteams/receipts/" + h.original_receipt_id, headers=h.headers("human"))
    assert wrong.status_code >= 400
    current = incoming(h)
    assert current.status_code == 200
    record = h.teams.for_obligation(current.json()["obligation_id"])
    h.runtime.controller.step(record.binding.binding_id)
    assert h.inbox.verify_receipt(h.original_receipt_id) == h.original_receipt
    assert len(h.sent) == 1


def test_restart_reloads_original_activity_job_profile_and_does_not_restart_sdk(metrics):
    h = metrics
    response = incoming(h)
    record = h.teams.for_obligation(response.json()["obligation_id"])
    h.runtime.controller.step(record.binding.binding_id)
    old = h.teams
    profile = old.base_profiles[h.binding.binding_id]
    h.runtime = AssistantRuntime(h.dep, (h.binding,), profiles=(profile,), clock=lambda: h.time,
                                 mode="offline_contract", controller_id="internal-controller")
    h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
    h.parent.runtime, h.parent.assistant.authority = h.runtime, h.assistant
    h.teams = TeamsMetrics(h.parent, jwks_transport=identity().jwks_transport,
                           send_transport=old.send_transport, bot_token=old.bot_token)
    h.teams.start()
    app = FastAPI()
    from shared_context.behavior_api import _install_error_handlers
    _install_error_handlers(app)
    h.teams.install_routes(app, h.verifier)
    h.client = TestClient(app)
    assert incoming(h).json()["obligation_id"] == record.binding.obligation_id
    assert h.runtime.controller.drain(limit=1) == ()
    assert h.teams.for_obligation(record.binding.obligation_id) == record
    assert len(h.sdk.starts) == len(h.sent) == 1


def test_artifact_endpoint_requires_the_designated_human_and_current_route_acl(metrics):
    h = metrics
    record = h.teams.for_obligation(incoming(h).json()["obligation_id"])
    h.runtime.controller.step(record.binding.binding_id)
    path = f"/teams/{A.team}/artifacts/{record.profile.artifact_id}/versions/{record.profile.artifact_version}/content"
    assert h.client.get(path).status_code == 401
    response = h.client.get(path, headers=h.headers("human"))
    assert response.status_code == 200, response.text
    assert response.json()["remaining_count"] == 1
    key = control_key("teams-route-access", ROUTE.route_id)
    h.mutate(key, active=False)
    assert h.client.get(path, headers=h.headers("human")).status_code == 403
    assert len(h.sent) == 1


def test_shared_delivery_page_selects_teams_without_another_root_scan_budget(metrics):
    h = metrics
    record = h.teams.for_obligation(incoming(h).json()["obligation_id"])
    context = h.runtime.resolve_owner(record.binding.owner.owner_record_id)
    h.workers.start(record.binding.job_id, context)
    h.workers.forward_result(record.binding.job_id, context, h.teams.channel.core)
    from shared_context.behavior_delivery import DeliveryConsumer
    key = control_key("delivery-quota", "root", "root")
    before = h.store.read_snapshot((key,))[key].payload["data"]["scans_remaining"]
    page = DeliveryConsumer(h.delivery).drain("root", h.runtime.resolve_owner, "same-controller", limit=1,
                                             select_core=h.runtime.delivery_for_obligation)
    delivered = h.teams.deliver_page(page)
    assert delivered[0]["fulfillment"] == "fulfilled"
    after = h.store.read_snapshot((key,))[key].payload["data"]["scans_remaining"]
    assert after == before - 1
    assert len(h.sent) == 1


def test_verified_failure_keeps_consumed_model_liability_in_original_ledger(metrics):
    h = metrics
    record = h.teams.for_obligation(incoming(h).json()["obligation_id"])
    h.sdk.mode = "failed-after-model"
    result = h.runtime.controller.step(record.binding.binding_id)
    assert result.state == "failed" and result.obligation_fulfilled
    from shared_context.behavior_assistant import key, _Session
    from shared_context.behavior_delivery import _parse
    sid = key("session-id", h.sdk.starts[0].binding.start_request_id)
    session_key = key("session", sid)
    session = _parse(h.store.read_snapshot((session_key,))[session_key].payload["data"], _Session)
    assert len(session.model_intent_ids) == 1
    intent = h.store.read_snapshot(session.model_intent_ids)[session.model_intent_ids[0]]
    assert intent.payload["data"]["consumed"] is True
    assert session.model_intent_ids[0] in json.loads(h.sent[0].content)["text"]


def test_native_server_build_has_concrete_sdk_and_channels_without_network_or_bootstrap(metrics, monkeypatch):
    from shared_context import behavior_server as server
    from shared_context.behavior_foundry import FoundryExecutor
    from test_behavior_store import Credential, WireTransport
    monkeypatch.setattr(server, "ServerTokenizer", lambda _: SimpleNamespace(count=len))
    transport = WireTransport()
    composed = server.build_server(metrics.v2, credential=Credential(), table_transport=transport)
    try:
        assert composed.config == metrics.v2
        assert composed.teams is not None
        assert composed.teams.runtime.dep.workers is composed.runtime.dep.workers
        assert composed.runtime.dep.workers.contract.capacity == 1
        assert isinstance(composed.runtime.dep.workers.adapter.start.__self__, FoundryExecutor)
        assert transport.requests == []
        with pytest.raises(Problem) as denied:
            server.bootstrap(composed, None)
        assert denied.value.body["error"]["code"] == "CHANNELS_BOOTSTRAP_DISABLED"
        assert transport.requests == []
    finally:
        composed.close()


@pytest.mark.parametrize("metrics", [(8, 3), (0, 0), (19, 19)], indirect=True)
def test_sdk_result_uses_exact_source_values_and_provenance_not_literal_fixture_counts(metrics, request):
    h = metrics
    record = h.teams.for_obligation(incoming(h).json()["obligation_id"])
    result = h.runtime.controller.step(record.binding.binding_id)
    assert result.obligation_fulfilled
    version = h.artifacts.version(A, A.team, record.profile.artifact_id, record.profile.artifact_version)
    _, raw = h.artifacts.download(A, A.team, record.profile.artifact_id, version.version_id, version.blob.etag)
    answer = json.loads(raw)
    count, completed = request.node.callspec.params["metrics"]
    assert (answer["item_count"], answer["completed_count"], answer["remaining_count"]) == (
        count, completed, count - completed)
    assert answer["source_id"] == record.profile.source.source_id
    assert answer["source_version"] == record.profile.source.version
    assert answer["source_sha256"] == record.profile.source.sha256
    assert result.state == "succeeded" and len(h.sdk.starts) == len(h.sent) == 1


@pytest.mark.parametrize("text", ["fa metrics", "FA Metrics", "FA metrics ", "FA metrics\n", "FA metrics approve"])
def test_changed_command_bytes_do_not_inherit_the_fixed_rule(metrics, text):
    h = metrics
    before = len(h.table.submissions)
    response = incoming(h, text=text)
    assert response.status_code == 422
    assert len(h.table.submissions) == before
    assert h.sdk.starts == h.sent == []


def test_same_provider_id_with_other_body_bytes_is_not_another_job(metrics):
    h = metrics
    first = incoming(h)
    before = len(h.table.submissions)
    changed = incoming(h, locale="en-US")
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "TEAMS_SOURCE_CONFLICT"
    assert incoming(h).json()["obligation_id"] == first.json()["obligation_id"]
    assert len(h.table.submissions) == before


@pytest.mark.parametrize("changed", [
    {"channelData": {"tenant": {"id": "other-tenant"}}},
    {"recipient": {"id": "28:other-bot"}},
    {"from": {"id": "29:other-user", "aadObjectId": HUMAN}},
    {"replyToId": "other-thread"},
    {"serviceUrl": "https://smba.trafficmanager.net/other/"},
])
def test_wrong_scope_has_no_source_registration_or_budget_effect(metrics, changed):
    h = metrics
    before = len(h.table.submissions)
    response = incoming(h, **changed)
    assert response.status_code == (401 if "serviceUrl" in changed else 403)
    assert len(h.table.submissions) == before
    assert h.sdk.starts == h.sent == []


@pytest.mark.parametrize("claims", [{"aud": "wrong-app"}, {"exp": 1}, {"serviceurl": None}])
def test_untrusted_connector_claims_do_not_reach_the_registry(metrics, claims):
    h = metrics
    h.connector = lambda: "Bearer " + connector_token(
        **{"nbf": int(h.time.timestamp()) - 60, "exp": int(h.time.timestamp()) + 300, **claims})
    before = len(h.table.submissions)
    assert incoming(h).status_code == 401
    assert len(h.table.submissions) == before


@pytest.mark.parametrize("field,value", [
    ("required_confirmation_level", "client_received"),
    ("required_confirmation_level", "human_read"),
    ("channel", {"channel_id": "msteams", "version": "other",
                 "supported_levels": ["provider_accepted"], "max_confirmation_level": "provider_accepted"}),
    ("channel", {"channel_id": "msteams", "version": "fa-teams-connector-v1",
                 "supported_levels": ["provider_accepted", "ui_rendered"], "max_confirmation_level": "ui_rendered"}),
])
def test_unsupported_confirmation_contracts_fail_at_configuration(metrics, field, value):
    raw = metrics.v2.model_dump(mode="json")
    raw["channels"][field] = value
    with pytest.raises(ValueError):
        ServerConfigurationV2.model_validate_json(json.dumps(raw))


def test_revocation_between_registration_and_admission_is_in_the_original_egt(metrics, monkeypatch):
    h = metrics
    original = h.teams.channel.core.admit
    def revoke_then_admit(command, context):
        h.mutate(control_key("teams-route-access", ROUTE.route_id), active=False)
        return original(command, context)
    monkeypatch.setattr(h.teams.channel.core, "admit", revoke_then_admit)
    response = incoming(h)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CHANNELS_ADMISSION_DENIED"
    assert h.store.read_snapshot((control_key("delivery-quota", "root", "root"),))[
        control_key("delivery-quota", "root", "root")].payload["data"]["pending"] == 0
    assert h.sdk.starts == h.sent == []


def test_root_revocation_cannot_mint_another_owner_or_work(metrics):
    h = metrics
    h.mutate(control_key("budget", "root"), active=False)
    before = len(h.table.submissions)
    assert incoming(h).status_code == 403
    assert len(h.table.submissions) == before
    assert h.teams.records == {}


def test_rule_revocation_is_delegated_idempotent_and_never_reenables_old_scope(metrics):
    h = metrics
    record = h.teams.for_obligation(incoming(h).json()["obligation_id"])
    endpoint = "/behavior/v2/channels/teams/revoke"
    assert h.client.post(endpoint, headers=h.headers("reader")).status_code == 403
    assert h.client.post(endpoint, headers={"Authorization": h.connector()}).status_code == 401
    response = h.client.post(endpoint, headers=h.headers("human"))
    assert response.status_code == 200
    assert response.json()["result"] == {"revoked": True, "inflight_effects_recalled": False}
    before = len(h.table.submissions)
    assert h.client.post(endpoint, headers=h.headers("human")).json() == response.json()
    with pytest.raises(Problem):
        h.runtime.controller.step(record.binding.binding_id)
    assert incoming(h, id="new-after-revocation").status_code == 403
    assert len(h.table.submissions) == before
    assert h.sdk.starts == h.sent == []


def test_revoke_after_result_before_consume_blocks_the_actual_post(metrics):
    h = metrics
    record = h.teams.for_obligation(incoming(h).json()["obligation_id"])
    def revoked_token(_):
        response = h.client.post("/behavior/v2/channels/teams/revoke", headers=h.headers("human"))
        assert response.status_code == 200
        return "offline-bot-token"
    h.teams.channel.adapter.bot_token = revoked_token
    with pytest.raises(Problem) as denied:
        h.runtime.controller.step(record.binding.binding_id)
    assert denied.value.body["error"]["code"] == "CHANNELS_RULE_DENIED"
    assert len(h.sdk.starts) == 1 and h.sent == []
    assert h.runtime.controller.drain(limit=1)[0]["problem"] == "TEAMS_SEND_UNKNOWN"
    assert incoming(h, id="cannot-escape-unknown").status_code == 403


def test_saved_provider_receipt_can_reconcile_after_rule_revocation_without_reposting(metrics, monkeypatch):
    h = metrics
    record = h.teams.for_obligation(incoming(h).json()["obligation_id"])
    original = h.teams.channel.core.record_receipt
    def lose_ack(*_):
        raise Problem(503, "OFFLINE_ACK_LOSS", "saved provider receipt; control ACK absent")
    monkeypatch.setattr(h.teams.channel.core, "record_receipt", lose_ack)
    with pytest.raises(Problem):
        h.runtime.controller.step(record.binding.binding_id)
    assert len(h.sdk.starts) == len(h.sent) == 1
    assert h.client.post("/behavior/v2/channels/teams/revoke", headers=h.headers("human")).status_code == 200
    monkeypatch.setattr(h.teams.channel.core, "record_receipt", original)
    h.runtime.validate_startup()
    result = h.runtime.controller.drain(limit=1)
    assert result[0]["fulfillment"] == "fulfilled"
    assert len(h.sdk.starts) == len(h.sent) == 1


@pytest.mark.parametrize("failure", ["bad-answer", "withdrawn-source"])
def test_invalid_or_revoked_source_cannot_produce_an_artifact_or_success_delivery(metrics, failure):
    h = metrics
    record = h.teams.for_obligation(incoming(h).json()["obligation_id"])
    if failure == "bad-answer":
        h.sdk.answer_override = 999
    else:
        from shared_context.source_materials import SourceReference
        context = h.runtime._source_context(record.binding, h.teams.template_policy,
            BehaviorPrincipal(TENANT, A.oid, A.team, "agent", A.actor, frozenset({"read"})))
        ref = SourceReference(scope=context.scope(h.teams.template_policy.source_visibility),
                              **record.profile.source.model_dump())
        h.sdk.before_save = lambda: h.mutate(ref.head_key, access_state="withdrawn", access_revision=2)
    with pytest.raises(Problem) as unknown:
        h.runtime.controller.step(record.binding.binding_id)
    assert unknown.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
    assert h.workers.inspect(record.binding.job_id).state == "starting"
    assert incoming(h, id="cannot-escape-source-failure").status_code == 409
    assert isinstance(h.sdk.error, Problem)
    assert h.sdk.error.body["error"]["code"] in {"ASSISTANT_ANSWER_INVALID", "SOURCE_WITHDRAWN"}
    assert len(h.sdk.starts) == 1 and h.sent == []
    with pytest.raises(Problem):
        h.artifacts.get(A, A.team, record.profile.artifact_id)


@pytest.mark.parametrize("metrics", ["legacy-pull"], indirect=True)
def test_retired_pull_keeps_its_status_and_receipt_not_its_old_source_capability(metrics):
    h = metrics
    principal = BehaviorPrincipal(TENANT, HUMAN, A.team, "human", None, frozenset({"read"}))
    assert h.runtime.job_status(h.binding.binding_id, principal).obligation_fulfilled
    with pytest.raises(Problem) as denied:
        h.runtime.read_source(h.binding.binding_id, h.teams.template_profile.source.source_id, 1, principal)
    assert denied.value.body["error"]["code"] == "CHANNELS_RETIRED_BINDING"
    response = h.client.get("/behavior/v2/channels/authenticated-pull/receipts/" + h.original_receipt_id,
                            headers=h.headers("human"))
    assert response.status_code == 200
    assert response.json()["confirmed_levels"] == ["client_received"]


def test_expired_rule_rejects_new_work_without_rewriting_the_approval(metrics):
    h = metrics
    pointer = h.teams.pointer()
    approved = h.store.read_snapshot((pointer.approval_id,))[pointer.approval_id]
    h.time = h.v2.channels.valid_until
    before = len(h.table.submissions)
    assert incoming(h).status_code == 403
    assert h.store.read_snapshot((pointer.approval_id,))[pointer.approval_id] == approved
    assert len(h.table.submissions) == before
    assert h.sdk.starts == h.sent == []


def test_completed_activity_replay_after_new_work_returns_only_its_original_receipt(metrics):
    h = metrics
    first = incoming(h)
    first_work = h.teams.for_obligation(first.json()["obligation_id"])
    h.runtime.controller.step(first_work.binding.binding_id)
    second = incoming(h, id="next-activity")
    second_work = h.teams.for_obligation(second.json()["obligation_id"])
    before = len(h.table.submissions)
    assert incoming(h).json() == first.json()
    assert len(h.table.submissions) == before
    assert len(h.sdk.starts) == len(h.sent) == len(h.sdk.model_requests) == 1
    h.runtime.controller.step(second_work.binding.binding_id)
    assert len(h.sdk.starts) == len(h.sent) == len(h.sdk.model_requests) == 2


def test_new_ids_do_not_reset_the_approved_activity_limit(metrics):
    h = metrics
    for index in range(h.v2.channels.max_requests):
        response = incoming(h, id=f"bounded-activity-{index}")
        assert response.status_code == 200
        record = h.teams.for_obligation(response.json()["obligation_id"])
        assert h.runtime.controller.step(record.binding.binding_id).obligation_fulfilled
    before = len(h.table.submissions)
    rejected = incoming(h, id="new-id-cannot-refill")
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "TEAMS_REQUEST_LIMIT"
    assert len(h.table.submissions) == before
    assert len(h.sdk.starts) == len(h.sent) == len(h.sdk.model_requests) == h.v2.channels.max_requests


@pytest.mark.parametrize("occupied", ["lease", "native-target"])
def test_inconsistent_idle_pool_or_native_reservation_cannot_be_bypassed_by_new_activity(metrics, occupied):
    from shared_context.behavior_foundry import _TargetReservation, native_key
    from shared_context.behavior_workers import worker_key
    h = metrics
    if occupied == "lease":
        key = worker_key("pool", h.v2.workers.workspace_id)
        slots = h.store.read_snapshot((key,))[key].payload["data"]["slots"]
        slots[0]["lease_until"] = (h.time + timedelta(minutes=1)).isoformat()
        h.mutate(key, slots=slots)
    else:
        target = h.v2.active_targets[0]
        seed(h, native_key("target", target.executor_id), _TargetReservation(
            target=target, configuration_digest=h.workers.configuration_digest(),
            active_launch_id="original-still-unknown",
        ).model_dump(mode="json"))
    before = len(h.table.submissions)
    response = incoming(h)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TEAMS_PRIOR_RESPONSIBILITY_PENDING"
    assert len(h.table.submissions) == before
    assert h.teams.records == {} and h.sdk.starts == h.sent == []
