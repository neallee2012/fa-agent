"""Signed JWT through the actual ASGI/HTTP parser; in-process, never live HTTP."""

import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_delivery import DeliveryBinding
from shared_context.behavior_settlement_api import create_settlement_app
from test_behavior_settlement import data, retained
from test_m3_auth import jwk_client, signing_key, token

PREFIX = "/operator/v1/retained-result"


def post(client, h, action, body, identity="human"):
    return client.post(PREFIX + "/" + action, json=body, headers=h.headers(identity))


def approved(client, h):
    prepared = post(client, h, "prepare", {})
    assert prepared.status_code == 200, prepared.text
    from shared_context.behavior_settlement import SettlementPlan
    plan = SettlementPlan.model_validate_json(prepared.content)
    response = post(client, h, "authorize", {
        "plan_sha256": plan.sha256, "decision_id": "explicit-http-human-settlement", "decision": "settle_retained_result",
    })
    assert response.status_code == 200, response.text
    assert "proof" not in response.json() and "protection" not in response.json()
    return response.json()["authorization_id"], plan


def test_signed_receiver_preserves_exact_bytes_and_requires_real_ack(retained):
    h, service, _, _ = retained
    client = TestClient(create_settlement_app(service, h.verifier))
    authorization_id, plan = approved(client, h)
    pulled = post(client, h, "fetch", {"authorization_id": authorization_id, "request_id": "pull"})
    assert pulled.status_code == 200, pulled.text
    assert pulled.headers["content-type"] == "application/octet-stream"
    assert pulled.headers["cache-control"] == "no-store"
    assert pulled.headers["x-behavior-send-outcome"] == "unknown"
    binding = DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(pulled.headers["x-behavior-binding"]))
    assert binding == plan.delivery and hashlib.sha256(pulled.content).hexdigest() == binding.payload_digest
    assert data(h, "delivery-obligation", h.binding.obligation_id)["fulfillment"] == "pending"
    body = {
        "authorization_id": authorization_id, "request_id": "ack",
        "exchange_id": pulled.headers["x-behavior-exchange"], "acknowledgment_token": pulled.headers["x-behavior-ack-token"],
        "binding": binding.model_dump(mode="json"), "confirmation_level": "client_received",
    }
    assert post(client, h, "acknowledge", {**body, "acknowledgment_token": "wrong"}).status_code == 403
    assert post(client, h, "acknowledge", {**body, "confirmation_level": "human_read"}).status_code == 422
    acknowledged = post(client, h, "acknowledge", body)
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["result"]["confirmed_levels"] == ["client_received"]
    assert acknowledged.json()["result"]["fulfillment"] == "fulfilled"
    assert post(client, h, "acknowledge", body).json() == acknowledged.json()
    assert post(client, h, "reconcile", {"authorization_id": authorization_id}).json() == acknowledged.json()
    assert h.executor.starts == []


@pytest.mark.parametrize("identity", ["agent", "old-m3", "read-only"])
def test_authentication_does_not_replace_explicit_human_approval(retained, identity):
    h, service, _, _ = retained
    client = TestClient(create_settlement_app(service, h.verifier))
    assert post(client, h, "prepare", {}, identity).status_code == 403
    assert post(client, h, "authorize", {
        "plan_sha256": "a" * 64, "decision_id": "not-authorized", "decision": "settle_retained_result",
    }, identity).status_code == 403


@pytest.mark.parametrize("claims", [
    {"oid": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
    {"appid": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
    {"tid": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
    {"aud": "wrong-audience"}, {"exp": 1},
])
def test_real_jwt_signature_claim_validation_not_dependency_override(retained, claims):
    h, service, _, _ = retained
    client = TestClient(create_settlement_app(service, h.verifier))
    encoded = token(h.signing, {"scp": "Behavior.Read Behavior.Approve", **claims})
    response = client.post(PREFIX + "/prepare", json={}, headers={"Authorization": "Bearer " + encoded})
    assert response.status_code in {401, 403}
    assert client.post(PREFIX + "/prepare", json={}).status_code == 401


@pytest.mark.parametrize("extra", [
    "policy", "principal", "clock", "endpoint", "provider_receipt", "protection", "backend", "skip_readiness",
])
def test_http_never_deserializes_authority_witnesses_or_callbacks(retained, extra):
    h, service, _, _ = retained
    client = TestClient(create_settlement_app(service, h.verifier))
    authorization_id, _ = approved(client, h)
    response = post(client, h, "fetch", {"authorization_id": authorization_id, "request_id": "pull", extra: {}})
    assert response.status_code == 422
    duplicate = client.post(PREFIX + "/fetch", content=(
        '{"authorization_id":"' + authorization_id + '","request_id":"one","request_id":"two"}'
    ), headers=h.headers())
    assert duplicate.status_code == 422
    assert client.post(PREFIX + "/prepare?clock=2099", json={}, headers=h.headers()).status_code == 422
    assert client.post("/behavior/v1/bindings/binding-one/work/submit", json={}, headers=h.headers()).status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_missing_human_decision_and_stale_plan_cannot_issue_grant(retained):
    h, service, _, _ = retained
    client = TestClient(create_settlement_app(service, h.verifier))
    assert post(client, h, "prepare", {}).status_code == 200
    assert post(client, h, "authorize", {"plan_sha256": "0" * 64, "decision_id": "unavailable"}).status_code == 422
    assert post(client, h, "authorize", {
        "plan_sha256": "0" * 64, "decision_id": "unavailable", "decision": "user not available",
    }).status_code == 422
    assert post(client, h, "authorize", {
        "plan_sha256": "0" * 64, "decision_id": "stale", "decision": "settle_retained_result",
    }).status_code == 409


@pytest.mark.parametrize("invalid", ["expiry", "revocation"])
def test_http_fetch_and_ack_stay_closed_after_grant_expiry_or_revocation(retained, invalid):
    h, service, _, _ = retained
    client = TestClient(create_settlement_app(service, h.verifier))
    authorization_id, _ = approved(client, h)
    pulled = post(client, h, "fetch", {"authorization_id": authorization_id, "request_id": "pull"})
    assert pulled.status_code == 200
    if invalid == "expiry":
        h.time = service.registration.expires_at
    else:
        assert post(client, h, "revoke", {"authorization_id": authorization_id, "decision_id": "revoke"}).status_code == 200
    assert post(client, h, "fetch", {"authorization_id": authorization_id, "request_id": "pull"}).status_code == 403
    ack = post(client, h, "acknowledge", {
        "authorization_id": authorization_id, "request_id": "ack",
        "exchange_id": pulled.headers["x-behavior-exchange"], "acknowledgment_token": pulled.headers["x-behavior-ack-token"],
        "binding": DeliveryBinding.model_validate_json(
            base64.urlsafe_b64decode(pulled.headers["x-behavior-binding"]),
        ).model_dump(mode="json"),
    })
    assert ack.status_code == 403
    assert data(h, "delivery-obligation", h.binding.obligation_id)["fulfillment"] == "pending"
