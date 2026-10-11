"""Offline exact-source verification; fake Table and locally signed JWT, not Azure evidence."""
import base64
import copy
import json
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.behavior_migration import installation_record, migration_key
from shared_context.behavior_store import GUARD_KEY
from shared_context.errors import Problem
from shared_context.source_materials import (
    AuthenticatedSourceContext, SourceInput, SourceMaterials, SourceWithdrawalInput, digest,
)
from test_behavior_migration import fixture
from test_behavior_server import NOW
from test_m3_auth import jwk_client, signing_key, token


ORIGINAL = b"private retained original, never an HTTP response!"


def verification_fixture(*, expired=True, visibility="private", apply=True):
    from shared_context.behavior_migration_verification import ExpiredSourceVerificationPermission

    migration, store, table, old, new, principal = fixture()
    context = AuthenticatedSourceContext(
        principal.tenant_id, principal.team, principal.source_actor_id, "offline-operator", ("metrics",),
    )
    source = SourceMaterials(store, clock=lambda: NOW)
    expiry = NOW + timedelta(seconds=1 if expired else 600)
    for version in (1, 2):
        source_plan = source.plan(context, SourceInput(
            source_id=old.bindings[0].source_ids[0], version=version, visibility=visibility,
            purpose="metrics", schema_id="fa-human-text-v1", media_type="text/plain", expires_at=expiry,
        ), ORIGINAL)
        source.commit(context, source_plan)
    plan = migration.plan(principal, migration_id="verify-expired-fixture")
    approval = migration.authorize(
        principal, plan, decision_id="offline-only-approval", expires_at=NOW + timedelta(minutes=5),
    )
    history_key = migration_key(old.table.scope.authority_domain, "history", plan.migration_id)
    history_sha = "a" * 64
    if apply:
        migration.apply(principal, plan, approval)
        history_sha = digest(store.read_snapshot((history_key,))[history_key].payload["data"])
    clock = [NOW + timedelta(seconds=2)]
    permission = ExpiredSourceVerificationPermission(
        permission_id="offline-only-verification", action="verify-expired-source",
        operator_oid=principal.oid, authority_domain=new.table.scope.authority_domain,
        control_binding=new.workers.control_binding, new_configuration_sha256=new.sha256,
        migration_history_key=history_key, migration_history_sha256=history_sha,
        code_sha256=plan.code_sha256, reference=source_plan.reference, purpose="metrics",
        expected_immutable_expiry=expiry,
        not_before=clock[0], expires_at=clock[0] + timedelta(minutes=10),
    )
    return migration, store, table, old, new, principal, permission, clock


def test_exact_expired_private_source_returns_only_safe_proof_without_authority_writes():
    from shared_context.behavior_migration_verification import ExpiredSourceVerification

    _, store, table, _, new, principal, permission, clock = verification_fixture()
    before = copy.deepcopy(table.backing.rows)
    submissions, reads = len(table.submissions), len(table.reads)
    service = ExpiredSourceVerification(store, new, permission, clock=lambda: clock[0])
    assert len(table.reads) == reads
    proof = service.verify(principal)
    assert proof.outcome == "PASS"
    assert proof.observed_code == "SOURCE_EXPIRED"
    assert proof.reference == permission.reference
    assert proof.expected_immutable_expiry == permission.expected_immutable_expiry
    assert proof.migration_history_sha256 == permission.migration_history_sha256
    assert proof.new_configuration_sha256 == new.sha256
    assert proof.activation_performed is False
    assert proof.recovery_authorized is False
    assert ORIGINAL.decode() not in proof.model_dump_json()
    assert base64.b64encode(ORIGINAL).decode() not in proof.model_dump_json()
    assert table.backing.rows == before
    assert len(table.submissions) == submissions


BASE = "/operator/v1/protection-migrations/verify-expired-fixture"
ENDPOINT = BASE + "/verify-expired-source"


def http_client(values, key_client, *, enabled=True):
    from shared_context.behavior_migration_api import create_migration_app

    migration, _, _, old, _, _, permission, clock = values
    migration.clock = lambda: clock[0]
    verifier = BehaviorJwtVerifier(
        old.identity.auth(),
        BehaviorAuthSettings(old.identity.human_oid, old.identity.admin_client_id, old.identity.team_id),
        key_client,
    )
    return TestClient(create_migration_app(
        migration, verifier, expired_source_permission=permission if enabled else None,
    ))


