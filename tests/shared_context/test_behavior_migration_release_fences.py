"""Offline regressions for the exact pre-release fencing findings."""
import json

import pytest

from shared_context.behavior_migration import MigrationStore, installation_record, migration_key
from shared_context.behavior_server import application, bootstrap, build_server
from shared_context.behavior_store import GUARD_KEY, ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from test_behavior_server import Harness, workspace
from test_m3_auth import jwk_client, signing_key


@pytest.mark.parametrize("preactivation", [False, True])
def test_factory_writer_is_bound_before_optional_installation_call(workspace, jwk_client, preactivation):
    harness = Harness(workspace, jwk_client)
    bootstrap(harness.composition, harness.approval, stage="records")
    writer = build_server(harness.config, **harness.options)
    operator = MigrationStore(writer.clients[0], harness.config.table.scope)
    key = migration_key(harness.config.table.scope.authority_domain, "current")
    installed_key, installed = installation_record(harness.config)
    try:
        if preactivation:
            application(writer, preactivation=True)
        operator.transact(
            ControlRequest(request_id="operator-advances-configuration", request_digest="f" * 64,
                           read_set=(installed_key, key)),
            lambda _: ControlChanges(writes=(ControlWrite(
                row_id=installed_key, mode="replace", data={**installed, "configuration_sha256": "f" * 64},
            ),), result={}),
        )
        before = len(harness.table.parts)
        with pytest.raises(Problem) as denied:
            writer.runtime.store.transact(ControlRequest(request_id="old-factory-write", request_digest="a" * 64),
                                          lambda _: ControlChanges(result={}))
        assert denied.value.body["error"]["code"] == "MIGRATION_INSTALLATION_CHANGED"
        assert len(harness.table.parts) == before
    finally:
        writer.close()
        harness.composition.close()


def test_cached_epoch_cannot_hide_physical_guard_downgrade(workspace, jwk_client):
    harness = Harness(workspace, jwk_client)
    bootstrap(harness.composition, harness.approval, stage="records")
    config = harness.config
    store = MigrationStore(harness.composition.clients[0], config.table.scope)
    entity = harness.table.backing.rows[(config.table.scope.table_name, config.table.scope.partition_key, GUARD_KEY)]
    legacy = entity["Payload"]
    try:
        entity["Payload"] = json.dumps({**json.loads(legacy), "schema_version": 2,
                                        "configuration_sha256": "f" * 64}).encode()
        store.read_snapshot(("any-row",))
        assert store._guard_epoch == "f" * 64
        entity["Payload"] = legacy
        with pytest.raises(Problem) as denied:
            store.read_snapshot(("any-row",))
        assert denied.value.status == 409
        assert denied.value.body["error"]["code"] == "MIGRATION_GUARD_DOWNGRADED"
    finally:
        harness.composition.close()
