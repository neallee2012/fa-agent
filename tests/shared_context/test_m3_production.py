from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from conftest import A, AUDIENCE, TENANT
from shared_context.m3_config import M3Settings
from shared_context.production import create_production_app
from test_m3_artifacts import HUMAN, SETTINGS


def env(monkeypatch):
    values = {
        "CONTEXT_TENANT_ID": TENANT, "CONTEXT_API_AUDIENCE": AUDIENCE,
        "CONTEXT_ACTORS_JSON": json.dumps({A.oid: {"actor": A.actor, "team": A.team, "role_instruction": A.role_instruction}}),
        "CONTEXT_TABLE_ENDPOINT": "https://synthetic.table.core.windows.net", "CONTEXT_TABLE_NAME": "workstate",
        "CONTEXT_MEMORY_ENDPOINT": "https://synthetic.services.ai.azure.com/api/projects/poc",
        "CONTEXT_MEMORY_STORE": "synthetic-memory",
        "CONTEXT_M3_BLOB_ENDPOINT": "https://synthetic.blob.core.windows.net",
        "CONTEXT_M3_CONTAINER": "m3-artifacts", "CONTEXT_M3_APPROVER_OID": HUMAN.oid,
        "CONTEXT_M3_CLIENT_ID": SETTINGS.client_id, "CONTEXT_M3_TEAM": A.team,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in ("CONTEXT_BACKUP_BLOB_ENDPOINT", "CONTEXT_BACKUP_CONTAINER", "CONTEXT_M3_POLICY_ARTIFACTS",
                 "CONTEXT_M2_RUN_ID", "CONTEXT_M2_MODEL", "CONTEXT_M2_MAX_REQUESTS", "CONTEXT_M2_ADDITIONAL_REQUESTS",
                 "CONTEXT_M2_AMENDMENT_ID", "CONTEXT_M3_RUN_ID", "CONTEXT_M3_MODEL", "CONTEXT_M3_MAX_REQUESTS"):
        monkeypatch.delenv(name, raising=False)
    return values


def test_production_wires_separate_artifact_container_and_closes_owned_clients(monkeypatch):
    env(monkeypatch)
    credential, table, project, container = Mock(), Mock(), Mock(), Mock()
    container.account_url = "https://synthetic.blob.core.windows.net"
    container.container_name = "m3-artifacts"
    containers = Mock(return_value=container)
    monkeypatch.setattr("shared_context.production.ManagedIdentityCredential", Mock(return_value=credential))
    monkeypatch.setattr("shared_context.production.TableClient", Mock(return_value=table))
    monkeypatch.setattr("shared_context.production.AIProjectClient", Mock(return_value=project))
    monkeypatch.setattr("azure.storage.blob.ContainerClient", containers)
    with TestClient(create_production_app()) as http:
        assert http.get("/healthz").status_code == 200
        assert http.get("/teams/team-one/artifacts/report").status_code == 401
        assert http.get("/m3/model/v1/budget").status_code == 404
    arguments = containers.call_args.kwargs
    assert arguments["credential"] is credential
    assert arguments["container_name"] == "m3-artifacts"
    assert arguments["retry_total"] == arguments["retry_connect"] == arguments["retry_read"] == arguments["retry_status"] == 0
    assert arguments["max_single_put_size"] == 512 * 1024
    for resource in (credential, table, project, container):
        resource.close.assert_called_once()
    container.create_container.assert_not_called()


def test_shared_m1_container_is_rejected_before_creating_credentials(monkeypatch):
    values = env(monkeypatch)
    monkeypatch.setenv("CONTEXT_BACKUP_BLOB_ENDPOINT", values["CONTEXT_M3_BLOB_ENDPOINT"])
    monkeypatch.setenv("CONTEXT_BACKUP_CONTAINER", values["CONTEXT_M3_CONTAINER"])
    credential = Mock()
    monkeypatch.setattr("shared_context.production.ManagedIdentityCredential", credential)
    with pytest.raises(ValueError, match="separate"):
        create_production_app()
    credential.assert_not_called()


@pytest.mark.parametrize("policies", ["a,a", "a,b,c,d,e", "../escape", "a, b"])
def test_server_policy_selection_rejects_duplicates_extra_items_or_invalid_ids(policies):
    values = {
        "CONTEXT_M3_BLOB_ENDPOINT": "https://synthetic.blob.core.windows.net",
        "CONTEXT_M3_CONTAINER": "m3-artifacts", "CONTEXT_M3_APPROVER_OID": HUMAN.oid,
        "CONTEXT_M3_CLIENT_ID": SETTINGS.client_id, "CONTEXT_M3_TEAM": A.team,
        "CONTEXT_M3_POLICY_ARTIFACTS": policies,
    }
    with pytest.raises(ValueError):
        M3Settings.from_env(values)
