"""Guarded complete-inventory/fencing seam; native Table is an offline adapter."""

import copy
import base64
import json
import logging
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from azure.data.tables import TableClient

from shared_context.behavior_store import BehaviorStore as BaseStore, ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_effects import _DeadlineClient
from shared_context import behavior_migration as migration
from shared_context.behavior_migration import MigrationStore as BehaviorStore
from shared_context.behavior_migration import DEFAULT_INVENTORY_MAX_BYTES, DEFAULT_INVENTORY_MAX_ENTITIES
from shared_context.errors import Problem
from test_behavior_store import Credential, FakeTable, SCOPE, WireTransport, json_response


class InventoryTable(FakeTable):
    after_page = None
    page_size = 2

    def query_entities(self, *, query_filter, parameters, raw_request_hook, **kwargs):
        assert query_filter == "PartitionKey eq @partition"
        assert parameters == {"partition": SCOPE.partition_key}
        assert kwargs["retry_total"] == 0
        table = self

        class Pages:
            def by_page(self):
                values = [copy.deepcopy(value) for (name, partition, _), value in sorted(table.backing.rows.items())
                          if name == table.table_name and partition == parameters["partition"]]
                for start in range(0, len(values), table.page_size):
                    raw_request_hook(SimpleNamespace(http_request=SimpleNamespace(
                        method="GET", url=table.url + "/Behavior()", body=None,
                    )))
                    yield iter(values[start:start + table.page_size])
                    if table.after_page:
                        callback, table.after_page = table.after_page, None
                        callback()
        return Pages()


def store_with_row():
    table = InventoryTable()
    store = BehaviorStore(table, SCOPE)
    store.initialize()
    request = ControlRequest(request_id="seed", request_digest="a" * 64)
    store.transact(request, lambda _: ControlChanges(
        writes=(ControlWrite(row_id="budget", mode="create", data={"remaining": 7}),), result={},
    ))
    return store, table


def test_complete_inventory_retains_rows_receipts_and_exact_guard_without_writing(caplog):
    store, table = store_with_row()
    before = len(table.submissions)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        inventory = store.read_inventory()
    assert not any(record.message.startswith("BEHAVIOR_INVENTORY_READ ") for record in caplog.records)
    assert inventory.scope == SCOPE
    assert inventory.rows["budget"].payload["data"] == {"remaining": 7}
    assert inventory.receipts["seed"].payload["request"]["request_id"] == "seed"
    assert inventory.guard.payload["revision"] == 1
    assert len(inventory.entity_sha256) == 3
    assert len(table.submissions) == before


@pytest.mark.parametrize("max_entities,reference_entities", [
    (3, DEFAULT_INVENTORY_MAX_ENTITIES), (None, DEFAULT_INVENTORY_MAX_ENTITIES), (None, 2),
])
def test_completed_inventory_logs_numeric_capacity_once(caplog, monkeypatch, max_entities, reference_entities):
    monkeypatch.setattr(migration, "DEFAULT_INVENTORY_MAX_ENTITIES", reference_entities)
    store, table = store_with_row()
    before = copy.deepcopy(table.backing.rows)
    size = sum(len(entity["Payload"]) for entity in before.values())
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        inventory = store.read_inventory(max_entities=max_entities, log_capacity=True)
    records = [record for record in caplog.records if record.message.startswith("BEHAVIOR_INVENTORY_READ ")]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == (
        f"BEHAVIOR_INVENTORY_READ bytes={size} entities=3 "
        f"bytes_pct_of_default={100 * size / DEFAULT_INVENTORY_MAX_BYTES:.2f} "
        f"entities_pct_of_default={100 * 3 / reference_entities:.2f}"
    )
    assert len(inventory.entity_sha256) == 3
    assert table.backing.rows == before


def test_historical_snapshot_does_not_repeat_live_inventory_log(caplog):
    from shared_context.behavior_runtime_rotation import _snapshot_store

    store, _ = store_with_row()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        live = store.read_inventory(log_capacity=True)
        replay = _snapshot_store(store, live, SimpleNamespace(sha256="b" * 64)).read_inventory()
    assert replay.rows == live.rows and replay.receipts == live.receipts
    records = [record for record in caplog.records if record.message.startswith("BEHAVIOR_INVENTORY_READ ")]
    assert len(records) == 1


