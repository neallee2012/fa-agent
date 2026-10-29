"""Complete-inventory limits and the real current standing-history validators.

Table/HTTP transport and retained receipts are synthetic. The shared startup
fixture does not reconstruct the historical Teams/WebHandoff outer chain.
"""

import base64
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from shared_context import behavior_recovery_history
from shared_context.behavior_migration import (
    DEFAULT_INVENTORY_MAX_BYTES, DEFAULT_INVENTORY_MAX_ENTITIES, DEFAULT_INVENTORY_MAX_PAGES,
)
from shared_context.behavior_recovery_history import validate_recovery_history
from shared_context.behavior_runtime_rotation import _measure_layout
from shared_context.behavior_store import GUARD_KEY, ControlChanges, ControlRequest, ControlWrite, _Receipt, _json
from shared_context.errors import Problem
from test_behavior_migration_store import store_with_row
from test_behavior_store import NativeEntity, SCOPE, json_response
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def payload_bytes(table, scope):
    return sum(len(entity["Payload"]) for (name, partition, _), entity in table.backing.rows.items()
               if name == scope.table_name and partition == scope.partition_key)


def append_receipts(table, scope, count, *, receipt_bytes=512, result=None):
    """Construct distinct, receipt-valid no-write EGTs without O(n**2) fake writes."""
    key = (scope.table_name, scope.partition_key, GUARD_KEY)
    entity = table.backing.rows[key]
    guard = json.loads(entity["Payload"])
    for number in range(count):
        revision = guard["revision"] + number + 1
        value = _Receipt(
            binding=guard["binding"], guard_revision=revision, writes=(),
            request=ControlRequest(request_id=f"inventory-size-{revision}", request_digest="a" * 64),
            result={**(result or {}), "padding": ""},
        )
        padding = receipt_bytes - len(_json(value))
        assert padding >= 0
        raw = _json(value.model_copy(update={"result": {**value.result, "padding": "x" * padding}}))
        assert len(raw) == receipt_bytes <= scope.max_payload_bytes
        row_key = "behavior:v1:receipt:" + value.request.request_id
        item_key = (scope.table_name, scope.partition_key, row_key)
        assert item_key not in table.backing.rows
        table.backing.rows[item_key] = NativeEntity({
            "PartitionKey": scope.partition_key, "RowKey": row_key, "Payload": raw,
        }, f'W/"inventory-size-{revision}"')
    guard["revision"] += count
    table.backing.rows[key] = NativeEntity({
        **entity, "Payload": json.dumps(guard, ensure_ascii=False, separators=(",", ":")).encode(),
    }, f'W/"inventory-guard-{guard["revision"]}"')


@pytest.fixture
def paged_standing(request, monkeypatch):
    import test_conversation_transfer_startup as startup

    class PagedStartupTransport(startup.StartupTableTransport):
        def send(self, request, **kwargs):
            url = urlsplit(request.url)
            if request.method != "GET" or url.path.rstrip("()") != "/Behavior":
                return super().send(request, **kwargs)
            self.requests.append(request)
            query = parse_qs(url.query)
            assert query["$filter"] == ["PartitionKey eq 'authority'"]
            assert query["$top"] == ["1000"]
            after = query.get("NextRowKey", [""])[0]
            keys = sorted(key for key in self.backing.rows
                          if key[:2] == ("Behavior", "authority") and key[2] > after)
            selected = keys[:1000]
            items = []
            for key in selected:
                entity = self.backing.rows[key]
                items.append({
                    **entity, "Payload": base64.b64encode(entity["Payload"]).decode(),
                    "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"],
                })
            headers = ({"x-ms-continuation-NextPartitionKey": "authority",
                        "x-ms-continuation-NextRowKey": selected[-1][2]} if len(keys) > 1000 else {})
            return json_response(request, 200, {"value": items}, headers)

    monkeypatch.setattr(startup, "StartupTableTransport", PagedStartupTransport)
    return request.getfixturevalue("synthetic_standing_v1")


@pytest.mark.parametrize("target_mib,receipt_bytes,structured", [
    (33, 33 * 1024, False), (130, 3 * 1024, True),
], ids=["above-32mib", "above-128mib"])
def test_startup_inventory_and_current_history_accept_over_32mib(
    paged_standing, monkeypatch, target_mib, receipt_bytes, structured,
):
    c = paged_standing
    scope, table = c.h.config.table.scope, c.h.table
    result = ({"measurements": [{"metric": f"counter-{index}", "value": index, "accepted": True,
                                "note": "synthetic-size-only"} for index in range(20)]} if structured else {})
    count = (target_mib * 1024 * 1024 - payload_bytes(table, scope)) // receipt_bytes + 1
    append_receipts(table, scope, count, receipt_bytes=receipt_bytes, result=result)
    total = payload_bytes(table, scope)
    assert target_mib * 1024 * 1024 < total < DEFAULT_INVENTORY_MAX_BYTES
    before = dict(table.backing.rows)
    parsed = behavior_recovery_history._parsed
    receipt_parses = []

    def counted(value, model):
        if model is _Receipt:
            receipt_parses.append(1)
        return parsed(value, model)

    monkeypatch.setattr(behavior_recovery_history, "_parsed", counted)
    inventory = read_startup(c)
    assert len(inventory.receipts) == len(c.before.receipts) + 1 + count
    # Every startup view of one inventory shares one checked receipt copy instead of re-parsing it.
    assert len(receipt_parses) == len(inventory.receipts)
    assert table.backing.rows == before
    assert not c.sessions.calls
    print(json.dumps({"inventory_bytes": total, "entities": len(before), "startup_readers_completed": True}))


