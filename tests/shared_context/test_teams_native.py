"""OFFLINE native-branch regressions: FakeTable, synthetic JWT/MI/HTTP only.

Monkeypatching the private network/credential constructors does NOT produce
real deployment evidence. No Azure, Connector, installed config or quota IO.
"""
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ServiceRequestError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared_context.behavior_delivery import ChannelContract, _material
from shared_context.behavior_operations import GrantRecord, GrantSubject, control_key
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from shared_context.teams_delivery import TeamsRouteAccess, TeamsSendContext
from shared_context.teams_identity import BOT_SCOPE, PublicCloudTransport, TEAMS_VERSION, digest
from shared_context.teams_native import (
    TeamsDeliveryCore, TeamsNativeEvidence, TeamsNativeRegistration, compose_installed_personal_channel,
)
from shared_context.teams_personal import (
    PersonalRouteEnrollment, ROUTE_HANDSHAKE, TeamsParentSeams, UamiBotToken, compose_personal_channel,
)
from shared_context.teams_runtime import TeamsAdmission
from shared_context.behavior_delivery import WorkDefinition
from test_behavior_delivery import CONTEXT, Harness
from test_teams_deployment import config
from test_teams_identity import NOW, ROUTE, activity, identity, token


def retain(h, key, value, *, material=True):
    h.serial += 1
    request = ControlRequest(request_id=f"native-seed-{h.serial}", request_digest=digest([key, value]))
    h.store.transact(request, lambda _: ControlChanges(writes=(ControlWrite(
        row_id=key, mode="create", data=value, material=_material(value) if material else None,
    ),), result={"offline_fixture": True}))