@pytest.mark.parametrize("limits", [{"max_entities": 1}, {"max_bytes": 1}, {"max_pages": 1}])
def test_incomplete_inventory_does_not_log_success(caplog, limits):
    store, _ = store_with_row()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"), pytest.raises(Problem) as denied:
        store.read_inventory(**limits, log_capacity=True)
    assert denied.value.status == 413
    assert not any(record.message.startswith("BEHAVIOR_INVENTORY_READ ") for record in caplog.records)


def test_inventory_refuses_a_race_or_partial_limits(caplog):
    store, table = store_with_row()
    competitor = BehaviorStore(table, SCOPE)
    table.after_page = lambda: competitor.transact(
        ControlRequest(request_id="concurrent", request_digest="b" * 64),
        lambda _: ControlChanges(writes=(ControlWrite(row_id="late", mode="create", data={}),), result={}),
    )
    with caplog.at_level(logging.INFO, logger="uvicorn.error"), pytest.raises(Problem) as raced:
        store.read_inventory(log_capacity=True)
    assert raced.value.status == 412
    assert not any(record.message.startswith("BEHAVIOR_INVENTORY_READ ") for record in caplog.records)
    with pytest.raises(Problem) as bounded:
        store.read_inventory(max_entities=1)
    assert bounded.value.body["error"]["code"] == "BEHAVIOR_INVENTORY_LIMIT"


def test_runtime_inventory_can_exceed_legacy_count_without_losing_byte_or_page_bounds():
    store, table = store_with_row()
    table.page_size = 100
    template = table.backing.rows[(SCOPE.table_name, SCOPE.partition_key, "behavior:v1:row:budget")]
    for number in range(4095):
        entity = copy.deepcopy(template)
        row_id = f"retained-{number}"
        body = json.loads(entity["Payload"])
        body["row_id"] = row_id
        entity["RowKey"] = "behavior:v1:row:" + row_id
        entity["Payload"] = json.dumps(body).encode()
        table.backing.rows[(SCOPE.table_name, SCOPE.partition_key, entity["RowKey"])] = entity
    writes = len(table.submissions)
    with pytest.raises(Problem):
        store.read_inventory(max_entities=4096)
    assert len(store.read_inventory().entity_sha256) == 4098
    inventory = store.read_inventory(max_entities=None)
    assert len(inventory.entity_sha256) == 4098 and len(table.submissions) == writes
    from shared_context.behavior_recovery_history import validate_recovery_history
    with pytest.raises(Problem):
        validate_recovery_history(inventory, None, max_entities=4096)
    assert dict(validate_recovery_history(inventory, None)) == {}
    assert dict(validate_recovery_history(inventory, None, max_entities=None)) == {}
    for limits in ({"max_bytes": 1024}, {"max_pages": 1}):
        with pytest.raises(Problem) as bounded:
            store.read_inventory(max_entities=None, **limits)
        assert bounded.value.body["error"]["code"] == "BEHAVIOR_INVENTORY_LIMIT"


def test_runtime_inventory_uses_full_table_pages_with_original_byte_and_page_bounds():
    store,table=store_with_row()
    requested=[]
    query=table.query_entities
    def actual_pages(**kwargs):
        requested.append(kwargs["results_per_page"])
        table.page_size=kwargs["results_per_page"]
        return query(**kwargs)
    table.query_entities=actual_pages
    template=table.backing.rows[(SCOPE.table_name,SCOPE.partition_key,"behavior:v1:row:budget")]
    for number in range(6400):
        entity=copy.deepcopy(template)
        row_id=f"page-retained-{number}"
        body=json.loads(entity["Payload"]);body["row_id"]=row_id
        entity["RowKey"]="behavior:v1:row:"+row_id
        entity["Payload"]=json.dumps(body).encode()
        table.backing.rows[(SCOPE.table_name,SCOPE.partition_key,entity["RowKey"])]=entity
    inventory=store.read_inventory(max_entities=None)
    assert requested==[1000] and len(inventory.entity_sha256)==6403
    with pytest.raises(Problem):
        store.read_inventory(max_entities=None,max_pages=1)
    with pytest.raises(Problem):
        store.read_inventory(max_entities=None,max_bytes=1024)


