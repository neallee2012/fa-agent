"""Signed operator transport exercises the actual transition with offline Table I/O."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.behavior_runtime_transition_api import create_transition_app
from test_behavior_runtime_transition import transition_fixture
from test_behavior_server import NOW
from test_m3_auth import jwk_client, signing_key, token


def http_fixture(signing_key, jwk_client, *, changed=None):
    service, store, _, _, old, new, _ = transition_fixture()
    verifier = BehaviorJwtVerifier(old.identity.auth(), BehaviorAuthSettings(
        old.identity.human_oid, old.identity.admin_client_id, old.identity.team_id), jwk_client)
    claims = {
        "tid": old.identity.tenant_id, "iss": f"https://sts.windows.net/{old.identity.tenant_id}/",
        "aud": old.identity.audience, "oid": old.identity.human_oid,
        "appid": old.identity.admin_client_id, "scp": "Behavior.Read Behavior.Approve",
        **(changed or {}),
    }
    return service, store, verifier, {"Authorization": "Bearer " + token(signing_key, claims)}, new


def test_runtime_transition_http_requires_exact_human_approval_and_default_apply_off(signing_key, jwk_client):
    service, store, verifier, headers, new = http_fixture(signing_key, jwk_client)
    client = TestClient(create_transition_app(service, verifier, allow_apply=True))
    base = "/operator/v1/runtime-transitions/sdk-release"
    assert client.post(base + "/plan", json={}).status_code == 401
    assert client.post(base + "/plan", json={"clock": "override"}, headers=headers).status_code == 422
    response = client.post(base + "/plan", json={}, headers=headers)
    assert response.status_code == 200, response.text
    plan = response.json()
    invalid = {"plan": plan, "decision_id": "not-UTC", "expires_at": "2026-09-16T01:05:00"}
    assert client.post(base + "/authorize", json=invalid, headers=headers).status_code == 422
    response = client.post(base + "/authorize", json={
        "plan": plan, "decision_id": "sdk-approved", "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }, headers=headers)
    assert response.status_code == 200, response.text
    command = {"plan": plan, "authorization": response.json()}
    before = dict(store.read_inventory().entity_sha256)
    review_only = TestClient(create_transition_app(service, verifier))
    assert review_only.post(base + "/apply", json=command, headers=headers).status_code == 403
    assert dict(store.read_inventory().entity_sha256) == before
    assert client.post(base + "/apply", json=command, headers=headers).status_code == 200
    response = client.post(base + "/verify", json=command, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["activation_performed"] is False
    assert client.get("/healthz").json()["work_admissions"] == "disabled"
    assert client.post("/behavior/v1/assistant/submit", json={}, headers=headers).status_code == 404


@pytest.mark.parametrize("claims", [
    {"oid": "eeeeeeee-eeee-eeee-eeeeeeeeeeee"}, {"appid": "eeeeeeee-eeee-eeee-eeeeeeeeeeee"},
    {"scp": "Behavior.Read"},
])
def test_nonoperator_is_rejected_before_body_or_authority_mutation(signing_key, jwk_client, claims):
    service, store, verifier, headers, _ = http_fixture(signing_key, jwk_client, changed=claims)
    client = TestClient(create_transition_app(service, verifier))
    before = dict(store.read_inventory().entity_sha256)
    response = client.post("/operator/v1/runtime-transitions/sdk-release/plan", content=b"not-json" * 8000,
                           headers={**headers, "Content-Type": "application/json"})
    assert response.status_code in {401, 403}
    assert dict(store.read_inventory().entity_sha256) == before