def native_runtime_fixture(monkeypatch, provider=None, *, publish=True):
    h = Harness(channel=ChannelContract(
        channel_id="msteams", version=TEAMS_VERSION,
        supported_levels=("provider_accepted",), max_confirmation_level="provider_accepted",
    ))
    h.time, h.guard_error, h.guard_reads = NOW, None, 0
    sent, acquired = [], []
    h.token_hook = lambda: None
    h.token_lifetime = 300
    def network(_, request):
        if request.url.host == "login.botframework.com":
            return identity().jwks_transport.handle_request(request)
        sent.append(request)
        return provider(request) if provider else httpx.Response(200, json={"id": "synthetic-native-provider-id"})
    class Credential:
        def __init__(self, **_):
            pass
        def get_token(self, scope):
            acquired.append(scope)
            h.token_hook()
            return AccessToken("SYNTHETIC-NATIVE-TOKEN", int(h.time.timestamp()) + h.token_lifetime)
        def close(self):
            pass
    monkeypatch.setattr(PublicCloudTransport, "handle_request", network)
    monkeypatch.setattr("shared_context.teams_personal.ManagedIdentityCredential", Credential)
    personal = config(team_id="team")
    enrollment = PersonalRouteEnrollment(h.store, personal, clock=lambda: h.time)
    enrollment.capture("Bearer " + token(), json.dumps(activity(text=ROUTE_HANDSHAKE)).encode())
    # This synthetic capture is labelled live ONLY to exercise native branches.
    # The complete tests remain OFFLINE; it is not an operator evidence artifact.
    installation_key = control_key("server-installation", "test-authority")
    installation = {"configuration_sha256": "a" * 64, "control_binding": h.config.control_binding}
    retain(h, installation_key, installation, material=False)
    witness = enrollment._witness()
    capture = {
        "route_row_id": enrollment.key,
        "route_material_sha256": _material(witness.model_dump(mode="json")).sha256,
        "personal_configuration_sha256": personal.configuration_digest,
        "control_binding": h.config.control_binding,
    }
    retain(h, control_key("teams-capture-route-binding", personal.configuration_digest), capture)
    def guard():
        h.guard_reads += 1
        if h.guard_error:
            raise Problem(403, "TEST_NATIVE_GUARD_DENIED", "synthetic installed guard denies")
    native = TeamsNativeRegistration(
        personal=personal, server_configuration_sha256="a" * 64,
        installation_row_id=installation_key, evidence_id="native-evidence-one",
        verify_current=lambda _: h.native_evidence, require_current=guard,
    )
    seams = TeamsParentSeams(
        resolve_admission=lambda incoming: TeamsAdmission(
            context=CONTEXT, root_request_id="root-request",
            work=WorkDefinition(work_id="work-" + incoming.event_id[:32], goal="Bounded verified answer",
                                criteria_version="answer-v1", result_kind="answer",
                                required_checks=("answer-checked",)),
        ),
        resolve_context=lambda _: CONTEXT,
        process_work=lambda ob, context: h.work_result(ob),
        resolve_send=lambda _: TeamsSendContext(context=CONTEXT, grant_id="teams-human-grant"),
    )
    channel = compose_personal_channel(
        enrollment, replace(h.registration, mode="native", verify_readiness=lambda: h.native_evidence.readiness),
        seams, native_registration=native,
    )
    h.core, h.registration = channel.core, channel.core.registration
    # Seed the empty fixture's quota/index as native BEFORE any work. Production
    # never rebinds these rows, and no test copies a delivery or receipt to native.
    for kind, name in (("team", "team"), ("entry", "entry"), ("root", "root")):
        h.mutate("delivery-quota", kind, name, configuration_digest=h.core.configuration_digest(), evidence_mode="native")
    h.mutate("delivery-index", "root", configuration_digest=h.core.configuration_digest(), evidence_mode="native")
    h.native_evidence = TeamsNativeEvidence(
        evidence_id=native.evidence_id, configuration_digest=h.core.configuration_digest(),
        server_configuration_sha256="a" * 64, control_binding=h.config.control_binding,
        personal_configuration_digest=personal.configuration_digest,
        settings_digest=digest(channel.adapter.settings.model_dump(mode="json")),
        installation_row_id=installation_key, installation_data_sha256=digest(installation),
        route_witness_sha256=capture["route_material_sha256"], capture_binding_sha256=digest(capture),
        bot_registration_sha256="b" * 64, personal_installation_sha256="c" * 64, package_sha256="d" * 64,
        callback_url=personal.endpoint, bot_app_id=personal.uami_client_id,
        bot_principal_id=personal.uami_principal_id, readiness=h.readiness().model_copy(update={
            "observed_at": NOW, "expires_at": NOW + timedelta(hours=2),
        }),
        observed_at=NOW, expires_at=NOW + timedelta(minutes=4),
    )
    if publish:
        channel.adapter.publish_installation()
        channel.runtime.enable_admission()
    app = FastAPI()
    app.include_router(channel.entry.router())
    return h, channel, TestClient(app), sent, acquired


def native_delivery_fixture(monkeypatch, provider=None):
    h, channel, client, sent, acquired = native_runtime_fixture(monkeypatch, provider)
    response = client.post("/behavior/v1/teams/activities", json=activity(),
                           headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 200, response.text
    ob = response.json()["obligation_id"]
    delivery_id = channel.runtime.prepare(ob)["binding"]["delivery_id"]
    adapter = channel.adapter
    h.mutate("owner", CONTEXT.owner_record_id, operation_ids=[adapter.operation_id(delivery_id)])
    h.put([(control_key("teams-route-access", ROUTE.route_id), TeamsRouteAccess(
        route_id=ROUTE.route_id, recipient_id=ROUTE.aad_object_id, active=True,
        owner_record_ids=(CONTEXT.owner_record_id,), artifacts=(),
    ))])
    plan = adapter.prepare(delivery_id)
    h.put([(control_key("grant", "teams-human-grant"), GrantRecord(
        grant_id="teams-human-grant", approval_source="human", approval_id="delegated-approval-one",
        subjects=(GrantSubject(principal_id=CONTEXT.principal_id, conversation_id=CONTEXT.conversation_id),),
        root_budget_id=CONTEXT.root_budget_id, description_digest=plan.description_digest,
        adapter_contract_digest=adapter.contract_digest, policy_id="policy", policy_revision=1,
        revocation_revision=0, revoked=False, not_before=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10), remaining_uses=1, remaining_units=1,
    ))])
    return h, channel, client, ob, delivery_id, sent, acquired


