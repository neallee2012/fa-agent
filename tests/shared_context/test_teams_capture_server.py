"""OFFLINE migrated authority + real Table SDK/ASGI; never native initialization."""
import json
import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_server_config import ServerConfiguration, synthetic_configuration
from shared_context.teams_capture_server import build_capture_server, capture_application, load_capture_configurations
from shared_context.behavior_migration import installation_record, migration_key, protection_keys
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from shared_context.errors import Problem
from shared_context.teams_deployment import HOST_RESOURCE_ID, INTENDED_USER, RESOURCE_GROUP, SUBSCRIPTION, TENANT
from shared_context.source_materials import digest
from shared_context.teams_personal import ROUTE_HANDSHAKE
from test_behavior_migration import fixture as migration_fixture
from test_behavior_server import NOW as MIGRATION_NOW
from test_behavior_store import Credential, WireTransport
from test_teams_deployment import CLIENT, config as personal_config
from test_teams_identity import NOW, activity, identity, token


def server_config():
    """Configure the existing migration fixture before it creates any test rows."""
    raw = synthetic_configuration().model_dump(mode="json")
    old_tenant = raw["identity"]["tenant_id"]
    raw["identity"].update(tenant_id=TENANT, human_oid=INTENDED_USER, managed_identity_client_id=CLIENT)
    prefix = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers/"
    for name in ("table", "sources", "backups"):
        raw[name]["account_resource_id"] = prefix + "Microsoft.Storage/storageAccounts/synthetic"
    for name in ("sources", "backups"):
        raw[name]["protection"]["protected_prefix"] = raw[name]["protection"]["protected_prefix"].replace(old_tenant, TENANT)
    raw["api_resource_id"] = HOST_RESOURCE_ID
    raw["controller_resource_id"] = prefix + "Microsoft.App/containerApps/controller"
    for binding in raw["bindings"]:
        binding.update(tenant_id=TENANT, human_oid=INTENDED_USER)
        binding["route"].update(tenant_id=TENANT, recipient_id=INTENDED_USER)
    for policy, binding in zip(raw["policies"], raw["bindings"]):
        policy["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", binding])
    return ServerConfiguration.model_validate_json(json.dumps(raw))


def fixture(*, applied=True):
    # Override only another TEST fixture's inputs, never production verifiers.
    with patch("test_behavior_migration.synthetic_configuration", return_value=server_config()):
        service, operator, table, old, new, principal = migration_fixture()
    plan = service.plan(principal, migration_id="teams-capture-installation")
    approval = service.authorize(principal, plan, decision_id="capture-installation-approved",
                                 expires_at=MIGRATION_NOW + timedelta(minutes=5))
    if applied:
        service.apply(principal, plan, approval)
    wire = WireTransport()
    wire.backing = table.backing
    composition = build_capture_server(
        new, personal_config(), credential=Credential(), table_transport=wire,
        jwks_transport=identity().jwks_transport, clock=lambda: NOW,
    )
    return composition, wire, operator, table, old, new


def test_capture_server_reads_existing_migration_and_exposes_only_capture_and_health():
    composition, wire, _, _, _, _ = fixture()
    assert wire.requests == []  # construction cannot bootstrap or probe authority
    with TestClient(capture_application(composition)) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["mode"] == "capture_only"
        assert health.json()["work_enabled"] is False
        assert health.json()["approval_enabled"] is False
        assert health.json()["model_enabled"] is False
        assert health.json()["sender_enabled"] is False
        assert all(request.method == "GET" for request in wire.requests)
        for path in ("/openapi.json", "/docs", "/behavior/v1/teams/team-one/work",
                     "/behavior/v1/teams/team-one/approve", "/behavior/v1/consume"):
            assert client.post(path, json={}).status_code == 404
        assert client.post("/behavior/v1/teams/activities", json=activity(),
                           headers={"Authorization": "Bearer " + token()}).status_code == 403
        captured = client.post("/behavior/v1/teams/activities", json=activity(text=ROUTE_HANDSHAKE),
                               headers={"Authorization": "Bearer " + token()})
        assert captured.status_code == 200, captured.text
        assert captured.json()["work_admitted"] is False
        assert captured.json()["native_gate"] == "closed"
        assert len(wire.parts) == 1
        assert len(wire.parts[0]) == 4  # guard, receipt, route, installation binding


