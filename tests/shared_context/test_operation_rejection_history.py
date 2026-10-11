"""Real API-produced rows remain classifiable; all native transports are local doubles."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from shared_context import behavior_wire as wire
from shared_context.behavior_migration import _quiescence
from shared_context.behavior_runtime_rotation_history import validate_additions
from shared_context.behavior_server import application
from shared_context.errors import Problem
from test_behavior_rotation_settlement_history import page_inventory
from test_behavior_server import Harness, workspace
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def produced_rejection(workspace, signing_key, jwk_client, monkeypatch):
    h = Harness(workspace, jwk_client)
    try:
        inventory_table = SimpleNamespace(
            backing=h.table.backing, table_name=h.config.table.scope.table_name, url=h.config.table.endpoint,
        )
        page_inventory(inventory_table)
        monkeypatch.setattr(h.composition.clients[0], "query_entities", inventory_table.query_entities)
        h.activate()
        baseline = h.runtime.store.read_inventory()
        binding = h.config.bindings[0]
        alias = next(item for item in h.config.operations if item.binding_id == binding.binding_id)
        path = "/behavior/v1/bindings/" + binding.binding_id
        headers = {**h.headers(signing_key, "Behavior.Read Behavior.Approve"), "Content-Type": "application/json"}
        with TestClient(application(h.composition)) as client:
            accepted = client.post(path + "/work/submit",
                content=wire.SubmitWork(request_id="history-work").model_dump_json(), headers=headers)
            assert accepted.status_code == 200
            prepared = client.post(path + "/operation/prepare",
                content=wire.OperationPrepare(request_id="history-prepare", input_ref=alias.input_ref).model_dump_json(),
                headers=headers)
            assert prepared.status_code == 200, prepared.text
            result = prepared.json()["result"]
            with pytest.raises(Problem) as pending_before:
                _quiescence(h.runtime.store.read_inventory(), h.config)
            pending_code = pending_before.value.body["error"]["code"]
            assert pending_code in {"MIGRATION_NOT_QUIESCENT", "MIGRATION_DELIVERY_PENDING"}
            rejected = client.post(path + "/operation/reject", headers=headers, json={
                "schema_version": "fa-operation-rejection-v1",
                "request_id": "history-reject", "decision_id": "history-decision",
                "description_digest": result["description_digest"],
                "adapter_contract_digest": result["adapter_contract_digest"],
            })
            assert rejected.status_code == 200, rejected.text
            assert h.calls == []
            yield h, baseline, h.runtime.store.read_inventory(), pending_code
    finally:
        h.composition.close()


@pytest.mark.parametrize("reader", ["rotation-additions", "migration-quiescence"])
def test_actual_rejection_is_classified_without_claiming_pending_work_is_quiescent(produced_rejection, reader):
    h, baseline, inventory, pending_code = produced_rejection
    before = dict(inventory.entity_sha256)
    with patch.object(h.runtime.store, "transact", side_effect=AssertionError("History reads must not write")):
        if reader == "rotation-additions":
            validate_additions(inventory, h.config, h.config, set(baseline.rows), set())
        else:
            with pytest.raises(Problem) as pending:
                _quiescence(inventory, h.config)
            assert pending.value.body["error"]["code"] == pending_code
    assert dict(h.runtime.store.read_inventory().entity_sha256) == before
    assert h.calls == []
