"""OFFLINE real HTTPX sender + real delivery/operation ledgers over FakeTable."""
import json
from dataclasses import replace
from datetime import timedelta
from urllib.parse import quote

import httpx
import pytest
from azure.core.exceptions import ServiceRequestError

from shared_context.behavior_delivery import ArtifactVersion, DeliveryConsumer, ReceiptRequest, VerifiedDeliveryReceipt
from shared_context.behavior_operations import GrantRecord, GrantSubject, control_key
from shared_context.errors import Problem
from shared_context.teams_delivery import TeamsArtifactLink, TeamsDeliveryAdapter, TeamsRouteAccess, TeamsSendContext
from test_behavior_delivery import CONTEXT
from test_teams_identity import NOW, ROUTE, SERVICE, activity, token
from test_teams_runtime import runtime_fixture


def delivery_fixture(provider=None, *, artifacts=(), artifact_links=None, route=ROUTE, incoming=None,
                     work_state="succeeded", known_effects=(), unresolved_operations=()):
    h, bridge, client = runtime_fixture(route=route)
    sent = []
    def receiver(request):
        sent.append(request)
        if provider:
            return provider(request)
        return httpx.Response(200, json={"id": "provider-accepted-original-id"})
    adapter = TeamsDeliveryAdapter(
        h.store, bridge.identity.settings, clock=lambda: h.time,
        resolve_send=lambda _: TeamsSendContext(context=CONTEXT, grant_id="teams-human-grant"),
        bot_token=lambda scope: "synthetic-bot-credential",
        transport=httpx.MockTransport(receiver),
        artifact_links=artifact_links, artifact_origins=("https://artifacts.example.org",),
    )
    h.registration = replace(h.registration, channel_adapter=adapter)
    h.core = h.broker(h.store)
    bridge.attach(h.core)
    adapter.attach(h.core)
    admitted = client.post("/behavior/v1/teams/activities", json=activity() if incoming is None else incoming,
                           headers={"Authorization": "Bearer " + token()}).json()
    ob = admitted["obligation_id"]
    # The trusted parent worker seam uses the existing result verifier/ledger.
    bridge.process_work = lambda obligation, context: h.work_result(
        obligation, state=work_state, artifacts=artifacts, known_effects=known_effects,
        unresolved_operations=unresolved_operations,
    )
    prepared = bridge.prepare(ob, "teams-worker")
    delivery_id = prepared["binding"]["delivery_id"]
    h.mutate("owner", CONTEXT.owner_record_id, operation_ids=[adapter.operation_id(delivery_id)])
    h.put([(control_key("teams-route-access", route.route_id), TeamsRouteAccess(
        route_id=route.route_id, recipient_id=route.aad_object_id, active=True,
        owner_record_ids=(CONTEXT.owner_record_id,), artifacts=artifacts,
    ))])
    plan = adapter.prepare(delivery_id)
    h.put([(control_key("grant", "teams-human-grant"), GrantRecord(
        grant_id="teams-human-grant", approval_source="human", approval_id="delegated-approval-1",
        subjects=(GrantSubject(principal_id=CONTEXT.principal_id, conversation_id=CONTEXT.conversation_id),),
        root_budget_id=CONTEXT.root_budget_id, description_digest=plan.description_digest,
        adapter_contract_digest=adapter.contract_digest, policy_id="policy",
        policy_revision=1, revocation_revision=0, revoked=False,
        not_before=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=10),
        remaining_uses=1, remaining_units=1,
    ))])
    return h, bridge, client, adapter, ob, delivery_id, sent


def test_incoming_to_admit_prepare_guarded_http_send_and_existing_receipt_ledger():
    h, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture()
    result = bridge.consume(ob)
    assert result["fulfillment"] == "fulfilled"
    assert result["confirmed_levels"] == ["provider_accepted"]
    assert result["required_confirmation_level"] == "provider_accepted"
    assert bridge.inspect(ob)["fulfillment_receipt_id"]
    assert len(sent) == 1
    assert str(sent[0].url) == SERVICE + "v3/conversations/" + quote(ROUTE.conversation_id, safe="") + "/activities"
    assert sent[0].method == "POST"
    body = json.loads(sent[0].content)
    assert body["conversation"]["id"] == ROUTE.conversation_id
    assert body["text"] == "verified synthetic answer"
    assert "id" not in body
    assert adapter.inspect(delivery_id)["provider_activity_id"] == "provider-accepted-original-id"
    assert adapter.send(delivery_id).payload["result"]["fulfillment"] == "fulfilled"
    assert len(sent) == 1