def test_conditional_transaction_cannot_use_a_stale_inventory_guard():
    store, table = store_with_row()
    inventory = store.read_inventory()
    store.transact(ControlRequest(request_id="advance", request_digest="b" * 64),
                   lambda _: ControlChanges(writes=(), result={}))
    before = len(table.submissions)
    with pytest.raises(Problem) as stale:
        store.transact(
            ControlRequest(request_id="conditional", request_digest="c" * 64),
            lambda _: ControlChanges(writes=(), result={}), expected_guard=inventory.guard,
        )
    assert stale.value.status == 412
    assert len(table.submissions) == before


def test_installation_fence_rejects_old_configuration_writes_and_reads():
    store, table = store_with_row()
    store.transact(ControlRequest(request_id="install", request_digest="b" * 64),
                   lambda _: ControlChanges(writes=(
                       ControlWrite(row_id="installation", mode="create", data={"configuration_sha256": "a" * 64}),
                   ), result={}))
    store.bind_installation("installation", "a" * 64)
    operator = BehaviorStore(table, SCOPE)
    operator.transact(ControlRequest(request_id="migration", request_digest="c" * 64, read_set=("installation",)),
                      lambda _: ControlChanges(writes=(
                          ControlWrite(row_id="installation", mode="replace", data={"configuration_sha256": "b" * 64}),
                      ), result={}))
    before = len(table.submissions)
    for operation in (
        lambda: store.read_snapshot(("budget",)),
        lambda: store.transact(ControlRequest(request_id="old-write", request_digest="d" * 64),
                               lambda _: ControlChanges(writes=(), result={})),
    ):
        with pytest.raises(Problem) as fenced:
            operation()
        assert fenced.value.body["error"]["code"] == "MIGRATION_INSTALLATION_CHANGED"
    assert len(table.submissions) == before


def test_existing_deadline_receiver_wrapper_cannot_bypass_installation_fence():
    store, table = store_with_row()
    store.transact(ControlRequest(request_id="install-receiver", request_digest="a" * 64),
                   lambda _: ControlChanges(writes=(ControlWrite(
                       row_id="installation", mode="create", data={"configuration_sha256": "a" * 64},
                   ),), result={}))
    store.bind_installation("installation", "a" * 64)
    now = datetime.now(timezone.utc)
    receiver = BaseStore(_DeadlineClient(store._client, lambda: now,
                                        [(now - timedelta(seconds=1), now + timedelta(seconds=1))]), SCOPE)
    operator = BehaviorStore(table, SCOPE)
    operator.transact(ControlRequest(request_id="replace-installation", request_digest="b" * 64, read_set=("installation",)),
                      lambda _: ControlChanges(writes=(ControlWrite(
                          row_id="installation", mode="replace", data={"configuration_sha256": "b" * 64},
                      ),), result={}))
    before = dict(operator.read_inventory().entity_sha256)
    with pytest.raises(Problem) as rejected:
        receiver.transact(ControlRequest(request_id="stale-receiver", request_digest="c" * 64),
                          lambda _: ControlChanges(writes=(), result={}))
    assert rejected.value.body["error"]["code"] == "MIGRATION_INSTALLATION_CHANGED"
    assert dict(operator.read_inventory().entity_sha256) == before


def test_complete_inventory_uses_real_Table_SDK_serialization_with_local_transport():
    class QueryTransport(WireTransport):
        def send(self, request, **kwargs):
            if request.method == "GET" and urlsplit(request.url).path.rstrip("()") == "/Behavior":
                self.requests.append(request)
                assert parse_qs(urlsplit(request.url).query)["$filter"] == ["PartitionKey eq 'control-one'"]
                items = [{
                    **entity, "Payload": base64.b64encode(entity["Payload"]).decode(),
                    "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"],
                } for (table, partition, _), entity in sorted(self.backing.rows.items())
                    if table == "Behavior" and partition == SCOPE.partition_key]
                return json_response(request, 200, {"value": items}, {})
            return super().send(request, **kwargs)
    transport = QueryTransport()
    with TableClient("https://synthetic.table.core.windows.net", "Behavior", credential=Credential(),
                     transport=transport) as client:
        store = BehaviorStore(client, SCOPE)
        store.initialize()
        store.transact(ControlRequest(request_id="sdk-row", request_digest="a" * 64),
                       lambda _: ControlChanges(writes=(ControlWrite(row_id="sdk-data", mode="create", data={}),), result={}))
        inventory = store.read_inventory()
        assert "sdk-data" in inventory.rows and "sdk-row" in inventory.receipts
