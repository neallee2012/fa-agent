"""Memory first-use with the standing writer's real final-send installation fence."""

import pytest

from shared_context import behavior_work_policy_installation as installation
from shared_context.behavior_channels_migration import CatalogChunk
from shared_context.behavior_migration import installation_record
from shared_context.behavior_operations import _parse, control_key
from shared_context.behavior_runtime_rotation_history import binding_scopes, shared_keys
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_memory_startup import test_memory_first_use_and_recall_records_remain_readable_at_startup as memory_first_use


@pytest.fixture(params=["legacy", "current"])
def fenced_standing(request, monkeypatch):
    if request.param == "legacy":
        # Create the original catalog shape; never rewrite a stored catalog or its digest.
        with monkeypatch.context() as setup:
            setup.setattr(installation, "standing_shared_keys", shared_keys)
            c = request.getfixturevalue("synthetic_standing_v1")
    else:
        c = request.getfixturevalue("synthetic_standing_v1")
    c.legacy_cursor_catalog = request.param == "legacy"
    protected = c.metadata | (set(c.before.rows) - installation.standing_shared_keys(c.h.config))
    c.h.runtime.store.bind_installation(installation_record(c.h.config)[0], c.h.config.sha256,
                                      protected_rows=tuple(protected))
    return c


def test_memory_source_does_not_mutate_a_frozen_installation_row(fenced_standing, monkeypatch):
    c = fenced_standing
    store, config = c.h.runtime.store, c.h.config
    scope = next(scope for scope in binding_scopes(config, config.bindings[0]) if scope.visibility == "team")
    cursor = control_key("source-cursor", scope.key)
    before = store.read_inventory()
    catalogs = {key: before.rows[key] for key, _ in c.history.catalog}
    entries = [entry for row in catalogs.values()
               for entry in _parse(row.payload["data"], CatalogChunk).entries]
    original_cursor = next(entry for entry in entries if entry.kind == "row" and entry.row_id == cursor)
    assert (original_cursor.preimage is None) is c.legacy_cursor_catalog
    blocked = []
    submit = store._client.submit_transaction

    def observe(operations, **kwargs):
        pins = [operation[1]["RowKey"] for operation in operations
                if operation[1]["RowKey"] in store._protected_rows]
        if pins:
            blocked.append(pins)
        return submit(operations, **kwargs)

    monkeypatch.setattr(store._client, "submit_transaction", observe)
    memory_first_use(c)
    assert blocked == []
    after = read_startup(c)
    assert after.rows[cursor].payload["data"]["sequence"] > before.rows[cursor].payload["data"]["sequence"]
    # The production catalog reader must reconstruct the original before-image after a new source commits.
    restored, receipts, hashes, _, _ = installation.read_standing_catalog(after, config, c.history)
    assert restored[cursor] == c.before.rows[cursor]
    assert receipts == dict(c.before.receipts)
    assert hashes == dict(c.before.entity_sha256)
    assert all(after.rows[key] == row for key, row in catalogs.items())


def test_true_installation_pins_and_retained_sources_stay_fenced(fenced_standing):
    c = fenced_standing
    store, config = c.h.runtime.store, c.h.config
    keys = (installation_record(config)[0], c.history.catalog[0][0],
            c.control.policy.profile.source.source_id)
    source_event = next(key for key, row in c.before.rows.items()
                        if row.payload["data"].get("schema_id")
                        and row.payload["data"].get("reference", {}).get("source_id") == keys[2])
    before = store.read_inventory()
    for number, key in enumerate((*keys[:2], source_event)):
        old = before.rows[key]
        assert "behavior:v1:row:" + key in store._protected_rows
        with pytest.raises(Problem) as denied:
            store.transact(ControlRequest(request_id="cannot-rewrite-pin-" + str(number), request_digest="a" * 64,
                                          read_set=(key,)),
                lambda _, key=key, old=old: ControlChanges(writes=(ControlWrite(
                    row_id=key, mode="replace", data=old.payload["data"]),), result={}))
        # Material retention may reject earlier than the final-send pin fence.
        expected = "BEHAVIOR_MATERIAL_RETAINED" if old.payload["material"] is not None else "MIGRATION_OPERATOR_REQUIRED"
        assert denied.value.body["error"]["code"] == expected
    assert store.read_inventory().entity_sha256 == before.entity_sha256