@pytest.mark.parametrize("body", [{}, {"id": ""}, {"id": None}, {"id": "   "}])
def test_2xx_without_nonempty_provider_id_remains_unfulfilled(body):
    h, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture(lambda _: httpx.Response(200, json=body))
    result = adapter.send(delivery_id).payload["result"]
    assert result["delivery_state"] == "delivery_unknown"
    assert result["fulfillment"] == "pending"
    assert result["confirmed_levels"] == []
    assert adapter.inspect(delivery_id)["provider_activity_id"] is None
    adapter.send(delivery_id)
    assert len(sent) == 1
    assert bridge.inspect(ob)["fulfillment"] == "pending"


def test_lost_response_after_provider_acceptance_never_reposts_after_restart():
    accepted = []
    def lost(request):
        accepted.append(request.content)
        raise httpx.ReadTimeout("synthetic-bot-credential must never be observed")
    h, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture(lost)
    adapter.send(delivery_id)
    restarted = TeamsDeliveryAdapter(
        h.store, adapter.settings, clock=lambda: h.time, resolve_send=adapter.resolve_send,
        bot_token=adapter.bot_token, transport=adapter.transport,
    )
    h.registration = replace(h.registration, channel_adapter=restarted)
    h.core = h.broker(h.store)
    restarted.attach(h.core)
    bridge.attach(h.core)
    assert bridge.consume(ob)["fulfillment"] == "pending"
    assert len(sent) == len(accepted) == 1
    assert "synthetic-bot-credential" not in json.dumps(restarted.inspect(delivery_id))


def test_retained_receipt_after_control_commit_lost_ack_reconciles_without_send():
    h, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture()
    original = h.client.submit_transaction
    failed = False
    def uncertain(operations, **kwargs):
        nonlocal failed
        result = original(operations, **kwargs)
        receipt_key = "behavior:v1:row:" + control_key("delivery-receipt", delivery_id, delivery_id)
        if not failed and any(op[1]["RowKey"] == receipt_key for op in operations):
            failed = True
            raise ServiceRequestError("synthetic control acknowledgment lost")
        return result
    h.client.submit_transaction = uncertain
    with pytest.raises(Problem):
        adapter.send(delivery_id)
    assert adapter.reconcile(delivery_id).payload["result"]["fulfillment"] == "fulfilled"
    assert bridge.inspect(ob)["fulfillment"] == "fulfilled"
    assert len(sent) == 1


def test_retained_provider_receipt_before_failed_control_commit_is_replayed_without_send():
    h, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture()
    original = h.client.submit_transaction
    failed = False
    def uncertain(operations, **kwargs):
        nonlocal failed
        receipt_key = "behavior:v1:row:" + control_key("delivery-receipt", delivery_id, delivery_id)
        if not failed and any(op[1]["RowKey"] == receipt_key for op in operations):
            failed = True
            raise ServiceRequestError("synthetic control write not acknowledged")
        return original(operations, **kwargs)
    h.client.submit_transaction = uncertain
    with pytest.raises(Problem):
        adapter.send(delivery_id)
    assert bridge.inspect(ob)["fulfillment"] == "pending"
    assert adapter.reconcile(delivery_id).payload["result"]["fulfillment"] == "fulfilled"
    assert len(sent) == 1


def test_redirect_response_is_unknown_and_never_forwards_bot_credentials():
    _, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture(
        lambda _: httpx.Response(307, headers={"Location": "https://127.0.0.1/private"}))
    assert adapter.send(delivery_id).payload["result"]["fulfillment"] == "pending"
    assert bridge.inspect(ob)["delivery_state"] == "delivery_unknown"
    assert len(sent) == 1