def test_native_default_closed_and_missing_installed_evidence_deny_admission(monkeypatch):
    h, channel, client, sent, acquired = native_runtime_fixture(monkeypatch, publish=False)
    assert channel.runtime.admission_enabled is False
    with pytest.raises(Problem):
        channel.runtime.enable_admission()
    response = client.post("/behavior/v1/teams/activities", json=activity(),
                           headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 403
    assert sent == acquired == []
    assert h.data("delivery-quota", "root", "root")["pending"] == 0


def test_native_installation_does_not_enable_intake_or_reset_any_budget(monkeypatch):
    h, channel, _, sent, acquired = native_runtime_fixture(monkeypatch, publish=False)
    budget, quota = h.data("budget", "root"), h.data("delivery-quota", "root", "root")
    channel.adapter.publish_installation()
    assert channel.runtime.admission_enabled is False
    assert h.data("budget", "root") == budget
    assert h.data("delivery-quota", "root", "root") == quota
    assert sent == acquired == []


def test_native_sender_retains_real_branch_provenance_without_promoting_confirmation(monkeypatch):
    h, channel, _, ob, delivery, sent, acquired = native_delivery_fixture(monkeypatch)
    result = channel.runtime.consume(ob)
    assert result["fulfillment"] == "fulfilled"
    assert result["confirmed_levels"] == ["provider_accepted"]
    receipt = channel.adapter.inspect(delivery)
    assert receipt["evidence_mode"] == "connector_http"
    assert receipt["native_evidence_id"] == h.native_evidence.evidence_id
    assert receipt["native_evidence_digest"] == digest(h.native_evidence.model_dump(mode="json"))
    assert receipt["actual_environment"] == "NOT_RUN"
    assert len(sent) == 1 and acquired == [BOT_SCOPE]
    assert h.guard_reads > 5
    assert "SYNTHETIC-NATIVE-TOKEN" not in json.dumps(receipt)


@pytest.mark.parametrize("change", ["expired", "not_yet_valid", "revoke", "observer", "installed_config", "native_guard"])
def test_current_native_gates_deny_prepare_and_new_admission(monkeypatch, change):
    h, channel, client, ob, delivery, sent, acquired = native_delivery_fixture(monkeypatch)
    if change == "expired":
        h.time = h.native_evidence.expires_at
    elif change == "not_yet_valid":
        h.time = NOW - timedelta(seconds=1)
    elif change == "revoke":
        h.mutate("teams-native-current", channel.adapter.native_registration.personal.configuration_digest, active=False)
    elif change == "observer":
        h.native_evidence = h.native_evidence.model_copy(update={"package_sha256": "f" * 64})
    elif change == "installed_config":
        h.mutate("server-installation", "test-authority", configuration_sha256="f" * 64)
    else:
        h.guard_error = True
    with pytest.raises(Problem):
        channel.adapter.prepare(delivery)
    with pytest.raises(Problem):
        channel.runtime.prepare(ob)
    response = client.post("/behavior/v1/teams/activities", json=activity(id="second"),
                           headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 403
    with pytest.raises(Problem):
        channel.adapter.send(delivery)
    assert sent == acquired == []


@pytest.mark.parametrize("field", [
    "configuration_digest", "server_configuration_sha256", "control_binding", "settings_digest",
    "personal_configuration_digest", "installation_data_sha256", "capture_binding_sha256",
    "route_witness_sha256", "bot_app_id", "bot_principal_id", "callback_url",
])
def test_native_publication_rejects_mismatched_identity_route_and_installation(monkeypatch, field):
    h, channel, _, sent, acquired = native_runtime_fixture(monkeypatch, publish=False)
    value = "https://different.example/activities" if field == "callback_url" else "f" * 64
    h.native_evidence = h.native_evidence.model_copy(update={field: value})
    with pytest.raises(Problem):
        channel.adapter.publish_installation()
    assert sent == acquired == []


def test_native_never_accepts_injected_transport_or_credential(monkeypatch):
    h, channel, _, _, _, sent, _ = native_delivery_fixture(monkeypatch)
    channel.adapter.transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"id": "fake"}))
    with pytest.raises(Problem):
        channel.adapter.check_structure(channel.core)
    channel.adapter.transport = None
    personal = channel.adapter.native_registration.personal
    channel.adapter.bot_token = UamiBotToken(personal, clock=lambda: h.time, credential=object())
    with pytest.raises(Problem):
        channel.adapter.check_structure(channel.core)
    assert sent == []


