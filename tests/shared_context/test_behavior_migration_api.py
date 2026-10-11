"""OFFLINE signed JWT + migration authority over the existing fake Table transport."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.behavior_migration import MigrationAuthorization, MigrationPlan
from shared_context.behavior_operations import control_key
from test_behavior_migration import fixture
from test_behavior_server import NOW
from test_m3_auth import jwk_client, signing_key, token


def test_operator_http_migration_uses_verified_human_and_preserves_existing_budget(signing_key, jwk_client):
    from shared_context.behavior_migration_api import create_migration_app

    service, store, _, old, new, _ = fixture()
    verifier = BehaviorJwtVerifier(
        old.identity.auth(), BehaviorAuthSettings(old.identity.human_oid, old.identity.admin_client_id, old.identity.team_id),
        jwk_client,
    )
    encoded = token(signing_key, {
        "tid": old.identity.tenant_id, "iss": f"https://sts.windows.net/{old.identity.tenant_id}/",
        "aud": old.identity.audience, "oid": old.identity.human_oid,
        "appid": old.identity.admin_client_id, "scp": "Behavior.Read Behavior.Approve",
    })
    headers = {"Authorization": "Bearer " + encoded}
    client = TestClient(create_migration_app(service, verifier, allow_apply=True))
    base = "/operator/v1/protection-migrations/migrate-one"
    key = control_key("budget", old.roots[0].root_budget_id)
    before = store.read_snapshot((key,))[key]
    unauthorized = client.post(base + "/plan", content=b"x" * 50000, headers={"Content-Type": "application/json"})
    assert unauthorized.status_code == 401
    assert client.post(base + "/plan", json={"new_config": {}}, headers=headers).status_code == 422
    response = client.post(base + "/plan", json={}, headers=headers)
    assert response.status_code == 200
    plan = MigrationPlan.model_validate_json(response.content)
    assert client.post(base + "/authorize", json={
        "plan": plan.model_dump(mode="json"), "decision_id": "naive-time",
        "expires_at": "2026-09-16T23:59:00",
    }, headers=headers).status_code == 422
    authorized = client.post(base + "/authorize", json={
        "plan": plan.model_dump(mode="json"), "decision_id": "actual-test-operator",
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }, headers=headers)
    assert authorized.status_code == 200
    authorization = MigrationAuthorization.model_validate_json(authorized.content)
    body = {"plan": plan.model_dump(mode="json"), "authorization": authorization.model_dump(mode="json")}
    review_only = TestClient(create_migration_app(service, verifier))
    assert review_only.post(base + "/apply", json=body, headers=headers).status_code == 403
    assert store.read_snapshot((key,))[key] == before
    applied = client.post(base + "/apply", json=body, headers=headers)
    assert applied.status_code == 200
    assert client.post(base + "/apply", json=body, headers=headers).status_code == 409
    verified = client.post(base + "/verify", json=body, headers=headers)
    assert verified.status_code == 200
    assert verified.json()["new_configuration_sha256"] == new.sha256
    assert verified.json()["before_preserved_sha256"] == verified.json()["after_preserved_sha256"]
    assert store.read_snapshot((key,))[key] == before
    assert client.get("/healthz").json()["work_admissions"] == "disabled"
    assert client.post("/behavior/v1/bindings/arbitrary/work/submit", json={}, headers=headers).status_code == 404


@pytest.mark.parametrize("changed,removed", [
    ({"scp": "Behavior.Read"}, ()),
    ({"oid": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, ()),
    ({"appid": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, ()),
    ({"roles": ["Behavior.Agent"], "idtyp": "app"}, ("scp",)),
])
def test_operator_http_refuses_nonoperator_before_body_or_store_access(signing_key, jwk_client, changed, removed):
    from shared_context.behavior_migration_api import create_migration_app

    service, store, _, old, _, _ = fixture()
    verifier = BehaviorJwtVerifier(
        old.identity.auth(), BehaviorAuthSettings(old.identity.human_oid, old.identity.admin_client_id, old.identity.team_id),
        jwk_client,
    )
    claims = {
        "tid": old.identity.tenant_id, "iss": f"https://sts.windows.net/{old.identity.tenant_id}/",
        "aud": old.identity.audience, "oid": old.identity.human_oid,
        "appid": old.identity.admin_client_id, "scp": "Behavior.Read Behavior.Approve", **changed,
    }
    client = TestClient(create_migration_app(service, verifier))
    before = store.read_inventory().entity_sha256
    response = client.post("/operator/v1/protection-migrations/unauthorized/plan",
                           content=b"invalid" * 10000, headers={
                               "Authorization": "Bearer " + token(signing_key, claims, remove=removed),
                               "Content-Type": "application/json",
                           })
    assert response.status_code in {401, 403}
    assert dict(store.read_inventory().entity_sha256) == dict(before)
