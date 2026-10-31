"""A fresh installation refuses any legacy pointer written into its table afterwards."""

import pytest

from shared_context import behavior_migration as bm
from shared_context.behavior_fresh_installation import fresh_key
from shared_context.behavior_migration import origin_pointer_keys
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from test_behavior_assistant_server import assistant_server  # noqa: F401
from test_fresh_installation import fresh_case, initialize  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_per_request_verified_origin import external_write


@pytest.mark.parametrize("pointer", ["work-policy", "migration", "transition", "rotation"])
def test_fresh_table_with_a_legacy_pointer_is_refused(fresh_case, pointer):
    h = fresh_case
    initialize(h)
    assert bm.installed_origin(h.runtime.store, h.config) == h.config

    key = dict(zip(("work-policy", "migration", "transition", "rotation"),
                   origin_pointer_keys(h.config.table.scope.authority_domain)[1:]))[pointer]
    external_write(h, [(key, {"schema_version": "x"})], "create")

    with pytest.raises(Problem) as refused:
        bm.installed_origin(h.runtime.store, h.config)
    assert refused.value.status == 409
    assert refused.value.body["error"]["code"] == "FRESH_INITIALIZATION_INVALID"
    assert "mixed with a legacy" in str(refused.value)
    assert h.runtime.store._verified_fresh_initialization is None


@pytest.mark.parametrize("index", [2, 3, 4], ids=["migration", "transition", "rotation"])
def test_a_table_without_a_fresh_row_refuses_a_retired_pointer(fresh_case, index):
    h = fresh_case
    key, expected = bm.installation_record(h.config)
    pointer = origin_pointer_keys(h.config.table.scope.authority_domain)[index]
    bm.MigrationStore(h.composition.clients[0], h.config.table.scope).initialize()
    external_write(h, [(key, expected), (pointer, {"schema_version": "x"})], "create")

    with pytest.raises(Problem) as refused:
        bm.installed_origin(h.runtime.store, h.config)
    assert refused.value.status == 503
    assert refused.value.body["error"]["code"] == "BEHAVIOR_CONFIGURATION"
    assert "only a fresh installation" in str(refused.value)


@pytest.mark.parametrize("installed,fenced,expected", [
    (True, False, (503, "BEHAVIOR_CONFIGURATION")),
    (True, True, (503, "BEHAVIOR_CONFIGURATION")),
    (False, False, (503, "BEHAVIOR_CONFIGURATION")),
    (False, True, (409, "MIGRATION_INSTALLATION_CHANGED")),
], ids=["installed-unfenced", "installed-fenced", "no-row-unfenced", "no-row-fenced"])
def test_a_rotation_pointer_without_a_fresh_row_is_refused(fresh_case, installed, fenced, expected):
    h = fresh_case
    pointer = origin_pointer_keys(h.config.table.scope.authority_domain)[4]
    bm.MigrationStore(h.composition.clients[0], h.config.table.scope).initialize()
    external_write(h, [*([bm.installation_record(h.config)] if installed else []),
                       (pointer, {"schema_version": "x"})], "create")
    store = h.runtime.store if fenced else bm.MigrationStore(h.composition.clients[0], h.config.table.scope)

    with pytest.raises(Problem) as refused:
        bm.installed_origin(store, h.config)
    assert (refused.value.status, refused.value.body["error"]["code"]) == expected

    if fenced:  # the path every gated request takes
        with pytest.raises(Problem) as gated:
            h.composition.installation()
        assert (gated.value.status, gated.value.body["error"]["code"]) == expected


@pytest.mark.parametrize("row", ["installation", "fresh"])
def test_a_normal_writer_cannot_replace_an_installation_pin(fresh_case, row):
    h = fresh_case
    initialize(h)
    store = h.runtime.store
    assert bm.installed_origin(store, h.config) == h.config
    key = bm.installation_record(h.config)[0] if row == "installation" else fresh_key(h.config.table.scope.authority_domain)
    previous = store.read_snapshot((key,))[key]

    with pytest.raises(Problem) as denied:
        store.transact(
            ControlRequest(request_id="rewrite-installation", request_digest="e" * 64, read_set=(key,)),
            lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="replace",
                                                          data={**previous.payload["data"], "unexpected": True}),),
                                     result={}))
    assert (denied.value.status, denied.value.body["error"]["code"]) == (403, "MIGRATION_OPERATOR_REQUIRED")
    assert store.read_snapshot((key,))[key] == previous