@pytest.mark.parametrize("change", ["route", "route_record", "grant", "root", "subject", "owner", "body", "artifact", "policy"])
def test_revocation_and_binding_changes_between_prepare_and_send_deny_post(change):
    h, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture()
    if change == "route":
        h.mutate("teams-route-access", ROUTE.route_id, active=False)
    elif change == "route_record":
        h.mutate("teams-route", ROUTE.route_id, service_url="https://smba.trafficmanager.net/emea/")
    elif change == "grant":
        h.mutate("grant", "teams-human-grant", revoked=True, revocation_revision=1)
    elif change == "owner":
        h.mutate("owner", CONTEXT.owner_record_id, epoch=2)
    elif change == "policy":
        h.mutate("policy", "policy", active=False)
    elif change == "root":
        h.mutate("grant", "teams-human-grant", root_budget_id="wrong-root")
    elif change == "subject":
        h.mutate("grant", "teams-human-grant", subjects=[{"principal_id": "another-person", "conversation_id": "another-thread"}])
    else:
        binding = adapter.plan(delivery_id).binding.model_dump(mode="json")
        if change == "body":
            binding["payload_digest"] = "0" * 64
        else:
            binding["artifacts"] = [{"artifact_id": "wrong-work", "version": "v1", "sha256": "0" * 64}]
        h.mutate("delivery-outbox", delivery_id, binding=binding)
    with pytest.raises(Problem):
        adapter.send(delivery_id)
    assert sent == []


def test_token_warmup_route_revocation_is_seen_by_final_gate():
    h, _, _, adapter, _, delivery_id, sent = delivery_fixture()
    def credential(_):
        h.mutate("teams-route-access", ROUTE.route_id, active=False, revision=2)
        h.results["work-proof"] = h.results["work-proof"].model_copy(update={"content": b"unapproved later body B"})
        return "synthetic-bot-credential"
    adapter.bot_token = credential
    with pytest.raises(Problem) as error:
        adapter.send(delivery_id)
    assert error.value.body["error"]["code"] == "TEAMS_ROUTE_ACCESS_DENIED"
    assert sent == []


def test_cold_token_warmup_precedes_the_unchanged_two_second_send_gate():
    h, _, _, adapter, _, delivery_id, sent = delivery_fixture()
    def credential(_):
        h.time += timedelta(seconds=3)
        return "synthetic-bot-credential"
    adapter.bot_token = credential
    assert adapter.send(delivery_id).payload["result"]["fulfillment"] == "fulfilled"
    send = adapter._send_row(delivery_id)
    assert send.gated_at == NOW + timedelta(seconds=3)
    assert send.not_after - send.gated_at == timedelta(seconds=2)
    assert len(sent) == 1


def test_after_gate_expired_send_window_never_enters_http(monkeypatch):
    h, _, _, adapter, _, delivery_id, sent = delivery_fixture()
    original = adapter._http
    def delayed(*args):
        h.time += timedelta(seconds=3)
        return original(*args)
    monkeypatch.setattr(adapter, "_http", delayed)
    assert adapter.send(delivery_id).payload["result"]["delivery_state"] == "delivery_unknown"
    assert sent == []


def test_post_gate_inflight_revocation_does_not_change_the_immutable_request(monkeypatch):
    h, _, _, adapter, _, delivery_id, sent = delivery_fixture()
    approved = adapter.plan(delivery_id).description.content
    original = adapter._http
    def revoked(*args):
        h.mutate("teams-route-access", ROUTE.route_id, active=False, revision=2)
        h.results["work-proof"] = h.results["work-proof"].model_copy(update={"content": b"unapproved B"})
        return original(*args)
    monkeypatch.setattr(adapter, "_http", revoked)
    assert adapter.send(delivery_id).payload["result"]["fulfillment"] == "fulfilled"
    assert sent[0].content == approved
    assert str(sent[0].url) == ROUTE.target()


def test_cross_obligation_and_fake_receipts_cannot_fulfill_another_request():
    h, bridge, client, adapter, ob, delivery_id, sent = delivery_fixture()
    second = client.post("/behavior/v1/teams/activities", json=activity(id="provider-incoming-2"),
                         headers={"Authorization": "Bearer " + token()}).json()["obligation_id"]
    h.receipts[delivery_id] = VerifiedDeliveryReceipt(
        evidence_id=delivery_id, verifier_version=h.config.verifier_version,
        binding=adapter.plan(delivery_id).binding, send_outcome="emitted", confirmed_levels=("provider_accepted",),
        dispatch_authorization_ref="fake-root-gate", proof=b'{"id":"made-up-provider-id"}',
    )
    with pytest.raises(Problem):
        h.core.record_receipt(ReceiptRequest(obligation_id=ob, delivery_id=delivery_id,
                                            evidence_id=delivery_id), CONTEXT)
    assert bridge.inspect(ob)["fulfillment"] == "pending"
    adapter.send(delivery_id)
    with pytest.raises(Problem):
        h.core.record_receipt(ReceiptRequest(obligation_id=second, delivery_id=delivery_id,
                                            evidence_id=delivery_id), CONTEXT)
    assert bridge.inspect(second)["fulfillment"] == "pending"
    assert len(sent) == 1


