"""OFFLINE typed capture history on direct pin-v1 and rotated v1 authorities."""
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared_context.behavior_channels_migration import _verified_predecessor, _drained
from shared_context.behavior_migration import MigrationStore
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime_transition import _payload_hash
from shared_context.behavior_store import GUARD_KEY, ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from shared_context.records import Record
from test_behavior_channels_migration import channel_migration
from test_teams_metrics import metrics
from test_m3_auth import jwk_client, signing_key
from test_behavior_runtime_rotation import rotated as rotation_fixture, approved
from test_behavior_runtime_transition import transition_fixture, captured_route_history


@pytest.fixture(params=["pin", "rotation"])
def predecessor(request, channel_migration, tmp_path, monkeypatch):
    service, store, table, original, _ = channel_migration
    if request.param == "pin":
        return store, table, service.old, original
    def captured_transition():
        result = transition_fixture()
        _, store, _, original, pinned, _, _ = result
        captured_route_history(store, original, pinned)
        return result
    with patch("test_behavior_runtime_rotation.transition_fixture", side_effect=captured_transition):
        h = rotation_fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param=None))
    plan, authorization = approved(h)
    h.service.apply(h.principal, plan, authorization)
    h.service.verify(h.principal, plan, authorization)
    return MigrationStore(h.table, h.store._scope), h.table, h.new, h.original


def rewrite(inventory, *, row_changes=None, receipt_changes=None):
    rows, receipts = dict(inventory.rows), dict(inventory.receipts)
    for target, changes in ((rows, row_changes or {}), (receipts, receipt_changes or {})):
        for key, data in changes.items():
            if data is None:
                target.pop(key)
            else:
                target[key] = Record(data, target[key].etag)
    hashes = {GUARD_KEY: inventory.entity_sha256[GUARD_KEY]}
    for kind, values in (("row", rows), ("receipt", receipts)):
        hashes.update({"behavior:v1:" + kind + ":" + key: _payload_hash(value.payload)
                       for key, value in values.items()})
    return replace(inventory, rows=rows, receipts=receipts, entity_sha256=hashes)


def capture(inventory):
    row_id, row = next((key, value) for key, value in inventory.rows.items()
                      if value.payload["data"].get("schema_version") == "fa-teams-personal-route-v1")
    request_id = control_key("teams-personal-enroll", row.payload["data"]["configuration_digest"])
    return row_id, request_id


def test_capture_typed_view_preserves_original_inventory_receipts_and_guard(predecessor):
    store, table, config, original = predecessor
    before = store.read_inventory()
    submissions = len(table.submissions)
    assert _verified_predecessor(store, before, config) == original
    _drained(before)
    after = store.read_inventory()
    assert before.entity_sha256 == after.entity_sha256
    assert before.guard == after.guard and before.rows == after.rows and before.receipts == after.receipts
    assert len(table.submissions) == submissions


@pytest.mark.parametrize("failure", [
    "missing-receipt", "wrong-request", "wrong-write-hash", "guard-revision", "mixed-control-write",
    "missing-binding", "tenant", "bot", "user", "route",
])
def test_capture_cannot_hide_receipt_scope_or_mixed_transaction_errors(predecessor, failure):
    store, table, config, _ = predecessor
    inventory = store.read_inventory()
    key, request_id = capture(inventory)
    record = copy.deepcopy(inventory.rows[key].payload)
    receipt = copy.deepcopy(inventory.receipts[request_id].payload)
    rows, receipts = {}, {}
    if failure == "missing-receipt":
        receipts[request_id] = None
    elif failure == "missing-binding":
        rows[control_key("teams-capture-route-binding", record["data"]["configuration_digest"])] = None
    elif failure == "guard-revision":
        receipt["guard_revision"] = inventory.guard.payload["revision"] + 1
        receipts[request_id] = receipt
    elif failure == "wrong-request":
        receipt["request"]["request_digest"] = "f" * 64
        receipts[request_id] = receipt
    elif failure == "wrong-write-hash":
        receipt["writes"][0][1] = "f" * 64
        receipts[request_id] = receipt
    elif failure == "mixed-control-write":
        root = control_key("budget", config.roots[0].root_budget_id)
        receipt["writes"].append([root, _payload_hash(inventory.rows[root].payload)])
        receipts[request_id] = receipt
    else:
        field = {"tenant": "tenant_id", "bot": "bot_app_id", "user": "aad_object_id",
                 "route": "conversation_id"}[failure]
        record["data"]["route"][field] = "wrong-" + field
        rows[key] = record
    invalid = rewrite(inventory, row_changes=rows, receipt_changes=receipts)
    before = len(table.submissions)
    with pytest.raises(Problem):
        _verified_predecessor(store, invalid, config)
    assert len(table.submissions) == before
    assert store.read_inventory().entity_sha256 == inventory.entity_sha256


def test_receipt_covered_unknown_addition_is_never_a_capture_exemption(predecessor):
    store, table, config, _ = predecessor
    operator = MigrationStore(table, store._scope)
    operator.transact(ControlRequest(request_id="unrecognized-state", request_digest="a" * 64),
                      lambda _: ControlChanges(writes=(ControlWrite(
                          row_id="unknown-current-job", mode="create",
                          data={"schema_version": "unrecognized-v99", "state": "capacity_uncertain"},
                      ),), result={}))
    before = len(table.submissions)
    with pytest.raises(Problem):
        _verified_predecessor(operator, operator.read_inventory(), config)
    assert len(table.submissions) == before