def assert_cutover_limit(store, table, inventory, changes):
    writes = len(table.submissions)
    with pytest.raises(Problem) as denied:
        _measure_layout(
            store, inventory, SimpleNamespace(sha256="b" * 64), SimpleNamespace(sha256="c" * 64),
            ControlRequest(request_id="inventory-capacity-preview", request_digest="d" * 64), changes,
        )
    assert denied.value.status == 413
    assert denied.value.body["error"]["code"] == "ROTATION_INVENTORY_LIMIT"
    assert len(table.submissions) == writes


def test_inventory_default_entity_bound_accepts_its_limit_and_refuses_next_entity():
    store, table = store_with_row()
    table.page_size = 1000
    append_receipts(table, SCOPE, DEFAULT_INVENTORY_MAX_ENTITIES - len(table.backing.rows))
    inventory = store.read_inventory()
    assert len(inventory.entity_sha256) == DEFAULT_INVENTORY_MAX_ENTITIES == 131072
    assert dict(validate_recovery_history(inventory, None)) == {}
    assert_cutover_limit(store, table, inventory, ControlChanges(result={}))
    append_receipts(table, SCOPE, 1)
    with pytest.raises(Problem) as denied:
        store.read_inventory()
    assert denied.value.status == 413
    assert denied.value.body["error"]["code"] == "BEHAVIOR_INVENTORY_LIMIT"


def test_inventory_default_byte_bound_refuses_over_256mib():
    store, table = store_with_row()
    table.page_size = 1000
    append_receipts(table, SCOPE, DEFAULT_INVENTORY_MAX_BYTES // (32 * 1024) - 1, receipt_bytes=32 * 1024)
    inventory = store.read_inventory()
    assert DEFAULT_INVENTORY_MAX_BYTES == 256 * 1024 * 1024
    assert DEFAULT_INVENTORY_MAX_BYTES - 1024 * 1024 < payload_bytes(table, SCOPE) < DEFAULT_INVENTORY_MAX_BYTES
    assert_cutover_limit(store, table, inventory, ControlChanges(writes=(
        ControlWrite(row_id="cutover-extra", mode="create", data={"padding": "x" * 47000}),
    ), result={}))
    append_receipts(table, SCOPE, 1, receipt_bytes=32 * 1024)
    assert payload_bytes(table, SCOPE) > DEFAULT_INVENTORY_MAX_BYTES
    with pytest.raises(Problem) as denied:
        store.read_inventory(max_entities=None)
    assert denied.value.status == 413
    assert denied.value.body["error"]["code"] == "BEHAVIOR_INVENTORY_LIMIT"
    assert denied.value.body["error"]["message"] == "complete inventory exceeds its bound"


def test_startup_reads_its_inventory_with_the_default_byte_bound():
    from shared_context.behavior_work_policy_installation import installed_work_policy_origin

    class Stop(Exception):
        pass

    class Store:
        def read_inventory(self, **bounds):
            seen.update(bounds)
            raise Stop

    seen = {}
    with pytest.raises(Stop):
        installed_work_policy_origin(Store(), None)
    # No max_bytes: startup keeps the 256 MiB default; data near it can exhaust API memory before the 413.
    assert seen == {"max_entities": None, "log_capacity": True}


def test_inventory_default_page_bound_accepts_its_limit_and_refuses_next_page():
    store, table = store_with_row()
    table.page_size = 1
    append_receipts(table, SCOPE, DEFAULT_INVENTORY_MAX_PAGES - len(table.backing.rows))
    assert len(store.read_inventory().entity_sha256) == DEFAULT_INVENTORY_MAX_PAGES == 512
    append_receipts(table, SCOPE, 1)
    with pytest.raises(Problem) as denied:
        store.read_inventory()
    assert denied.value.status == 413
    assert denied.value.body["error"]["code"] == "BEHAVIOR_INVENTORY_LIMIT"


@pytest.mark.parametrize("limits", [
    {"max_entities": True}, {"max_entities": 0}, {"max_entities": 1.0},
    {"max_bytes": None}, {"max_bytes": False}, {"max_bytes": 0},
    {"max_pages": None}, {"max_pages": True}, {"max_pages": -1},
])
def test_invalid_inventory_bounds_still_fail_before_table_query(limits):
    store, table = store_with_row()
    queries = []
    table.query_entities = lambda **kwargs: queries.append(kwargs)
    with pytest.raises(Problem) as denied:
        store.read_inventory(**limits)
    assert denied.value.status == 422
    assert denied.value.body["error"]["code"] == "BEHAVIOR_INVENTORY_LIMIT"
    assert queries == []