def test_disabling_new_admission_does_not_abandon_old_outbox():
    _, bridge, client, _, ob, _, sent = delivery_fixture()
    bridge.admission_enabled = False
    rejected = client.post("/behavior/v1/teams/activities", json=activity(id="new-provider-id"),
                           headers={"Authorization": "Bearer " + token()})
    assert rejected.status_code == 403
    assert bridge.consume(ob)["fulfillment"] == "fulfilled"
    assert len(sent) == 1


def test_bounded_drain_keeps_working_when_admission_is_disabled_and_quota_full():
    h, bridge, _, _, ob, _, sent = delivery_fixture()
    bridge.resolve_context = lambda _: CONTEXT
    bridge.admission_enabled = False
    h.mutate("delivery-quota", "entry", "entry", pending_limit=1)
    results = bridge.drain("root", limit=1)
    assert results[0]["fulfillment"] == "fulfilled"
    assert bridge.inspect(ob)["fulfillment"] == "fulfilled"
    assert len(sent) == 1


@pytest.mark.parametrize("where", ["credential", "body", "error", "provider_id"])
def test_credential_boundary_outputs_only_fixed_safe_error_and_no_observed_token(where):
    secret = "SYNTHETIC-CREDENTIAL-NOT-ORDINARY-CONTENT"
    def provider(_):
        if where == "body":
            return httpx.Response(500, text="upstream leaked " + secret)
        if where == "error":
            raise httpx.ReadError(secret)
        return httpx.Response(200, json={"id": secret})
    h, _, _, adapter, _, delivery_id, sent = delivery_fixture(provider)
    def credential(_):
        if where == "credential":
            raise RuntimeError(secret)
        return secret
    adapter.bot_token = credential
    result = adapter.send(delivery_id).payload["result"]
    assert result["fulfillment"] == "pending"
    assert adapter.inspect(delivery_id)["problem_code"]
    assert secret not in json.dumps(adapter.inspect(delivery_id))
    assert all(secret.encode() not in op[1].get("Payload", b"") for batch in h.client.submissions for op in batch)


def test_reconcile_while_original_http_is_inflight_does_not_install_fake_missing_receipt():
    observed = []
    adapter = None
    delivery_id = None
    def provider(_):
        try:
            adapter.reconcile(delivery_id)
        except Problem as error:
            observed.append(error.body["error"]["code"])
        return httpx.Response(200, json={"id": "late-original-A"})
    _, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture(provider)
    assert adapter.send(delivery_id).payload["result"]["fulfillment"] == "fulfilled"
    assert observed == ["TEAMS_SEND_UNKNOWN"]
    assert adapter.inspect(delivery_id)["provider_activity_id"] == "late-original-A"
    assert len(sent) == 1


ARTIFACT = ArtifactVersion(artifact_id="report-one", version="1" * 32, sha256="a" * 64)


def test_required_fixed_artifact_reference_survives_alias_change_after_gate():
    alias = ["https://artifacts.example.org/teams/team/artifacts/report-one/versions/11111111111111111111111111111111/content"]
    def links(refs, route):
        return (TeamsArtifactLink(artifact=ARTIFACT, url=alias[0]),)
    h, _, _, adapter, _, delivery_id, sent = delivery_fixture(artifacts=(ARTIFACT,), artifact_links=links)
    def credential(_):
        alias[0] = "https://artifacts.example.org/teams/team/artifacts/report-one/versions/22222222222222222222222222222222/content"
        return "synthetic-bot-credential"
    adapter.bot_token = credential
    assert adapter.send(delivery_id).payload["result"]["fulfillment"] == "fulfilled"
    text = json.loads(sent[0].content)["text"]
    assert "/versions/11111111111111111111111111111111/content" in text
    assert "/versions/22222222222222222222222222222222/content" not in text
    assert "SHA-256 " + "a" * 64 in text
    assert adapter.plan(delivery_id).binding.artifacts == (ARTIFACT,)