def test_cold_native_token_runs_before_two_second_gate(monkeypatch):
    h, channel, _, _, delivery, sent, acquired = native_delivery_fixture(monkeypatch)
    h.token_hook = lambda: setattr(h, "time", h.time + timedelta(seconds=5))
    assert channel.adapter.send(delivery).payload["result"]["fulfillment"] == "fulfilled"
    send = channel.adapter._send_row(delivery)
    assert send.gated_at == NOW + timedelta(seconds=5)
    assert send.not_after - send.gated_at == timedelta(seconds=2)
    assert len(sent) == 1 and acquired == [BOT_SCOPE]


@pytest.mark.parametrize("change", ["grant", "route", "owner", "policy", "installation"])
def test_cold_token_does_not_hide_current_revocation(monkeypatch, change):
    h, channel, _, _, delivery, sent, acquired = native_delivery_fixture(monkeypatch)
    def revoke():
        if change == "grant":
            h.mutate("grant", "teams-human-grant", revoked=True, revocation_revision=1)
        elif change == "route":
            h.mutate("teams-route-access", ROUTE.route_id, active=False)
        elif change == "owner":
            h.mutate("owner", CONTEXT.owner_record_id, epoch=2)
        elif change == "policy":
            h.mutate("policy", "policy", active=False)
        else:
            h.mutate("server-installation", "test-authority", configuration_sha256="f" * 64)
    h.token_hook = revoke
    with pytest.raises(Problem):
        channel.adapter.send(delivery)
    assert sent == [] and acquired == [BOT_SCOPE]


def test_native_token_expiring_between_consumption_and_wire_never_posts(monkeypatch):
    h, channel, _, _, delivery, sent, _ = native_delivery_fixture(monkeypatch)
    h.token_lifetime = 1
    original = channel.adapter._http
    def delayed(*args):
        h.time += timedelta(seconds=1)
        return original(*args)
    monkeypatch.setattr(channel.adapter, "_http", delayed)
    assert channel.adapter.send(delivery).payload["result"]["fulfillment"] == "pending"
    assert channel.adapter.inspect(delivery)["problem_code"] == "TEAMS_SEND_GATE_DENIED"
    assert sent == []


def test_native_installation_revoked_after_consume_is_checked_at_wire(monkeypatch):
    h, channel, _, _, delivery, sent, _ = native_delivery_fixture(monkeypatch)
    original = channel.adapter._http
    def revoke(*args):
        h.mutate("teams-native-current", channel.adapter.native_registration.personal.configuration_digest, active=False)
        return original(*args)
    monkeypatch.setattr(channel.adapter, "_http", revoke)
    assert channel.adapter.send(delivery).payload["result"]["fulfillment"] == "pending"
    assert channel.adapter.inspect(delivery)["problem_code"] == "TEAMS_SEND_GATE_DENIED"
    assert sent == []


