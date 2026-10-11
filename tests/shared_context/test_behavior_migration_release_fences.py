"""Offline regressions for the exact pre-release fencing findings."""
import json
from datetime import timedelta

import pytest

from shared_context.behavior_migration import MigrationStore, installation_record, installed_origin, migration_key
from shared_context.behavior_server import application, bootstrap, build_server
from shared_context.behavior_store import GUARD_KEY, ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from test_behavior_migration import fixture
from test_behavior_server import Harness, NOW, workspace
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
            install_configuration_sha256="f" * 64,
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


def migrated():
    service, store, table, old, new, principal = fixture()
    plan = service.plan(principal, migration_id="release-fences")
    approval = service.authorize(principal, plan, decision_id="release-fences-approved",
                                 expires_at=NOW + timedelta(minutes=5))
    service.apply(principal, plan, approval)
    service.verify_applied(principal, plan, approval)
    return service, store, table, old, new, principal, plan, approval


def test_cached_epoch_cannot_hide_physical_guard_downgrade():
    service, store, table, _, _, principal, plan, approval = migrated()
    entity = table.backing.rows[(table.table_name, store._scope.partition_key, GUARD_KEY)]
    physical = json.loads(entity["Payload"])
    physical["schema_version"] = 1
    physical.pop("configuration_sha256")
    entity["Payload"] = json.dumps(physical, separators=(",", ":")).encode()
    entity.metadata["etag"] = 'W/"offline-admin-downgrade"'
    with pytest.raises(Problem):
        service.verify_applied(principal, plan, approval)


def test_verified_release_exposes_only_exact_history_binding_for_independent_checks():
    service, store, _, _, new, principal, plan, approval = migrated()
    verified = service.verify_applied(principal, plan, approval)
    pointer_key = migration_key(new.table.scope.authority_domain, "current")
    pointer = store.read_snapshot((pointer_key,))[pointer_key].payload["data"]
    assert verified["migration_history_key"] == pointer["history_key"]
    assert verified["migration_history_sha256"] == pointer["history_sha256"]
    assert verified["code_sha256"] == plan.code_sha256
    assert verified["activation_performed"] is False


@pytest.mark.parametrize("record_kind", ["history", "authorization"])
def test_bound_writer_cannot_replace_immutable_migration_evidence(record_kind):
    _, store, table, _, new, _, plan, approval = migrated()
    writer = MigrationStore(table, store._scope)
    installed_origin(writer, new)
    identity = plan.migration_id if record_kind == "history" else approval.decision_id
    key = migration_key(new.table.scope.authority_domain, record_kind, identity)
    previous = writer.read_snapshot((key,))[key]
    data = {**previous.payload["data"], "unexpected_mutation": True}
    before = dict(writer.read_inventory().entity_sha256)
    with pytest.raises(Problem) as denied:
        writer.transact(ControlRequest(request_id="rewrite-" + record_kind, request_digest="d" * 64, read_set=(key,)),
                        lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="replace", data=data),), result={}))
    assert denied.value.body["error"]["code"] == "MIGRATION_OPERATOR_REQUIRED"
    assert dict(writer.read_inventory().entity_sha256) == before
    assert writer.read_snapshot((key,))[key] == previous


def test_new_config_alone_cannot_replace_migration_history_before_lineage_verification():
    _, store, table, _, new, _, plan, _ = migrated()
    writer = MigrationStore(table, store._scope,
                            expected_installation=(installation_record(new)[0], new.sha256))
    key = migration_key(new.table.scope.authority_domain, "history", plan.migration_id)
    previous = writer.read_snapshot((key,))[key]
    with pytest.raises(Problem) as denied:
        writer.transact(ControlRequest(request_id="unverified-lineage-write", request_digest="e" * 64, read_set=(key,)),
                        lambda _: ControlChanges(writes=(ControlWrite(
                            row_id=key, mode="replace", data={**previous.payload["data"], "unexpected_mutation": True},
                        ),), result={}))
    assert denied.value.body["error"]["code"] == "MIGRATION_HISTORY_UNVERIFIED"
    assert writer.read_snapshot((key,))[key] == previous


@pytest.mark.parametrize("coverage", ["extra", "duplicate"])
def test_migration_coverage_requires_the_same_exact_scope_set_as_server(coverage):
    service, _, _, _, _, principal = fixture()
    observed = service.observations["sources"]
    extra = "unapproved-inventory-scope" if coverage == "extra" else observed.inventory_coverage[0]
    service.observations["sources"] = observed.model_copy(update={
        "inventory_coverage": (*observed.inventory_coverage, extra),
    })
    with pytest.raises(Problem) as denied:
        service.plan(principal, migration_id="incompatible-coverage")
    assert denied.value.body["error"]["code"] == "MIGRATION_OBSERVATION_STALE"