def test_required_artifacts_are_not_silently_dropped_without_trusted_download_seam():
    with pytest.raises(Problem) as error:
        delivery_fixture(artifacts=(ARTIFACT,))
    assert error.value.body["error"]["code"] == "TEAMS_ARTIFACTS_DISABLED"


def test_current_artifact_read_scope_is_checked_again_at_actual_send():
    def links(refs, route):
        return (TeamsArtifactLink(artifact=ARTIFACT, url="https://artifacts.example.org/teams/team/artifacts/report-one/versions/11111111111111111111111111111111/content"),)
    h, _, _, adapter, _, delivery_id, sent = delivery_fixture(artifacts=(ARTIFACT,), artifact_links=links)
    h.mutate("teams-route-access", ROUTE.route_id, artifacts=[])
    with pytest.raises(Problem):
        adapter.send(delivery_id)
    assert sent == []


def test_unknown_consumption_commit_is_not_a_send_permission_after_restart():
    h, _, _, adapter, _, delivery_id, sent = delivery_fixture()
    original = h.client.submit_transaction
    failed = False
    def uncertain(operations, **kwargs):
        nonlocal failed
        result = original(operations, **kwargs)
        request_key = "behavior:v1:receipt:" + control_key("teams-consume", delivery_id)
        if not failed and any(op[1]["RowKey"] == request_key for op in operations):
            failed = True
            raise ServiceRequestError("synthetic lost final gate response")
        return result
    h.client.submit_transaction = uncertain
    with pytest.raises(Problem):
        adapter.send(delivery_id)
    with pytest.raises(Problem) as error:
        adapter.send(delivery_id)
    assert error.value.body["error"]["code"] == "TEAMS_SEND_UNKNOWN"
    assert sent == []


def test_claim_loss_after_reservation_does_not_create_a_second_attempt_or_refund():
    h, _, _, adapter, _, delivery_id, sent = delivery_fixture()
    original = h.client.submit_transaction
    failed = False
    def uncertain(operations, **kwargs):
        nonlocal failed
        result = original(operations, **kwargs)
        attempt_key = "behavior:v1:row:" + control_key("attempt", adapter.operation_id(delivery_id),
                                                     control_key("teams-attempt", delivery_id))
        if not failed and any(op[1]["RowKey"] == attempt_key for op in operations):
            failed = True
            raise ServiceRequestError("synthetic lost authorization response")
        return result
    h.client.submit_transaction = uncertain
    with pytest.raises(Problem):
        adapter.send(delivery_id)
    with pytest.raises(Problem) as error:
        adapter.send(delivery_id)
    assert error.value.body["error"]["code"] == "TEAMS_SEND_UNKNOWN"
    assert sent == []


def test_native_mode_and_higher_confirmation_capabilities_stay_disabled():
    h, bridge, _, adapter, _, delivery_id, sent = delivery_fixture()
    h.registration = replace(h.registration, mode="native")
    h.core = h.broker(h.store)
    with pytest.raises(Problem) as error:
        adapter.attach(h.core)
    assert error.value.body["error"]["code"] == "TEAMS_NATIVE_DISABLED"
    assert sent == []


@pytest.mark.parametrize("url", [
    "https://artifacts.example.org/teams/other-team/artifacts/report-one/versions/11111111111111111111111111111111/content",
    "https://artifacts.example.org/teams/team/artifacts/report-one/versions/22222222222222222222222222222222/content",
    "https://artifacts.example.org/teams/team/artifacts/other-report/versions/11111111111111111111111111111111/content",
])
def test_artifact_link_cannot_lie_about_work_scope_or_fixed_version(url):
    with pytest.raises(Problem) as error:
        delivery_fixture(artifacts=(ARTIFACT,), artifact_links=lambda *_: (TeamsArtifactLink(artifact=ARTIFACT, url=url),))
    assert error.value.body["error"]["code"] == "TEAMS_ARTIFACT_SCOPE_DENIED"


def test_obligation_cannot_silently_lower_or_exceed_confirmation_contract():
    h, _, _, adapter, ob, delivery_id, sent = delivery_fixture()
    h.mutate("delivery-obligation", ob, required_confirmation_level="human_read")
    with pytest.raises(Problem):
        adapter.send(delivery_id)
    assert sent == []