def human_headers(values, signing_key, changed=None, removed=()):
    identity = values[3].identity
    claims = {
        "tid": identity.tenant_id, "iss": f"https://sts.windows.net/{identity.tenant_id}/",
        "aud": identity.audience, "oid": identity.human_oid,
        "appid": identity.admin_client_id, "scp": "Behavior.Read Behavior.Approve",
        **(changed or {}),
    }
    return {"Authorization": "Bearer " + token(signing_key, claims, remove=removed)}


def test_http_verification_is_default_hidden_and_requires_real_signed_human(signing_key, jwk_client):
    values = verification_fixture()
    _, _, table, _, new, _, _, _ = values
    headers = human_headers(values, signing_key)
    disabled = http_client(values, jwk_client, enabled=False)
    assert disabled.post(ENDPOINT, json={}, headers=headers).status_code == 404
    client = http_client(values, jwk_client)
    before = copy.deepcopy(table.backing.rows)
    submissions, reads = len(table.submissions), len(table.reads)
    assert client.post(ENDPOINT, content=b"invalid" * 10000).status_code == 401
    assert len(table.reads) == reads
    response = client.post(ENDPOINT, json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "PASS"
    assert response.json()["observed_code"] == "SOURCE_EXPIRED"
    assert response.json()["new_configuration_sha256"] == new.sha256
    assert response.headers["cache-control"] == "no-store"
    assert ORIGINAL.decode() not in response.text
    assert base64.b64encode(ORIGINAL).decode() not in response.text
    health = client.get("/healthz").json()
    assert health["expired_source_verification"] == "enabled"
    assert health["work_admissions"] == "disabled"
    assert health["migration_apply"] == "disabled"
    assert client.post("/behavior/v1/bindings/arbitrary/work/submit", json={}, headers=headers).status_code == 404
    assert table.backing.rows == before
    assert len(table.submissions) == submissions


@pytest.mark.parametrize("changed,removed", [
    ({"oid": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, ()),
    ({"scp": "Behavior.Read"}, ()),
    ({"scp": "Behavior.Approve"}, ()),
    ({"scp": "Artifacts.Approve Context.Read Behavior.Restore"}, ()),
    ({"appid": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, ()),
    ({"idtyp": "app", "roles": ["Behavior.Agent"]}, ("scp",)),
    ({"tid": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, ()),
    ({"aud": "another-api"}, ()),
    ({"exp": 1}, ()),
])
def test_wrong_jwt_cannot_read_body_or_authority(signing_key, jwk_client, changed, removed):
    values = verification_fixture()
    client = http_client(values, jwk_client)
    table = values[2]
    reads, writes = len(table.reads), len(table.submissions)
    response = client.post(ENDPOINT, content=b"invalid" * 10000, headers={
        **human_headers(values, signing_key, changed, removed), "Content-Type": "application/json",
    })
    assert response.status_code in {401, 403}
    assert len(table.reads) == reads
    assert len(table.submissions) == writes


def test_real_registered_agent_token_and_wrong_signature_cannot_verify(signing_key, jwk_client):
    from cryptography.hazmat.primitives.asymmetric import rsa

    values = verification_fixture()
    client = http_client(values, jwk_client)
    table = values[2]
    agent_oid = next(iter(values[3].identity.auth().actors))
    headers = human_headers(values, signing_key, {
        "oid": agent_oid, "roles": ["Behavior.Agent"], "idtyp": "app",
    }, ("scp",))
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    reads = len(table.reads)
    assert client.post(ENDPOINT, json={}, headers=headers).status_code == 403
    assert client.post(ENDPOINT, json={}, headers=human_headers(values, wrong_key)).status_code == 401
    assert len(table.reads) == reads


@pytest.mark.parametrize("body", [
    {"clock": "2099-01-01T00:00:00Z"}, {"owner": "someone"}, {"owner_actor": "another-human"},
    {"policy": {}}, {"reference": {}}, {"expected_immutable_expiry": "2000-01-01T00:00:00Z"},
    {"not_before": "2000-01-01T00:00:00Z"}, {"expires_at": "2099-01-01T00:00:00Z"},
    {"purpose": "recovery"}, {"action": "restore"}, {"new_configuration_sha256": "a" * 64},
    {"migration_history_key": "any"}, {"operator_oid": "any"}, {"permission": {}},
    {"source_id": "another"}, [], None,
])
def test_body_cannot_override_any_verification_input(signing_key, jwk_client, body):
    values = verification_fixture()
    client = http_client(values, jwk_client)
    table = values[2]
    reads, writes = len(table.reads), len(table.submissions)
    response = client.post(ENDPOINT, content=json.dumps(body),
                           headers={**human_headers(values, signing_key), "Content-Type": "application/json"})
    assert response.status_code == 422
    assert len(table.reads) == reads
    assert len(table.submissions) == writes


def test_query_and_wrong_migration_selector_never_read_authority(signing_key, jwk_client):
    values = verification_fixture()
    client = http_client(values, jwk_client)
    table = values[2]
    reads = len(table.reads)
    headers = human_headers(values, signing_key)
    assert client.post(ENDPOINT + "?clock=2099", json={}, headers=headers).status_code == 422
    assert client.post(ENDPOINT.replace("verify-expired-fixture", "another"), json={}, headers=headers).status_code == 409
    assert len(table.reads) == reads


def with_permission(values, **changes):
    from shared_context.behavior_migration_verification import ExpiredSourceVerificationPermission

    data = values[6].model_dump(mode="json")
    data.update(changes)
    permission = ExpiredSourceVerificationPermission.model_validate_json(json.dumps(data))
    return (*values[:6], permission, values[7])


@pytest.mark.parametrize("changes", [
    {"operator_oid": "another"}, {"authority_domain": "another"},
    {"control_binding": "a" * 64}, {"new_configuration_sha256": "a" * 64},
])
def test_server_rejects_permission_not_bound_to_exact_new_config(jwk_client, changes):
    values = with_permission(verification_fixture(), **changes)
    table = values[2]
    reads, writes = len(table.reads), len(table.submissions)
    with pytest.raises(ValueError):
        http_client(values, jwk_client)
    assert len(table.reads) == reads
    assert len(table.submissions) == writes


@pytest.mark.parametrize("change", [
    {"migration_history_key": "another"},
    {"migration_history_sha256": "a" * 64},
    {"code_sha256": "a" * 64},
])
def test_wrong_history_or_frozen_code_cannot_pass(signing_key, jwk_client, change):
    values = with_permission(verification_fixture(), **change)
    client = http_client(values, jwk_client)
    table = values[2]
    writes = len(table.submissions)
    reads = len(table.reads)
    before = copy.deepcopy(table.backing.rows)
    response = client.post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code == 409
    assert "PASS" not in response.text
    assert table.backing.rows == before
    assert len(table.submissions) == writes
    assert not any(row == "behavior:v1:row:" + values[6].reference.key for _, row in table.reads[reads:])


def test_verification_cannot_bootstrap_an_unapplied_migration(signing_key, jwk_client):
    values = verification_fixture(apply=False)
    client = http_client(values, jwk_client)
    table = values[2]
    before = copy.deepcopy(table.backing.rows)
    writes = len(table.submissions)
    response = client.post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXPIRED_SOURCE_INSTALLATION_MISMATCH"
    assert table.backing.rows == before
    assert len(table.submissions) == writes


@pytest.mark.parametrize("field,value", [
    ("source_id", "not-present"), ("version", 3), ("sha256", "a" * 64), ("byte_length", 48),
    ("scope.tenant_id", "another"), ("scope.team_id", "another"),
    ("scope.owner_actor", "human-another"),
])
def test_reference_or_private_scope_mismatch_is_not_pass(signing_key, jwk_client, field, value):
    values = verification_fixture()
    reference = values[6].reference.model_dump(mode="json")
    if field.startswith("scope."):
        reference["scope"][field.split(".")[1]] = value
    else:
        reference[field] = value
    values = with_permission(values, reference=reference)
    client = http_client(values, jwk_client)
    before = copy.deepcopy(values[2].backing.rows)
    writes = len(values[2].submissions)
    response = client.post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code in {403, 409}
    assert "PASS" not in response.text
    assert values[2].backing.rows == before
    assert len(values[2].submissions) == writes


@pytest.mark.parametrize("change,code", [
    ({"purpose": "recovery"}, "SOURCE_SCOPE_DENIED"),
    ({"expected_immutable_expiry": (NOW + timedelta(seconds=2)).isoformat()}, "EXPIRED_SOURCE_EXPIRY_MISMATCH"),
])
def test_exact_source_purpose_and_immutable_expiry_are_required(signing_key, jwk_client, change, code):
    values = with_permission(verification_fixture(), **change)
    response = http_client(values, jwk_client).post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code in {403, 409}
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("position", ["before", "expires_at", "after"])
def test_permission_window_is_checked_every_request_without_authority_writes(signing_key, jwk_client, position):
    values = verification_fixture()
    client = http_client(values, jwk_client)
    permission, clock = values[6:]
    clock[0] = {
        "before": permission.not_before - timedelta(microseconds=1),
        "expires_at": permission.expires_at,
        "after": permission.expires_at + timedelta(microseconds=1),
    }[position]
    reads, writes = len(values[2].reads), len(values[2].submissions)
    response = client.post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXPIRED_SOURCE_PERMISSION_INACTIVE"
    assert len(values[2].reads) == reads
    assert len(values[2].submissions) == writes


def test_unexpired_source_never_passes_or_returns_any_readable_original(signing_key, jwk_client):
    values = verification_fixture(expired=False)
    before = copy.deepcopy(values[2].backing.rows)
    writes = len(values[2].submissions)
    response = http_client(values, jwk_client).post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXPIRED_SOURCE_NOT_EXPIRED"
    assert ORIGINAL.decode() not in response.text
    assert base64.b64encode(ORIGINAL).decode() not in response.text
    assert "PASS" not in response.text
    assert values[2].backing.rows == before
    assert len(values[2].submissions) == writes


def test_withdrawn_expired_source_is_not_pass(signing_key, jwk_client):
    values = verification_fixture()
    _, store, table, _, _, principal, permission, clock = values
    context = AuthenticatedSourceContext(
        principal.tenant_id, principal.team, principal.source_actor_id, "withdraw-fixture", ("metrics",),
    )
    SourceMaterials(store, clock=lambda: clock[0]).withdraw_source(principal, context, SourceWithdrawalInput(
        withdrawal_id="offline-withdrawal", reference=permission.reference, expected_access_revision=1,
        reason="offline denial fixture",
    ))
    before = copy.deepcopy(table.backing.rows)
    writes = len(table.submissions)
    response = http_client(values, jwk_client).post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SOURCE_WITHDRAWN"
    assert table.backing.rows == before
    assert len(table.submissions) == writes


def test_old_expired_RuntimePolicy_stays_unchanged_while_independent_verification_passes(signing_key, jwk_client):
    values = verification_fixture()
    expiry = max(item.policy.valid_until for item in values[3].policies)
    values[7][0] = expiry + timedelta(seconds=1)
    values = with_permission(values, not_before=values[7][0].isoformat(),
                             expires_at=(values[7][0] + timedelta(minutes=10)).isoformat())
    before = copy.deepcopy(values[2].backing.rows)
    writes = len(values[2].submissions)
    client = http_client(values, jwk_client)
    response = client.post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "PASS"
    assert client.get("/healthz").json()["work_admissions"] == "disabled"
    assert values[2].backing.rows == before
    assert len(values[2].submissions) == writes


@pytest.mark.parametrize("seconds", [0, -1, 1201])
def test_server_permission_rejects_nonpositive_or_overlong_ttl(seconds):
    values = verification_fixture()
    with pytest.raises(ValidationError):
        with_permission(values, expires_at=(values[6].not_before + timedelta(seconds=seconds)).isoformat())


@pytest.mark.parametrize("field,value", [
    ("action", "restore"), ("schema_version", "unknown"), ("unknown_policy", {}),
    ("not_before", "2026-09-16T01:00:02"),
    ("expires_at", "2026-09-16T09:10:02+08:00"),
    ("expected_immutable_expiry", "2026-09-16T01:00:01"),
])
def test_server_permission_rejects_unknown_fields_actions_versions_and_non_utc_times(field, value):
    with pytest.raises(ValidationError):
        with_permission(verification_fixture(), **{field: value})


@pytest.mark.parametrize("record", ["source", "head", "source-receipt", "history", "current"])
def test_missing_exact_required_record_is_not_pass(signing_key, jwk_client, record):
    values = verification_fixture()
    _, store, table, _, _, _, permission, _ = values
    source = store.read_snapshot((permission.reference.key,))[permission.reference.key].payload["data"]
    row = {
        "source": "behavior:v1:row:" + permission.reference.key,
        "head": "behavior:v1:row:" + permission.reference.head_key,
        "source-receipt": "behavior:v1:receipt:" + source["request"]["request_id"],
        "history": "behavior:v1:row:" + permission.migration_history_key,
        "current": "behavior:v1:row:" + migration_key(permission.authority_domain, "current"),
    }[record]
    del table.backing.rows[(table.table_name, store._scope.partition_key, row)]
    before = copy.deepcopy(table.backing.rows)
    writes = len(table.submissions)
    response = http_client(values, jwk_client).post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code in {409, 502, 503}
    assert "PASS" not in response.text
    assert table.backing.rows == before
    assert len(table.submissions) == writes


@pytest.mark.parametrize("record", ["installation", "current", "history", "guard", "expiry"])
def test_tampered_authority_cannot_claim_exact_immutable_expiry(signing_key, jwk_client, record):
    values = verification_fixture()
    _, store, table, _, new, _, permission, _ = values
    row = {
        "installation": "behavior:v1:row:" + installation_record(new)[0],
        "current": "behavior:v1:row:" + migration_key(permission.authority_domain, "current"),
        "history": "behavior:v1:row:" + permission.migration_history_key,
        "guard": GUARD_KEY,
        "expiry": "behavior:v1:row:" + permission.reference.key,
    }[record]
    entity = table.backing.rows[(table.table_name, store._scope.partition_key, row)]
    payload = json.loads(entity["Payload"])
    if record in {"installation", "current"}:
        payload["data"]["configuration_sha256"] = "a" * 64
    elif record == "guard":
        payload["configuration_sha256"] = "a" * 64
    elif record == "history":
        payload["data"]["plan"]["operator_oid"] = "another"
    else:
        expiry = NOW.isoformat()
        payload["data"]["expires_at"] = expiry
        values = with_permission(values, expected_immutable_expiry=expiry)
    entity["Payload"] = json.dumps(payload, separators=(",", ":")).encode()
    before = copy.deepcopy(table.backing.rows)
    writes = len(table.submissions)
    response = http_client(values, jwk_client).post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code in {409, 502, 503}
    assert "PASS" not in response.text
    assert table.backing.rows == before
    assert len(table.submissions) == writes


@pytest.mark.parametrize("error", [
    RuntimeError(ORIGINAL.decode()),
    Problem(503, "UPSTREAM_ERROR", ORIGINAL.decode(), raw_body=ORIGINAL.decode()),
    Problem(409, "SOURCE_EXPIRED", ORIGINAL.decode()),
])
def test_unknown_table_error_cannot_leak_bytes_or_fake_a_source_expiry_pass(
    signing_key, jwk_client, monkeypatch, error,
):
    values = verification_fixture()
    client = http_client(values, jwk_client)
    table = values[2]
    before = copy.deepcopy(table.backing.rows)
    writes = len(table.submissions)

    def unavailable(**_):
        raise error

    monkeypatch.setattr(table, "get_entity", unavailable)
    response = client.post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code in {409, 503}
    assert "PASS" not in response.text
    assert ORIGINAL.decode() not in response.text
    assert base64.b64encode(ORIGINAL).decode() not in response.text
    assert table.backing.rows == before
    assert len(table.submissions) == writes


def test_permission_expiring_during_authority_io_cannot_pass(signing_key, jwk_client):
    values = verification_fixture()
    client = http_client(values, jwk_client)
    table, permission, clock = values[2], values[6], values[7]
    before = copy.deepcopy(table.backing.rows)
    writes = len(table.submissions)
    table.after_get = lambda _: clock.__setitem__(0, permission.expires_at)
    response = client.post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXPIRED_SOURCE_PERMISSION_INACTIVE"
    assert table.backing.rows == before
    assert len(table.submissions) == writes


def test_actual_changed_frozen_module_digest_cannot_pass(signing_key, jwk_client, monkeypatch):
    values = verification_fixture()
    client = http_client(values, jwk_client)
    original_read = Path.read_bytes

    def changed_code(path):
        if path.name == "behavior_migration_verification.py":
            return b"different installed frozen verification code"
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", changed_code)
    response = client.post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXPIRED_SOURCE_INSTALLATION_MISMATCH"


def test_separate_readonly_process_uses_fresh_expected_installation_without_constructor_io():
    from shared_context.behavior_migration import MigrationStore
    from shared_context.behavior_migration_verification import ExpiredSourceVerification

    _, _, table, _, new, principal, permission, clock = verification_fixture()
    before = copy.deepcopy(table.backing.rows)
    reads, writes = len(table.reads), len(table.submissions)
    store = MigrationStore(table, new.table.scope, expected_installation=(installation_record(new)[0], new.sha256))
    service = ExpiredSourceVerification(store, new, permission, clock=lambda: clock[0])
    assert len(table.reads) == reads
    assert service.verify(principal).outcome == "PASS"
    assert service.verify(principal).outcome == "PASS"
    clock[0] = permission.expires_at
    with pytest.raises(Problem) as inactive:
        service.verify(principal)
    assert inactive.value.body["error"]["code"] == "EXPIRED_SOURCE_PERMISSION_INACTIVE"
    assert table.backing.rows == before
    assert len(table.submissions) == writes


def test_cli_emits_only_unusable_shape_example_without_starting_server(monkeypatch, capsys):
    from shared_context import behavior_migration_api
    from shared_context.behavior_migration_verification import ExpiredSourceVerificationPermission

    monkeypatch.setattr("sys.argv", ["operator", "--print-expired-source-permission-example"])
    behavior_migration_api.main()
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "verify-expired-source"
    assert set(output) == set(ExpiredSourceVerificationPermission.model_fields)
    with pytest.raises(ValidationError):
        ExpiredSourceVerificationPermission.model_validate_json(json.dumps(output))


@pytest.mark.parametrize("enabled", [False, True])
def test_operator_factory_loads_optional_server_file_with_same_table_and_no_constructor_io(
    monkeypatch, enabled,
):
    from shared_context.behavior_migration_api import operator_application

    values = verification_fixture()
    migration, _, table, old, new, _, permission, _ = values
    documents = {
        "old.json": old.model_dump_json().encode(),
        "new.json": new.model_dump_json().encode(),
        "old-evidence.json": json.dumps({
            lane: value.model_dump(mode="json") for lane, value in migration.previous_observations.items()
        }).encode(),
        "sources.json": migration.observations["sources"].model_dump_json().encode(),
        "backups.json": migration.observations["backups"].model_dump_json().encode(),
        "permission.json": permission.model_dump_json().encode(),
    }
    original_read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: documents[path.name] if path.name in documents else original_read(path))
    credentials, table_clients = [], []

    def credential(**kwargs):
        credentials.append(kwargs)
        return nullcontext("offline-credential")

    def table_client(endpoint, name, **kwargs):
        table_clients.append((endpoint, name, kwargs))
        return nullcontext(table)

    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", credential)
    monkeypatch.setattr("azure.data.tables.TableClient", table_client)
    reads, writes = len(table.reads), len(table.submissions)
    app = operator_application(
        Path("old.json"), Path("new.json"), Path("old-evidence.json"), Path("sources.json"), Path("backups.json"),
        expired_source_permission=Path("permission.json") if enabled else None,
    )
    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["expired_source_verification"] == ("enabled" if enabled else "disabled")
        assert health["migration_apply"] == "disabled"
        assert client.post(ENDPOINT, json={}).status_code == (401 if enabled else 404)
    assert len(credentials) == len(table_clients) == 1
    assert table_clients[0][0:2] == (old.table.endpoint, old.table.scope.table_name)
    assert len(table.reads) == reads
    assert len(table.submissions) == writes


def test_cli_forwards_explicit_permission_path_and_keeps_apply_default_off(monkeypatch):
    from shared_context import behavior_migration_api

    captured = {}
    sentinel = object()

    def application(*paths, **kwargs):
        captured.update(paths=paths, **kwargs)
        return sentinel

    monkeypatch.setattr(behavior_migration_api, "operator_application", application)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app, server=kwargs))
    monkeypatch.setattr("sys.argv", [
        "operator", "--old-config", "old.json", "--new-config", "new.json", "--old-evidence", "previous.json",
        "--sources", "sources.json", "--backups", "backups.json",
        "--expired-source-permission", "server-only-permission.json",
    ])
    behavior_migration_api.main()
    assert captured["expired_source_permission"] == Path("server-only-permission.json")
    assert captured["allow_apply"] is False
    assert captured["app"] is sentinel
    assert captured["server"]["access_log"] is False


def test_maximum_twenty_minute_permission_and_team_scope_are_supported(signing_key, jwk_client):
    values = verification_fixture(visibility="team")
    values = with_permission(values, expires_at=(values[6].not_before + timedelta(minutes=20)).isoformat())
    response = http_client(values, jwk_client).post(ENDPOINT, json={}, headers=human_headers(values, signing_key))
    assert response.status_code == 200
    assert response.json()["outcome"] == "PASS"
    assert response.json()["reference"]["scope"]["visibility"] == "team"