def test_public_poc_notices_do_not_change_authority_or_enable_work():
    from shared_context.teams_notices import NOTICES

    composition, wire, _, _, _, _ = fixture()
    with TestClient(capture_application(composition)) as client:
        before = len(wire.requests)
        for path, content in NOTICES.items():
            response = client.get(path)
            assert response.status_code == 200
            assert response.text == content
            assert response.headers["content-type"].startswith("text/plain")
        assert len(wire.requests) == before
        assert not wire.parts


def change(operator, key, updates):
    def decide(rows):
        data = rows[key].payload
        material = None if data["material"] is None else InlineMaterial.model_validate_json(json.dumps(data["material"]))
        return ControlChanges(writes=(ControlWrite(
            row_id=key, mode="replace", data={**data["data"], **updates}, material=material,
        ),), result={})
    return operator.transact(ControlRequest(request_id=control_key("operator-change", key),
                                            request_digest=digest(updates), read_set=(key,)), decide)


def test_plan_and_authorization_without_apply_cannot_start_or_write():
    composition, wire, _, _, _, _ = fixture(applied=False)
    with pytest.raises(Problem) as blocked:
        with TestClient(capture_application(composition)):
            raise AssertionError("unapplied migration started")
    assert blocked.value.body["error"]["code"] == "TEAMS_CAPTURE_MIGRATION_REQUIRED"
    assert not wire.parts
    assert composition.closed


@pytest.mark.parametrize("part", ["installation", "current", "history", "authorization", "sources", "backups"])
def test_startup_rejects_different_or_corrupt_migrated_installation_rows(part):
    composition, wire, operator, _, _, current = fixture()
    pointer_key = migration_key(current.table.scope.authority_domain, "current")
    pointer = operator.read_snapshot((pointer_key,))[pointer_key].payload["data"]
    key, update = {
        "installation": (installation_record(current)[0], {"configuration_sha256": "f" * 64}),
        "current": (pointer_key, {"history_sha256": "f" * 64}),
        "history": (pointer["history_key"], {"authorization_sha256": "f" * 64}),
        "authorization": (pointer["authorization_key"], {"operator_oid": "ffffffff-ffff-ffff-ffff-ffffffffffff"}),
        "sources": (protection_keys(current)[0], {"evidence_sha256": "f" * 64}),
        "backups": (protection_keys(current)[1], {"evidence_sha256": "f" * 64}),
    }[part]
    change(operator, key, update)
    with pytest.raises(Problem):
        with TestClient(capture_application(composition)):
            raise AssertionError("invalid installation started")
    assert not wire.parts


def test_current_installation_is_rechecked_on_health_and_before_capture():
    composition, wire, operator, _, _, current = fixture()
    with TestClient(capture_application(composition)) as client:
        assert client.get("/healthz").status_code == 200
        change(operator, installation_record(current)[0], {"configuration_sha256": "f" * 64})
        assert client.get("/healthz").status_code == 503
        result = client.post("/behavior/v1/teams/activities", json=activity(text=ROUTE_HANDSHAKE),
                             headers={"Authorization": "Bearer " + token()})
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "MIGRATION_INSTALLATION_CHANGED"
        assert not wire.parts


def test_guard_rejects_route_write_if_installation_changes_after_final_check():
    composition, wire, operator, _, _, current = fixture()
    send = wire.send
    raced = False
    def race(request, **kwargs):
        nonlocal raced
        if request.method == "POST" and not raced:
            raced = True
            change(operator, installation_record(current)[0], {"configuration_sha256": "f" * 64})
        return send(request, **kwargs)
    with TestClient(capture_application(composition)) as client:
        wire.send = race
        result = client.post("/behavior/v1/teams/activities", json=activity(text=ROUTE_HANDSHAKE),
                             headers={"Authorization": "Bearer " + token()})
        assert result.status_code != 200
        rows = operator.read_snapshot((composition.store.route_key, composition.store.binding_key))
        assert all(row is None for row in rows.values())
        assert raced


def test_route_binding_is_atomic_and_replayed_without_another_write_after_restart():
    composition, wire, _, _, _, current = fixture()
    body = activity(text=ROUTE_HANDSHAKE)
    with TestClient(capture_application(composition)) as client:
        captured = client.post("/behavior/v1/teams/activities", json=body,
                               headers={"Authorization": "Bearer " + token()})
        assert captured.status_code == 200
    restarted = build_capture_server(
        current, personal_config(), credential=Credential(), table_transport=wire,
        jwks_transport=identity().jwks_transport, clock=lambda: NOW,
    )
    with TestClient(capture_application(restarted)) as client:
        assert client.get("/healthz").status_code == 200
        replay = client.post("/behavior/v1/teams/activities", json=body,
                             headers={"Authorization": "Bearer " + token()})
        assert replay.json() == captured.json()
    assert len(wire.parts) == 1