def test_provider_acceptance_without_durable_receipt_holds_unknown_not_negative_proof():
    h, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture()
    original = h.client.submit_transaction
    def fail_receipt(operations, **kwargs):
        key = "behavior:v1:row:" + control_key("teams-provider", delivery_id)
        if any(op[1]["RowKey"] == key for op in operations):
            raise ServiceRequestError("synthetic receipt storage unavailable")
        return original(operations, **kwargs)
    h.client.submit_transaction = fail_receipt
    with pytest.raises(Problem):
        adapter.send(delivery_id)
    assert adapter.inspect(delivery_id)["send_outcome"] == "unknown"
    assert adapter.inspect(delivery_id)["provider_activity_id"] is None
    with pytest.raises(Problem):
        adapter.send(delivery_id)
    assert bridge.inspect(ob)["fulfillment"] == "pending"
    assert len(sent) == 1


def test_channel_reply_uses_exact_opaque_root_in_connector_http_path_and_body():
    root = "root/one%encoded:" + "thread" * 30
    route = ROUTE.model_copy(update={"conversation_type": "channel", "thread_id": root})
    incoming = activity(
        replyToId=root,
        conversation={"id": route.conversation_id, "conversationType": "channel", "tenantId": route.tenant_id},
    )
    _, bridge, _, adapter, ob, delivery_id, sent = delivery_fixture(route=route, incoming=incoming)
    assert bridge.consume(ob)["fulfillment"] == "fulfilled"
    assert str(sent[0].url) == SERVICE + "v3/conversations/" + quote(route.conversation_id, safe="") + "/activities/" + quote(root, safe="")
    assert json.loads(sent[0].content)["replyToId"] == root
    assert "id" not in json.loads(sent[0].content)


def test_delivered_failure_preserves_known_effect_unknown_operation_and_followup_owner():
    _, bridge, _, _, ob, _, sent = delivery_fixture(
        work_state="failed", known_effects=("saved-artifact",), unresolved_operations=("unknown-operation-A",),
    )
    assert bridge.consume(ob)["fulfillment"] == "fulfilled"
    text = json.loads(sent[0].content)["text"]
    assert "工作狀態：failed" in text
    assert "已知效果：saved-artifact" in text
    assert "待核對操作：unknown-operation-A" in text
    assert "後續負責人：owner-one" in text


def test_offline_evidence_cannot_accidentally_enable_real_network_delivery():
    _, _, _, adapter, _, delivery_id, sent = delivery_fixture()
    def no_live_credentials(_):
        raise AssertionError("offline fixture must never acquire network credentials")
    adapter.bot_token = no_live_credentials
    adapter.transport = None
    with pytest.raises(Problem) as error:
        adapter.send(delivery_id)
    assert error.value.body["error"]["code"] == "TEAMS_OFFLINE_TRANSPORT_REQUIRED"
    assert sent == []


def test_offline_retained_plan_gate_and_receipt_keep_the_original_serialized_contract():
    h, _, _, adapter, _, delivery, sent = delivery_fixture()
    assert adapter.send(delivery).payload["result"]["fulfillment"] == "fulfilled"
    for kind in ("teams-send-plan", "teams-send", "teams-provider"):
        data = h.data(kind, delivery)
        assert "evidence_mode" not in data
        assert "native_evidence_id" not in data
        assert "native_evidence_digest" not in data
    assert adapter.reconcile(delivery).payload["result"]["fulfillment"] == "fulfilled"
    assert len(sent) == 1


@pytest.mark.parametrize("change", ["method", "url", "body", "repeat"])
def test_one_send_transport_requires_exact_consumed_request_once(change):
    from shared_context.teams_delivery import _OneSend
    _, _, _, adapter, _, delivery, sent = delivery_fixture()
    adapter.send(delivery)
    plan, send = adapter.plan(delivery), adapter._send_row(delivery)
    observed = []
    gate = _OneSend(httpx.MockTransport(lambda r: observed.append(r) or httpx.Response(200)), plan, send, lambda: NOW)
    method = "PUT" if change == "method" else "POST"
    url = plan.description.target + "/different" if change == "url" else plan.description.target
    body = b"different" if change == "body" else plan.description.content
    request = httpx.Request(method, url, content=body)
    if change == "repeat":
        gate.handle_request(request)
    with pytest.raises(Problem):
        gate.handle_request(request)
    assert len(observed) == (1 if change == "repeat" else 0)
    assert len(sent) == 1