def test_wire_window_does_not_repeat_slow_observer_and_history_verification(monkeypatch):
    h, channel, _, _, delivery, sent, _ = native_delivery_fixture(monkeypatch)
    original = channel.adapter._http
    def observe(*args):
        before = h.guard_reads
        result = original(*args)
        assert h.guard_reads == before
        return result
    monkeypatch.setattr(channel.adapter, "_http", observe)
    assert channel.adapter.send(delivery).payload["result"]["fulfillment"] == "fulfilled"
    assert len(sent) == 1


def test_native_lost_provider_response_never_reposts_after_restart(monkeypatch):
    def lost(_):
        raise httpx.ReadTimeout("SYNTHETIC-NATIVE-TOKEN")
    h, channel, _, _, delivery, sent, acquired = native_delivery_fixture(monkeypatch, lost)
    assert channel.adapter.send(delivery).payload["result"]["fulfillment"] == "pending"
    from shared_context.teams_delivery import TeamsDeliveryAdapter
    restarted = TeamsDeliveryAdapter(
        h.store, channel.adapter.settings, clock=lambda: h.time,
        resolve_send=channel.adapter.resolve_send, bot_token=channel.adapter.bot_token,
        native_registration=channel.adapter.native_registration,
    )
    h.core = TeamsDeliveryCore(h.store, clock=lambda: h.time,
                               registration=replace(h.registration, channel_adapter=restarted))
    restarted.attach(h.core)
    assert restarted.send(delivery).payload["result"]["fulfillment"] == "pending"
    assert len(sent) == 1 and acquired == [BOT_SCOPE]


@pytest.mark.parametrize("where", ["consume", "provider", "delivery"])
def test_native_committed_lost_ack_never_reposts(monkeypatch, where):
    h, channel, _, _, delivery, sent, acquired = native_delivery_fixture(monkeypatch)
    original = h.client.submit_transaction
    key = {
        "consume": "behavior:v1:receipt:" + control_key("teams-consume", delivery),
        "provider": "behavior:v1:row:" + control_key("teams-provider", delivery),
        "delivery": "behavior:v1:row:" + control_key("delivery-receipt", delivery, delivery),
    }[where]
    failed = False
    def lose(operations, **kwargs):
        nonlocal failed
        result = original(operations, **kwargs)
        if not failed and any(op[1]["RowKey"] == key for op in operations):
            failed = True
            raise ServiceRequestError("synthetic dropped acknowledgment")
        return result
    monkeypatch.setattr(h.client, "submit_transaction", lose)
    with pytest.raises(Problem):
        channel.adapter.send(delivery)
    if where == "consume":
        with pytest.raises(Problem):
            channel.adapter.send(delivery)
        assert sent == []
    else:
        assert channel.adapter.send(delivery).payload["result"]["fulfillment"] == "fulfilled"
        assert len(sent) == 1
    assert acquired == [BOT_SCOPE]


def test_retained_native_receipt_reconciles_when_current_installation_expires(monkeypatch):
    h, channel, _, ob, delivery, sent, acquired = native_delivery_fixture(monkeypatch)
    assert channel.adapter.send(delivery).payload["result"]["fulfillment"] == "fulfilled"
    assert channel.runtime.consume(ob)["fulfillment"] == "fulfilled"
    h.time = h.native_evidence.expires_at
    h.guard_error = True
    assert channel.adapter.send(delivery).payload["result"]["fulfillment"] == "fulfilled"
    assert channel.runtime.consume(ob)["fulfillment"] == "fulfilled"
    assert channel.adapter.inspect(delivery)["evidence_mode"] == "connector_http"
    assert len(sent) == 1 and acquired == [BOT_SCOPE]