def test_capture_cannot_bootstrap_mutate_protected_rows_or_attach_work_runtime():
    composition, wire, _, _, _, current = fixture()
    with TestClient(capture_application(composition)) as client:
        with pytest.raises(Problem):
            composition.store.initialize()
        for key in (installation_record(current)[0], *protection_keys(current)):
            with pytest.raises(Problem):
                composition.store.transact(ControlRequest(request_id="not-a-route", request_digest="f" * 64),
                                           lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="create", data={}),), result={}))
        with pytest.raises(Problem):
            composition.entry.attach_runtime(object())
        assert client.get("/healthz").json()["work_enabled"] is False
        assert not wire.parts


def test_capture_configuration_uses_exact_server_owned_file_or_json_not_overrides(tmp_path):
    current = server_config()
    personal = personal_config()
    server_file, personal_file = tmp_path / "server.json", tmp_path / "personal.json"
    server_file.write_text(current.model_dump_json())
    personal_file.write_text(personal.model_dump_json())
    pair = load_capture_configurations(environ={
        "BEHAVIOR_SERVER_CONFIG": str(server_file), "TEAMS_PERSONAL_CONFIG": str(personal_file),
        "BEHAVIOR_ADMISSION_ENABLED": "true", "TEAMS_NATIVE_ENABLED": "true",
    })
    assert pair == (current, personal)
    with pytest.raises(Problem):
        load_capture_configurations(environ={
            "BEHAVIOR_SERVER_CONFIG": str(server_file), "TEAMS_PERSONAL_CONFIG": str(personal_file),
            "TEAMS_PERSONAL_CONFIG_JSON": personal.model_dump_json(),
        })


def test_capture_import_and_help_do_not_load_builder_only_dependencies():
    from shared_context import teams_capture_server
    package_root = Path(teams_capture_server.__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(package_root)}
    script = """
import importlib.abc, sys
class NoBuilder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'PIL', 'jsonschema'}:
            raise AssertionError('builder dependency imported')
sys.meta_path.insert(0, NoBuilder())
from shared_context.teams_capture_server import main
main(['--help'])
"""
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "serve" in result.stdout
    assert "bootstrap" not in result.stdout


def test_capture_import_does_not_require_repository_ancestors_in_container():
    from shared_context import teams_capture_server
    package_root = Path(teams_capture_server.__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(package_root)}
    script = """
from pathlib import Path, PurePosixPath
original = Path.resolve
def deployed_path(self, *args, **kwargs):
    if self.name == 'teams_deployment.py':
        return PurePosixPath('/app/shared_context/teams_deployment.py')
    return original(self, *args, **kwargs)
Path.resolve = deployed_path
from shared_context.teams_capture_server import main
main(['--help'])
"""
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "serve" in result.stdout


def test_rejection_diagnostics_never_log_text_or_bearer_credentials(caplog):
    composition, _, _, _, _, _ = fixture()
    secret = "SYNTHETIC_PRIVATE_TEXT_DO_NOT_LOG"
    bearer = "Bearer SYNTHETIC_PRIVATE_BEARER_DO_NOT_LOG"
    with TestClient(capture_application(composition)) as client, caplog.at_level(logging.WARNING):
        response = client.post("/behavior/v1/teams/activities", json=activity(text=secret),
                               headers={"Authorization": bearer})
    assert response.status_code == 403
    assert "DEBUG-teams-capture-0917" in caplog.text
    assert "TEAMS_PERSONAL_NATIVE_CLOSED" in caplog.text
    assert secret not in caplog.text and bearer not in caplog.text


def test_rejection_diagnostics_distinguish_passive_client_entities(caplog):
    composition, _, _, _, _, _ = fixture()
    body = activity(text=ROUTE_HANDSHAKE)
    body["entities"] = [{"type": "clientInfo", "locale": "zh-TW", "country": "TW", "platform": "Web",
                         "unrecognized_extension": "rejected"}]
    with TestClient(capture_application(composition)) as client, caplog.at_level(logging.WARNING):
        response = client.post("/behavior/v1/teams/activities", json=body,
                               headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 422
    assert "TEAMS_CONTENT_UNSUPPORTED" in caplog.text
    assert '"text_matches_handshake": true' in caplog.text
    assert '"client_info_entities": 1' in caplog.text
    assert body["text"] not in caplog.text