def test_native_personal_client_metadata_keeps_original_source_hash(monkeypatch):
    import hashlib
    h, channel, client, sent, acquired = native_runtime_fixture(monkeypatch)
    body = json.dumps(activity(
        text="metrics", attachments=[{"contentType": "text/html", "content": "<p>metrics</p>"}],
        entities=[{"type": "clientInfo", "locale": "zh-TW", "platform": "Web"}],
    )).encode()
    response = client.post("/behavior/v1/teams/activities", content=body,
                           headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    ob = channel.runtime.inspect(response.json()["obligation_id"])
    assert h.data("teams-source", ob["event_id"])["body_digest"] == hashlib.sha256(body).hexdigest()
    assert h.data("teams-source", ob["event_id"])["evidence"]["source_kind"] == "human"
    proof = json.loads(channel.runtime.verify_ingress(ob["event_id"]).proof)
    assert proof["evidence_mode"] == "connector_http"
    assert proof["native_evidence_id"] == h.native_evidence.evidence_id
    assert proof["native_evidence_digest"] == digest(h.native_evidence.model_dump(mode="json"))
    assert proof["delegated_approval"] is False
    assert sent == acquired == []


def test_work_entry_never_restarts_completed_handshake(monkeypatch):
    _, _, client, sent, acquired = native_runtime_fixture(monkeypatch)
    response = client.post("/behavior/v1/teams/activities", json=activity(text=ROUTE_HANDSHAKE),
                           headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TEAMS_PERSONAL_ENROLLMENT_CLOSED"
    assert sent == acquired == []


def test_existing_pull_parent_cannot_be_relabelled_as_teams(monkeypatch):
    h, channel, _, sent, acquired = native_runtime_fixture(monkeypatch)
    parent = replace(h.registration, contract=h.config.model_copy(update={"channel": ChannelContract(
        channel_id="authenticated-pull", version="fa-pull-inbox-v1",
        supported_levels=("client_received",), max_confirmation_level="client_received",
    )}))
    with pytest.raises(Problem) as error:
        compose_personal_channel(
            channel.entry.enrollment, parent,
            TeamsParentSeams(
                resolve_admission=channel.runtime.resolve_admission,
                resolve_context=channel.runtime.resolve_context, process_work=channel.runtime.process_work,
                resolve_send=channel.adapter.resolve_send,
            ), native_registration=channel.adapter.native_registration,
        )
    assert error.value.body["error"]["code"] == "TEAMS_PARENT_CHANNEL_INCOMPATIBLE"
    assert sent == acquired == []


def test_installed_parent_composition_reuses_the_exact_store_and_guard(monkeypatch):
    from types import SimpleNamespace
    h, channel, _, sent, acquired = native_runtime_fixture(monkeypatch)
    parent = SimpleNamespace(
        runtime=SimpleNamespace(store=h.store, dep=SimpleNamespace(delivery=h.core)),
        config=SimpleNamespace(delivery=h.config, sha256="a" * 64,
                               table=SimpleNamespace(scope=SimpleNamespace(authority_domain="test-authority"))),
        require_current_native_evidence=channel.adapter.native_registration.require_current,
    )
    seams = TeamsParentSeams(
        resolve_admission=channel.runtime.resolve_admission, resolve_context=channel.runtime.resolve_context,
        process_work=channel.runtime.process_work, resolve_send=channel.adapter.resolve_send,
    )
    composed = compose_installed_personal_channel(
        parent, channel.entry.enrollment, seams, evidence_id=h.native_evidence.evidence_id,
        verify_current=lambda _: h.native_evidence,
    )
    assert composed.core.store is h.store
    assert composed.adapter.native_registration.require_current is parent.require_current_native_evidence
    assert composed.runtime.admission_enabled is False
    composed.adapter.check_installation(composed.core)
    parent.runtime.dep.delivery = h.broker(h.store, replace(h.registration, contract=h.config.model_copy(update={
        "channel": ChannelContract(channel_id="authenticated-pull", version="fa-pull-inbox-v1",
                                   supported_levels=("client_received",), max_confirmation_level="client_received"),
    })))
    with pytest.raises(Problem) as error:
        compose_installed_personal_channel(
            parent, channel.entry.enrollment, seams, evidence_id=h.native_evidence.evidence_id,
            verify_current=lambda _: h.native_evidence,
        )
    assert error.value.body["error"]["code"] == "TEAMS_PARENT_CHANNEL_INCOMPATIBLE"
    assert sent == acquired == []


@pytest.mark.parametrize("field", ["native_evidence_digest", "evidence_mode"])
def test_historical_native_receipt_cannot_change_provenance(monkeypatch, field):
    h, channel, _, _, delivery, sent, acquired = native_delivery_fixture(monkeypatch)
    channel.adapter.send(delivery)
    h.mutate("teams-provider", delivery, **{field: "offline_contract" if field == "evidence_mode" else "f" * 64})
    with pytest.raises(Problem):
        channel.adapter.send(delivery)
    assert len(sent) == 1 and acquired == [BOT_SCOPE]


def test_expired_or_revoked_installation_publication_receipt_is_not_reactivation(monkeypatch):
    h, channel, _, sent, acquired = native_runtime_fixture(monkeypatch)
    h.mutate("teams-native-current", channel.adapter.native_registration.personal.configuration_digest, active=False)
    with pytest.raises(Problem):
        channel.adapter.publish_installation()
    assert h.data("teams-native-current", channel.adapter.native_registration.personal.configuration_digest)["active"] is False
    assert sent == acquired == []


def test_revocation_after_readiness_is_rechecked_inside_original_admission_egt(monkeypatch):
    h, channel, client, sent, acquired = native_runtime_fixture(monkeypatch)
    original = channel.runtime.verify_ingress
    def revoke(evidence_id):
        evidence = original(evidence_id)
        h.mutate("teams-native-current", channel.adapter.native_registration.personal.configuration_digest, active=False)
        return evidence
    channel.core.registration = replace(channel.core.registration, verify_ingress=revoke)
    response = client.post("/behavior/v1/teams/activities", json=activity(),
                           headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 403
    assert h.data("delivery-quota", "root", "root")["pending"] == 0
    assert h.data("delivery-index", "root")["entries"] == []
    assert sent == acquired == []


def test_evidence_only_renewal_keeps_original_admission_request_and_prepared_payload(monkeypatch):
    h, channel, _, ob, delivery, sent, _ = native_delivery_fixture(monkeypatch)
    original_plan = channel.adapter.plan(delivery)
    h.time += timedelta(seconds=1)
    h.native_evidence = h.native_evidence.model_copy(update={
        "evidence_id": "native-evidence-two", "observed_at": h.time,
        "expires_at": h.time + timedelta(minutes=4),
    })
    renewed = compose_personal_channel(
        channel.entry.enrollment, h.registration,
        TeamsParentSeams(
            resolve_admission=channel.runtime.resolve_admission, resolve_context=channel.runtime.resolve_context,
            process_work=channel.runtime.process_work, resolve_send=channel.adapter.resolve_send,
        ), native_registration=replace(channel.adapter.native_registration, evidence_id="native-evidence-two"),
    )
    h.core, h.registration = renewed.core, renewed.core.registration
    renewed.adapter.publish_installation()
    renewed.runtime.enable_admission()
    app = FastAPI()
    app.include_router(renewed.entry.router())
    repeated = TestClient(app).post("/behavior/v1/teams/activities", json=activity(),
                                    headers={"Authorization": "Bearer " + token()})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["obligation_id"] == ob
    assert h.data("delivery-quota", "root", "root")["pending"] == 1
    assert renewed.adapter.plan(delivery) == original_plan
    assert renewed.adapter.send(delivery).payload["result"]["fulfillment"] == "fulfilled"
    assert renewed.adapter.inspect(delivery)["native_evidence_id"] == "native-evidence-two"
    assert len(sent) == 1
